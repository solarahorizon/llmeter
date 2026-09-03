"""llmeter ttl — which prompt-cache TTL is cheaper for *your* traffic.

Claude Code offers two prompt-cache lifetimes. A five-minute cache bills writes
at 1.25x the input rate; a one-hour cache bills them at 2x but survives longer
breaks. On a Claude subscription within plan usage, the main conversation gets
the one-hour cache unless you choose otherwise; everything else gets five
minutes. See https://code.claude.com/docs/en/prompt-caching.

**The premium and the payoff are charged on different quantities.** You pay the
one-hour premium on every token you *write*. You earn it back only on the
prefix you *would have re-written* after idling past five minutes. So the
deciding number is not how often you step away — it is how your write volume
compares to your idle-gap volume.

Take one request that follows a gap of between five minutes and an hour. Its
input splits into the prefix ``P`` it shares with the previous request and the
delta ``D`` the turn appends. A one-hour cache reads P and writes D; a
five-minute cache has expired, so it writes both::

    cost(1h) = 0.1 x P + 2.00 x D
    cost(5m) =           1.25 x (P + D)
    difference          = 0.75 x D  -  (1.25 - read_rate) x P

D is written under either lifetime, so it belongs with every other write; only
P is the quantity the two price differently. Summed over a window::

    cost(1h) - cost(5m) = 0.75 x W  -  (1.25 - read_rate) x G

    W = every token written to cache, D on gap requests included
    G = prefix tokens on gap requests — a hit at 1h, a re-write at 5m

One hour is cheaper only when that difference is negative, which needs W below
roughly 1.5x G. Gaps longer than an hour miss under both lifetimes and cancel;
gaps under five minutes hit under both and cancel too.

Splitting P from D takes care, because a transcript records them differently
depending on which lifetime was in force. On a hit the request reports
``cache_read = P`` and ``cache_creation = D`` separately; on a miss it reports
``cache_read = 0`` and ``cache_creation = P + D`` fused. Where they are fused
this module estimates D from the delta writes on that conversation's own cache
hits, falling back to the bucket's median; where the transcript separates them,
the recorded values are used and nothing is estimated.

This module reads your own transcripts under ``~/.claude/projects`` to measure
W and G per TTL bucket, and prints the verdict with the margin that would flip
it. Offline: it opens local transcripts and makes no network call. It writes
nothing unless ``--html`` asks for a page, and then only that page.

Run it::

    python3 -m llmeter.ttl                   # report only
    python3 -m llmeter.ttl --html            # report and the page
    python3 -m llmeter.ttl --html --quiet    # the page only
    python3 -m llmeter.ttl --days 30
    python3 -m llmeter.ttl --json
"""

import argparse
import datetime
import glob
import json
import os
import statistics
import sys

# Cache-write prices as a multiple of the model's input-token price, per
# https://platform.claude.com/docs/en/build-with-claude/prompt-caching#pricing
WRITE_5M = 1.25
WRITE_1H = 2.00

# Cache reads bill at 0.1x input for most models. Fable 5.1 publishes $0.25/Mtok
# reads against $10/Mtok input (Claude Code CHANGELOG 2.1.257), so it reads
# cheaper and leans a little further toward the five-minute cache.
READ_DEFAULT = 0.10
READ_BY_MODEL = {"fable-5-1": 0.025}

TTL_5M_SECONDS = 300
TTL_1H_SECONDS = 3600

# The two buckets Claude Code sorts every request into, and the setting that
# governs each. A subagent, workflow, fork, compaction or title request is
# "everything else" no matter which model serves it.
BUCKET_MAIN = "main conversation"
BUCKET_OTHER = "everything else"
SETTING_FOR_BUCKET = {
    BUCKET_MAIN: "promptCacheTtl",
    BUCKET_OTHER: "subagentPromptCacheTtl",
}

DEFAULT_PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
DEFAULT_SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def read_multiplier(model):
    """Cache-read price as a multiple of input price, for a model id."""
    for key, mult in READ_BY_MODEL.items():
        if key in (model or ""):
            return mult
    return READ_DEFAULT


class Request(object):
    """One API request, as a transcript records it.

    ``read`` and ``written`` are the request's own cache token counts; ``at_1h``
    and ``at_5m`` split ``written`` by the lifetime the API actually granted,
    the only place a transcript states which TTL a request really got.
    """

    __slots__ = ("when", "read", "written", "model", "sidechain",
                 "at_1h", "at_5m", "uuid", "parent")

    def __init__(self, when, read, written, model, sidechain, at_1h, at_5m,
                 uuid, parent):
        self.when = when
        self.read = read
        self.written = written
        self.model = model
        self.sidechain = sidechain
        self.at_1h = at_1h
        self.at_5m = at_5m
        self.uuid = uuid
        self.parent = parent


def _counter(value):
    """A usage counter, or 0 when the transcript holds something else there.

    Every field taken off a transcript is checked where it is read, because the
    only thing downstream can do with a surprise is raise. A count that is
    reported but not priced degrades to 0 rather than dropping the whole reply;
    one the verdict rests on drops the reply instead.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _requests_in(path):
    """Yield one Request per assistant reply in a transcript.

    A multi-block reply is written as several lines sharing one ``message.id``,
    each re-sending the same cumulative usage, so only the first line of a run
    counts. Comparing against the previous id alone is enough, because the
    repeats are always adjacent.

    Any line that isn't a well-formed assistant turn carrying usage is skipped.
    A transcript is the host tool's schema and can change shape at any time, so
    a line that can't be read is dropped rather than raised on.
    """
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    previous_id = None
    with handle:
        for line in handle:
            if '"usage"' not in line:
                continue
            try:
                entry = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            if message_id is not None and message_id == previous_id:
                continue
            previous_id = message_id
            usage = message.get("usage")
            stamp = entry.get("timestamp")
            if not isinstance(usage, dict) or not isinstance(stamp, str):
                continue
            try:
                when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
            read = usage.get("cache_read_input_tokens") or 0
            written = usage.get("cache_creation_input_tokens") or 0
            if _counter(read) != read or _counter(written) != written:
                continue
            split = usage.get("cache_creation")
            if not isinstance(split, dict):
                split = {}
            model = message.get("model")
            node = entry.get("uuid")
            up = entry.get("parentUuid")
            yield Request(
                when=when,
                read=read,
                written=written,
                model=model if isinstance(model, str) else "",
                sidechain=bool(entry.get("isSidechain")),
                at_1h=_counter(split.get("ephemeral_1h_input_tokens")),
                at_5m=_counter(split.get("ephemeral_5m_input_tokens")),
                uuid=node if isinstance(node, str) else None,
                parent=up if isinstance(up, str) else None,
            )


def _parent_links(path):
    """``uuid -> parentUuid``, kept only where both ends are sidechain lines.

    A reply's parent is the attachment or user line that prompted it, never
    another reply, so a table built from the replies alone links nothing and
    every turn becomes its own group. Following links out of the sidechain
    instead walks every agent up to the turn that spawned it, and agents spawned
    together would then share that root. Keeping only intra-sidechain links
    stops the walk where the parent leaves the sidechain: an agent's own first
    turn, or the outermost agent's when one agent spawned another.
    """
    sidechain, parent = set(), {}
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return {}
    with handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(entry, dict):
                continue
            node = entry.get("uuid")
            if not isinstance(node, str):
                continue
            up = entry.get("parentUuid")
            parent[node] = up if isinstance(up, str) else None
            if entry.get("isSidechain"):
                sidechain.add(node)
    return {
        node: up
        for node, up in parent.items()
        if node in sidechain and up in sidechain
    }


def _chain_root(request, parent_of):
    """The id at the head of this request's parent chain, or its own id."""
    seen = set()
    node = request.uuid
    while parent_of.get(node) is not None:
        if node in seen:
            break
        seen.add(node)
        node = parent_of[node]
    return node


def _group_by_chain(requests, parent_of):
    """Split inlined sidechain turns into one group per agent.

    Older Claude Code versions wrote several subagents into one session file.
    Each holds its own cache, so their turns must not be read as one sequence —
    the handover from one agent to the next would otherwise register as an idle
    gap that neither of them took.

    ``parent_of`` links the transcript's lines, not only the replies being
    grouped; a reply's parent is the line that prompted it.
    """
    groups = {}
    for request in requests:
        groups.setdefault(_chain_root(request, parent_of), []).append(request)
    return list(groups.values())


def conversations(projects_dir):
    """Group every request into the conversation whose cache prefix it shares.

    One conversation is one cache: a session's own turns, or one subagent's.
    Gaps only mean something inside a conversation, so they are never measured
    across this boundary. Yields (bucket, list-of-requests).
    """
    session_files = glob.glob(os.path.join(projects_dir, "*", "*.jsonl"))
    subagent_files = glob.glob(os.path.join(projects_dir, "*", "*", "subagents", "*.jsonl"))

    for path in sorted(session_files):
        main, side = [], []
        for request in _requests_in(path):
            (side if request.sidechain else main).append(request)
        if main:
            yield BUCKET_MAIN, main
        if side:
            for group in _group_by_chain(side, _parent_links(path)):
                yield BUCKET_OTHER, group

    for path in sorted(subagent_files):
        requests = list(_requests_in(path))
        if requests:
            yield BUCKET_OTHER, requests


def _delta_write_samples(requests):
    """Delta writes on this conversation's cache hits.

    A hit writes only the turn's new content, so its ``cache_creation`` is a
    direct sample of D — the quantity a miss fuses into the prefix.
    """
    samples = []
    previous = None
    for request in requests:
        gap = (request.when - previous).total_seconds() if previous else None
        previous = request.when
        if gap is not None and gap <= TTL_5M_SECONDS and request.read > 0:
            samples.append(request.written)
    return samples


def _new_bucket(name):
    return {
        "bucket": name,
        "setting": SETTING_FOR_BUCKET[name],
        "requests": 0,
        "conversations": 0,
        "write_tokens": 0,
        "gap_tokens": 0,
        "gap_count": 0,
        "gaps_over_1h": 0,
        "gaps_estimated": 0,
        "gaps_unsampled": 0,
        "read_weighted": 0.0,
        "read_weight_base": 0,
        "observed_1h_writes": 0,
        "observed_5m_writes": 0,
    }


def measure(projects_dir=None, days=14, now=None):
    """Measure W, G and the verdict for each TTL bucket.

    The window is the ``days`` before ``now``, both edges applied: a request
    outside it is dropped before gaps are computed, so a gap straddling either
    edge is not counted, and a frozen ``now`` measures the same window later.
    """
    projects_dir = projects_dir or DEFAULT_PROJECTS_DIR
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=days)

    grouped = {BUCKET_MAIN: [], BUCKET_OTHER: []}
    for name, requests in conversations(projects_dir):
        requests = sorted((r for r in requests if cutoff <= r.when <= now),
                          key=lambda r: r.when)
        if requests:
            grouped[name].append(requests)

    buckets = {}
    for name in (BUCKET_MAIN, BUCKET_OTHER):
        buckets[name] = _score(_new_bucket(name), grouped[name])
    return {"window_days": days, "generated_at": now, "buckets": buckets}


def _score(stats, conversation_list):
    # A fused gap request needs a stand-in for D. The conversation's own hits
    # are the closest sample; the bucket's median covers a conversation that
    # holds no hit of its own.
    bucket_samples = []
    for requests in conversation_list:
        bucket_samples.extend(_delta_write_samples(requests))
    bucket_median = statistics.median(bucket_samples) if bucket_samples else 0

    for requests in conversation_list:
        stats["conversations"] += 1
        stats["requests"] += len(requests)
        samples = _delta_write_samples(requests)
        typical_delta = statistics.median(samples) if samples else bucket_median

        previous = None
        for request in requests:
            gap = (request.when - previous).total_seconds() if previous else 0.0
            previous = request.when
            stats["observed_1h_writes"] += request.at_1h
            stats["observed_5m_writes"] += request.at_5m

            if not TTL_5M_SECONDS < gap <= TTL_1H_SECONDS:
                if gap > TTL_1H_SECONDS:
                    stats["gaps_over_1h"] += 1
                stats["write_tokens"] += request.written
                continue

            stats["gap_count"] += 1
            if request.read > 0:
                # A hit: the transcript already separates prefix from delta.
                prefix, delta = request.read, request.written
            else:
                # A miss fused them. Hold back a typical delta as the write both
                # lifetimes pay for, and treat the remainder as prefix.
                stats["gaps_estimated"] += 1
                if typical_delta <= 0:
                    stats["gaps_unsampled"] += 1
                delta = min(request.written, typical_delta)
                prefix = request.written - delta
            stats["gap_tokens"] += prefix
            stats["write_tokens"] += delta
            # The read rate is only ever applied to gap prefixes, so it is
            # weighted by them and by nothing else.
            stats["read_weighted"] += read_multiplier(request.model) * prefix
            stats["read_weight_base"] += prefix

    stats["read_rate"] = _weighted_read_rate(stats)
    observed = stats["observed_1h_writes"] + stats["observed_5m_writes"]
    stats["observed_1h_share"] = (
        stats["observed_1h_writes"] / observed if observed else None
    )
    _decide(stats)
    return stats


def _weighted_read_rate(stats):
    """Cache-read multiplier weighted by the gap prefixes it is applied to.

    ``penalty_5m`` charges this rate against ``gap_tokens`` and nothing else, so
    the average is taken over those same tokens: a bucket whose gaps are all on
    one model gets that model's rate however much OTHER traffic it carries.
    Numerator and denominator accumulate over the same tokens, so the result is
    a convex combination of the per-model multipliers and can never exceed the
    largest of them — which is what keeps ``WRITE_5M - read_rate`` positive.
    A bucket with no gap prefix has nothing to average and reports the default.
    """
    base = stats["read_weight_base"]
    if base <= 0:
        return READ_DEFAULT
    return stats["read_weighted"] / base


def _decide(stats):
    """Price the two lifetimes against each other and record the margin."""
    write_tokens = stats["write_tokens"]
    gap_tokens = stats["gap_tokens"]
    premium_per_write = WRITE_1H - WRITE_5M
    saved_per_gap_token = WRITE_5M - stats["read_rate"]

    stats["premium_1h"] = premium_per_write * write_tokens
    stats["penalty_5m"] = saved_per_gap_token * gap_tokens
    stats["delta"] = stats["premium_1h"] - stats["penalty_5m"]

    if stats["requests"] == 0:
        stats["verdict"] = None
        stats["breakeven_gap_tokens"] = 0
        stats["flip_factor"] = None
        return

    # A tie carries no reason to pay the premium, so it goes to 5m.
    stats["verdict"] = "5m" if stats["delta"] >= 0 else "1h"
    stats["breakeven_gap_tokens"] = stats["premium_1h"] / saved_per_gap_token
    if stats["delta"] == 0:
        stats["flip_factor"] = None
    elif stats["verdict"] == "5m":
        # How much more idle-gap volume it would take to make 1h worth it.
        stats["flip_factor"] = (
            stats["breakeven_gap_tokens"] / gap_tokens if gap_tokens else None
        )
    else:
        # How much more writing it would take to make 5m worth it.
        stats["flip_factor"] = (
            stats["penalty_5m"] / premium_per_write / write_tokens
            if write_tokens
            else None
        )


def current_settings(settings_path=None, environ=None):
    """What each bucket is set to right now, and where that came from.

    Reports the highest-precedence control in force, per the order documented at
    https://code.claude.com/docs/en/prompt-caching#choose-the-ttl-yourself. A
    subagent's own ``experimental.cacheTtl`` frontmatter sits between the
    setting and ``ENABLE_PROMPT_CACHING_1H`` and is not resolved here, because
    it has no single value across agents — an individual agent can override the
    "everything else" value this reports.
    """
    settings_path = settings_path or DEFAULT_SETTINGS_PATH
    environ = environ if environ is not None else os.environ
    stored = {}
    try:
        with open(settings_path, "r", encoding="utf-8", errors="replace") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            stored = loaded
    except (OSError, ValueError, TypeError):
        stored = {}

    env_key = {
        BUCKET_MAIN: "CLAUDE_CODE_PROMPT_CACHE_TTL",
        BUCKET_OTHER: "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL",
    }
    result = {}
    for bucket, setting in SETTING_FOR_BUCKET.items():
        if environ.get("FORCE_PROMPT_CACHING_5M") == "1":
            result[bucket] = ("5m", "FORCE_PROMPT_CACHING_5M=1")
        elif environ.get(env_key[bucket]) in ("5m", "1h"):
            result[bucket] = (environ[env_key[bucket]], env_key[bucket])
        elif stored.get(setting) in ("5m", "1h"):
            result[bucket] = (stored[setting], settings_path)
        elif environ.get("ENABLE_PROMPT_CACHING_1H") == "1":
            result[bucket] = ("1h", "ENABLE_PROMPT_CACHING_1H=1")
        else:
            result[bucket] = (None, settings_path)
    return result


def _millions(tokens):
    return "%.1fM" % (tokens / 1e6)


# Report rows are three columns: an upper-case label, the figure, then the
# sentence about it. Upper case is what separates a label from its own value on
# a line that reads as prose, and the fixed widths keep the figures and the
# ``<-`` arrows in one column down the block.
_LABEL_WIDTH, _VALUE_WIDTH = 16, 8
_CONT = " " * (2 + _LABEL_WIDTH + 1)


def _row(label, value, note=""):
    """One report line, padded so labels, figures and notes each hold a column."""
    body = "  %-*s %-*s" % (_LABEL_WIDTH, label, _VALUE_WIDTH, value)
    return (body + note).rstrip()


def render(report, settings=None):
    """Format a measurement as the terminal report."""
    settings = settings if settings is not None else current_settings()
    lines = []
    lines.append("llmeter ttl — which prompt-cache lifetime is cheaper for your traffic")

    generated = report["generated_at"].astimezone()
    lines.append(
        "window: last %d days, to %s"
        % (report["window_days"], generated.strftime("%Y-%m-%d %H:%M %Z"))
    )
    lines.append("")

    for name in (BUCKET_MAIN, BUCKET_OTHER):
        stats = report["buckets"][name]
        value, source = settings.get(name, (None, "unknown"))
        lines.append("%s  (%s)" % (name.upper(), stats["setting"]))
        lines.append(_row("SET TO", value or "default", "[%s]" % source))

        if stats["requests"] == 0:
            lines.append("  no requests in this window — nothing to compare.")
            lines.append("")
            continue

        share = stats.get("observed_1h_share")
        if share is not None:
            lines.append(
                _row(
                    "ACTUALLY GOT",
                    "%.0f%%" % (share * 100),
                    "of writes on the 1h cache, %.0f%% on 5m" % ((1 - share) * 100),
                )
            )
            if value and value != ("1h" if share >= 0.5 else "5m"):
                lines.append(_CONT + "(most of this window predates that setting)")
        lines.append(
            _row(
                "MEASURED",
                f"{stats['requests']:,}",
                "requests across %s conversations" % f"{stats['conversations']:,}",
            )
        )
        lines.append(
            _row(
                "CACHE WRITES",
                _millions(stats["write_tokens"]),
                "<- the 1h premium is charged on all of this",
            )
        )
        lines.append(
            _row(
                "IDLE-GAP PREFIX",
                _millions(stats["gap_tokens"]),
                "<- %d gaps of 5-60 min; only this earns it back" % stats["gap_count"],
            )
        )
        if stats["gaps_estimated"]:
            unsampled = stats["gaps_unsampled"]
            note = _CONT + "%d of those gaps had to be estimated" % stats["gaps_estimated"]
            if unsampled:
                note += " (%d with no sample to go on, counted wholly as prefix)" % unsampled
            lines.append(note)
        lines.append(
            _row(
                "1H PAYS EXTRA",
                _millions(stats["premium_1h"]),
                "(input-token equivalents)",
            )
        )
        lines.append(_row("5M PAYS EXTRA", _millions(stats["penalty_5m"])))

        verdict = stats["verdict"]
        lines.append(
            _row("--> USE %s" % verdict, "", "cheaper by %s" % _millions(abs(stats["delta"])))
        )
        if stats["flip_factor"]:
            moved = "idle-gap volume" if verdict == "5m" else "write volume"
            lines.append(
                "      to flip: your %s would have to be %.2fx what it is"
                % (moved, stats["flip_factor"])
            )
        if value and value != verdict:
            lines.append("      note: this differs from your current setting (%s)." % value)
        lines.append("")

    lines.append("Read this as a direction, not a bill:")
    lines.append(
        "  - On a subscription you spend quota, not dollars. How the meter weights"
    )
    lines.append("    a 1h write against a 5m one is not derivable from a transcript.")
    lines.append("  - Gaps over an hour miss under both lifetimes, so they are left out.")
    lines.append(
        "  - A gap whose prefix had already been dropped — after a compaction or a"
    )
    lines.append(
        "    model switch — is counted as though 1h would have held it, which"
    )
    lines.append("    flatters 1h a little.")
    lines.append(
        "  - A gap the cache missed fuses prefix and appended turn into one"
    )
    lines.append(
        "    number. The split uses your own short-gap turns as the sample, which"
    )
    lines.append(
        "    tend to be smaller than a post-pause turn, so it leans mildly to 1h."
    )
    lines.append(
        "  - A quieter week lowers writes and raises gaps. Re-run it when your"
    )
    lines.append("    working pattern changes.")
    return "\n".join(lines)


def _jsonable(report):
    hidden = ("read_weighted", "read_weight_base")
    out = {
        "window_days": report["window_days"],
        "generated_at": report["generated_at"].isoformat(),
        "buckets": {},
    }
    for name, stats in report["buckets"].items():
        out["buckets"][name] = {k: v for k, v in stats.items() if k not in hidden}
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="llmeter ttl",
        description="Recommend promptCacheTtl / subagentPromptCacheTtl from your own transcripts.",
    )
    parser.add_argument("--days", type=int, default=14, help="window to measure (default 14)")
    parser.add_argument("--projects-dir", default=None, help="override ~/.claude/projects")
    parser.add_argument("--json", action="store_true", help="emit the measurement as JSON")
    parser.add_argument(
        "--html",
        nargs="?",
        const="llmeter-ttl.html",
        default=None,
        metavar="PATH",
        help="also write a standalone HTML page explaining the result "
             "(default llmeter-ttl.html)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="skip the terminal report; with --html, write only the page",
    )
    args = parser.parse_args(argv)

    if args.quiet and args.json:
        # Asking for JSON and for no output at once has no reading that helps
        # the caller, so name it rather than silently honouring one of them.
        sys.stderr.write("--quiet and --json ask for opposite things\n")
        return 2

    projects_dir = args.projects_dir or DEFAULT_PROJECTS_DIR
    if not os.path.isdir(projects_dir):
        sys.stderr.write("no transcripts found at %s\n" % projects_dir)
        return 1

    report = measure(projects_dir=projects_dir, days=args.days)
    settings = current_settings()
    if args.json:
        print(json.dumps(_jsonable(report), indent=2))
    elif not args.quiet:
        print(render(report, settings))

    if args.html:
        # Imported here so the default run never pays for the page's markup, and
        # so the module holding it can import this one without a cycle.
        from llmeter import ttl_report

        page = ttl_report.render_html(report, settings)
        try:
            with open(args.html, "w", encoding="utf-8") as handle:
                handle.write(page)
        except OSError as failure:
            sys.stderr.write("could not write %s: %s\n" % (args.html, failure))
            return 1
        # Printed even under --quiet, because the caller still needs to know
        # where the page went; under --json it goes to stderr so stdout stays
        # one parseable document.
        stream = sys.stderr if args.json else sys.stdout
        stream.write("%swrote %s\n" % ("" if args.quiet or args.json else "\n",
                                        os.path.abspath(args.html)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
