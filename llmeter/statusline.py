"""llmeter statusline entry point — ``python3 -m llmeter.statusline``.

Reads one usage payload on stdin, harvests it via the Claude Code adapter,
and prints the ambient status line. Wired into Claude Code as the
``statusLine`` command by ``install.sh`` (see the wrapper
``llmeter-statusline.sh``). Fail-soft: any error still prints a line so the
host prompt is never broken.

When a payload lacks caps (a fresh window before its first API response), the
account-level cap is filled from the freshest snapshot any window persisted —
the weekly cap is account-wide, so last-writer-wins is correct.
"""

import json
import sys

from . import core
from .adapters import claude_code


def main():
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}

    reading = claude_code.parse(data)

    try:
        snap = core.write_snapshot(reading)
    except Exception:  # never break the status line over a harvest problem
        snap = None
    if snap is None:
        # This payload had no cap data — show the freshest account-level cap
        # captured by any window instead of dropping the wk field.
        try:
            snap = core.read_snapshot()
        except Exception:
            snap = None

    try:
        print(core.format_line(reading, snap))
    except Exception:  # schema surprise in formatting: still print SOMETHING
        print("llmeter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
