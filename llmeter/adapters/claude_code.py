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

# We persist ONLY these windows + fields — an allowlist, not the raw
# rate_limits dict. If Claude Code ever nests account/plan/user metadata under
# rate_limits, it must never silently land in ~/.claude/llmeter/ (codex P2:
# that would break the "only local usage numbers are saved" privacy promise).
_WINDOWS = ("five_hour", "seven_day")
_FIELDS = ("used_percentage", "resets_at")


def _clean_caps(rl):
    """Extract only the known windows + fields from rate_limits."""
    if not isinstance(rl, dict):
        return {}
    out = {}
    for window in _WINDOWS:
        w = rl.get(window)
        if not isinstance(w, dict):
            continue
        entry = {}
        for field in _FIELDS:
            if field in w and isinstance(w[field], (int, float, str)):
                entry[field] = w[field]
        if entry:
            out[window] = entry
    return out


def parse(data):
    """Claude Code statusLine payload -> normalized Reading (see core).

    Always returns a Reading (so the live line can show model + context even
    before any cap is known); ``caps`` is {} until the session's first API
    response populates ``rate_limits``. Never raises on a surprising shape,
    and only ever carries the allowlisted usage fields (see _clean_caps).
    """
    if not isinstance(data, dict):
        data = {}
    model = core.dget(data, "model")
    return {
        "source": SOURCE,
        "model": model.get("display_name") or model.get("id"),
        "context_pct": core.dget(data, "context_window").get("used_percentage"),
        "caps": _clean_caps(data.get("rate_limits")),
        "cost": None,
        "session_id": data.get("session_id"),
    }
