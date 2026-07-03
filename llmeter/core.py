"""llmeter core — the vendor-neutral spine shared by every adapter.

An adapter (``llmeter.adapters.<tool>``) turns one tool's raw usage payload
into a **normalized Reading**; core persists it and renders the ambient line.

Normalized Reading (what every adapter returns) — a plain dict::

    {
        "source": "claude-code",          # which adapter produced this
        "model": "Opus 4.8 (1M context)", # display name, or None
        "context_pct": 30,                 # % of context window used, or None
        "caps": {                          # subscription cap windows (may be {})
            "seven_day": {"used_percentage": 37, "resets_at": <epoch|iso>},
            "five_hour": {"used_percentage": 1,  "resets_at": <epoch|iso>},
        },
        "cost": {"session_usd": 0.42, "tokens": 12000},  # pay-per-token, or None
        "session_id": "…",                 # opaque, for cross-window dedup
    }

Cap-metered tools (Claude Code, Codex, Antigravity) fill ``caps``.
Pay-per-token tools (DeepSeek) leave ``caps`` empty and fill ``cost``.

Everything here is stdlib-only and **fail-soft**: a malformed payload or a
hostile schema must still print *something* and never break the host tool's
prompt. The host tool owns these schemas and can change them any time, so we
read defensively and never raise from the render path.
"""

import datetime
import json
import os

# Output dir is overridable so a caller can point it at another location
# (e.g. a menu-bar consumer's dir, or a temp dir in tests).
DIR = os.environ.get("LLMETER_DIR") or os.path.join(
    os.path.expanduser("~"), ".claude", "llmeter")
SNAPSHOT_PATH = os.path.join(DIR, "usage-snapshot.json")
HISTORY_PATH = os.path.join(DIR, "usage-history.jsonl")


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def dget(obj, key):
    """dict.get that returns {} unless the value is itself a dict — so an
    adapter can walk a payload whose shape the vendor may have changed
    without ever raising on a wrong type."""
    v = obj.get(key) if isinstance(obj, dict) else None
    return v if isinstance(v, dict) else {}


def _has_usable_data(reading):
    return isinstance(reading, dict) and (reading.get("caps") or reading.get("cost"))


def write_snapshot(reading, snapshot_path=None, history_path=None, now=None):
    """Persist a normalized Reading. Returns the stored snapshot dict, or None
    if the reading carries no account-level usage worth persisting (e.g. a
    tool's first message before any usage is known).

    - The snapshot is the account-level truth used for cross-window fallback,
      so we persist only when ``caps`` or ``cost`` is present (context % is
      per-session and not persisted).
    - Write is atomic (tmp + os.replace) so a concurrent reader never sees a
      torn file — multiple CLI panes may write these same files at once.
    - History appends one line only when a cap percentage actually changes
      (the change-log a retrospective consumer joins against).
    """
    snapshot_path = snapshot_path or SNAPSHOT_PATH
    history_path = history_path or HISTORY_PATH
    if not _has_usable_data(reading):
        return None
    snap = dict(reading)
    snap["captured_at"] = now or now_iso()
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)

    prev = read_snapshot(snapshot_path, max_age_secs=None)
    if _caps_changed((prev or {}).get("caps"), reading.get("caps")):
        try:
            with open(history_path, "a") as f:
                f.write(json.dumps({"captured_at": snap["captured_at"],
                                    "caps": reading.get("caps") or {}}) + "\n")
        except OSError:
            pass

    tmp = snapshot_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f)
    os.replace(tmp, snapshot_path)  # atomic: readers never see a partial file
    return snap


def _caps_changed(prev_caps, new_caps):
    prev_caps = prev_caps or {}
    new_caps = new_caps or {}
    return any(
        (new_caps.get(w) or {}).get("used_percentage")
        != (prev_caps.get(w) or {}).get("used_percentage")
        for w in set(new_caps) | set(prev_caps))


def read_snapshot(path=None, max_age_secs=6 * 3600):
    """Latest persisted snapshot, or None if absent/malformed/older than
    max_age_secs (None = no age limit). Adds ``age_secs`` for freshness
    labels. A tz-naive ``captured_at`` is treated as local time, never raised
    on."""
    path = path or SNAPSHOT_PATH
    try:
        with open(path) as f:
            snap = json.load(f)
        if not isinstance(snap, dict):
            return None
        captured = datetime.datetime.fromisoformat(snap["captured_at"])
        if captured.tzinfo is None:
            captured = captured.astimezone()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = (datetime.datetime.now().astimezone() - captured).total_seconds()
    if max_age_secs is not None and age > max_age_secs:
        return None
    snap["age_secs"] = max(0, int(age))
    return snap


def fmt_reset(epoch):
    """'Tue 10:00' from a resets_at value (epoch seconds or ISO string)."""
    try:
        if isinstance(epoch, (int, float)):
            dt = datetime.datetime.fromtimestamp(epoch)
        else:
            dt = datetime.datetime.fromisoformat(str(epoch)).astimezone()
        return dt.strftime("%a %H:%M")
    except (ValueError, TypeError, OSError, OverflowError):
        return "?"


def format_line(reading, snap=None):
    """The visible status line: model · ctx N% · wk N% (reset) [· $cost].

    ``reading`` is this message's live reading (model + context + maybe caps).
    ``snap`` is the freshest persisted snapshot — used only to fill the
    account-level cap when THIS message's payload lacks it (a fresh window
    before its first API response). Falls back to a bare "llmeter" so the host
    tool's prompt never shows an empty or broken line.
    """
    reading = reading if isinstance(reading, dict) else {}
    parts = []
    model = reading.get("model")
    if isinstance(model, str) and model:
        parts.append(model)
    ctx = reading.get("context_pct")
    if isinstance(ctx, (int, float)):
        parts.append("ctx {:.0f}%".format(ctx))

    # Prefer this message's own caps; else the cross-window persisted snapshot.
    # Coerce every step to a dict — the host owns this schema and may hand us
    # an int/str/list where a dict is expected (see the hostile-shape tests).
    caps = reading.get("caps")
    if not (isinstance(caps, dict) and caps):
        caps = dget(snap or {}, "caps")
    week = caps.get("seven_day") if isinstance(caps, dict) else None
    week = week if isinstance(week, dict) else {}
    if isinstance(week.get("used_percentage"), (int, float)):
        parts.append("wk {:.0f}% (resets {})".format(
            week["used_percentage"], fmt_reset(week.get("resets_at"))))

    # Pay-per-token tools surface cost instead of a cap.
    cost = reading.get("cost")
    if not (isinstance(cost, dict) and cost):
        cost = dget(snap or {}, "cost")
    spent = cost.get("session_usd") if isinstance(cost, dict) else None
    if isinstance(spent, (int, float)):
        parts.append("${:.2f}".format(spent))

    return " · ".join(parts) if parts else "llmeter"
