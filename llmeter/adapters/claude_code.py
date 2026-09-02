"""Adapter #1 — Claude Code.

Claude Code is the one tool that *pushes* its usage signal: it spawns the
configured ``statusLine`` command on every message and pipes a JSON payload
to it on stdin. For Pro/Max subscribers that payload carries ``rate_limits``
— the same five-hour / seven-day ``used_percentage`` + ``resets_at`` the
in-CLI /usage panel shows — which appears nowhere else on disk. This adapter
maps that payload to a normalized Reading.

Payload shape (Anthropic's, and theirs to change — read defensively)::

    { "session_id": "…",
      "transcript_path": "/path/to/transcript.jsonl",
      "model": {"id": "…", "display_name": "…"},
      "context_window": {"used_percentage": 30,
                         "total_input_tokens": 295000,
                         "context_window_size": 1000000},
      "rate_limits": {"five_hour": {"used_percentage", "resets_at"},
                      "seven_day": {"used_percentage", "resets_at"}} }

``transcript_path`` (documented at
https://code.claude.com/docs/en/statusline: "Your status line command
receives this JSON structure via stdin" — the schema table lists
``transcript_path`` as "Path to conversation transcript file") points at the
session's own JSONL log. This adapter reads it ONLY to sum the two
cache-creation-lifetime fields on each assistant message (see
``_cache_ttl_totals``) — nothing else in the transcript is read or kept. A
small per-transcript resume file under ``core.DIR`` (see ``_ttl_cache_path``)
lets each render scan only the lines appended since the last one, rather than
re-parsing the whole transcript on every message.
"""

import hashlib
import json
import math
import os
import tempfile

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


def _ctx_window_int(v):
    """Parse a context-window token count; None on anything unusable."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _context_window_overrides():
    """model id -> true context-window tokens, for custom models Claude Code
    mis-sizes (it falls back to 200k for any model outside its own table).
    Built-in defaults are merged under the LLMETER_CONTEXT_WINDOWS env var
    ("id1=tokens1,id2=tokens2"); malformed entries are silently ignored so a
    bad env value can never break the status line."""
    overrides = {
        # Custom models routed through a proxy: Claude Code reports 200k, the
        # real window is larger. Add yours here, or set LLMETER_CONTEXT_WINDOWS.
        "qwen3.8-max": 1_000_000,
        # Retired by Alibaba 2026-08-05; kept so replayed old sessions still size right.
        "qwen3.8-max-preview": 1_000_000,
        # Moonshot Kimi. Sizes per platform.kimi.ai/docs/guide/claude-code-kimi
        # (checked 2026-08-10). The "[1m]" spelling is the model id used on the
        # pay-as-you-go endpoint; the subscription endpoint takes the bare id.
        "kimi-k3": 1_048_576,
        "kimi-k3[1m]": 1_048_576,
        "kimi-k2.7-code": 262_144,
        "kimi-k2.7-code-highspeed": 262_144,
    }
    for chunk in os.environ.get("LLMETER_CONTEXT_WINDOWS", "").split(","):
        if "=" not in chunk:
            continue
        mid, _, val = chunk.partition("=")
        n = _ctx_window_int(val.strip())
        if mid.strip() and n:
            overrides[mid.strip()] = n
    return overrides


def _session_spend(data):
    """Session cost, but only for a session routed to a third-party endpoint.

    Such a session can never show ``wk``. Claude Code fetches plan utilization
    only for the Anthropic subscription (``GET /api/oauth/usage``, an OAuth
    call that ignores ``ANTHROPIC_BASE_URL``) and forwards no cap of any kind
    in the status-line payload. Measured 2026-08-10 on Claude Code 2.1.226: 57
    consecutive payloads from a live Kimi session carried no ``rate_limits``
    key at all; the vendor returned no rate-limit response headers; and no
    usage endpoint answered on that vendor's API. So the number is not being
    dropped by llmeter, it never arrives.

    ``cost.total_cost_usd`` does arrive, and for a metered vendor session it is
    the usage signal that matters. Rendered as ``$N.NN`` in the slot ``wk``
    cannot fill (see ``core.format_line``).

    Left as None on the default provider, where spend is not the meaningful
    number on a subscription and ``wk`` is the real signal. Returns a value for
    0.0 as well, so a fresh window shows an honest ``$0.00`` rather than
    falling back to the previous session's total.
    """
    try:
        if core.provider_key() == core.DEFAULT_PROVIDER:
            return None
    except Exception:  # never break the status line over provider detection
        return None
    spend = core.dget(data, "cost").get("total_cost_usd")
    if isinstance(spend, bool) or not isinstance(spend, (int, float)):
        return None
    if spend < 0:
        return None
    return {"session_usd": spend}


def _ttl_cache_path(transcript_path):
    """Resume-file path for one transcript's cache-TTL totals, under the same
    directory as ``usage-snapshot.json`` (``core.DIR`` — overridable via
    ``LLMETER_DIR``). Named by a hash of the transcript path, not the path
    itself, so the filename never leaks the session id or project directory
    name it usually embeds."""
    digest = hashlib.sha256(
        transcript_path.encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return os.path.join(core.DIR, "cache-ttl-{}.json".format(digest))


def _load_ttl_cache(cache_path):
    """(offset, cache_5m, cache_1h, last_id) from the prior render, or None
    if the file is missing, unreadable, or not exactly that shape. Malformed
    input reads as "no cache" — a hostile or corrupt resume file must trigger
    a fresh full scan, never a wrong number."""
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    offset = data.get("offset")
    cache_5m = data.get("cache_5m")
    cache_1h = data.get("cache_1h")
    last_id = data.get("last_id")
    if not (isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0):
        return None
    for total in (cache_5m, cache_1h):
        if not (isinstance(total, (int, float)) and not isinstance(total, bool)
                and math.isfinite(total)):
            return None
    if last_id is not None and not isinstance(last_id, str):
        return None
    return offset, cache_5m, cache_1h, last_id


def _save_ttl_cache(cache_path, offset, cache_5m, cache_1h, last_id):
    """Best-effort atomic write of the resume file. Any failure here is
    swallowed rather than raised: a broken/unwritable cache directory must
    only cost the NEXT render a full re-scan, never this one. Writes exactly
    the four fields ``_load_ttl_cache`` reads — no transcript content, no
    message text, nothing beyond the two running totals, the resume offset
    and the last message id."""
    try:
        cache_dir = os.path.dirname(cache_path)
        os.makedirs(cache_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".cache-ttl.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"offset": offset, "cache_5m": cache_5m,
                          "cache_1h": cache_1h, "last_id": last_id}, f)
            os.replace(tmp, cache_path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
    except OSError:
        pass


def _scan_transcript(f, last_id):
    """Parse complete ``type: "assistant"`` lines from the binary file object
    ``f``, starting at its current position, accumulating cache-creation
    totals. Deduped against ``last_id`` — the caller's running last-seen
    message id, not a full historical set, because on a real transcript a
    repeated id is the very next assistant line (a multi-block reply
    re-sending the same cumulative usage), and that is the only repeat this
    needs to catch across a resume boundary.

    A final line with no trailing ``\\n`` is left UNCONSUMED — the returned
    offset stops before it — so a write still in progress is picked up whole
    on the next scan rather than parsed half-written. Returns
    (end_offset, cache_5m_delta, cache_1h_delta, last_id).
    """
    cache_5m = 0
    cache_1h = 0
    offset = f.tell()
    while True:
        raw = f.readline()
        if not raw or not raw.endswith(b"\n"):
            break
        offset = f.tell()
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        msg_id = message.get("id")
        if msg_id is not None:
            if msg_id == last_id:
                continue
            last_id = msg_id
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        cc = usage.get("cache_creation")
        if not isinstance(cc, dict):
            continue
        m5 = cc.get("ephemeral_5m_input_tokens")
        h1 = cc.get("ephemeral_1h_input_tokens")
        # math.isfinite, not just isinstance: json.loads accepts the
        # non-standard literals Infinity/-Infinity/NaN, and the final
        # int(cache_5m)/int(cache_1h) raises OverflowError/ValueError on
        # those — outside this module's own except clauses — so a
        # non-finite value must be rejected here, before it is summed.
        if (isinstance(m5, (int, float)) and not isinstance(m5, bool)
                and math.isfinite(m5)):
            cache_5m += m5
        if (isinstance(h1, (int, float)) and not isinstance(h1, bool)
                and math.isfinite(h1)):
            cache_1h += h1
    return offset, cache_5m, cache_1h, last_id


def _cache_ttl_totals(transcript_path):
    """Sum ``cache_creation`` ephemeral-lifetime tokens over the session
    transcript. Resumes from a per-transcript on-disk cache (see
    ``_ttl_cache_path``) so a render only scans the bytes appended since the
    last one, not the whole transcript again — a status line fires on every
    message, so re-parsing a long-running session's full transcript each time
    would grow unboundedly with session length.

    A missing/unreadable/non-string path, or a transcript ``os.path.getsize``
    cannot read, reads as (0, 0), never raises — a NUL byte in the path
    raises ``ValueError`` from ``getsize``/``open`` themselves, not
    ``OSError``, hence the paired except below. A cached offset larger than
    the transcript's current size (the file shrank, or a new session reused
    the path) discards the cache and rescans from the start, same as a
    missing or corrupt cache. Any failure reading or writing the cache file
    itself falls back to a full scan for THIS render and never raises — see
    ``_load_ttl_cache`` / ``_save_ttl_cache``. Returns (cache_5m_tokens,
    cache_1h_tokens), both plain ints.
    """
    if not isinstance(transcript_path, str) or not transcript_path:
        return (0, 0)
    try:
        size = os.path.getsize(transcript_path)
    except (OSError, ValueError):
        return (0, 0)

    cache_path = _ttl_cache_path(transcript_path)
    cached = _load_ttl_cache(cache_path)
    if cached is not None and cached[0] <= size:
        start_offset, base_5m, base_1h, last_id = cached
    else:
        start_offset, base_5m, base_1h, last_id = 0, 0, 0, None

    try:
        with open(transcript_path, "rb") as f:
            f.seek(start_offset)
            end_offset, delta_5m, delta_1h, last_id = _scan_transcript(f, last_id)
    except (OSError, ValueError):
        return (0, 0)

    cache_5m = base_5m + delta_5m
    cache_1h = base_1h + delta_1h
    _save_ttl_cache(cache_path, end_offset, cache_5m, cache_1h, last_id)
    return (int(cache_5m), int(cache_1h))


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
    cw = core.dget(data, "context_window")
    reading = {
        "source": SOURCE,
        "model": model.get("display_name") or model.get("id"),
        "context_pct": cw.get("used_percentage"),
        # total_input_tokens is the exact sum used_percentage is computed from
        # (input + cache_creation + cache_read); context_window_size is the
        # model's max (200k, or 1M for extended-context models).
        "context_tokens": cw.get("total_input_tokens"),
        "context_window_size": cw.get("context_window_size"),
        "caps": _clean_caps(data.get("rate_limits")),
        "cost": _session_spend(data),
        "session_id": data.get("session_id"),
    }
    cache_5m, cache_1h = _cache_ttl_totals(data.get("transcript_path"))
    reading["cache_ttl"] = {"cache_5m_tokens": cache_5m, "cache_1h_tokens": cache_1h}
    # Custom-model context-window correction. Claude Code only knows the
    # window of models in its own table; for everything else (e.g. a custom
    # qwen routed through a proxy) it reports 200k. Substitute the real window
    # and recompute the percentage from the absolute token count so the line
    # (ctx% and tokens/window) stays internally consistent. Built-in defaults
    # cover known custom models; extend per-user via LLMETER_CONTEXT_WINDOWS.
    _ovs = _context_window_overrides()
    _ov = _ovs.get(model.get("id")) or _ovs.get(model.get("display_name"))
    if _ov:
        _old = reading["context_window_size"]
        _toks = reading["context_tokens"]
        if isinstance(_toks, (int, float)) and not isinstance(_toks, bool) and _toks > 0:
            reading["context_pct"] = min(100.0, _toks * 100.0 / _ov)
        elif isinstance(reading["context_pct"], (int, float)) and isinstance(_old, (int, float)) and _old > 0:
            reading["context_pct"] = reading["context_pct"] * _old / _ov
        reading["context_window_size"] = _ov
    return reading
