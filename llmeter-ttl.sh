#!/usr/bin/env zsh
# llmeter ttl — recommend a prompt-cache lifetime from your own transcripts.
#
# Reads ~/.claude/projects (or $CLAUDE_CONFIG_DIR/projects) offline and prints
# which of Claude Code's two cache TTLs is cheaper for the way you actually
# work, per setting. No network. It writes nothing unless --html asks for a
# page, and then only that page, into the directory you run it from. Run it by
# hand whenever you want the number.
#
#   ./llmeter-ttl.sh              # last 14 days
#   ./llmeter-ttl.sh --days 30
#   ./llmeter-ttl.sh --json
#   ./llmeter-ttl.sh --html       # adds llmeter-ttl.html, with the reasoning
#
# `${0:A:h}` resolves to this script's own directory, so the repo works
# wherever it is checked out. Override with LLMETER_REPO if you symlink it.

REPO="${LLMETER_REPO:-${0:A:h}}"
PYTHONPATH="$REPO" exec /usr/bin/env python3 -m llmeter.ttl "$@"
