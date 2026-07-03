#!/bin/zsh
# llmeter installer — registers the status line in Claude Code.
#
# Installing llmeter is a single top-level key in ~/.claude/settings.json;
# Claude Code invokes the command for you and hot-reloads the setting. This
# script does that merge SAFELY: it backs up the existing settings, adds (or
# updates) only the `statusLine` key, validates the result parses, and rolls
# back if anything goes wrong. Idempotent — safe to re-run.
#
# Usage:
#   ./install.sh                 # install to ~/.claude/settings.json
#   LLMETER_SETTINGS=/path ./install.sh   # install to a different settings file
#
# Requires: python3 (already required to run llmeter itself).
set -eu

REPO="${0:A:h}"
WRAPPER="$REPO/llmeter-statusline.sh"
SETTINGS="${LLMETER_SETTINGS:-$HOME/.claude/settings.json}"
COMMAND="/bin/zsh $WRAPPER"

if ! command -v python3 >/dev/null 2>&1; then
  echo "llmeter: python3 not found on PATH — it is required to run the status line." >&2
  exit 1
fi
if [ ! -f "$WRAPPER" ]; then
  echo "llmeter: wrapper not found at $WRAPPER (run install.sh from inside the repo)." >&2
  exit 1
fi
chmod +x "$WRAPPER" "$REPO/install.sh" "$REPO/uninstall.sh" 2>/dev/null || true

REPO="$REPO" WRAPPER="$WRAPPER" SETTINGS="$SETTINGS" COMMAND="$COMMAND" python3 - <<'PY'
import json, os, shutil, sys, time

settings = os.environ["SETTINGS"]
command  = os.environ["COMMAND"]

os.makedirs(os.path.dirname(settings), exist_ok=True)

# Load existing settings (tolerate absent / empty; refuse to clobber a file we
# cannot parse — better to stop than to destroy a hand-edited config).
data = {}
if os.path.exists(settings) and os.path.getsize(settings) > 0:
    try:
        with open(settings) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print(f"llmeter: {settings} is not a JSON object — aborting, nothing changed.", file=sys.stderr)
            sys.exit(1)
    except ValueError as e:
        print(f"llmeter: {settings} is not valid JSON ({e}) — aborting, nothing changed.", file=sys.stderr)
        sys.exit(1)

desired = {"type": "command", "command": command, "refreshInterval": 60}
if data.get("statusLine") == desired:
    print(f"llmeter: already installed in {settings} — no change.")
    sys.exit(0)

existing = data.get("statusLine")
if existing and existing.get("command") != command:
    print(f"llmeter: NOTE — replacing an existing statusLine command:\n    {existing.get('command')}")

# Back up before writing (only when a real file exists).
if os.path.exists(settings) and os.path.getsize(settings) > 0:
    backup = f"{settings}.llmeter-bak-{int(time.time())}"
    shutil.copy2(settings, backup)
    print(f"llmeter: backed up existing settings -> {backup}")

data["statusLine"] = desired

# Atomic write, then re-parse to prove we produced valid JSON.
tmp = settings + ".llmeter-tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
try:
    with open(tmp) as f:
        json.load(f)
except ValueError as e:
    os.remove(tmp)
    print(f"llmeter: internal error — produced invalid JSON ({e}); nothing changed.", file=sys.stderr)
    sys.exit(1)
os.replace(tmp, settings)
print(f"llmeter: installed statusLine -> {settings}")
PY

echo ""
echo "llmeter installed. Start a new Claude Code session (or send a message) and"
echo "look under the prompt for:  <model> · ctx N% · wk N% (resets …)"
echo "Verify the capture:  cat ~/.claude/llmeter/usage-snapshot.json"
echo "Uninstall:  $REPO/uninstall.sh"
