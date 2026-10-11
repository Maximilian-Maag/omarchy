#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

require_command jq

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

bindir="$tmp/bin"
mkdir -p "$bindir" "$tmp/home"
log="$tmp/hyprctl.log"
cat > "$bindir/hyprctl" <<'STUB'
#!/bin/bash
echo "$*" >> "$HYPRCTL_LOG"
exit 0
STUB
chmod 755 "$bindir/hyprctl"

export HYPRCTL_LOG="$log"
export XDG_STATE_HOME="$tmp/state"
export HOME="$tmp/home"
export PATH="$bindir:$ROOT/bin:$PATH"

run_cli() { omarchy-toggle-invert "$@"; }
calls() { cat "$log" 2>/dev/null || true; }

# ── dispatch: every scope/action reaches the module as the expected call ─────
while IFS='|' read -r args expected; do
  : > "$log"
  # shellcheck disable=SC2086
  run_cli $args
  grep -qF "require('default.hypr.invert').$expected" "$log" \
    || fail "omarchy toggle invert $args dispatches $expected" "got: $(calls)"
  pass "omarchy toggle invert $args dispatches $expected"
done <<'CASES'
desktop on|set('desktop', 'invert')
desktop off|set('desktop', 'off')
desktop invert|set('desktop', 'invert')
desktop contrast|set('desktop', 'contrast')
desktop toggle|cycle('desktop')
window contrast|set('window', 'contrast')
window toggle|cycle('window')
window|cycle('window')
CASES

# ── validation: a bad scope/action is rejected before dispatch ───────────────
: > "$log"
if run_cli monitor on 2>/dev/null; then
  fail "an invalid scope exits non-zero"
fi
if [[ -s $log ]]; then
  fail "an invalid scope never dispatches"
fi
pass "an invalid scope is rejected without dispatching"

: > "$log"
if run_cli desktop blink 2>/dev/null; then
  fail "an invalid action exits non-zero"
fi
if [[ -s $log ]]; then
  fail "an invalid action never dispatches"
fi
pass "an invalid action is rejected without dispatching"

# ── status readout ───────────────────────────────────────────────────────────
status_file="$XDG_STATE_HOME/omarchy/invert.status"

rm -rf "$XDG_STATE_HOME"
run_cli desktop --status >/dev/null
[[ ! -d $XDG_STATE_HOME/omarchy ]] || fail "reading status does not create the state dir"
pass "reading status does not create the state dir"

mkdir -p "$(dirname -- "$status_file")"
printf 'desktop=contrast\nwindow=off\n' > "$status_file"
out=$(run_cli desktop --status)
[[ $out == '{"mode":"desktop","effect":"contrast","enabled":true}' ]] \
  || fail "status reports the contrast effect" "got: $out"
pass "status reports the contrast effect"

printf 'desktop=off\nwindow=invert\n' > "$status_file"
out=$(run_cli desktop --status)
[[ $out == '{"mode":"desktop","effect":"off","enabled":false}' ]] \
  || fail "status is per-scope" "got: $out"
pass "status is per-scope"

printf 'desktop=true\nwindow=false\n' > "$status_file"
out=$(run_cli desktop --status)
[[ $out == '{"mode":"desktop","effect":"invert","enabled":true}' ]] \
  || fail "a pre-effect-model boolean status still reads" "got: $out"
pass "a pre-effect-model boolean status still reads"

# ── the module's generated shader (skipped where lua is unavailable) ─────────
if command -v lua >/dev/null; then
  cat > "$tmp/shader.lua" <<'LUA'
package.path = os.getenv("ROOT") .. "/default/hypr/?.lua;" .. package.path
local state_home = os.getenv("XDG_STATE_HOME")
package.preload["default.hypr.paths"] = function()
  return { state_home = state_home, home = os.getenv("HOME") }
end

local windows = {}
local active = nil
hl = {
  get_windows = function() return windows end,
  get_active_window = function() return active end,
  config = function() end,
  get_config = function() return 2 end,
  timer = function() end,
  on = function() end,
}
os.execute("mkdir -p '" .. state_home .. "/omarchy'")

local function shader()
  local f = io.open(state_home .. "/omarchy/invert.frag", "r")
  if not f then return "" end
  local s = f:read("a"); f:close(); return s
end

local function ok(cond, msg)
  if cond then io.write("ok - ", msg, "\n")
  else io.write("not ok - ", msg, "\n"); os.exit(1) end
end

-- desktop -> invert -> contrast: the shader selects mode 1 then mode 2.
package.loaded["invert"] = nil
local inv = require("invert")
inv.cycle("desktop")
ok(shader():find("int mode = 1;", 1, true) and shader():find("vec3(1.0) - rgb", 1, true),
   "desktop invert selects the mode-1 invert branch")
inv.cycle("desktop")
ok(shader():find("int mode = 2;", 1, true) and shader():find("smoothstep(0.15, 0.85, luma)", 1, true)
   and shader():find("vec3(1.0, 1.0, 0.0)", 1, true),
   "desktop contrast selects the yellow-on-black branch")

-- desktop invert + focused window invert cancel inside the rectangle.
windows = { { address = "0x1", at = { x = 0, y = 0 }, size = { x = 100, y = 100 },
              monitor = { id = 0, transform = 0, position = { x = 0, y = 0 },
                          size = { width = 100, height = 100 }, scale = 1.0,
                          active_workspace = { id = 1 } },
              workspace = { id = 1 } } }
active = windows[1]
inv.set("desktop", "invert")
inv.set("window", "invert")
ok(shader():find("    mode = 0;", 1, true),
   "desktop invert + window invert cancels inside the window rectangle")
LUA
  ROOT="$ROOT" XDG_STATE_HOME="$tmp/shader-state" HOME="$tmp/home" lua "$tmp/shader.lua" \
    || fail "the module's generated shader matches the effect model"
  pass "the module's generated shader matches the effect model"
else
  pass "no lua interpreter; skipping module shader assertions"
fi
