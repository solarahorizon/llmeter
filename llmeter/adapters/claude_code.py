"""Adapter #1 — Claude Code.

Claude Code is the one tool that *pushes* its usage signal: it spawns the
configured ``statusLine`` command on every message and pipes a JSON payload
to it on stdin. For Pro/Max subscribers that payload carries ``rate_limits``
— the same five-hour / seven-day ``used_percentage`` + ``resets_at`` the
in-CLI /usage panel shows — which appears nowhere else on disk. This adapter
maps that payload to a normalized Reading.

Payload shape (Anthropic's, and theirs to change — read defensively)::

    { "session_id": "…",
      "model": {"id": "…", "display_name": "…"},
      "context_window": {"used_percentage": 30},
      "rate_limits": {"five_hour": {"used_percentage", "resets_at"},
                      "seven_day": {"used_percentage", "resets_at"}} }
"""

from .. import core

SOURCE = "claude-code"


def parse(data):
    """Claude Code statusLine payload -> normalized Reading (see core).

    Always returns a Reading (so the live line can show model + context even
    before any cap is known); ``caps`` is {} until the session's first API
    response populates ``rate_limits``. Never raises on a surprising shape.
    """
    if not isinstance(data, dict):
        data = {}
    rl = data.get("rate_limits")
    caps = rl if isinstance(rl, dict) else {}
    model = core.dget(data, "model")
    return {
        "source": SOURCE,
        "model": model.get("display_name") or model.get("id"),
        "context_pct": core.dget(data, "context_window").get("used_percentage"),
        "caps": caps,
        "cost": None,
        "session_id": data.get("session_id"),
    }
