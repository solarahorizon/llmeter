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
        "session_id": "…",                 # opaque, for cross-window dedup
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

# Output dir is overridable so a caller can point it at another location
# (e.g. a menu-bar consumer's dir, or a temp dir in tests).
DIR = os.environ.get("LLMETER_DIR") or os.path.join(
    os.path.expanduser("~"), ".claude", "llmeter")
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
_SNAPSHOT_FIELDS = ("source", "model", "context_pct", "context_tokens",
                    "context_window_size", "caps", "cost", "session_id")

# Written by core itself rather than copied from a Reading, so the allowlist
# above still holds: no adapter can widen what is persisted. Listed explicitly
# so "what reaches disk" stays answerable from one place (codex P2).
# NOTE: `provider` records the HOST (and path) of a non-default
# ANTHROPIC_BASE_URL. If that endpoint is an internal gateway, redact it before
# pasting a snapshot into a bug report — see CONTRIBUTING.
_STAMPED_FIELDS = ("captured_at", "provider")


def write_snapshot(reading, snapshot_path=None, history_path=None, now=None):
    """Persist a normalized Reading. Returns the stored snapshot dict, or None
    if the reading carries no account-level usage worth persisting (e.g. a
    tool's first message before any usage is known).

    - The snapshot is the account-level truth used for cross-window fallback,
      so we persist only when ``caps`` or ``cost`` is present.
    - Only ``_SNAPSHOT_FIELDS`` are written — a field not on that allowlist
      never reaches disk, whatever an adapter puts in the Reading.
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
    snap = {k: reading[k] for k in _SNAPSHOT_FIELDS if k in reading}
    snap["captured_at"] = now or now_iso()
    # Stamped by core, not copied from the Reading: the provider is a property
    # of the environment the session runs in, not of anything an adapter parsed.
    snap["provider"] = provider
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)

    # provider-checked: never merge another account's percentages into this
    # one, even if both somehow resolved to the same file.
    prev = read_snapshot(snapshot_path, max_age_secs=None, provider=provider)
    # Unconditional: {} is safer than leaving a hostile non-dict "caps" from
    # the raw reading in the snapshot (deepseek review).
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

    # A UNIQUE temp file per writer (not a shared "<name>.tmp") so concurrent
    # panes never clobber each other's in-flight write; os.replace is atomic, so
    # a reader always sees a whole old-or-new file. (codex P2: a fixed temp path
    # let two writers race and could promote malformed JSON.)
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
    return snap


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
    """Merge cap windows so the snapshot is the account-level truth, not the
    last writer's view. Idle CLI sessions re-publish their LAST-KNOWN caps on
    every statusline refresh, so a session that has not called the API for
    hours keeps stamping yesterday's percentage with a fresh captured_at —
    last-writer-wins made the meter flap (69->82->69 within a minute, seen
    live 2026-07-06). Per window: a later resets_at is a newer window and wins
    outright; the SAME resets_at means the same window, where the account
    percentage is monotonically non-decreasing -> keep the max.

    Which window a reading belongs to is decided by the PARSED instant, not by
    the wire type. Comparing types instead let two ISO resets_at values fall
    through to max-wins (a meter frozen at its high-water mark) and, in the
    mixed case, handed the win to whichever side happened to be numeric. A
    weekly window hides both; a 5-hour window rolls five times a day."""
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
        if old_r is not None and new_r is not None and old_r != new_r:
            merged[w] = new_w if new_r > old_r else old_w
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
