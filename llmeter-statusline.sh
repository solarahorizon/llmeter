#!/bin/zsh
# llmeter — Claude Code status-line command.
#
# Claude Code pipes a JSON payload to this script on every message; it prints
# a one-line `model · ctx N% · wk N%` readout and harvests the real /usage cap
# data to ~/.claude/llmeter/ before the payload is discarded. Zero tokens,
# zero network, ~20ms.
#
# You do not run this by hand — install.sh registers it as the `statusLine`
# command in ~/.claude/settings.json. `${0:A:h}` resolves to this script's own
# directory, so the repo works wherever it is checked out (no hard-coded path).
# Override with LLMETER_REPO if you symlink the wrapper elsewhere.

REPO="${LLMETER_REPO:-${0:A:h}}"
PYTHONPATH="$REPO" exec /usr/bin/env python3 -m llmeter.statusline
