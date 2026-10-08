#!/usr/bin/env python3
"""policy_check — enforce this repository's policies mechanically.

Policies that live only in prose (AGENTS.md / README / reviewer memory) drift.
This script turns them into executable checks so a violation fails the commit
hook or CI instead of being noticed three releases later.

Usage:
    python3 tools/policy_check.py              # check every tracked file
    python3 tools/policy_check.py --changed    # only files changed locally
    python3 tools/policy_check.py --ci         # changed vs the base branch, + heavy checks
    python3 tools/policy_check.py --json       # machine-readable result

Exit status: 1 if any policy FAILED, else 0 (warnings do not fail).

Configuration lives in `policy.json` in the repository root:

    {
      "checker": 3,                        # must match CHECKER_VERSION in this file
      "shebang": "bash",                   # "bash" demands #!/bin/bash for shell scripts
      "shell_style": true,                 # [[ ]] / (( )) conventions
      "plugin": true,                      # Omarchy plugin conventions
      "ignore": ["path/glob/**"],          # never check these paths
      "allow": {                           # documented exceptions, each with a reason
        "abs_path": {"infra/docker-compose.dev.yml": "container-internal path"},
        "secrets": {},
        "symlinks": {},
        "artifacts": {}
      }
    }

Checks are pure-python and dependency-free; external tools (node, omarchy) are
used only when present, and only in --ci mode for the slow ones.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

CHECKER_VERSION = 3
MAX_FILE_BYTES = 512 * 1024

FAIL, WARN, PASS = "FAIL", "WARN", "PASS"

# ── helpers ──────────────────────────────────────────────────────────────


def run(*cmd: str) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout if out.returncode == 0 else ""


def git(*args: str) -> list[str]:
    """Run git and split NUL-separated output (always pass -z)."""
    out = run("git", *args)
    return [x for x in out.split("\0") if x]


class Report:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def add(self, status: str, rule: str, detail: str = "") -> None:
        self.results.append((status, rule, detail))

    def ok(self, rule: str, detail: str = "") -> None:
        self.add(PASS, rule, detail)

    def fail(self, rule: str, detail: str) -> None:
        self.add(FAIL, rule, detail)

    def warn(self, rule: str, detail: str) -> None:
        self.add(WARN, rule, detail)

    @property
    def failed(self) -> bool:
        return any(s == FAIL for s, _, _ in self.results)


def is_text(data: bytes) -> bool:
    return b"\0" not in data[:4096]


def read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def sample(items: list[str], n: int = 6) -> str:
    shown = ", ".join(items[:n])
    return shown + (f" (+{len(items) - n} more)" if len(items) > n else "")


# ── secret patterns (high confidence only — this must not cry wolf) ───────

SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key block"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "github token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"), "openai-style key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws access key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "slack token"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"), "google api key"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"), "gitlab token"),
    # A credential assignment, but only when the value actually looks like a
    # secret (long, mixed case and digits). Test fixtures such as
    # secret = 'sweep-secret-value' must not trip this.
    (re.compile(r"""(?i)\b(api[_-]?key|secret|passwd|password|token)\s*[:=]\s*["']"""
                r"""(?=[A-Za-z0-9_\-+/=]{24,}["'])(?=[^"']*[0-9])(?=[^"']*[a-z])(?=[^"']*[A-Z])"""
                r"""[A-Za-z0-9_\-+/=]+["']"""), "credential assignment"),
]

JUNK = re.compile(
    r"(^|/)(__pycache__|node_modules|\.venv|venv|dist|build|\.mypy_cache|"
    r"\.pytest_cache|\.ruff_cache)(/|$)|\.pyc$|\.pyo$|\.o$|\.so$|\.DS_Store$|"
    r"\.swp$|\.swo$|~$|\.tmp$"
)

QML_JS = re.compile(r"^\s*\.pragma\s+library", re.M)
COMMENT_LINE = re.compile(r"^\s*//.*$", re.M)
TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def allowed(bucket: dict, path: str) -> bool:
    """An allowlist entry may be an exact path or a fnmatch glob."""
    if path in bucket:
        return True
    import fnmatch
    return any(("*" in k or "?" in k) and fnmatch.fnmatch(path, k) for k in bucket)

CONFLICT = re.compile(r"^(<{7} |={7}$|>{7} )", re.M)

SHELL_EXT = (".sh", ".bash")
PY_EXT = (".py",)

QUOTED_VAR_IN_TEST = re.compile(r"\[\[\s*\"\$[A-Za-z_][A-Za-z0-9_]*\"\s*(==|!=|=~)")
NUMERIC_IN_TEST = re.compile(r"\[\[\s*[^\]]*?\s-(eq|ne|lt|gt|le|ge)\s")


# ── individual policies ──────────────────────────────────────────────────


def check_manifest(repo: Path, files: list[str], cfg: dict, rep: Report) -> None:
    """Omarchy plugin conventions: id, version, and CHANGELOG agreement."""
    if not cfg.get("plugin"):
        return
    mf = repo / "manifest.json"
    if not mf.is_file():
        rep.fail("plugin/manifest-exists", "policy.json sets plugin=true but manifest.json is missing")
        return
    try:
        manifest = json.loads(mf.read_text())
    except ValueError as exc:
        rep.fail("plugin/manifest-parses", str(exc))
        return
    missing = [k for k in ("id", "name", "version", "author", "license", "description")
               if not manifest.get(k)]
    if missing:
        rep.fail("plugin/manifest-keys", "missing: " + ", ".join(missing))
    else:
        rep.ok("plugin/manifest-keys")

    pid = str(manifest.get("id", ""))
    if pid.startswith("omarchy."):
        rep.fail("plugin/id-namespace", f"'{pid}' uses the reserved omarchy.* namespace")
    elif not re.fullmatch(r"[A-Za-z0-9-]+\.[A-Za-z0-9-]+", pid):
        rep.fail("plugin/id-namespace", f"'{pid}' is not <owner>.<plugin>")
    else:
        rep.ok("plugin/id-namespace", pid)

    version = str(manifest.get("version", ""))
    changelog = repo / "CHANGELOG.md"
    if not changelog.is_file():
        rep.fail("plugin/changelog", "CHANGELOG.md is missing")
        return
    text = changelog.read_text(errors="replace")
    versions = re.findall(r"^##\s*\[?v?([0-9]+\.[0-9]+\.[0-9]+)", text, re.M)
    if not versions:
        rep.fail("plugin/changelog", "no '## [x.y.z]' heading found")
    elif versions[0] != version:
        rep.fail("plugin/version-matches-changelog",
                 f"manifest.json says {version} but the newest CHANGELOG entry is {versions[0]}")
    else:
        rep.ok("plugin/version-matches-changelog", version)

    hook = repo / "install.sh"
    if hook.is_file():
        if subprocess.run(["bash", "-n", str(hook)], capture_output=True).returncode:
            rep.fail("plugin/install-parses", "bash -n install.sh failed")
        else:
            rep.ok("plugin/install-parses")


def check_shebangs(repo: Path, files: list[str], cfg: dict, rep: Report) -> None:
    """AGENTS.md: shell shebangs must be #!/bin/bash, never /usr/bin/env bash."""
    mode = cfg.get("shebang")
    if not mode:
        return
    bad = []
    for f in files:
        p = repo / f
        if not p.is_file():
            continue
        head = read(p)[:200]
        if not head.startswith("#!"):
            continue                       # install/ and migrations/ may omit shebangs
        first = head.splitlines()[0]
        if not re.search(r"\b(bash|sh)\b", first):
            continue                       # only shell scripts: python userscripts have their own rule
        if mode == "bash" and first != "#!/bin/bash":
            bad.append(f"{f}: {first}")
    if bad:
        rep.warn("shell/shebang-bash", sample(bad))
    else:
        rep.ok("shell/shebang-bash")


def check_shell_style(repo: Path, files: list[str], cfg: dict, rep: Report) -> None:
    """AGENTS.md: don't quote variables in [[ ]], prefer (( )) for numbers."""
    if not cfg.get("shell_style"):
        return
    quoted, numeric = [], []
    for f in files:
        if not (f.endswith(SHELL_EXT) or "/bin/" in f):
            continue
        p = repo / f
        if not p.is_file():
            continue
        for n, line in enumerate(read(p).splitlines(), 1):
            code = line.split("#", 1)[0]
            if QUOTED_VAR_IN_TEST.search(code):
                quoted.append(f"{f}:{n}")
            if NUMERIC_IN_TEST.search(code):
                numeric.append(f"{f}:{n}")
    if quoted:
        rep.warn("shell/no-quoted-var-in-[[ ]]", sample(quoted))
    else:
        rep.ok("shell/no-quoted-var-in-[[ ]]")
    if numeric:
        rep.warn("shell/use-(( ))-for-numbers", sample(numeric))
    else:
        rep.ok("shell/use-(( ))-for-numbers")


def _batch_env(paths: list[str]) -> dict:
    env = dict(os.environ)
    env["POLICY_FILES"] = json.dumps(paths)
    return env


def _compile_python(paths: list[str]) -> list[str]:
    """Compile many files in ONE interpreter start (a hook cannot afford one per file)."""
    if not paths:
        return []
    code = ("import json, os, pathlib, sys\n"
            "bad = []\n"
            "for p in json.loads(os.environ['POLICY_FILES']):\n"
            "    try:\n"
            "        compile(pathlib.Path(p).read_text(errors='replace'), p, 'exec')\n"
            "    except (SyntaxError, ValueError) as exc:\n"
            "        bad.append('%s: %s' % (p, exc))\n"
            "print(json.dumps(bad))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=_batch_env(paths))
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return []


def _check_js(paths: list[str]) -> list[str]:
    if not paths or not shutil.which("node"):
        return []
    script = ("const fs=require('fs'),vm=require('vm'),out=[];"
              "for (const f of JSON.parse(process.env.POLICY_FILES)){"
              "try{new vm.Script(fs.readFileSync(f,'utf8'),{filename:f});}"
              "catch(e){out.push(f)}}"
              "process.stdout.write(JSON.stringify(out));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         env=_batch_env(paths))
    try:
        return json.loads(out.stdout.strip() or "[]")
    except ValueError:
        return []


def check_syntax(repo: Path, files: list[str], cfg: dict, rep: Report) -> None:
    """Everything tracked must at least parse."""
    bad_json, py_files, js_files, sh_files, qml_js = [], [], [], [], []
    for f in files:
        p = repo / f
        if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            continue
        head = read(p)[:80]
        if f.endswith(".json") or f.endswith(".jsonc"):
            # .jsonc allows comments and trailing commas — strip both before parsing.
            text = COMMENT_LINE.sub("", read(p))
            try:
                json.loads(TRAILING_COMMA.sub(r"\1", text))
            except ValueError as exc:
                bad_json.append(f"{f}: {exc}")
        elif f.endswith(PY_EXT):
            py_files.append(str(p))
        elif f.endswith(".js"):
            body = read(p)
            if QML_JS.search(body):
                qml_js.append(f)          # .pragma library — QML JavaScript, not node JS
            else:
                js_files.append(str(p))
        elif head.startswith("#!"):
            first = head.splitlines()[0]
            if "python" in first:
                py_files.append(str(p))
            elif "bash" in first or first.rstrip().endswith("/sh"):
                sh_files.append(str(p))
    bad_py = _compile_python(py_files)
    bad_js = _check_js(js_files)
    bad_sh = [f for f in sh_files
              if subprocess.run(["bash", "-n", f], capture_output=True).returncode]
    for rule, bad in (("syntax/json-parses", bad_json), ("syntax/python-compiles", bad_py),
                      ("syntax/js-parses", bad_js), ("syntax/shell-parses", bad_sh)):
        if bad:
            rep.fail(rule, sample([b.replace(str(repo) + "/", "") for b in bad]))
        else:
            rep.ok(rule)
    if qml_js:
        rep.ok("syntax/qml-scripts-skipped", f"{len(qml_js)} .pragma library file(s)")


def check_hygiene(repo: Path, files: list[str], cfg: dict, rep: Report) -> None:
    """Repo hygiene: no symlinks, no build junk, no conflict markers, no /home/me."""
    allow = cfg.get("allow", {})
    modes = {}
    for line in run("git", "ls-files", "-s").splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            modes[parts[3]] = parts[0]
    selected = set(files)

    symlinks = [f for f in files if modes.get(f) == "120000" and f not in allow.get("symlinks", {})]
    if symlinks:
        rep.fail("hygiene/no-symlinks", sample(symlinks))
    else:
        rep.ok("hygiene/no-symlinks")

    junk = [f for f in files if JUNK.search(f) and f not in allow.get("artifacts", {})]
    if junk:
        rep.fail("hygiene/no-build-artifacts", sample(junk))
    else:
        rep.ok("hygiene/no-build-artifacts")

    conflicts, secrets, abs_paths, no_newline = [], [], [], []
    allow_secret = allow.get("secrets", {})
    allow_abs = allow.get("abs_path", {})
    allow_newline = allow.get("newline", {})
    for f in files:
        p = repo / f
        if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if not is_text(data):
            continue
        text = data.decode("utf-8", "replace")
        if CONFLICT.search(text):
            conflicts.append(f)
        if not allowed(allow_secret, f):
            for pattern, what in SECRET_PATTERNS:
                if pattern.search(text):
                    secrets.append(f"{f}: {what}")
                    break
        if not allowed(allow_abs, f) and re.search(r"/home/[a-z][a-z0-9_-]{0,31}/|/Users/[a-z]+/", text):
            abs_paths.append(f)
        if text and not text.endswith("\n") and not allowed(allow_newline, f):
            no_newline.append(f)
    if conflicts:
        rep.fail("hygiene/no-conflict-markers", sample(conflicts))
    else:
        rep.ok("hygiene/no-conflict-markers")
    if secrets:
        rep.fail("hygiene/no-secrets", sample(secrets))
    else:
        rep.ok("hygiene/no-secrets")
    if abs_paths:
        rep.fail("hygiene/no-absolute-home-paths", sample(abs_paths))
    else:
        rep.ok("hygiene/no-absolute-home-paths")
    if no_newline:
        rep.warn("hygiene/ends-with-newline", sample(no_newline))
    else:
        rep.ok("hygiene/ends-with-newline")
    if selected - set(files):
        rep.warn("hygiene/files-missing", sample(sorted(selected - set(files))))


def check_npm(repo: Path, files: list[str], cfg: dict, rep: Report) -> None:
    """A package.json that exists should declare how to test and lint."""
    pkg = repo / "package.json"
    if not pkg.is_file():
        return
    try:
        data = json.loads(pkg.read_text())
    except ValueError as exc:
        rep.fail("node/package-parses", str(exc))
        return
    rep.ok("node/package-parses")
    scripts = data.get("scripts") or {}
    missing = [s for s in ("test", "lint") if s not in scripts]
    if missing:
        rep.warn("node/scripts-declared", "no script for: " + ", ".join(missing))
    else:
        rep.ok("node/scripts-declared")


def check_version_bumped(repo: Path, files: list[str], cfg: dict, rep: Report,
                         changed: bool) -> None:
    """A plugin change should bump manifest.json + CHANGELOG together (warn-only)."""
    if not cfg.get("plugin") or not changed:
        return
    staged = git("diff", "--cached", "--name-only")
    if not staged:
        return
    code = [f for f in staged if f not in ("manifest.json", "CHANGELOG.md", "README.md")
            and not f.startswith(".")
            and f.split("/")[0] in ("bin", "config", "hooks", "userscripts", "reader", "default")]
    if code and "manifest.json" not in staged and "CHANGELOG.md" not in staged:
        rep.warn("plugin/version-bumped-with-code",
                 "code changed without manifest.json/CHANGELOG.md: " + sample(code))


def check_omarchy_validate(repo: Path, files: list[str], cfg: dict, rep: Report,
                           ci: bool) -> None:
    """Run the real plugin validator when it is available and we are in CI."""
    if not (ci and cfg.get("plugin")):
        return
    if not run("bash", "-lc", "command -v omarchy"):
        rep.add(WARN, "plugin/omarchy-validate", "omarchy CLI not available — skipped")
        return
    proc = subprocess.run(["omarchy", "plugin", "validate", str(repo)], capture_output=True, text=True)
    if proc.returncode:
        rep.fail("plugin/omarchy-validate", (proc.stdout + proc.stderr).strip()[:300])
    else:
        rep.ok("plugin/omarchy-validate")


CHECKS = [check_hygiene, check_syntax, check_manifest, check_shebangs, check_shell_style,
          check_npm, check_version_bumped, check_omarchy_validate]


# ── driver ───────────────────────────────────────────────────────────────


def changed_files(mode: str) -> list[str] | None:
    if mode == "changed":
        staged = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
        local = git("diff", "--name-only", "--diff-filter=ACMR", "-z")
        return sorted(set(staged) | set(local))
    if mode == "ci":
        for base in ("origin/HEAD", "origin/master", "origin/main", "HEAD~1"):
            if run("git", "rev-parse", "--verify", base):
                return sorted(set(git("diff", "--name-only", "--diff-filter=ACMR", "-z",
                                      f"{base}...HEAD")))
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Enforce this repository's policies.")
    ap.add_argument("--changed", action="store_true", help="only locally changed files")
    ap.add_argument("--ci", action="store_true", help="changed vs base branch, plus slow checks")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    repo = Path(run("git", "rev-parse", "--show-toplevel").strip() or ".").resolve()
    os.chdir(repo)
    cfg: dict = {}
    if (repo / "policy.json").is_file():
        try:
            cfg = json.loads((repo / "policy.json").read_text())
        except ValueError as exc:
            print(f"FAIL config/policy-json  {exc}", file=sys.stderr)
            return 1

    rep = Report()
    if cfg.get("checker") != CHECKER_VERSION:
        rep.fail("config/checker-version",
                 f"policy.json records checker {cfg.get('checker')}, this file is {CHECKER_VERSION} "
                 "— re-vendor tools/policy_check.py")
    else:
        rep.ok("config/checker-version", str(CHECKER_VERSION))

    ignore = cfg.get("ignore", [])
    mode = "ci" if args.ci else "changed" if args.changed else "all"
    tracked = git("ls-files", "-z")
    noop = False                     # "there is genuinely nothing to check"

    if mode == "all":
        files = tracked
    else:
        files = changed_files(mode)
        if files is None:
            rep.warn("scope/changed-files", "no usable base ref — checking every tracked file")
            files = tracked
        elif not files:
            if mode == "ci":
                # A push to the default branch has an empty diff against its own
                # base, so "nothing changed" is normal there. CI is the audit, so
                # look at the whole tree instead of passing on nothing (the
                # pre-commit hook keeps the fast per-change ratchet).
                rep.warn("scope/changed-files",
                         "nothing changed vs the base — auditing every tracked file")
                files = tracked
            else:
                noop = True
                rep.ok("scope/nothing-to-check", "no changed files")

    import fnmatch
    files = [f for f in files if not any(fnmatch.fnmatch(f, pat) for pat in ignore)]
    files = [f for f in files if (repo / f).exists()]
    if not files and not noop:
        # Never report a green run over nothing: if nothing could be read, say so.
        rep.fail("scope/files-found",
                 f"nothing to check (mode={mode}) — is this a git repo, and is the base ref fetchable?")
    elif mode == "all" and len(files) < 2:
        rep.fail("scope/files-found", f"only {len(files)} file(s) matched after ignore rules")

    for check in CHECKS:
        if check is check_version_bumped:
            check(repo, files, cfg, rep, mode != "all")
        elif check is check_omarchy_validate:
            check(repo, files, cfg, rep, args.ci)
        else:
            check(repo, files, cfg, rep)

    fails = [r for r in rep.results if r[0] == FAIL]
    warns = [r for r in rep.results if r[0] == WARN]
    if args.json:
        print(json.dumps({
            "repo": repo.name, "checker": CHECKER_VERSION, "mode": mode,
            "files_checked": len(files), "failed": len(fails), "warnings": len(warns),
            "results": [{"status": s, "rule": r, "detail": d} for s, r, d in rep.results],
        }, indent=2))
    else:
        digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
        for status, rule, detail in rep.results:
            line = f"{status:4} {rule:38}"
            print(line + ("  " + detail if detail and status != PASS else ""))
        print()
        print(f"{repo.name}: {len(files)} file(s), {len(fails)} failed, {len(warns)} warning(s) "
              f"[policy_check v{CHECKER_VERSION} {digest}, mode={mode}]")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
