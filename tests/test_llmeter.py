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
        lines = open(self.hist).read().strip().splitlines()
        self.assertEqual(len(lines), 1)  # only the first real value logged

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
        # Identity is host + path: one gateway can front several accounts.
        with self._env(self.ALIYUN):
            self.assertEqual(
                core.provider_key(),
                "token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic")

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
        expected = "token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic"
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
        self.assertLessEqual(len(slug), 64)
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

    def test_persisted_fields_are_exactly_the_documented_set(self):
        with self._env(None):
            self._run(PAYLOAD)
        allowed = set(core._SNAPSHOT_FIELDS) | set(core._STAMPED_FIELDS)
        self.assertTrue(set(_json(self.snap)).issubset(allowed),
                        "a field reached disk that is on no allowlist")
