-- Color effects, for reading a window whose own contrast fights you.
--
-- Hyprland exposes exactly one screen shader (decoration:screen_shader) and runs
-- it over a whole output at the end of rendering, so every effect has to share
-- that single slot. This module owns it; nothing else writes screen_shader.
--
-- Three effects are available:
--
--   off       — the pixels untouched (no effect)
--   invert    — every channel flipped, 1 - rgb
--   contrast  — luminance mapped to yellow on black (maximum-contrast mode,
--               the accessibility one: bright pixels become yellow, dark pixels
--               black, with an S-curve that hardens the mid-tones)
--
-- Each scope holds one of these effects:
--
--   desktop — applies to every pixel of an output
--   window  — applies to the focused window's rectangle
--
-- Turning desktop invert and window invert on together cancels the inversion
-- inside the focused window, which leaves that one window untouched against an
-- otherwise inverted desktop. Contrast always wins, since it is the
-- accessibility effect and should never silently disappear.
--
-- The shortcuts cycle a scope through off -> invert -> contrast -> off.

local paths = require("default.hypr.paths")

local state_dir = paths.state_home .. "/omarchy"
local shader_path = state_dir .. "/invert.frag"
local status_path = state_dir .. "/invert.status"

-- Luminance band the contrast effect stretches to the full black/yellow range.
-- Below CONTRAST_LO a pixel is black, above CONTRAST_HI it is pure yellow, so
-- text and UI chrome snap to maximum contrast while a gentle ramp between them
-- keeps photos from turning into hard two-tone blocks.
local CONTRAST_LO = 0.15
local CONTRAST_HI = 0.85

-- Hyprland has no window-moved or window-resized event, so a focused window that
-- is dragged or resized only stays covered if its rectangle is re-read on a
-- timer. The timer runs only while window mode is on, and a tick that finds the
-- rectangle unchanged rewrites nothing.
local poll_interval = 100

local invert = {}

local effects = { off = true, invert = true, contrast = true }
local cycle_next = { off = "invert", invert = "contrast", contrast = "off" }

local desktop_effect = "off"
local window_effect = "off"
local polling = false
local applied = nil
local damage_tracking = nil
local restore_pending = false

local function effect_int(effect)
  if effect == "invert" then
    return 1
  elseif effect == "contrast" then
    return 2
  end
  return 0
end

local function desktop_on()
  return desktop_effect ~= "off"
end

local function window_on()
  return window_effect ~= "off"
end

-- A focused window's border comes from the theme, and a mid-luminance theme
-- accent (Omarchy's active border is a green gradient at ~0.5 luminance) maps to
-- a dull olive under the contrast effect rather than a bright yellow. While any
-- contrast effect is live the focused border is forced to full yellow, which the
-- shader — seeing a full-luminance colour — keeps as pure yellow; the theme's
-- own border (gradient or solid, whatever it is) is put back when the last
-- contrast effect goes away.
local saved_border = nil
local border_forced = false

local function contrast_active()
  return desktop_effect == "contrast" or window_effect == "contrast"
end

local function sync_border()
  local want = contrast_active()

  if want and not border_forced then
    saved_border = hl.get_config("general:col.active_border")
    hl.config({ general = { col = { active_border = "rgba(ffff00ff)" } } })
    border_forced = true
  elseif not want and border_forced then
    if saved_border ~= nil then
      hl.config({ general = { col = { active_border = saved_border } } })
    end
    saved_border = nil
    border_forced = false
  end
end

-- Effective effect for the focused window, given the current desktop effect.
-- The desktop-invert + window-invert pair cancels; every other combination
-- resolves to the window's own effect.
local function resolved_window_effect()
  if window_effect == "invert" and desktop_effect == "invert" then
    return "off"
  end
  return window_effect
end

local function write_file(path, contents)
  local file = io.open(path, "w")
  if not file then
    return false
  end

  file:write(contents)
  file:close()

  return true
end

-- On a transformed output Hyprland's framebuffer is rotated relative to the
-- layout coordinates a window reports, so a rectangle derived from them would
-- land somewhere else on screen. Rather than draw a misplaced box, window mode
-- applies to such an output whole: no rectangle, just the output id.
local function focused_target()
  local window = hl.get_active_window()
  if not window then
    return nil
  end

  local monitor = window.monitor
  if not monitor then
    return nil
  end

  local target = { output = monitor.id }
  if monitor.transform ~= 0 then
    return target
  end

  local width = monitor.size.width / monitor.scale
  local height = monitor.size.height / monitor.scale
  if width <= 0 or height <= 0 then
    return target
  end

  target.left = (window.at.x - monitor.position.x) / width
  target.top = (window.at.y - monitor.position.y) / height
  target.right = target.left + window.size.x / width
  target.bottom = target.top + window.size.y / height

  return target
end

local function shader_source(target)
  local lines = {
    "#version 300 es",
    "precision highp float;",
    "",
    "in vec2 v_texcoord;",
    "out vec4 fragColor;",
    "uniform sampler2D tex;",
    "uniform int wl_output;",
    "",
    "void main() {",
    "  vec4 pixel = texture(tex, v_texcoord);",
    string.format("  int mode = %d;", effect_int(desktop_effect)),
  }

  if target then
    if target.left then
      table.insert(
        lines,
        string.format(
          "  if (wl_output == %d && v_texcoord.x >= %.6f && v_texcoord.x <= %.6f && v_texcoord.y >= %.6f && v_texcoord.y <= %.6f) {",
          target.output,
          target.left,
          target.right,
          target.top,
          target.bottom
        )
      )
    else
      table.insert(lines, string.format("  if (wl_output == %d) {", target.output))
    end

    table.insert(lines, string.format("    mode = %d;", effect_int(resolved_window_effect())))
    table.insert(lines, "  }")
  end

  table.insert(lines, "  vec3 rgb = pixel.rgb;")
  table.insert(lines, "  if (mode == 1) {")
  table.insert(lines, "    rgb = vec3(1.0) - rgb;")
  table.insert(lines, "  } else if (mode == 2) {")
  table.insert(lines, "    vec3 lw = vec3(0.2126, 0.7152, 0.0722);")
  table.insert(lines, "    float luma = dot(rgb, lw);")
  table.insert(lines, "    vec2 texel = 1.0 / vec2(textureSize(tex, 0));")
  table.insert(lines, "    float ln = dot(texture(tex, v_texcoord - vec2(3.0 * texel.x, 0.0)).rgb, lw);")
  table.insert(lines, "    float rn = dot(texture(tex, v_texcoord + vec2(3.0 * texel.x, 0.0)).rgb, lw);")
  table.insert(lines, string.format(
    "    float base = smoothstep(%.2f, %.2f, luma);", CONTRAST_LO, CONTRAST_HI
  ))
  table.insert(lines, "    float bg = 0.5 * (ln + rn);")
  table.insert(lines, "    float outl = (abs(ln - rn) < 0.06 && abs(luma - bg) > 0.06) ? (bg > 0.5 ? 0.0 : 1.0) : base;")
  table.insert(lines, "    rgb = vec3(outl) * vec3(1.0, 1.0, 0.0);")
  table.insert(lines, "  }")
  table.insert(lines, "  fragColor = vec4(rgb, pixel.a);")
  table.insert(lines, "}")

  return table.concat(lines, "\n") .. "\n"
end

local function apply()
  sync_border()

  local target = nil
  if window_on() then
    target = focused_target()
  end

  if not desktop_on() and not target then
    if applied ~= nil then
      applied = nil
      hl.config({ decoration = { screen_shader = "" } })
    end

    return
  end

  local source = shader_source(target)
  if source == applied then
    return
  end

  if write_file(shader_path, source) then
    applied = source
    -- Hyprland recompiles the shader whenever the option is set, even when the
    -- path has not changed, so rewriting this one file moves the rectangle.
    hl.config({ decoration = { screen_shader = shader_path } })
  end
end

-- A self-rescheduling oneshot rather than a repeating timer: turning window mode
-- off simply stops the chain, so there is no timer handle to hold or cancel.
local function poll()
  if not window_on() then
    polling = false
    return
  end

  apply()
  hl.timer(poll, { timeout = poll_interval, type = "oneshot" })
end

local function start_polling()
  if window_on() and not polling then
    polling = true
    hl.timer(poll, { timeout = poll_interval, type = "oneshot" })
  end
end

-- Hyprland runs the screen shader over damaged regions only, and nothing tells it
-- that the shader's own output changed. Undamaged areas keep the pixels the
-- previous shader produced, so the screen breaks into inverted and uninverted
-- fragments — as windows are focused and typed into while the rectangle moves,
-- and on the frame a mode is switched at all. Hyprland's answer for a screen
-- shader that changes is to turn damage tracking off, which redraws whole frames
-- continuously and costs GPU time.
--
-- So it goes off for exactly as long as the screen can still be wrong: for as
-- long as window mode runs, since its rectangle keeps moving, and otherwise just
-- long enough for the new shader to cover the screen. The user's own setting is
-- what gets put back.
local settle_duration = 150

local function suspend_damage_tracking()
  if damage_tracking == nil then
    damage_tracking = hl.get_config("debug.damage_tracking") or 2
    hl.config({ debug = { damage_tracking = 0 } })
  end
end

local function restore_damage_tracking()
  if damage_tracking ~= nil then
    hl.config({ debug = { damage_tracking = damage_tracking } })
    damage_tracking = nil
  end
end

local function settle_damage_tracking()
  suspend_damage_tracking()

  if window_on() or desktop_on() or restore_pending then
    return
  end

  restore_pending = true
  hl.timer(function()
    restore_pending = false

    if not window_on() and not desktop_on() then
      restore_damage_tracking()
    end
  end, { timeout = settle_duration, type = "oneshot" })
end

local function write_status()
  write_file(status_path, string.format(
    "desktop=%s\nwindow=%s\n",
    desktop_effect, window_effect
  ))
end

local function refresh()
  start_polling()
  apply()
  settle_damage_tracking()
  write_status()
end

-- set(scope, effect): set an explicit effect on a scope.
--   set('desktop', 'off'|'invert'|'contrast')  — the whole output
--   set('window',  'off'|'invert'|'contrast')  — the focused window
function invert.set(scope, effect)
  if not effects[effect] then
    return
  end

  if scope == "desktop" then
    desktop_effect = effect
    refresh()
    return
  end

  if scope == "window" then
    window_effect = effect
    refresh()
    return
  end
end

-- cycle(scope): advance a scope through off -> invert -> contrast -> off.
--   cycle('desktop') — the whole output
--   cycle('window')  — the focused window
function invert.cycle(scope)
  if scope == "desktop" then
    invert.set("desktop", cycle_next[desktop_effect])
  elseif scope == "window" then
    invert.set("window", cycle_next[window_effect])
  end
end

-- Backwards-compatible alias: the original API exposed toggle() for both scopes.
invert.toggle = invert.cycle

-- A newly focused window needs the rectangle moved before the next poll tick, so
-- the eye never catches the old one. Fullscreening keeps the same window focused
-- but resizes it, and a monitor coming or going renumbers outputs.
hl.on("window.active", apply)
hl.on("window.fullscreen", apply)
hl.on("monitor.layout_changed", apply)

write_status()

return invert
