"""Tests for llmeter — the Claude Code adapter, the core harvester, and the
fail-soft render path. Stdlib unittest, no dependencies.

Run:  python3 -m unittest discover -s tests  (from the repo root)
"""

import contextlib
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


class HarvestTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="llmeter-")
        self.addCleanup(shutil.rmtree, d)
        self.snap = os.path.join(d, "usage-snapshot.json")
        self.hist = os.path.join(d, "usage-history.jsonl")

    def _write(self, payload, now="2026-07-03T22:40:00+10:00"):
        return core.write_snapshot(claude_code.parse(payload),
                                   self.snap, self.hist, now=now)

    def test_writes_snapshot_and_history(self):
        snap = self._write(PAYLOAD)
        self.assertEqual(snap["caps"]["seven_day"]["used_percentage"], 10.0)
        on_disk = _json(self.snap)
        self.assertEqual(on_disk["model"], "Fable 5")
        self.assertEqual(on_disk["context_pct"], 34.5)
        self.assertEqual(on_disk["source"], "claude-code")
        self.assertEqual(len(_lines(self.hist)), 1)

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
        self.assertIn("wk 10%", line)
        self.assertTrue(os.path.exists(self.snap))

    def test_main_falls_back_to_cached_snapshot(self):
        self._run(PAYLOAD)  # first window populates the account-level cap
        line = self._run({"model": {"display_name": "Opus 4.8"},
                          "context_window": {"used_percentage": 16}})
        self.assertIn("Opus 4.8", line)
        self.assertIn("ctx 16%", line)
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
