"""Tests for llmeter — the Claude Code adapter, the core harvester, and the
fail-soft render path. Stdlib unittest, no dependencies.

Run:  python3 -m unittest discover -s tests  (from the repo root)
"""

import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llmeter import core  # noqa: E402
from llmeter.adapters import claude_code  # noqa: E402


def _read(path):
    with open(path) as f:
        return f.read()


def _lines(path):
    return _read(path).strip().splitlines()


def _json(path):
    with open(path) as f:
        return json.load(f)


def _segment(line, label):
    """The status line part starting with ``label``, or None. Lets a test
    assert a whole segment exactly, so a stray weekday or a changed suffix
    fails rather than sliding past a substring check."""
    return next((p for p in line.split(" · ") if p.startswith(label + " ")), None)


def _iso(epoch):
    """The same instant as ``epoch``, written the other way the host may send
    it. Never hardcode a clock string: the assertion has to hold in any zone."""
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).isoformat()

# A representative Claude Code statusLine payload (Pro/Max, mid-session).
PAYLOAD = {
    "session_id": "s-1",
    "model": {"id": "claude-fable-5", "display_name": "Fable 5"},
    "context_window": {"used_percentage": 34.5},
    "rate_limits": {
        "five_hour": {"used_percentage": 22.0, "resets_at": 1782050340},
        "seven_day": {"used_percentage": 10.0, "resets_at": 1782518400},
    },
}


class AdapterTests(unittest.TestCase):
    def test_parse_maps_payload_to_reading(self):
        r = claude_code.parse(PAYLOAD)
        self.assertEqual(r["source"], "claude-code")
        self.assertEqual(r["model"], "Fable 5")
        self.assertEqual(r["context_pct"], 34.5)
        self.assertEqual(r["caps"]["five_hour"]["used_percentage"], 22.0)
        self.assertEqual(r["caps"]["seven_day"]["used_percentage"], 10.0)
        self.assertIsNone(r["cost"])

    def test_parse_falls_back_to_model_id(self):
        r = claude_code.parse({"model": {"id": "claude-x"}})
        self.assertEqual(r["model"], "claude-x")
        self.assertEqual(r["caps"], {})

    def test_parse_maps_context_tokens(self):
        r = claude_code.parse({"context_window": {
            "used_percentage": 20, "total_input_tokens": 205_600,
            "context_window_size": 1_000_000}})
        self.assertEqual(r["context_tokens"], 205_600)
        self.assertEqual(r["context_window_size"], 1_000_000)
        # Absent fields stay None, never raise.
        r = claude_code.parse(PAYLOAD)
        self.assertIsNone(r["context_tokens"])
        self.assertIsNone(r["context_window_size"])

    def test_known_custom_models_override_the_200k_fallback(self):
        # Claude Code reports 200k for any model outside its own table, so a
        # 1M-context model reads 5x too full. Seen live 2026-08-10: a kimi-k3
        # session showed "ctx 20% (41k/200k)" when the real window is 1M.
        for model_id, window in (("kimi-k3", 1_048_576),
                                 ("kimi-k3[1m]", 1_048_576),
                                 ("kimi-k2.7-code", 262_144),
                                 ("qwen3.8-max", 1_000_000)):
            r = claude_code.parse({
                "model": {"display_name": model_id},
                "context_window": {"used_percentage": 20,
                                   "total_input_tokens": 41_000,
                                   "context_window_size": 200_000}})
            self.assertEqual(r["context_window_size"], window, model_id)
            # Percentage is recomputed from the absolute tokens, so the line
            # stays internally consistent rather than keeping the stale 20%.
            self.assertAlmostEqual(r["context_pct"], 41_000 * 100.0 / window,
                                   places=6, msg=model_id)

    def test_unknown_model_keeps_the_reported_window(self):
        r = claude_code.parse({
            "model": {"display_name": "some-model-we-never-heard-of"},
            "context_window": {"used_percentage": 20,
                               "total_input_tokens": 41_000,
                               "context_window_size": 200_000}})
        self.assertEqual(r["context_window_size"], 200_000)
        self.assertEqual(r["context_pct"], 20)

    def test_third_party_session_shows_spend_instead_of_a_cap(self):
        # A vendor session can never show wk: Claude Code fetches plan
        # utilization only for the Anthropic subscription and forwards no cap
        # in the payload (57/57 payloads from a live Kimi session, 2026-08-10).
        # cost.total_cost_usd DOES arrive, so it fills the empty slot.
        payload = {"model": {"display_name": "kimi-k3"},
                   "context_window": {"used_percentage": 20,
                                      "total_input_tokens": 40_400,
                                      "context_window_size": 200_000},
                   "cost": {"total_cost_usd": 0.06335}}
        with mock.patch.dict(os.environ,
                             {"ANTHROPIC_BASE_URL": "https://api.kimi.com/coding"}):
            r = claude_code.parse(payload)
        self.assertEqual(r["cost"], {"session_usd": 0.06335})
        self.assertEqual(r["caps"], {})
        self.assertEqual(core.format_line(r), "kimi-k3 · ctx 4% (40k/1M) · $0.06")

    def test_default_provider_keeps_the_cap_and_shows_no_spend(self):
        # On a subscription, wk is the meaningful number and a dollar figure
        # would be noise — so cost stays None there.
        payload = {"model": {"display_name": "Opus 5 (1M context)"},
                   "context_window": {"used_percentage": 6},
                   "cost": {"total_cost_usd": 4.20}}
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": ""}):
            r = claude_code.parse(payload)
        self.assertIsNone(r["cost"])
        self.assertNotIn("$", core.format_line(r))

    def test_fresh_third_party_window_shows_zero_not_a_stale_total(self):
        # 0.0 must still produce a cost dict. If it returned None, format_line
        # would fall back to the persisted snapshot and print the PREVIOUS
        # session's total under a brand-new session — a confident wrong number.
        with mock.patch.dict(os.environ,
                             {"ANTHROPIC_BASE_URL": "https://api.kimi.com/coding"}):
            r = claude_code.parse({"model": {"display_name": "kimi-k3"},
                                   "cost": {"total_cost_usd": 0.0}})
        self.assertEqual(r["cost"], {"session_usd": 0.0})
        stale = {"cost": {"session_usd": 9.99}}
        self.assertIn("$0.00", core.format_line(r, stale))
        self.assertNotIn("9.99", core.format_line(r, stale))

    def test_spend_ignores_junk_cost_shapes(self):
        with mock.patch.dict(os.environ,
                             {"ANTHROPIC_BASE_URL": "https://api.kimi.com/coding"}):
            for bad in ({"cost": {"total_cost_usd": "1.00"}},
                        {"cost": {"total_cost_usd": True}},
                        {"cost": {"total_cost_usd": -1}},
                        {"cost": "free"}, {"cost": None}, {}):
                self.assertIsNone(claude_code.parse(bad)["cost"], bad)

    def test_parse_hostile_shapes_never_raise(self):
        for bad in ({}, {"model": "a-string", "rate_limits": 5}, {"context_window": 3}):
            r = claude_code.parse(bad)
            self.assertEqual(r["source"], "claude-code")
            self.assertEqual(r["caps"], {})

    def test_parse_non_dict_input(self):
        self.assertEqual(claude_code.parse(None)["caps"], {})
        self.assertEqual(claude_code.parse([1, 2])["caps"], {})

    def test_parse_allowlists_cap_fields(self):
        # Only known windows + fields are carried — unexpected metadata under
        # rate_limits must never reach disk (privacy).
        r = claude_code.parse({"rate_limits": {
            "seven_day": {"used_percentage": 10.0, "resets_at": 123,
                          "account_id": "SECRET", "plan": "max"},
            "thirty_day": {"used_percentage": 5.0},   # unknown window -> dropped
        }})
        self.assertEqual(set(r["caps"]), {"seven_day"})
        self.assertEqual(set(r["caps"]["seven_day"]), {"used_percentage", "resets_at"})
        self.assertNotIn("account_id", r["caps"]["seven_day"])


class CacheTtlTests(unittest.TestCase):
    """cache_ttl: the session's cumulative cache_creation tokens, split by
    ephemeral lifetime, summed straight from the transcript (see
    claude_code._cache_ttl_totals)."""

    def setUp(self):
        d = tempfile.mkdtemp(prefix="llmeter-transcript-")
        self.addCleanup(shutil.rmtree, d)
        self.transcript = os.path.join(d, "session.jsonl")
        self.cache_dir = tempfile.mkdtemp(prefix="llmeter-ttlcache-")
        self.addCleanup(shutil.rmtree, self.cache_dir)

    def _parse(self, data):
        # Every real parse() call in this class routes through here so the
        # resume-cache file it writes never lands in the developer's actual
        # LLMETER_DIR (~/.claude/llmeter/ or a real override).
        with mock.patch.object(core, "DIR", self.cache_dir):
            return claude_code.parse(data)

    def _assistant_line(self, msg_id, ephemeral_5m=0, ephemeral_1h=0):
        return json.dumps({
            "type": "assistant",
            "message": {
                "id": msg_id,
                "usage": {
                    "input_tokens": 85,
                    "cache_creation_input_tokens": ephemeral_5m + ephemeral_1h,
                    "cache_read_input_tokens": 100981,
                    "output_tokens": 421,
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": ephemeral_1h,
                        "ephemeral_5m_input_tokens": ephemeral_5m,
                    },
                },
            },
        })

    def _write(self, lines):
        with open(self.transcript, "w") as f:
            f.write("\n".join(lines) + "\n")

    def test_sums_split_by_lifetime_and_dedupes_by_message_id(self):
        dup = self._assistant_line("msg_2", ephemeral_5m=3000)
        self._write([
            self._assistant_line("msg_1", ephemeral_1h=5000),  # 1h-only
            dup,                                               # 5m-only
            dup,                                               # repeated id: multi-block reply
        ])
        r = self._parse({"transcript_path": self.transcript})
        self.assertEqual(r["cache_ttl"],
                         {"cache_5m_tokens": 3000, "cache_1h_tokens": 5000})

    def test_missing_cache_creation_field_counts_as_zero(self):
        # Older transcript lines predate the cache_creation field.
        self._write([json.dumps({"type": "assistant", "message": {
            "id": "msg_1", "usage": {"input_tokens": 10, "output_tokens": 5}}})])
        r = self._parse({"transcript_path": self.transcript})
        self.assertEqual(r["cache_ttl"],
                         {"cache_5m_tokens": 0, "cache_1h_tokens": 0})

    def test_non_assistant_and_malformed_lines_are_skipped(self):
        self._write([
            "not json at all",
            json.dumps({"type": "user", "message": {"id": "u1"}}),
            self._assistant_line("msg_1", ephemeral_5m=100),
        ])
        r = self._parse({"transcript_path": self.transcript})
        self.assertEqual(r["cache_ttl"]["cache_5m_tokens"], 100)

    def test_unreadable_or_missing_path_is_zero_and_never_raises(self):
        for bad in (os.path.join(os.path.dirname(self.transcript), "nope.jsonl"),
                   None, "", 5, {}):
            r = self._parse({"transcript_path": bad})
            self.assertEqual(r["cache_ttl"],
                             {"cache_5m_tokens": 0, "cache_1h_tokens": 0}, bad)

    def test_embedded_nul_byte_path_never_raises(self):
        # A path string with an embedded NUL byte raises ValueError from
        # open() itself, not OSError.
        r = self._parse({"transcript_path": "abc\x00def"})
        self.assertEqual(r["cache_ttl"],
                         {"cache_5m_tokens": 0, "cache_1h_tokens": 0})

    def test_non_finite_cache_values_are_rejected_not_summed(self):
        # json.loads accepts the non-standard Infinity/-Infinity/NaN
        # literals, and int(inf)/int(nan) raise OverflowError/ValueError
        # outside this module's own except clauses -- so a non-finite value
        # must be rejected before it is summed, not merely type-checked.
        self._write([
            '{"type": "assistant", "message": {"id": "msg_1", "usage": '
            '{"cache_creation": {"ephemeral_5m_input_tokens": Infinity, '
            '"ephemeral_1h_input_tokens": 0}}}}',
            '{"type": "assistant", "message": {"id": "msg_2", "usage": '
            '{"cache_creation": {"ephemeral_5m_input_tokens": NaN, '
            '"ephemeral_1h_input_tokens": -Infinity}}}}',
            self._assistant_line("msg_3", ephemeral_5m=42),
        ])
        r = self._parse({"transcript_path": self.transcript})
        self.assertEqual(r["cache_ttl"],
                         {"cache_5m_tokens": 42, "cache_1h_tokens": 0})

    def test_render_shows_only_nonzero_buckets(self):
        line = core.format_line({"cache_ttl": {"cache_5m_tokens": 220_000,
                                               "cache_1h_tokens": 0}})
        self.assertEqual(line, "cache 5m:220k")
        line = core.format_line({"cache_ttl": {"cache_5m_tokens": 0,
                                               "cache_1h_tokens": 180_000}})
        self.assertEqual(line, "cache 1h:180k")

    def test_render_shows_both_buckets_joined(self):
        line = core.format_line({"cache_ttl": {"cache_5m_tokens": 220_000,
                                               "cache_1h_tokens": 12_000}})
        self.assertEqual(line, "cache 5m:220k · 1h:12k")

    def test_render_hides_slot_when_both_zero_or_unreadable(self):
        self.assertEqual(core.format_line(
            {"cache_ttl": {"cache_5m_tokens": 0, "cache_1h_tokens": 0}}), "llmeter")
        self.assertEqual(core.format_line({"cache_ttl": None}), "llmeter")
        self.assertEqual(core.format_line({}), "llmeter")

    def test_render_hostile_shapes_never_raise(self):
        self.assertEqual(core.format_line(
            {"cache_ttl": "junk"}), "llmeter")
        self.assertEqual(core.format_line(
            {"cache_ttl": {"cache_5m_tokens": "not-a-number",
                           "cache_1h_tokens": True}}), "llmeter")

    def test_end_to_end_parse_then_render(self):
        self._write([self._assistant_line("msg_1", ephemeral_5m=220_000),
                     self._assistant_line("msg_2", ephemeral_1h=12_000)])
        r = self._parse({"model": {"display_name": "Fable 5"},
                         "transcript_path": self.transcript})
        line = core.format_line(r)
        self.assertIn("cache 5m:220k · 1h:12k", line)

    def test_truncated_utf8_mid_write_never_raises(self):
        # Claude Code may still be appending the transcript when the status
        # line reads it: a multi-byte UTF-8 character split across that
        # write boundary must not raise UnicodeDecodeError out of the line
        # iterator.
        good = self._assistant_line("msg_1", ephemeral_5m=500).encode("utf-8")
        with open(self.transcript, "wb") as f:
            f.write(good + b"\n")
            f.write(b'{"type": "assistant", "message": {"id": "msg_2", '
                    b'"usage": {"cache_creation": {"ephemeral_1h_input_tokens": 7')
            f.write("café".encode("utf-8")[:-1])  # truncated multi-byte char, no trailing newline
        r = self._parse({"transcript_path": self.transcript})
        self.assertEqual(r["cache_ttl"]["cache_5m_tokens"], 500)

    def test_resume_parses_only_the_appended_tail(self):
        self._write([self._assistant_line("msg_1", ephemeral_5m=100)])
        r1 = self._parse({"transcript_path": self.transcript})
        self.assertEqual(r1["cache_ttl"],
                         {"cache_5m_tokens": 100, "cache_1h_tokens": 0})
        with open(self.transcript, "a") as f:
            f.write(self._assistant_line("msg_2", ephemeral_5m=200) + "\n")
            f.write(self._assistant_line("msg_3", ephemeral_1h=300) + "\n")
        r2 = self._parse({"transcript_path": self.transcript})
        self.assertEqual(r2["cache_ttl"],
                         {"cache_5m_tokens": 300, "cache_1h_tokens": 300})
        # A full parse of the same final file (fresh cache dir, no resume
        # state) must total the same as the resumed parse above.
        fresh_dir = tempfile.mkdtemp(prefix="llmeter-ttlcache-fresh-")
        self.addCleanup(shutil.rmtree, fresh_dir)
        with mock.patch.object(core, "DIR", fresh_dir):
            r_full = claude_code.parse({"transcript_path": self.transcript})
        self.assertEqual(r2["cache_ttl"], r_full["cache_ttl"])

    def test_trailing_partial_line_is_deferred_until_complete(self):
        complete = self._assistant_line("msg_1", ephemeral_5m=100)
        partial = self._assistant_line("msg_2", ephemeral_5m=200)
        with open(self.transcript, "w") as f:
            f.write(complete + "\n")
            f.write(partial)  # no trailing newline: write still in progress
        r1 = self._parse({"transcript_path": self.transcript})
        self.assertEqual(r1["cache_ttl"],
                         {"cache_5m_tokens": 100, "cache_1h_tokens": 0})
        with open(self.transcript, "a") as f:
            f.write("\n")  # the line completes
        r2 = self._parse({"transcript_path": self.transcript})
        self.assertEqual(r2["cache_ttl"],
                         {"cache_5m_tokens": 300, "cache_1h_tokens": 0})

    def test_duplicate_id_across_a_resume_boundary_counts_once(self):
        self._write([self._assistant_line("msg_1", ephemeral_5m=100)])
        r1 = self._parse({"transcript_path": self.transcript})
        self.assertEqual(r1["cache_ttl"]["cache_5m_tokens"], 100)
        # The same id reappears after the resume point (a resent multi-block
        # reply straddling where the last render left off).
        with open(self.transcript, "a") as f:
            f.write(self._assistant_line("msg_1", ephemeral_5m=100) + "\n")
        r2 = self._parse({"transcript_path": self.transcript})
        self.assertEqual(r2["cache_ttl"]["cache_5m_tokens"], 100)

    def test_shrunk_file_triggers_a_full_rescan(self):
        self._write([self._assistant_line("msg_1", ephemeral_5m=100),
                     self._assistant_line("msg_2", ephemeral_5m=200)])
        r1 = self._parse({"transcript_path": self.transcript})
        self.assertEqual(r1["cache_ttl"]["cache_5m_tokens"], 300)
        # File shrinks/replaced (e.g. a new session reusing the path): the
        # cached offset now exceeds the file's size and must be discarded.
        self._write([self._assistant_line("msg_3", ephemeral_5m=42)])
        r2 = self._parse({"transcript_path": self.transcript})
        self.assertEqual(r2["cache_ttl"]["cache_5m_tokens"], 42)

    def test_unreadable_cache_dir_falls_back_to_full_scan(self):
        self._write([self._assistant_line("msg_1", ephemeral_5m=100)])
        # A regular file occupying the cache directory's own path blocks any
        # read or write under it (NotADirectoryError, an OSError), without
        # relying on platform-specific permission bits.
        blocked = os.path.join(self.cache_dir, "blocked")
        with open(blocked, "w") as f:
            f.write("not a directory")
        with mock.patch.object(core, "DIR", blocked):
            r = claude_code.parse({"transcript_path": self.transcript})
        self.assertEqual(r["cache_ttl"],
                         {"cache_5m_tokens": 100, "cache_1h_tokens": 0})


class HarvestTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="llmeter-")
        self.addCleanup(shutil.rmtree, d)
        self.snap = os.path.join(d, "usage-snapshot.json")
        self.hist = os.path.join(d, "usage-history.jsonl")

    def _write(self, payload, now="2026-07-03T22:40:00+10:00"):
        return core.write_snapshot(claude_code.parse(payload),
                                   self.snap, self.hist, now=now)

    def test_snapshot_allowlists_reading_fields(self):
        # A rogue/unknown Reading field must never reach disk (codex P2 —
        # CONTRIBUTING ground rule 3: persisted fields are explicitly
        # allowlisted, never dict(reading) wholesale).
        reading = claude_code.parse(PAYLOAD)
        reading["rogue_field"] = {"account_email": "leak@example.com"}
        core.write_snapshot(reading, self.snap, self.hist)
        on_disk = _json(self.snap)
        self.assertNotIn("rogue_field", on_disk)
        self.assertEqual(on_disk["caps"]["seven_day"]["used_percentage"], 10.0)

    def test_writes_snapshot_and_history(self):
        snap = self._write(PAYLOAD)
        self.assertEqual(snap["caps"]["seven_day"]["used_percentage"], 10.0)
        self.assertEqual(snap["caps"]["five_hour"]["used_percentage"], 22.0)
        on_disk = _json(self.snap)
        self.assertEqual(on_disk["caps"]["five_hour"]["resets_at"], 1782050340)
        self.assertEqual(on_disk["model"], "Fable 5")
        self.assertEqual(on_disk["context_pct"], 34.5)
        self.assertEqual(on_disk["source"], "claude-code")
        self.assertEqual(len(_lines(self.hist)), 1)

    def test_snapshot_keeps_the_untrimmed_model_name(self):
        # Trimming "(1M context)" is a display concern only: what reaches disk
        # stays exactly what the host reported, so a later consumer is not
        # reading llmeter's abbreviations back as fact.
        self._write(dict(PAYLOAD, model={"display_name": "Opus 5 (1M context)"}))
        self.assertEqual(_json(self.snap)["model"], "Opus 5 (1M context)")

    def test_history_appends_only_on_change(self):
        self._write(PAYLOAD)
        self._write(PAYLOAD)  # identical caps -> no new history line
        self.assertEqual(len(_lines(self.hist)), 1)
        moved = json.loads(json.dumps(PAYLOAD))
        moved["rate_limits"]["seven_day"]["used_percentage"] = 11.0
        self._write(moved)
        self.assertEqual(len(_lines(self.hist)), 2)

    def test_no_caps_is_noop(self):
        # A payload with no rate_limits persists nothing (first-message case).
        self.assertIsNone(self._write({"model": {"id": "x"}}))
        self.assertFalse(os.path.exists(self.snap))

    def test_read_snapshot_staleness(self):
        self._write(PAYLOAD, now="2020-01-01T00:00:00+10:00")
        self.assertIsNone(core.read_snapshot(self.snap, max_age_secs=3600))
        old = core.read_snapshot(self.snap, max_age_secs=None)
        self.assertEqual(old["caps"]["seven_day"]["used_percentage"], 10.0)
        self.assertGreater(old["age_secs"], 3600)

    def test_read_snapshot_missing_or_malformed(self):
        self.assertIsNone(core.read_snapshot(self.snap))
        with open(self.snap, "w") as f:
            f.write("{not json")
        self.assertIsNone(core.read_snapshot(self.snap))
        with open(self.snap, "w") as f:
            f.write("[1, 2, 3]")
        self.assertIsNone(core.read_snapshot(self.snap))

    def test_read_snapshot_naive_timestamp_no_crash(self):
        with open(self.snap, "w") as f:
            json.dump({"captured_at": "2026-07-03T22:40:00",
                       "caps": {"seven_day": {"used_percentage": 7.0}}}, f)
        snap = core.read_snapshot(self.snap, max_age_secs=None)
        self.assertIsNotNone(snap)
        self.assertIn("age_secs", snap)


class RenderTests(unittest.TestCase):
    def test_format_line(self):
        line = core.format_line(claude_code.parse(PAYLOAD))
        self.assertIn("Fable 5", line)
        self.assertIn("ctx 34%", line)
        self.assertIn("wk 10%", line)
        self.assertIn("resets", line)

    def test_format_line_shows_the_five_hour_window(self):
        # The session limit is the one you actually hit mid-afternoon, and
        # Claude Code has always sent it — only the render dropped it.
        # A reset later today is the common case, and there the clock time
        # alone says everything. Whole-segment equality, so a redundant
        # weekday would fail.
        noon = (datetime.datetime.combine(datetime.date.today(),
                                          datetime.time())
                + datetime.timedelta(hours=12)).timestamp()
        payload = json.loads(json.dumps(PAYLOAD))
        payload["rate_limits"]["five_hour"]["resets_at"] = noon
        line = core.format_line(claude_code.parse(payload))
        self.assertEqual(_segment(line, "5h"), "5h 22% (resets 12:00)")
        self.assertLess(line.index("ctx"), line.index("5h"))
        self.assertLess(line.index("5h"), line.index("wk"))

    def test_five_hour_reset_after_midnight_keeps_its_weekday(self):
        # A 5-hour window opened in the evening resets TOMORROW: 33.8% of
        # 29,166 captured history rows have a five_hour reset on a different
        # local date than the capture (real row: Tue 20:02 -> resets Wed 01:00).
        # A bare "01:00" there reads as a time this morning that already went.
        # Anchored to local midnight so the expectation holds whatever the
        # clock says when the suite runs.
        midnight = datetime.datetime.combine(datetime.date.today(),
                                             datetime.time())
        today = (midnight + datetime.timedelta(hours=12)).timestamp()
        tomorrow = (midnight + datetime.timedelta(hours=36)).timestamp()

        line = core.format_line(
            {"caps": {"five_hour": {"used_percentage": 31, "resets_at": today}}})
        self.assertEqual(_segment(line, "5h"), "5h 31% (resets 12:00)")

        line = core.format_line(
            {"caps": {"five_hour": {"used_percentage": 0,
                                    "resets_at": tomorrow}}})
        weekday = datetime.datetime.fromtimestamp(tomorrow).strftime("%a")
        self.assertEqual(_segment(line, "5h"),
                         "5h 0% (resets {} 12:00)".format(weekday))

    def test_fmt_reset_soon_marks_only_a_different_day(self):
        midnight = datetime.datetime.combine(datetime.date.today(),
                                             datetime.time())
        for hours, want_weekday in ((1, False), (12, False), (23.5, False),
                                    (36, True), (-12, True)):
            stamp = (midnight + datetime.timedelta(hours=hours)).timestamp()
            got = core.fmt_reset_soon(stamp)
            # "12:00" for today, "Wed 12:00" otherwise: the space is the tell.
            self.assertEqual(" " in got, want_weekday, (hours, got))
        for bad in (True, None, "soon", []):
            self.assertEqual(core.fmt_reset_soon(bad), "?", bad)

    def test_weekly_reset_always_keeps_its_weekday(self):
        # The weekly line is untouched: its reset is days out, so the weekday
        # is never redundant even when it happens to land today.
        noon = datetime.datetime.combine(
            datetime.date.today(), datetime.time()) + datetime.timedelta(hours=12)
        line = core.format_line(
            {"caps": {"seven_day": {"used_percentage": 37,
                                    "resets_at": noon.timestamp()}}})
        self.assertEqual(_segment(line, "wk"),
                         "wk 37% (resets {} 12:00)".format(noon.strftime("%a")))

    def test_format_line_renders_each_window_on_its_own(self):
        five = core.format_line(
            {"caps": {"five_hour": {"used_percentage": 22.0,
                                    "resets_at": 1782050340}}})
        self.assertIn("5h 22%", five)
        self.assertNotIn("wk", five)
        week = core.format_line(
            {"caps": {"seven_day": {"used_percentage": 10.0,
                                    "resets_at": 1782518400}}})
        self.assertIn("wk 10%", week)
        self.assertNotIn("5h", week)

    def test_format_line_missing_reset_time(self):
        self.assertEqual(
            core.format_line({"caps": {"five_hour": {"used_percentage": 22.0}}}),
            "5h 22% (resets ?)")

    def test_format_line_hostile_cap_windows(self):
        # The host owns this schema. Anything that is not a real percentage
        # suppresses the segment rather than rendering a confident wrong
        # number — True in particular, which isinstance(x, int) waves through.
        for caps in ({"five_hour": 5}, {"five_hour": None}, {"five_hour": []},
                     {"five_hour": {"used_percentage": True}},
                     {"five_hour": {"used_percentage": "22"}},
                     {"five_hour": {"used_percentage": None}},
                     {"seven_day": {"used_percentage": True}}):
            self.assertEqual(
                core.format_line({"model": "M", "caps": caps}), "M", caps)

    def test_format_line_trims_the_context_words_from_the_model(self):
        # Claude Code reports names like "Opus 5 (1M context)", but the window
        # size is already spelled out in the ctx segment, so the word is dead
        # columns on a line that has to fit a terminal.
        self.assertEqual(
            core.format_line({"model": "Opus 5 (1M context)",
                              "context_pct": 30}),
            "Opus 5 (1M) · ctx 30%")
        self.assertEqual(core.format_line({"model": "Sonnet 5 (200k context)"}),
                         "Sonnet 5 (200k)")
        # A name with no such suffix is left exactly alone.
        self.assertEqual(core.format_line({"model": "kimi-k3"}), "kimi-k3")

    def test_format_line_absolute_tokens(self):
        payload = {"model": {"display_name": "Fable 5"},
                   "context_window": {"used_percentage": 20,
                                      "total_input_tokens": 205_600,
                                      "context_window_size": 1_000_000}}
        self.assertIn("ctx 20% (206k/1M)",
                      core.format_line(claude_code.parse(payload)))
        # No window size -> tokens alone; no pct -> tokens still shown.
        line = core.format_line({"context_tokens": 9_500})
        self.assertIn("ctx 9.5k", line)
        # 0 = pre-first-response -> suppressed, pct-only.
        line = core.format_line({"context_pct": 12, "context_tokens": 0})
        self.assertEqual(line, "ctx 12%")

    def test_fmt_tokens(self):
        for n, want in ((618, "618"), (1_500, "1.5k"), (9_000, "9k"),
                        (94_000, "94k"), (205_600, "206k"),
                        (200_000, "200k"), (1_000_000, "1M"),
                        (1_500_000, "1.5M")):
            self.assertEqual(core.fmt_tokens(n), want)

    def test_reset_epoch_parses_both_wire_forms(self):
        # resets_at arrives as epoch seconds OR an ISO string, and the two can
        # name the SAME instant — so the parsed epoch, not the wire type, is
        # what identifies a window.
        stamp = 1782050340
        self.assertEqual(core._reset_epoch(stamp), float(stamp))
        self.assertEqual(core._reset_epoch(_iso(stamp)), float(stamp))
        # A naive ISO string is local time, exactly as fmt_reset always read it.
        naive = "2026-06-21T14:39:00"
        self.assertEqual(
            core._reset_epoch(naive),
            datetime.datetime.fromisoformat(naive).astimezone().timestamp())

    def test_reset_epoch_rejects_junk_and_bools(self):
        # JSON true is not epoch 1. isinstance(True, int) is the trap.
        for bad in (True, False, None, "soon", "", [], {}):
            self.assertIsNone(core._reset_epoch(bad), bad)

    def test_fmt_reset_default_format_is_unchanged(self):
        stamp = 1782050340
        self.assertEqual(
            core.fmt_reset(stamp),
            datetime.datetime.fromtimestamp(stamp).strftime("%a %H:%M"))
        self.assertEqual(core.fmt_reset(_iso(stamp)), core.fmt_reset(stamp))

    def test_fmt_reset_honours_an_explicit_format(self):
        stamp = 1782050340
        self.assertEqual(
            core.fmt_reset(stamp, "%H:%M"),
            datetime.datetime.fromtimestamp(stamp).strftime("%H:%M"))
        self.assertEqual(core.fmt_reset(_iso(stamp), "%H:%M"),
                         core.fmt_reset(stamp, "%H:%M"))

    def test_fmt_reset_junk_and_bools_render_a_question_mark(self):
        for bad in (True, False, None, "soon", [], {}):
            self.assertEqual(core.fmt_reset(bad), "?", bad)

    def test_format_line_fail_soft(self):
        self.assertEqual(core.format_line({}, None), "llmeter")
        self.assertEqual(core.format_line(None, None), "llmeter")

    def test_format_line_hostile_shapes(self):
        self.assertEqual(core.format_line(
            {"model": 5, "context_pct": "x", "caps": 3}, {"caps": 9}), "llmeter")
        # Hostile shapes in the new token fields must not raise or render junk.
        self.assertEqual(core.format_line(
            {"context_pct": 12, "context_tokens": "junk",
             "context_window_size": []}), "ctx 12%")
        self.assertEqual(core.format_line(
            {"context_tokens": True, "context_window_size": True}), "llmeter")

    def test_format_line_cost_only(self):
        # A pay-per-token reading (v2 shape) renders $ instead of a cap.
        r = {"model": "DeepSeek V4", "context_pct": 12, "caps": {},
             "cost": {"session_usd": 0.42}}
        line = core.format_line(r)
        self.assertIn("DeepSeek V4", line)
        self.assertIn("$0.42", line)

    def test_cross_window_cap_fallback(self):
        # A window whose payload lacks caps still shows the account-level wk %
        # from the freshest snapshot any other window persisted.
        reading = claude_code.parse(PAYLOAD)
        snap = dict(reading, captured_at=core.now_iso())
        no_caps = claude_code.parse({"model": {"display_name": "Opus 4.8"},
                                     "context_window": {"used_percentage": 16}})
        line = core.format_line(no_caps, snap)
        self.assertIn("Opus 4.8", line)
        self.assertIn("ctx 16%", line)
        self.assertIn("5h 22%", line)
        self.assertIn("wk 10%", line)


class MainTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="llmeter-main-")
        self.addCleanup(shutil.rmtree, d)
        self.snap = os.path.join(d, "usage-snapshot.json")
        self.hist = os.path.join(d, "usage-history.jsonl")

    def _run(self, payload):
        from llmeter import statusline
        out = io.StringIO()
        with mock.patch.object(core, "SNAPSHOT_PATH", self.snap), \
             mock.patch.object(core, "HISTORY_PATH", self.hist), \
             mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(out):
            statusline.main()
        return out.getvalue().strip()

    def test_main_harvests_and_prints(self):
        line = self._run(PAYLOAD)
        self.assertIn("Fable 5", line)
        self.assertIn("5h 22%", line)
        self.assertIn("wk 10%", line)
        self.assertTrue(os.path.exists(self.snap))

    def test_main_falls_back_to_cached_snapshot(self):
        self._run(PAYLOAD)  # first window populates the account-level cap
        line = self._run({"model": {"display_name": "Opus 4.8"},
                          "context_window": {"used_percentage": 16}})
        self.assertIn("Opus 4.8", line)
        self.assertIn("ctx 16%", line)
        self.assertIn("5h 22%", line)  # both windows ride the cache
        self.assertIn("wk 10%", line)  # from the cross-window cache

    def test_main_invalid_stdin_still_prints(self):
        out = io.StringIO()
        from llmeter import statusline
        with mock.patch.object(core, "SNAPSHOT_PATH", self.snap), \
             mock.patch.object(core, "HISTORY_PATH", self.hist), \
             mock.patch("sys.stdin", io.StringIO("{not json")), \
             contextlib.redirect_stdout(out):
            rc = statusline.main()
        self.assertEqual(rc, 0)
        self.assertTrue(out.getvalue().strip())  # printed SOMETHING


if __name__ == "__main__":
    unittest.main()


class MergeCapsTests(unittest.TestCase):
    """Idle sessions re-publish stale caps with fresh captured_at; the
    snapshot must keep the per-window truth (2026-07-06 flapping bug)."""

    def setUp(self):
        d = tempfile.mkdtemp(prefix="llm-merge-")
        self.addCleanup(shutil.rmtree, d)
        self.snap = os.path.join(d, "s.json")
        self.hist = os.path.join(d, "h.jsonl")

    def _reading(self, pct, resets=1783468800):
        return {"source": "claude-code", "model": "M",
                "caps": {"seven_day": {"used_percentage": pct,
                                       "resets_at": resets}}}

    def _write(self, pct, resets=1783468800):
        return core.write_snapshot(self._reading(pct, resets),
                                   snapshot_path=self.snap,
                                   history_path=self.hist)

    def test_same_window_keeps_max(self):
        self._write(82)
        snap = self._write(58)  # stale session republishing yesterday's view
        self.assertEqual(
            snap["caps"]["seven_day"]["used_percentage"], 82)

    def test_stale_republish_appends_no_history(self):
        self._write(82)
        self._write(58)
        self._write(76)
        self.assertEqual(len(_lines(self.hist)), 1)  # only the first value logged

    def test_new_window_wins_even_if_lower(self):
        self._write(82, resets=1783468800)
        snap = self._write(3, resets=1784073600)  # next week's window
        self.assertEqual(
            snap["caps"]["seven_day"]["used_percentage"], 3)

    def test_hostile_prev_caps_shape(self):
        core.write_snapshot({"source": "x", "model": "M", "caps":
                             {"seven_day": "junk"}},
                            snapshot_path=self.snap, history_path=self.hist)
        snap = self._write(50)
        self.assertEqual(
            snap["caps"]["seven_day"]["used_percentage"], 50)

    def test_legacy_entry_without_resets_cannot_block_new_window(self):
        core.write_snapshot({"source": "x", "model": "M", "caps":
                             {"seven_day": {"used_percentage": 90}}},
                            snapshot_path=self.snap, history_path=self.hist)
        snap = self._write(5, resets=1784073600)
        self.assertEqual(
            snap["caps"]["seven_day"]["used_percentage"], 5)

    def test_junk_new_reading_keeps_valid_old(self):
        self._write(82)
        snap = core.write_snapshot({"source": "x", "model": "M",
                                    "caps": {"seven_day": "junk"}},
                                   snapshot_path=self.snap,
                                   history_path=self.hist)
        self.assertEqual(
            snap["caps"]["seven_day"]["used_percentage"], 82)

    # --- window identity is the PARSED instant, not the wire type ---------
    # A weekly window rolls once a week, so the gaps below rarely bit. A
    # 5-hour window rolls five times a day, so each one is a stuck meter
    # within hours. These use five_hour to say which window they protect.

    def _win(self, pct, resets="absent"):
        win = {"used_percentage": pct}
        if resets != "absent":
            win["resets_at"] = resets
        return {"five_hour": win}

    def _merged(self, old, new):
        merged = core._merge_caps(old, new)
        return merged["five_hour"]["used_percentage"]

    def test_two_iso_resets_do_not_ratchet_the_meter(self):
        # The regression. Two ISO strings failed the numeric-type check and
        # fell through to max-wins, so the meter kept its high-water mark for
        # ever and a fresh window read as nearly exhausted.
        old = self._win(88, _iso(1782050340))
        new = self._win(4, _iso(1782050340 + 5 * 3600))
        self.assertEqual(self._merged(old, new), 4)
        self.assertEqual(self._merged(new, old), 4)  # order must not matter

    def test_iso_same_window_keeps_max(self):
        stamp = _iso(1782050340)
        self.assertEqual(self._merged(self._win(88, stamp),
                                      self._win(41, stamp)), 88)

    def test_epoch_and_equivalent_iso_are_one_window(self):
        stamp = 1782050340
        self.assertEqual(self._merged(self._win(88, stamp),
                                      self._win(41, _iso(stamp))), 88)
        self.assertEqual(self._merged(self._win(41, _iso(stamp)),
                                      self._win(88, stamp)), 88)

    def test_later_iso_beats_an_earlier_epoch(self):
        # Mixed forms handed the win to whichever side happened to be numeric,
        # so a newer ISO-identified window lost to an older epoch one.
        stamp = 1782050340
        self.assertEqual(self._merged(self._win(88, stamp),
                                      self._win(2, _iso(stamp + 5 * 3600))), 2)
        self.assertEqual(self._merged(self._win(2, _iso(stamp + 5 * 3600)),
                                      self._win(88, stamp)), 2)

    def test_unidentified_window_loses_to_an_identified_one(self):
        for junk in ("absent", None, "soon", True, False, []):
            self.assertEqual(self._merged(self._win(90, junk),
                                          self._win(3, 1782050340)), 3, junk)
            self.assertEqual(self._merged(self._win(3, 1782050340),
                                          self._win(90, junk)), 3, junk)

    def test_two_unidentified_windows_keep_the_max(self):
        self.assertEqual(self._merged(self._win(90), self._win(12)), 90)

    def test_bool_percentage_is_not_a_valid_window(self):
        stamp = 1782050340
        self.assertEqual(self._merged(self._win(True, stamp),
                                      self._win(7, stamp)), 7)
        self.assertEqual(self._merged(self._win(7, stamp),
                                      self._win(True, stamp)), 7)
        # Bool on both sides is no window at all, so nothing is stored.
        self.assertEqual(core._merge_caps(self._win(True, stamp),
                                          self._win(False, stamp)), {})


class ProviderScopeTests(unittest.TestCase):
    """A usage cap belongs to an account, not a machine.

    Live bug, 2026-08-09: one project routed at Alibaba via ANTHROPIC_BASE_URL
    showed "qwen3.8-max · wk 42%" — the 42% was the DEFAULT account's weekly,
    borrowed from the shared snapshot, while the vendor's own quota was
    exhausted and returning 429s. A confident number from the wrong account is
    worse than no number, so the cap is now scoped per provider.
    """

    ALIYUN = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic"

    def setUp(self):
        d = tempfile.mkdtemp(prefix="llmeter-provider-")
        self.addCleanup(shutil.rmtree, d)
        self.dir = d
        self.snap = os.path.join(d, "usage-snapshot.json")
        self.hist = os.path.join(d, "usage-history.jsonl")

    @contextlib.contextmanager
    def _env(self, base_url):
        env = dict(os.environ)
        env.pop("ANTHROPIC_BASE_URL", None)
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url
        with mock.patch.dict(os.environ, env, clear=True):
            yield

    def _run(self, payload):
        from llmeter import statusline
        out = io.StringIO()
        with mock.patch.object(core, "DIR", self.dir), \
             mock.patch.object(core, "SNAPSHOT_PATH", self.snap), \
             mock.patch.object(core, "HISTORY_PATH", self.hist), \
             mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(out):
            statusline.main()
        return out.getvalue().strip()

    # --- provider_key ---------------------------------------------------
    def test_unset_or_blank_base_url_is_the_default_provider(self):
        for value in (None, "", "   "):
            with self._env(value):
                self.assertEqual(core.provider_key(), core.DEFAULT_PROVIDER)

    def test_anthropics_own_host_is_the_default_provider(self):
        # Setting the canonical base URL explicitly must not split the cap.
        with self._env("https://api.anthropic.com"):
            self.assertEqual(core.provider_key(), core.DEFAULT_PROVIDER)

    def test_third_party_host_is_its_own_provider(self):
        # Key is "<readable label>#<digest>": the label carries the canonical
        # URL so a human can read it, the digest makes identity injective.
        with self._env(self.ALIYUN):
            key = core.provider_key()
        self.assertTrue(key.startswith(
            "https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic#"),
            key)

    def test_provider_key_never_raises_on_junk(self):
        for junk in ("not a url", "http://", "://///", "http://[oops"):
            with self._env(junk):
                self.assertIsInstance(core.provider_key(), str)

    def test_case_does_not_split_a_provider(self):
        with self._env("HTTPS://Gateway.Example.COM/v1"):
            first = core.provider_key()
        with self._env("https://gateway.example.com/v1"):
            self.assertEqual(core.provider_key(), first)

    # --- snapshot paths -------------------------------------------------
    def test_default_provider_keeps_the_original_filename(self):
        # Existing consumers read usage-snapshot.json; that must not move.
        with self._env(None), mock.patch.object(core, "SNAPSHOT_PATH", self.snap):
            self.assertEqual(core.snapshot_path_for(), self.snap)

    def test_third_party_gets_its_own_file(self):
        with self._env(self.ALIYUN), mock.patch.object(core, "DIR", self.dir):
            path = core.snapshot_path_for()
        self.assertNotEqual(path, self.snap)
        self.assertTrue(os.path.basename(path).startswith("usage-snapshot."))
        self.assertTrue(path.endswith(".json"))

    def test_provider_slug_is_filesystem_safe(self):
        slug = core._provider_slug("Weird/Host:8080\\..")
        self.assertNotIn("/", slug)
        self.assertNotIn("\\", slug)
        self.assertNotIn(":", slug)
        self.assertTrue(slug)

    # --- the regression --------------------------------------------------
    def test_third_party_session_does_not_borrow_the_default_account_cap(self):
        with self._env(None):
            seeded = self._run(PAYLOAD)
        self.assertIn("wk 10%", seeded)  # default account persisted its cap

        with self._env(self.ALIYUN):
            line = self._run({"model": {"display_name": "qwen3.8-max"},
                              "context_window": {"used_percentage": 16}})
        self.assertIn("qwen3.8-max", line)
        self.assertIn("ctx 16%", line)
        self.assertNotIn("wk", line)  # the whole point: no borrowed number
        self.assertNotIn("5h", line)  # and the session window is no different

    def test_same_provider_fallback_still_works(self):
        # The cross-window fallback is the feature; only cross-PROVIDER is wrong.
        with self._env(None):
            self._run(PAYLOAD)
            line = self._run({"model": {"display_name": "Opus 4.8"},
                              "context_window": {"used_percentage": 16}})
        self.assertIn("wk 10%", line)

    def test_third_party_caps_never_land_in_the_default_snapshot(self):
        with self._env(None):
            self._run(PAYLOAD)
        before = _json(self.snap)

        vendor = json.loads(json.dumps(PAYLOAD))
        vendor["rate_limits"]["seven_day"]["used_percentage"] = 99.0
        vendor["model"]["display_name"] = "qwen3.8-max"
        with self._env(self.ALIYUN):
            self._run(vendor)

        after = _json(self.snap)
        self.assertEqual(after["caps"]["seven_day"]["used_percentage"],
                         before["caps"]["seven_day"]["used_percentage"])
        self.assertEqual(after["model"], "Fable 5")

    def test_each_provider_keeps_its_own_cap(self):
        vendor = json.loads(json.dumps(PAYLOAD))
        vendor["rate_limits"]["seven_day"]["used_percentage"] = 99.0
        with self._env(self.ALIYUN):
            self._run(vendor)
            line = self._run({"model": {"display_name": "qwen3.8-max"},
                              "context_window": {"used_percentage": 16}})
        self.assertIn("wk 99%", line)  # its OWN number, not the default's

    def test_snapshot_and_history_record_the_provider(self):
        with self._env(self.ALIYUN):
            expected = core.provider_key()
        with self._env(self.ALIYUN):
            self._run(PAYLOAD)
            with mock.patch.object(core, "DIR", self.dir), \
                 mock.patch.object(core, "HISTORY_PATH", self.hist):
                snap_path = core.snapshot_path_for()
                hist_path = core.history_path_for()
        self.assertEqual(_json(snap_path)["provider"], expected)
        row = json.loads(_lines(hist_path)[-1])
        self.assertEqual(row["provider"], expected)

    # --- codex review round 1 -------------------------------------------
    def test_same_host_different_routes_are_different_providers(self):
        # [P1] A gateway multiplexes upstreams off one hostname. Host-only
        # identity collided them, so A's cap surfaced as B's fallback.
        with self._env("https://gateway.example.com/provider-a"):
            a = core.provider_key()
            a_path = core.snapshot_path_for()
        with self._env("https://gateway.example.com/provider-b"):
            b = core.provider_key()
            b_path = core.snapshot_path_for()
        self.assertNotEqual(a, b)
        self.assertNotEqual(a_path, b_path)

    def test_port_is_part_of_the_identity(self):
        with self._env("https://gw.example.com:8443/v1"):
            a = core.provider_key()
        with self._env("https://gw.example.com:9443/v1"):
            self.assertNotEqual(core.provider_key(), a)

    def test_trailing_slash_and_fqdn_dot_do_not_split_a_provider(self):
        with self._env("https://GW.Example.COM./v1/"):
            a = core.provider_key()
        with self._env("https://gw.example.com/v1"):
            self.assertEqual(core.provider_key(), a)

    def test_gateway_route_caps_stay_separate_end_to_end(self):
        route_a = "https://gateway.example.com/provider-a"
        route_b = "https://gateway.example.com/provider-b"
        with self._env(route_a):
            self._run(PAYLOAD)  # A persists wk 10%
        with self._env(route_b):
            line = self._run({"model": {"display_name": "B"},
                              "context_window": {"used_percentage": 5}})
        self.assertNotIn("wk", line)
        self.assertNotIn("5h", line)

    def test_explicit_path_cannot_bypass_provider_isolation(self):
        # [P2] read_snapshot(SNAPSHOT_PATH) reached the default account's file
        # regardless of who was asking. The stamped provider is now verified.
        with self._env(None):
            self._run(PAYLOAD)
        with self._env(self.ALIYUN):
            self.assertIsNone(core.read_snapshot(self.snap, max_age_secs=None))
        # ...and an explicit opt-out still allows deliberate inspection.
        with self._env(self.ALIYUN):
            forced = core.read_snapshot(self.snap, max_age_secs=None,
                                        provider=False)
        self.assertEqual(forced["caps"]["seven_day"]["used_percentage"], 10.0)

    def test_legacy_snapshot_without_provider_reads_as_default_account(self):
        # Migration: files written before this field existed were the default.
        with self._env(None):
            self._run(PAYLOAD)
        legacy = _json(self.snap)
        legacy.pop("provider", None)
        with open(self.snap, "w") as f:
            json.dump(legacy, f)
        with self._env(None):
            self.assertIsNotNone(core.read_snapshot(self.snap,
                                                    max_age_secs=None))
        with self._env(self.ALIYUN):
            self.assertIsNone(core.read_snapshot(self.snap, max_age_secs=None))

    def test_history_is_per_provider(self):
        # [P2] A consumer predating this change must not read a third party's
        # rows as the default account's cap changes.
        with self._env(None):
            self._run(PAYLOAD)
        with self._env(self.ALIYUN):
            vendor = json.loads(json.dumps(PAYLOAD))
            vendor["rate_limits"]["seven_day"]["used_percentage"] = 99.0
            self._run(vendor)
        rows = [json.loads(l) for l in _lines(self.hist)]
        self.assertTrue(rows)
        for row in rows:  # default history stays purely default-account
            self.assertEqual(row.get("provider"), core.DEFAULT_PROVIDER)

    def test_slug_is_bounded_and_collision_free(self):
        long_host = ".".join(["averyverylongsubdomainlabel"] * 12) + ".example.com"
        slug = core._provider_slug(long_host)
        # What actually matters is that the FILENAME stays inside the 255-byte
        # limit every common filesystem enforces, so persistence can't fail.
        with mock.patch.object(core, "DIR", self.dir):
            name = os.path.basename(core.snapshot_path_for(long_host))
        self.assertLess(len(name.encode("utf-8")), 255)
        # Two hosts sharing a sanitised prefix must not share a file.
        a = core._provider_slug("a" * 60 + "-one")
        b = core._provider_slug("a" * 60 + "-two")
        self.assertNotEqual(a, b)

    def test_environment_switch_round_trip_restores_the_cap(self):
        with self._env(None):
            self._run(PAYLOAD)
        with self._env(self.ALIYUN):
            self._run({"model": {"display_name": "qwen3.8-max"},
                       "context_window": {"used_percentage": 3}})
        with self._env(None):  # back to the default account
            line = self._run({"model": {"display_name": "Opus 4.8"},
                              "context_window": {"used_percentage": 9}})
        self.assertIn("wk 10%", line)

    def test_two_distinct_third_parties_do_not_share(self):
        with self._env("https://vendor-one.example.com"):
            self._run(PAYLOAD)
        with self._env("https://vendor-two.example.com"):
            line = self._run({"model": {"display_name": "two"},
                              "context_window": {"used_percentage": 4}})
        self.assertNotIn("wk", line)
        self.assertNotIn("5h", line)

    def test_persisted_fields_are_exactly_the_documented_set(self):
        with self._env(None):
            self._run(PAYLOAD)
        allowed = set(core._SNAPSHOT_FIELDS) | set(core._STAMPED_FIELDS)
        self.assertTrue(set(_json(self.snap)).issubset(allowed),
                        "a field reached disk that is on no allowlist")

    # --- codex review round 2: identity must be injective ----------------
    def _keys(self, *urls):
        out = []
        for u in urls:
            with self._env(u):
                out.append(core.provider_key())
        return out

    def test_scheme_is_part_of_the_identity(self):
        a, b = self._keys("http://gateway.example.com/v1",
                          "https://gateway.example.com/v1")
        self.assertNotEqual(a, b)

    def test_query_is_part_of_the_identity(self):
        # A multi-tenant gateway may route by query string.
        a, b = self._keys("https://gw.example.com/v1?account=a",
                          "https://gw.example.com/v1?account=b")
        self.assertNotEqual(a, b)

    def test_userinfo_is_part_of_the_identity(self):
        a, b = self._keys("https://alice:token-a@gw.example.com/v1",
                          "https://bob:token-b@gw.example.com/v1")
        self.assertNotEqual(a, b)

    def test_userinfo_never_reaches_the_readable_key_or_disk(self):
        # llmeter's promise is that no credential lands on disk. Userinfo may
        # only influence the one-way digest.
        with self._env("https://alice:s3cret-token@gw.example.com/v1"):
            key = core.provider_key()
            self._run(PAYLOAD)
            with mock.patch.object(core, "DIR", self.dir):
                path = core.snapshot_path_for()
        self.assertNotIn("s3cret-token", key)
        self.assertNotIn("alice", key)
        self.assertNotIn("s3cret-token", path)
        self.assertNotIn("s3cret-token", _read(path))

    def test_ipv6_literal_and_port_cannot_blur(self):
        a, b = self._keys("https://[2001:db8::1]:8443/v1",
                          "https://[2001:db8::1:8443]/v1")
        self.assertNotEqual(a, b)

    def test_default_port_does_not_split_a_provider(self):
        a, b = self._keys("https://gw.example.com/v1",
                          "https://gw.example.com:443/v1")
        self.assertEqual(a, b)
        c, d = self._keys("http://gw.example.com/v1",
                          "http://gw.example.com:80/v1")
        self.assertEqual(c, d)

    def test_non_default_port_still_splits(self):
        a, b = self._keys("https://gw.example.com/v1",
                          "https://gw.example.com:8443/v1")
        self.assertNotEqual(a, b)

    def test_scheme_variants_do_not_share_a_cap_end_to_end(self):
        with self._env("https://gw.example.com/v1"):
            self._run(PAYLOAD)
        with self._env("http://gw.example.com/v1"):
            line = self._run({"model": {"display_name": "other"},
                              "context_window": {"used_percentage": 5}})
        self.assertNotIn("wk", line)
        self.assertNotIn("5h", line)

    # --- codex review round 3 --------------------------------------------
    def test_query_secret_never_reaches_disk(self):
        # A query string routinely carries credentials. It must still change
        # identity, but must not appear in the key, the filename or the file.
        url = "https://gw.example.com/v1?api_key=s3cret-value"
        with self._env(url):
            key = core.provider_key()
            self._run(PAYLOAD)
            with mock.patch.object(core, "DIR", self.dir):
                path = core.snapshot_path_for()
                hist = core.history_path_for()
        for blob in (key, path, _read(path), _read(hist)):
            self.assertNotIn("s3cret-value", blob)
            self.assertNotIn("api_key", blob)

    def test_query_still_separates_accounts(self):
        a, b = self._keys("https://gw.example.com/v1?account=a",
                          "https://gw.example.com/v1?account=b")
        self.assertNotEqual(a, b)

    def test_hostile_env_values_never_raise(self):
        hostile = ["https://" + "\udcff" + ".example/v1", "https://gw.example:bad",
                   "\udcff", "https://[oops", "://", "%%%", "https://" + "x" * 5000]
        for value in hostile:
            with self._env(value):
                key = core.provider_key()
                self.assertIsInstance(key, str)
                self.assertTrue(key)
        self.assertIsInstance(core._provider_slug("\udcff"), str)

    def test_hostile_env_value_still_persists(self):
        # Fail-soft must not mean "silently stop recording".
        with self._env("https://" + "\udcff" + ".example/v1"):
            line = self._run(PAYLOAD)
        self.assertIn("wk 10%", line)
