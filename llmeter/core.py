"""llmeter core — the vendor-neutral spine shared by every adapter.

An adapter (``llmeter.adapters.<tool>``) turns one tool's raw usage payload
into a **normalized Reading**; core persists it and renders the ambient line.

Normalized Reading (what every adapter returns) — a plain dict::

    {
        "source": "claude-code",          # which adapter produced this
        "model": "Opus 4.8 (1M context)", # display name, or None
        "context_pct": 30,                 # % of context window used, or None
        "context_tokens": 205600,          # tokens currently in context, or None
        "context_window_size": 1000000,    # model's max context tokens, or None
        "caps": {                          # subscription cap windows (may be {})
            "seven_day": {"used_percentage": 37, "resets_at": <epoch|iso>},
            "five_hour": {"used_percentage": 1,  "resets_at": <epoch|iso>},
        },
        "cost": {"session_usd": 0.42, "tokens": 12000},  # pay-per-token, or None
        "session_id": "…",                 # opaque; keys the republish fingerprint
        "cache_ttl": {                     # session-cumulative, summed fresh
            "cache_5m_tokens": 220000,      # from the transcript every render
            "cache_1h_tokens": 12000,       # (not carried by the snapshot)
            "active": "5m",                # which bucket the last write used,
        },                                  # or None if none seen yet
    }

Cap-metered tools (Claude Code, Codex, Antigravity) fill ``caps``.
Pay-per-token tools (DeepSeek) leave ``caps`` empty and fill ``cost``.

Everything here is stdlib-only and **fail-soft**: a malformed payload or a
hostile schema must still print *something* and never break the host tool's
prompt. The host tool owns these schemas and can change them any time, so we
read defensively and never raise from the render path.
"""

import datetime
import hashlib
import json
import os
import tempfile
import urllib.parse

# Claude Code's config dir moves with CLAUDE_CONFIG_DIR, so llmeter's files
# must follow it — otherwise we'd write beside a settings.json the user never
# reads. Resolved the way the host does: the env var wins only when it is set
# to a non-empty value, and its value is used verbatim (the host does no tilde
# expansion of its own, and a shell already expanded an unquoted `~`).
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"


def config_dir():
    """Claude Code's config dir: ``$CLAUDE_CONFIG_DIR`` if populated, else
    ``~/.claude``."""
    override = (os.environ.get(CONFIG_DIR_ENV) or "").strip()
    return override or os.path.join(os.path.expanduser("~"), ".claude")


# Output dir is overridable so a caller can point it at another location
# (e.g. a menu-bar consumer's dir, or a temp dir in tests).
DIR = os.environ.get("LLMETER_DIR") or os.path.join(config_dir(), "llmeter")
SNAPSHOT_PATH = os.path.join(DIR, "usage-snapshot.json")
HISTORY_PATH = os.path.join(DIR, "usage-history.jsonl")

# Hosts that are Anthropic's own API — pointing ANTHROPIC_BASE_URL at one of
# these is still the default account, not a third party.
_ANTHROPIC_HOSTS = frozenset(("api.anthropic.com",))
_DEFAULT_PORTS = {"http": 80, "https": 443}
DEFAULT_PROVIDER = "anthropic"


def _normalized_host(host):
    """Lowercase, drop the FQDN trailing dot, IDNA-encode unicode. So
    ``API.Example.COM.`` and ``api.example.com`` are one provider, not two."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return ""
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        try:
            host = host.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            pass  # unencodable: keep the raw form, still a stable key
    return host


def _canonical_url(parts):
    """One canonical string for an endpoint, or "" if unusable.

    Every component that can select a different account is kept, and kept
    UNAMBIGUOUSLY — the previous version glued host, port and path together,
    which let distinct endpoints produce one key (codex P1): ``http://`` vs
    ``https://``, ``?account=a`` vs ``?account=b``, and an IPv6 literal plus
    port vs a longer IPv6 literal. Equivalences that are genuinely the same
    endpoint are normalised away: case, the FQDN trailing dot, unicode via
    IDNA, a scheme's default port, and a trailing slash on the path."""
    scheme = (parts.scheme or "").strip().lower()
    host = _normalized_host(parts.hostname)
    if not host:
        return ""
    if ":" in host:  # IPv6 literal — keep the brackets so host:port can't blur
        host = "[{}]".format(host)
    port = parts.port
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None
    netloc = host if port is None else "{}:{}".format(host, port)
    return "{}://{}{}".format(scheme, netloc, (parts.path or "").rstrip("/"))


def _utf8(s):
    """Bytes for hashing that never raise on a lone surrogate — an env var can
    hold anything, and losing persistence to a decode error is not fail-soft."""
    return s.encode("utf-8", "surrogatepass")


def provider_key():
    """Which API this session is pointed at, as a stable identity string.

    A usage cap belongs to an ACCOUNT, not a machine. A session routed
    elsewhere via ``ANTHROPIC_BASE_URL`` (an LLM gateway, or a vendor serving
    an Anthropic-compatible endpoint) is spending a different account's quota,
    so its numbers must never fill in for — or be merged into — the default
    account's. Unset, blank, or Anthropic's own host all mean
    ``DEFAULT_PROVIDER``, so an ordinary setup keeps exactly the paths and
    behaviour it has today.

    The key is ``<readable label>#<digest>``. The label is the canonical URL
    with every secret-bearing part removed, so a human can tell whose file is
    whose; the digest covers the label PLUS the userinfo and query, and is
    what distinguishes endpoints the label deliberately cannot show.

    Userinfo and the query string are kept out of the readable half because
    both routinely carry credentials (``alice:token@gw``, ``?api_key=…``), and
    llmeter's promise is that no credential reaches disk — not in a file, not
    in a filename. They still must affect identity, since they can select
    different accounts, so they are folded into the one-way digest only. (The
    digest is a weak offline verifier for a guessable credential, but anyone
    who can read it can already read the plaintext in your settings file.)

    "Injective" here means over distinct ENDPOINTS, not over distinct URL
    strings: forms that address the same endpoint — case, the FQDN trailing
    dot, a scheme's default port, a trailing slash — are deliberately
    normalised together, and Anthropic's own host is always the default
    account whatever the scheme.

    Claude Code applies a settings ``env`` block to the processes it spawns,
    which is how a per-project base URL reaches the status line at all
    (verified 2026-08-09 against Claude Code 2.1.226).

    Fail-soft: any unparseable or hostile value reads as the default account
    rather than raising, because a status line that crashes is worse than one
    that is conservative.
    """
    raw = (os.environ.get("ANTHROPIC_BASE_URL") or "").strip()
    if not raw:
        return DEFAULT_PROVIDER
    try:
        parts = urllib.parse.urlsplit(raw)
        label = _canonical_url(parts)
        if not label:
            return DEFAULT_PROVIDER
        if _normalized_host(parts.hostname) in _ANTHROPIC_HOSTS:
            return DEFAULT_PROVIDER
        secret = "{}:{}\x00{}".format(parts.username or "",
                                      parts.password or "", parts.query or "")
        digest = hashlib.sha256(_utf8(label + "\x00" + secret)).hexdigest()[:16]
    except Exception:  # malformed URL, bad port, lone surrogate: stay usable
        return DEFAULT_PROVIDER
    return "{}#{}".format(label, digest)


def _provider_slug(provider):
    """Filesystem-safe, length-bounded filename fragment.

    The readable part is best-effort for a human reading ``ls``; the 64-bit
    digest is what separates two providers whose readable parts sanitise to
    the same thing, and it bounds the name so a maximum-length hostname can
    never blow the filesystem's limit (codex P1/P3). 64 bits makes an
    accidental collision negligible rather than impossible — and a collision
    here cannot expose the wrong cap regardless, because ``read_snapshot``
    verifies the stamped provider before using a file's contents."""
    keep = "abcdefghijklmnopqrstuvwxyz0123456789.-_"
    readable = "".join(c if c in keep else "-" for c in provider.lower())
    readable = readable.strip("-.")[:48] or "provider"
    digest = hashlib.sha256(_utf8(provider)).hexdigest()[:16]
    return "{}-{}".format(readable, digest)


def snapshot_path_for(provider=None):
    """Snapshot path for a provider. The default account keeps the original
    filename untouched, so existing readers of ``usage-snapshot.json`` see no
    change; every other provider gets its own file. Separate files (rather
    than one file with a provider field) mean the two accounts can never
    overwrite or merge into each other, even when panes on both are live."""
    provider = provider or provider_key()
    if provider == DEFAULT_PROVIDER:
        return SNAPSHOT_PATH
    return os.path.join(DIR, "usage-snapshot.{}.json".format(
        _provider_slug(provider)))


def history_path_for(provider=None):
    """History path for a provider. Split for the same reason as the
    snapshot, and so a consumer written before this change never reads another
    account's rows as if they were the default account's (codex P2)."""
    provider = provider or provider_key()
    if provider == DEFAULT_PROVIDER:
        return HISTORY_PATH
    return os.path.join(DIR, "usage-history.{}.jsonl".format(
        _provider_slug(provider)))


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


# The ONLY Reading fields that ever reach disk (CONTRIBUTING ground rule 3):
# every persisted field is explicitly allowlisted, so an adapter (or a future
# Reading extension) can never silently widen what lands in ~/.claude/llmeter/.
# ``sessions`` is on this list too even though core computes it rather than
# copying it from the Reading (see write_snapshot) — it holds a hash and a
# timestamp per session, never the raw context_tokens/caps it was hashed from.
_SNAPSHOT_FIELDS = ("source", "model", "context_pct", "context_tokens",
                    "context_window_size", "caps", "cost", "session_id",
                    "sessions")

# Written by core itself rather than copied from a Reading, so the allowlist
# above still holds: no adapter can widen what is persisted. Listed explicitly
# so "what reaches disk" stays answerable from one place (codex P2).
# NOTE: `provider` records the HOST (and path) of a non-default
# ANTHROPIC_BASE_URL. If that endpoint is an internal gateway, redact it before
# pasting a snapshot into a bug report — see CONTRIBUTING.
_STAMPED_FIELDS = ("captured_at", "provider")

# What a session's republish fingerprint hashes: every persisted Reading field
# except the key of the map itself and the map core writes.
_FINGERPRINT_FIELDS = tuple(k for k in _SNAPSHOT_FIELDS
                            if k not in ("session_id", "sessions"))


def _session_id(reading):
    """The reading's session id, if it's a nonempty string, else None. A
    missing/non-string/empty id can't key a fingerprint, so ``write_snapshot``
    treats that reading as fresh on every call rather than deduping it."""
    sid = reading.get("session_id") if isinstance(reading, dict) else None
    return sid if isinstance(sid, str) and sid else None


def _session_fingerprint(reading):
    """sha256 hex of every ``_SNAPSHOT_FIELDS`` value the reading carries, or
    None if they can't be serialized. Derived from the allowlist, not listed by
    hand, so it covers every persisted field a session can change on its own
    by construction — ``cost`` alone on a pay-per-token proxy, ``model`` and
    ``context_window_size`` on a model switch, ``context_pct`` alone. An idle
    session republishes these byte-identical on every statusline refresh; a
    session that just received an API response changes at least one. That
    difference separates a stale replay from a genuine change, a cap reset
    included — a value-based rule cannot, since a reset also arrives as a
    lower number in the same window."""
    reading = reading if isinstance(reading, dict) else {}
    try:
        payload = json.dumps({k: reading.get(k) for k in _FINGERPRINT_FIELDS},
                             sort_keys=True)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(_utf8(payload)).hexdigest()


def _prune_sessions(sessions, now_dt):
    """Keep only entries whose ``at`` parses as a timestamp within 24h of
    ``now_dt``, so a long-running install's session map does not grow
    forever. An entry with an unparseable ``at`` or a non-string ``fp`` is
    dropped, not kept — a hostile or legacy shape must not survive."""
    sessions = sessions if isinstance(sessions, dict) else {}
    kept = {}
    for sid, entry in sessions.items():
        if not isinstance(sid, str) or not isinstance(entry, dict):
            continue
        fp, at = entry.get("fp"), entry.get("at")
        if not isinstance(fp, str) or not fp or not isinstance(at, str):
            continue
        try:
            at_dt = datetime.datetime.fromisoformat(at)
        except ValueError:
            continue
        if at_dt.tzinfo is None:
            at_dt = at_dt.astimezone()
        if (now_dt - at_dt).total_seconds() > 24 * 3600:
            continue
        kept[sid] = {"fp": fp, "at": at}
    return kept


def write_snapshot(reading, snapshot_path=None, history_path=None, now=None):
    """Persist a normalized Reading. Returns the stored snapshot dict, or None
    if the reading carries no account-level usage worth persisting (e.g. a
    tool's first message before any usage is known).

    - The snapshot is the account-level truth used for cross-window fallback,
      so we persist only when ``caps`` or ``cost`` is present.
    - Only ``_SNAPSHOT_FIELDS`` are written — a field not on that allowlist
      never reaches disk, whatever an adapter puts in the Reading.
    - A reading whose fingerprint (see ``_session_fingerprint``) matches the
      stored fingerprint for its ``session_id`` is a republish of an unchanged
      payload: the merge and the history append are both skipped, and the
      previously stored snapshot is returned untouched. A reading with no
      usable session id (missing, non-string, empty) gets no fingerprint and
      is always treated as fresh.
    - Write is atomic (tmp + os.replace) so a concurrent reader never sees a
      torn file — multiple CLI panes may write these same files at once.
    - History appends one line only when a cap percentage actually changes
      (the change-log a retrospective consumer joins against).
    - Snapshots are per-provider: a session routed at another account writes
      its own file and merges only against that file, so one account's cap can
      never be folded into another's.
    """
    provider = provider_key()
    snapshot_path = snapshot_path or snapshot_path_for(provider)
    history_path = history_path or history_path_for(provider)
    if not _has_usable_data(reading):
        return None

    # provider-checked: never merge another account's percentages into this
    # one, even if both somehow resolved to the same file.
    prev = read_snapshot(snapshot_path, max_age_secs=None, provider=provider)
    prev_sessions = (prev or {}).get("sessions")
    prev_sessions = prev_sessions if isinstance(prev_sessions, dict) else {}

    sid = _session_id(reading)
    fp = _session_fingerprint(reading) if sid is not None else None
    captured_at = now or now_iso()
    now_dt = _as_datetime(captured_at)
    if fp is not None and isinstance(prev_sessions.get(sid), dict) \
            and prev_sessions[sid].get("fp") == fp:
        # A republish renews only this session's entry, so the prune measures
        # liveness rather than time since the payload last changed. It re-reads
        # the file first and writes THAT copy: `prev` may predate another pane's
        # reset, and writing it back would revert the reset.
        latest = read_snapshot(snapshot_path, max_age_secs=None, provider=provider) or prev
        latest = dict(latest)
        latest.pop("age_secs", None)
        sessions = latest.get("sessions")
        sessions = dict(sessions) if isinstance(sessions, dict) else {}
        sessions[sid] = {"fp": fp, "at": captured_at}
        latest["sessions"] = _prune_sessions(sessions, now_dt)
        os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
        _write_atomic(snapshot_path, latest)
        return latest

    snap = {k: reading[k] for k in _SNAPSHOT_FIELDS if k in reading}
    snap["captured_at"] = captured_at
    # Stamped by core, not copied from the Reading: the provider is a property
    # of the environment the session runs in, not of anything an adapter parsed.
    snap["provider"] = provider
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)

    # Unconditional: {} is safer than leaving a hostile non-dict "caps" from
    # the raw reading in the snapshot.
    snap["caps"] = _merge_caps((prev or {}).get("caps"), snap.get("caps"))
    # History logs the MERGED truth: a stale session re-publishing old numbers
    # merges to no-change and appends nothing (no more flapping in the log).
    if _caps_changed((prev or {}).get("caps"), snap.get("caps")):
        try:
            with open(history_path, "a") as f:
                f.write(json.dumps({"captured_at": snap["captured_at"],
                                    "provider": provider,
                                    "caps": snap.get("caps") or {}}) + "\n")
        except OSError:
            pass

    # This reading's fingerprint is the one future republishes from the same
    # session will be compared against; other sessions' entries carry over
    # untouched except for the 24h prune.
    if fp is not None:
        prev_sessions = dict(prev_sessions)
        prev_sessions[sid] = {"fp": fp, "at": captured_at}
    snap["sessions"] = _prune_sessions(prev_sessions, now_dt)
    _write_atomic(snapshot_path, snap)
    return snap


def _as_datetime(iso):
    """An aware datetime for an ISO timestamp; now() when it does not parse."""
    try:
        parsed = datetime.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return datetime.datetime.now().astimezone()
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _write_atomic(snapshot_path, snap):
    """Write ``snap`` as JSON to ``snapshot_path`` through a unique temp file
    and ``os.replace``, so concurrent panes never clobber each other's
    in-flight write and a reader always sees a whole old-or-new file. Raises
    OSError after removing its temp file."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(snapshot_path),
                               prefix=".usage-snapshot.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, snapshot_path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _cap_pct(win):
    """A cap window's used_percentage, or None if the window is unusable.

    bool is excluded deliberately: JSON true is not 1%, and isinstance(True,
    int) would otherwise let it render and win a merge. One guard shared by
    the merge and the render path, so both agree on what a window even is."""
    if not isinstance(win, dict):
        return None
    pct = win.get("used_percentage")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    return pct


def _merge_caps(prev_caps, new_caps):
    """Merge cap windows into the account-level truth. ``write_snapshot``
    already filters out a session's byte-identical republish by fingerprint
    before calling this, so every ``new_caps`` window that reaches here is a
    reading that actually changed — freshness decides the winner, not size.
    Per window: a later resets_at names a newer window and wins outright; an
    earlier one loses; the SAME resets_at is the SAME window, where the new
    reading always replaces the stored one (a cap reset included). When only
    one side names a window at all, the named side wins — a legacy/hostile
    entry with no resets_at must never block every future window forever.
    Neither side naming a window is the one shape with no freshness signal at
    all, so the higher percentage is kept there.

    Which window a reading belongs to is decided by the PARSED instant, not by
    the wire type: an epoch int and an ISO string naming the same instant are
    one window, not two."""
    prev_caps = prev_caps if isinstance(prev_caps, dict) else {}
    new_caps = new_caps if isinstance(new_caps, dict) else {}
    merged = {}
    for w in set(prev_caps) | set(new_caps):
        old_w, new_w = prev_caps.get(w), new_caps.get(w)
        old_pct, new_pct = _cap_pct(old_w), _cap_pct(new_w)
        if old_pct is None and new_pct is None:
            continue  # neither side is a valid window: store nothing
        if old_pct is None or new_pct is None:
            merged[w] = new_w if new_pct is not None else old_w
            continue
        old_r = _reset_epoch(old_w.get("resets_at"))
        new_r = _reset_epoch(new_w.get("resets_at"))
        if old_r is not None and new_r is not None:
            merged[w] = new_w if new_r >= old_r else old_w
        elif (old_r is None) != (new_r is None):
            # Only one side is window-identified: it wins — otherwise a
            # legacy/hostile entry with no resets_at but a higher % would
            # block every future window forever (deepseek review).
            merged[w] = new_w if new_r is not None else old_w
        else:
            merged[w] = new_w if new_pct >= old_pct else old_w
    return merged


def _caps_changed(prev_caps, new_caps):
    prev_caps = prev_caps if isinstance(prev_caps, dict) else {}
    new_caps = new_caps if isinstance(new_caps, dict) else {}

    def _pct(caps, w):
        v = caps.get(w)
        return v.get("used_percentage") if isinstance(v, dict) else None

    return any(_pct(new_caps, w) != _pct(prev_caps, w)
               for w in set(new_caps) | set(prev_caps))


def read_snapshot(path=None, max_age_secs=6 * 3600, provider=None):
    """Latest persisted snapshot, or None if absent/malformed/older than
    max_age_secs (None = no age limit). Adds ``age_secs`` for freshness
    labels. A tz-naive ``captured_at`` is treated as local time, never raised
    on.

    With no explicit path this reads the CURRENT provider's snapshot, so a
    session routed at another account never borrows the default account's
    numbers.

    The stamped ``provider`` is then VERIFIED against the caller's, and a
    mismatch reads as absent. Path separation alone was not enough: an
    explicit ``path`` argument bypassed it entirely, and any future slug
    collision would silently reopen the bug this fix exists to close (codex
    P1/P2). A snapshot written before this field existed is treated as the
    default account, which is what it was. Pass ``provider=False`` to skip the
    check when deliberately inspecting another account's file."""
    path = path or snapshot_path_for()
    want = provider_key() if provider is None else provider
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
    if want is not False:
        stamped = snap.get("provider")
        if not isinstance(stamped, str) or not stamped:
            stamped = DEFAULT_PROVIDER  # pre-dates the field: it was the default
        if stamped != want:
            return None
    age = (datetime.datetime.now().astimezone() - captured).total_seconds()
    if max_age_secs is not None and age > max_age_secs:
        return None
    snap["age_secs"] = max(0, int(age))
    return snap


def _reset_epoch(value):
    """Epoch seconds for a resets_at value, or None if it names no instant.

    The host sends either epoch seconds or an ISO 8601 string, and the two
    forms can name the SAME instant — so everything that has to know *which*
    window a reading belongs to (display and the merge alike) compares the
    parsed instant, never the wire type. A bool is JSON true/false, never a
    timestamp; isinstance(True, int) would otherwise make it epoch 1.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.datetime.fromisoformat(str(value)).astimezone().timestamp()
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def fmt_reset(value, fmt="%a %H:%M"):
    """'Tue 10:00' from a resets_at value (epoch seconds or ISO string).

    ``fmt`` sets the granularity. The weekly reset can be days out so it needs
    the weekday; the 5-hour one always lands today, where the clock time alone
    is unambiguous and shorter. The default keeps every existing caller's
    output byte for byte."""
    epoch = _reset_epoch(value)
    if epoch is None:
        return "?"
    try:
        return datetime.datetime.fromtimestamp(epoch).strftime(fmt)
    except (ValueError, OSError, OverflowError):
        return "?"


def fmt_reset_soon(value):
    """A reset for an hours-long window: the clock time when it lands today,
    weekday-qualified when it does not.

    A 5-hour window opened in the evening resets after midnight — 33.8% of
    29,166 captured history rows have a five_hour reset on a different local
    date than the capture (real row: Tue 20:02 -> resets Wed 01:00). A bare
    "01:00" there reads as a time this morning that has already gone, so the
    weekday is only dropped when it genuinely says nothing."""
    epoch = _reset_epoch(value)
    if epoch is None:
        return "?"
    try:
        lands_on = datetime.datetime.fromtimestamp(epoch).date()
        today = datetime.date.today()
    except (ValueError, OSError, OverflowError):
        return "?"
    return fmt_reset(value, "%H:%M" if lands_on == today else "%a %H:%M")


def fmt_tokens(n):
    """Compact token count: 618 · 9.5k · 206k · 1M."""
    if n >= 1_000_000:
        return "{:.1f}M".format(n / 1_000_000.0).replace(".0M", "M")
    if n >= 10_000:
        return "{:.0f}k".format(n / 1_000.0)
    if n >= 1_000:
        return "{:.1f}k".format(n / 1_000.0).replace(".0k", "k")
    return "{:.0f}".format(n)


def format_line(reading, snap=None):
    """The visible status line: model · ctx N% (tokens/window) · 5h N% (reset)
    [· cache 5m:Nk | 1h:Nk] · wk N% (reset) [· $cost].

    ``reading`` is this message's live reading (model + context + maybe caps).
    ``snap`` is the freshest persisted snapshot — used only to fill the
    account-level cap when THIS message's payload lacks it (a fresh window
    before its first API response). Falls back to a bare "llmeter" so the host
    tool's prompt never shows an empty or broken line.

    Every segment is rendered only when its data is actually present, which is
    what keeps a non-Claude session unchanged: those payloads carry no
    ``rate_limits`` at all, so neither cap window can appear.
    """
    reading = reading if isinstance(reading, dict) else {}
    parts = []
    model = reading.get("model")
    if isinstance(model, str) and model:
        # "Opus 5 (1M context)" -> "Opus 5 (1M)". The window size is already
        # spelled out in the ctx segment, so the word is 8 columns of nothing
        # on a line that competes with the prompt for width. Display only: the
        # snapshot keeps whatever the host actually reported.
        parts.append(model.replace(" context)", ")"))
    ctx = reading.get("context_pct")
    ctx_part = "ctx {:.0f}%".format(ctx) if isinstance(ctx, (int, float)) else None
    # Absolute tokens ride along when the adapter surfaced them; 0 means "no
    # API response yet this session" — suppress rather than show a noisy 0.
    toks = reading.get("context_tokens")
    if isinstance(toks, (int, float)) and not isinstance(toks, bool) and toks > 0:
        detail = fmt_tokens(toks)
        size = reading.get("context_window_size")
        if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0:
            detail += "/" + fmt_tokens(size)
        ctx_part = "{} ({})".format(ctx_part, detail) if ctx_part else "ctx " + detail
    if ctx_part:
        parts.append(ctx_part)

    # Cache-creation lifetime readout: only the bucket the most recent
    # assistant message wrote to (``active``), summed fresh from the
    # transcript on every render — not merged with the cross-window snapshot,
    # since the transcript already holds the whole session's history. Hidden
    # entirely until a cache write has been seen (``active`` is None), since
    # there is nothing meaningful to call "active" yet.
    cache_ttl = reading.get("cache_ttl")
    if isinstance(cache_ttl, dict):
        active = cache_ttl.get("active")
        if active in ("5m", "1h"):
            key = "cache_5m_tokens" if active == "5m" else "cache_1h_tokens"
            total = cache_ttl.get(key)
            if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
                parts.append("cache {}:{}".format(active, fmt_tokens(total)))

    # Prefer this message's own caps; else the cross-window persisted snapshot.
    # Coerce every step to a dict — the host owns this schema and may hand us
    # an int/str/list where a dict is expected (see the hostile-shape tests).
    caps = reading.get("caps")
    if not (isinstance(caps, dict) and caps):
        caps = dget(snap or {}, "caps")
    # Shortest window first, so the number you are most likely to hit next is
    # the one nearer the model name. The weekly reset is days out and always
    # carries its weekday; the 5-hour one usually lands today, where the clock
    # time alone is shorter and says the same thing.
    for window, label, show_reset in (("five_hour", "5h", fmt_reset_soon),
                                      ("seven_day", "wk", fmt_reset)):
        win = caps.get(window) if isinstance(caps, dict) else None
        pct = _cap_pct(win)
        if pct is not None:
            parts.append("{} {:.0f}% (resets {})".format(
                label, pct, show_reset(win.get("resets_at"))))

    # Pay-per-token tools surface cost instead of a cap.
    cost = reading.get("cost")
    if not (isinstance(cost, dict) and cost):
        cost = dget(snap or {}, "cost")
    spent = cost.get("session_usd") if isinstance(cost, dict) else None
    if isinstance(spent, (int, float)):
        parts.append("${:.2f}".format(spent))

    return " · ".join(parts) if parts else "llmeter"
