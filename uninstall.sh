#!/bin/zsh
# llmeter uninstaller — removes the status line from Claude Code.
#
# Removes the `statusLine` key from ~/.claude/settings.json ONLY if it points
# at llmeter's wrapper (never touches a statusLine you set to something else).
# Backs up first and validates the result. Optionally clears the harvested
# data dir.
#
# Usage:
#   ./uninstall.sh            # remove the statusLine key
#   ./uninstall.sh --purge    # also rm -r ~/.claude/llmeter/
set -eu

REPO="${0:A:h}"
WRAPPER="$REPO/llmeter-statusline.sh"
SETTINGS="${LLMETER_SETTINGS:-${HOME:?llmeter: set HOME or LLMETER_SETTINGS}/.claude/settings.json}"
PURGE="no"
[ "${1:-}" = "--purge" ] && PURGE="yes"

if ! command -v python3 >/dev/null 2>&1; then
  echo "llmeter: python3 not found on PATH." >&2
  exit 1
fi

WRAPPER="$WRAPPER" SETTINGS="$SETTINGS" python3 - <<'PY'
import glob, json, os, shlex, shutil, sys, time

# Resolve symlinks + build the command identically to install (quoted wrapper
# path) so the "does this statusLine belong to llmeter?" comparison matches.
settings = os.path.realpath(os.environ["SETTINGS"])
command  = "/bin/zsh " + shlex.quote(os.environ["WRAPPER"])

if not (os.path.exists(settings) and os.path.getsize(settings) > 0):
    print(f"llmeter: no settings file at {settings} — nothing to remove.")
    sys.exit(0)
try:
    with open(settings, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("not an object")
except ValueError as e:
    print(f"llmeter: {settings} is not valid JSON ({e}) — leaving it untouched.", file=sys.stderr)
    sys.exit(1)

sl = data.get("statusLine")
if not sl:
    print("llmeter: no statusLine key set — nothing to remove.")
    sys.exit(0)
if not isinstance(sl, dict) or sl.get("command") != command:
    shown = sl.get("command") if isinstance(sl, dict) else sl
    print("llmeter: the statusLine is not llmeter's — leaving it untouched:")
    print(f"    {shown!r}")
    sys.exit(0)

backup = f"{settings}.llmeter-bak-{int(time.time())}"
shutil.copy2(settings, backup)
for old in sorted(glob.glob(f"{settings}.llmeter-bak-*"))[:-5]:
    try:
        os.remove(old)
    except OSError:
        pass
del data["statusLine"]

tmp = settings + ".llmeter-tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
os.replace(tmp, settings)
print(f"llmeter: removed statusLine from {settings} (backup: {backup})")
PY

if [ "$PURGE" = "yes" ]; then
  DIR="${LLMETER_DIR:-$HOME/.claude/llmeter}"
  rm -rf "$DIR"
  echo "llmeter: purged data dir $DIR"
fi
