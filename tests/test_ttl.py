"""Tests for llmeter.ttl — the prompt-cache TTL advisor.

The advisor's whole job is to pick a side, so the suite pins both answers: a
write-heavy fixture must recommend 5m and a gap-heavy one must recommend 1h. A
suite that only ever sees one verdict cannot tell a working comparison from a
constant, so the margins and the flip factors are asserted numerically too.
"""

import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

from llmeter import ttl
from llmeter import ttl_report

NOW = datetime.datetime(2026, 9, 2, 12, 0, 0, tzinfo=datetime.timezone.utc)


def turn(offset_seconds, written=0, read=0, model="claude-opus-5", at_1h=None,
         at_5m=None, message_id=None, uuid=None, parent=None):
    """One assistant turn, offset back from NOW."""
    when = NOW - datetime.timedelta(seconds=offset_seconds)
    if at_1h is None and at_5m is None:
        at_1h, at_5m = 0, written
    entry = {
        "type": "assistant",
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "message": {
            "model": model,
            "usage": {
                "cache_creation_input_tokens": written,
                "cache_read_input_tokens": read,
                "cache_creation": {
                    "ephemeral_1h_input_tokens": at_1h or 0,
                    "ephemeral_5m_input_tokens": at_5m or 0,
                },
            },
        },
    }
    if message_id is not None:
        entry["message"]["id"] = message_id
    if uuid is not None:
        entry["uuid"] = uuid
    if parent is not None:
        entry["parentUuid"] = parent
    return entry


def line(uuid, parent=None, kind="user"):
    """A transcript line that is not an assistant reply — a prompt or a result."""
    entry = {"type": kind, "uuid": uuid}
    if parent is not None:
        entry["parentUuid"] = parent
    return entry


class TTLFixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        # Claude Code names these after the encoded project path; the advisor
        # globs whatever is there, so the fixture uses a plain name.
        self.project = os.path.join(self.root, "encoded-project-dir")
        os.makedirs(self.project)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_session(self, name, turns, sidechain=False):
        path = os.path.join(self.project, name + ".jsonl")
        with open(path, "a") as handle:
            for entry in turns:
                entry = dict(entry)
                if sidechain:
                    entry["isSidechain"] = True
                handle.write(json.dumps(entry) + "\n")
        return path

    def write_subagent(self, session, name, turns):
        folder = os.path.join(self.project, session, "subagents")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name + ".jsonl")
        with open(path, "w") as handle:
            for entry in turns:
                handle.write(json.dumps(entry) + "\n")
        return path

    def measure(self, days=14):
        return ttl.measure(projects_dir=self.root, days=days, now=NOW)

    def main(self, days=14):
        return self.measure(days)["buckets"][ttl.BUCKET_MAIN]

    def other(self, days=14):
        return self.measure(days)["buckets"][ttl.BUCKET_OTHER]


class DeduplicationTest(TTLFixture):
    def test_multi_block_reply_counts_once(self):
        # Claude Code writes one line per content block, each re-sending the
        # same cumulative usage. Counting them all inflates writes without
        # inflating gaps, which manufactures a 5m verdict.
        self.write_session("s1", [
            turn(600, written=50_000, message_id="msg_a"),
            turn(599, written=50_000, message_id="msg_a"),
            turn(598, written=50_000, message_id="msg_a"),
        ])
        stats = self.main()
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["write_tokens"], 50_000)

    def test_the_same_id_returning_later_is_a_separate_reply(self):
        self.write_session("s1", [
            turn(600, written=10_000, message_id="msg_a"),
            turn(590, written=10_000, message_id="msg_b"),
            turn(580, written=10_000, message_id="msg_a"),
        ])
        self.assertEqual(self.main()["requests"], 3)

    def test_turns_without_an_id_are_never_deduped(self):
        self.write_session("s1", [turn(600, written=10_000), turn(590, written=10_000)])
        self.assertEqual(self.main()["requests"], 2)


class VerdictTest(TTLFixture):
    def test_write_heavy_traffic_recommends_5m(self):
        # Continuous work: big writes, no idle gap to earn the premium back.
        self.write_session("s1", [turn(600, written=100_000),
                                  turn(540, written=100_000, read=50_000)])
        stats = self.main()
        self.assertEqual(stats["verdict"], "5m")
        self.assertEqual(stats["write_tokens"], 200_000)
        self.assertEqual(stats["gap_tokens"], 0)
        self.assertGreater(stats["delta"], 0)

    def test_gap_heavy_traffic_recommends_1h(self):
        # Small writes, then a 10-minute break onto a large cached prefix: a hit
        # at 1h, a full re-write at 5m. This is the case 1h exists for.
        self.write_session("s1", [turn(1200, written=1_000),
                                  turn(600, written=1_000, read=200_000)])
        stats = self.main()
        self.assertEqual(stats["verdict"], "1h")
        self.assertEqual(stats["gap_count"], 1)
        self.assertLess(stats["delta"], 0)

    def test_gap_delta_is_a_write_both_lifetimes_pay(self):
        # On a hit the transcript separates prefix from delta. Only the prefix
        # is priced differently; the delta is an ordinary write.
        self.write_session("s1", [turn(1200, written=1_000),
                                  turn(600, written=7_000, read=200_000)])
        stats = self.main()
        self.assertEqual(stats["gap_tokens"], 200_000)
        self.assertEqual(stats["write_tokens"], 8_000)

    def test_a_fused_miss_holds_back_a_typical_delta(self):
        # A miss reports prefix and delta as one number. The conversation's own
        # hits say what a delta usually costs, so that much is held back.
        self.write_session("s1", [
            turn(1800, written=100_000),
            turn(1700, written=2_000, read=100_000),
            turn(1600, written=2_000, read=102_000),
            turn(1000, written=110_000, read=0),
        ])
        stats = self.main()
        self.assertEqual(stats["gaps_estimated"], 1)
        self.assertEqual(stats["gap_tokens"], 108_000)
        self.assertEqual(stats["write_tokens"], 106_000)

    def test_a_bucket_with_no_sample_counts_the_whole_fused_write_as_prefix(self):
        # No short-gap hit anywhere, so there is nothing to size the appended
        # turn from. The split is disclosed rather than guessed at.
        self.write_session("s1", [turn(1600, written=5_000),
                                  turn(800, written=120_000, read=0)])
        stats = self.main()
        self.assertEqual(stats["gaps_estimated"], 1)
        self.assertEqual(stats["gaps_unsampled"], 1)
        self.assertEqual(stats["gap_tokens"], 120_000)
        self.assertEqual(stats["write_tokens"], 5_000)
        self.assertEqual(stats["verdict"], "1h")

    def test_a_conversation_sizes_its_gap_from_its_own_turns(self):
        # s1 appends 20k per turn, s2 appends 4k, so the bucket median (4k) is
        # not s1's (20k). s1's fused miss must be split by s1's own figure.
        self.write_session("s1", [turn(2000, written=5_000),
                                  turn(1990, written=20_000, read=50_000),
                                  turn(1000, written=100_000, read=0)])
        self.write_session("s2", [turn(2000, written=5_000),
                                  turn(1995, written=4_000, read=50_000),
                                  turn(1990, written=4_000, read=50_000)])
        stats = self.main()
        self.assertEqual(stats["gaps_estimated"], 1)
        # 100,000 less s1's own 20,000; the bucket median would leave 96,000.
        self.assertEqual(stats["gap_tokens"], 80_000)

    def test_a_sampled_bucket_reports_no_unsampled_gap(self):
        self.write_session("s1", [turn(1600, written=5_000),
                                  turn(1500, written=2_000, read=40_000),
                                  turn(800, written=120_000, read=0)])
        stats = self.main()
        self.assertEqual(stats["gaps_estimated"], 1)
        self.assertEqual(stats["gaps_unsampled"], 0)

    def test_a_fused_miss_never_reports_a_negative_prefix(self):
        self.write_session("s1", [
            turn(1800, written=100_000),
            turn(1700, written=90_000, read=100_000),
            turn(1000, written=500, read=0),
        ])
        stats = self.main()
        self.assertEqual(stats["gap_tokens"], 0)

    def test_gap_of_exactly_five_minutes_is_not_a_gap(self):
        self.write_session("s1", [turn(900, written=1_000),
                                  turn(600, written=5_000, read=90_000)])
        stats = self.main()
        self.assertEqual(stats["gap_count"], 0)
        self.assertEqual(stats["gap_tokens"], 0)

    def test_gap_of_exactly_one_hour_still_counts(self):
        self.write_session("s1", [turn(4200, written=1_000),
                                  turn(600, written=5_000, read=90_000)])
        stats = self.main()
        self.assertEqual(stats["gap_count"], 1)
        self.assertEqual(stats["gaps_over_1h"], 0)
        self.assertEqual(stats["gap_tokens"], 90_000)

    def test_gap_longer_than_an_hour_favours_neither(self):
        # Both lifetimes miss, so the re-write is an ordinary write.
        self.write_session("s1", [turn(9000, written=1_000), turn(600, written=200_000)])
        stats = self.main()
        self.assertEqual(stats["gap_count"], 0)
        self.assertEqual(stats["gaps_over_1h"], 1)
        self.assertEqual(stats["gap_tokens"], 0)
        self.assertEqual(stats["write_tokens"], 201_000)


class MarginTest(TTLFixture):
    def test_five_minute_margin_and_flip_factor_are_exact(self):
        self.write_session("s1", [turn(1200, written=100_000),
                                  turn(600, written=0, read=10_000)])
        stats = self.main()
        # W = 100_000, G = 10_000, read rate 0.1
        self.assertEqual(stats["write_tokens"], 100_000)
        self.assertEqual(stats["gap_tokens"], 10_000)
        self.assertAlmostEqual(stats["premium_1h"], 75_000.0, places=3)
        self.assertAlmostEqual(stats["penalty_5m"], 11_500.0, places=3)
        self.assertAlmostEqual(stats["delta"], 63_500.0, places=3)
        self.assertEqual(stats["verdict"], "5m")
        # 75_000 / 1.15 = 65_217.4 tokens of gap prefix to break even.
        self.assertAlmostEqual(stats["flip_factor"], 6.5217, places=3)

    def test_one_hour_margin_and_flip_factor_are_exact(self):
        self.write_session("s1", [turn(1200, written=1_000),
                                  turn(600, written=0, read=200_000)])
        stats = self.main()
        self.assertEqual(stats["write_tokens"], 1_000)
        self.assertEqual(stats["gap_tokens"], 200_000)
        self.assertAlmostEqual(stats["premium_1h"], 750.0, places=3)
        self.assertAlmostEqual(stats["penalty_5m"], 230_000.0, places=3)
        self.assertEqual(stats["verdict"], "1h")
        # 230_000 / 0.75 = 306_666 tokens of writing to break even, on 1_000.
        self.assertAlmostEqual(stats["flip_factor"], 306.667, places=2)

    def test_an_exact_tie_resolves_to_five_minutes(self):
        # 0.75 x 23,000 == 1.15 x 15,000. Both terms are non-zero, so the tie
        # is decided by the delta == 0 branch and not by an empty-input guard.
        self.write_session("s1", [turn(1200, written=23_000),
                                  turn(600, written=0, read=15_000)])
        stats = self.main()
        self.assertEqual(stats["write_tokens"], 23_000)
        self.assertEqual(stats["gap_tokens"], 15_000)
        self.assertAlmostEqual(stats["premium_1h"], 17_250.0, places=3)
        self.assertAlmostEqual(stats["penalty_5m"], 17_250.0, places=3)
        self.assertEqual(stats["delta"], 0)
        self.assertEqual(stats["verdict"], "5m")
        self.assertIsNone(stats["flip_factor"])

    def test_flip_factor_is_absent_when_there_is_nothing_to_multiply(self):
        self.write_session("s1", [turn(600, written=100_000),
                                  turn(540, written=100_000, read=1_000)])
        stats = self.main()
        self.assertEqual(stats["verdict"], "5m")
        self.assertIsNone(stats["flip_factor"])


class ReadRateTest(TTLFixture):
    def test_rate_is_weighted_by_the_gap_prefixes_it_prices(self):
        self.write_session("s1", [turn(1200, written=1_000, model="claude-opus-5"),
                                  turn(600, written=0, read=100_000,
                                       model="claude-opus-5")])
        self.write_session("s2", [turn(1200, written=1_000, model="claude-fable-5-1"),
                                  turn(600, written=0, read=100_000,
                                       model="claude-fable-5-1")])
        stats = self.main()
        # (0.1 * 100_000 + 0.025 * 100_000) / 200_000
        self.assertAlmostEqual(stats["read_rate"], 0.0625, places=6)

    def test_non_gap_writes_on_another_model_do_not_dilute_the_rate(self):
        # A large Fable write block that never idles, plus one Opus gap. The
        # penalty is charged against Opus-priced prefix, so the rate is Opus's.
        self.write_session("s1", [turn(1200, written=145_000,
                                       model="claude-fable-5-1")])
        self.write_session("s2", [turn(1200, written=10_000, model="claude-opus-5"),
                                  turn(600, written=0, read=100_000,
                                       model="claude-opus-5")])
        stats = self.main()
        self.assertEqual(stats["write_tokens"], 155_000)
        self.assertEqual(stats["gap_tokens"], 100_000)
        self.assertAlmostEqual(stats["read_rate"], 0.1, places=6)
        self.assertAlmostEqual(stats["premium_1h"], 116_250.0, places=3)
        self.assertAlmostEqual(stats["penalty_5m"], 115_000.0, places=3)
        self.assertEqual(stats["verdict"], "5m")

    def test_a_bucket_with_no_gap_prefix_reports_the_default_rate(self):
        self.write_session("s1", [turn(600, written=50_000,
                                       model="claude-fable-5-1")])
        stats = self.main()
        self.assertEqual(stats["gap_tokens"], 0)
        self.assertAlmostEqual(stats["read_rate"], ttl.READ_DEFAULT, places=6)

    def test_rate_can_never_exceed_the_dearest_model(self):
        self.write_session("s1", [turn(1200, written=50_000, model="claude-opus-5"),
                                  turn(600, written=0, read=50_000,
                                       model="claude-opus-5"),
                                  turn(100, written=0, read=50_000,
                                       model="claude-fable-5-1")])
        stats = self.main()
        self.assertLessEqual(stats["read_rate"], max(
            [ttl.READ_DEFAULT] + list(ttl.READ_BY_MODEL.values())))

    def test_a_cheaper_read_leans_further_toward_five_minutes(self):
        self.write_session("s1", [turn(1200, written=10_000, model="claude-opus-5"),
                                  turn(600, written=0, read=100_000, model="claude-opus-5")])
        opus = self.main()["penalty_5m"]
        shutil.rmtree(self.project)
        os.makedirs(self.project)
        self.write_session("s1", [turn(1200, written=10_000, model="claude-fable-5-1"),
                                  turn(600, written=0, read=100_000, model="claude-fable-5-1")])
        fable = self.main()["penalty_5m"]
        self.assertGreater(fable, opus)


class BucketTest(TTLFixture):
    def test_subagent_transcripts_are_the_other_bucket(self):
        self.write_subagent("s1", "agent-abc", [turn(600, written=50_000)])
        self.assertEqual(self.other()["requests"], 1)
        self.assertEqual(self.main()["requests"], 0)

    def test_sidechain_turns_in_a_session_are_the_other_bucket(self):
        self.write_session("s1", [turn(600, written=10_000)])
        self.write_session("s1", [turn(590, written=50_000)], sidechain=True)
        self.assertEqual(self.main()["requests"], 1)
        self.assertEqual(self.other()["requests"], 1)

    def test_two_inlined_agents_do_not_share_a_gap(self):
        # An older session file holds several subagents. Each has its own cache,
        # so the handover between them is not an idle gap either of them took.
        self.write_session("s1", [
            turn(1800, written=10_000, uuid="a1"),
            turn(1750, written=1_000, uuid="a2", parent="a1"),
            turn(600, written=10_000, uuid="b1"),
            turn(550, written=1_000, uuid="b2", parent="b1"),
        ], sidechain=True)
        stats = self.other()
        self.assertEqual(stats["conversations"], 2)
        self.assertEqual(stats["gap_count"], 0)

    def test_agents_are_grouped_through_the_lines_between_their_replies(self):
        # The shape a real transcript has: a reply's parent is the prompt or
        # tool result before it, and the agents share the turn that spawned them.
        self.write_session("s1", [line("spawn")])
        self.write_session("s1", [
            line("ua1", parent="spawn"),
            turn(1800, written=10_000, uuid="a1", parent="ua1"),
            line("ra1", parent="a1"),
            turn(1200, written=0, read=50_000, uuid="a2", parent="ra1"),
            line("ub1", parent="spawn"),
            turn(500, written=10_000, uuid="b1", parent="ub1"),
        ], sidechain=True)
        stats = self.other()
        # Two agents, not six singletons and not one merged run.
        self.assertEqual(stats["conversations"], 2)
        # The 10-minute gap inside agent A is real; the handover to B is not.
        self.assertEqual(stats["gap_count"], 1)
        self.assertEqual(stats["gap_tokens"], 50_000)

    def test_gaps_are_not_measured_across_two_conversations(self):
        # Two sessions 10 minutes apart hold separate caches, so neither turn
        # follows a gap. Counting across them would invent a saving.
        self.write_session("s1", [turn(1200, written=10_000)])
        self.write_session("s2", [turn(600, written=10_000, read=200_000)])
        self.assertEqual(self.main()["gap_count"], 0)

    def test_observed_split_reports_the_ttl_actually_granted(self):
        self.write_session("s1", [turn(600, written=80_000, at_1h=80_000, at_5m=0)])
        self.assertEqual(self.main()["observed_1h_share"], 1.0)

    def test_observed_split_is_a_ratio_not_a_constant(self):
        self.write_session("s1", [turn(600, written=30_000, at_1h=30_000, at_5m=0),
                                  turn(400, written=10_000, at_1h=0, at_5m=10_000)])
        self.assertAlmostEqual(self.main()["observed_1h_share"], 0.75, places=6)


class WindowTest(TTLFixture):
    def test_requests_outside_the_window_are_dropped(self):
        self.write_session("s1", [turn(60 * 60 * 24 * 30, written=999_000),
                                  turn(600, written=1_000)])
        stats = self.main(days=7)
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["write_tokens"], 1_000)

    def test_a_request_after_now_is_outside_the_window(self):
        # A frozen ``now`` has to measure the same window later, so traffic
        # written after it is dropped, not counted.
        self.write_session("s1", [turn(600, written=1_000), turn(-600, written=999_000)])
        stats = self.main(days=7)
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["write_tokens"], 1_000)

    def test_empty_window_yields_no_verdict(self):
        self.write_session("s1", [turn(60 * 60 * 24 * 30, written=999_000)])
        stats = self.main(days=1)
        self.assertEqual(stats["requests"], 0)
        self.assertIsNone(stats["verdict"])
        self.assertIn("nothing to compare", ttl.render(self.measure(days=1), settings={}))


class HostileShapeTest(TTLFixture):
    def test_malformed_lines_are_skipped_without_raising(self):
        path = os.path.join(self.project, "s1.jsonl")
        with open(path, "w") as handle:
            handle.write("not json at all\n")
            handle.write('{"usage": "a string where a dict belongs"}\n')
            handle.write(json.dumps({"type": "assistant", "message": {"usage": {}}}) + "\n")
            handle.write(json.dumps({"type": "user", "message": {"usage": {}}}) + "\n")
            handle.write(json.dumps({"type": "assistant", "timestamp": "not-a-date",
                                     "message": {"usage": {"cache_read_input_tokens": 1}}}) + "\n")
            handle.write(json.dumps({"type": "assistant", "timestamp": NOW.isoformat(),
                                     "message": "a string where a dict belongs"}) + "\n")
            handle.write(json.dumps(turn(600, written=5_000)) + "\n")
        stats = self.main()
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["write_tokens"], 5_000)

    def test_non_integer_token_counts_are_skipped(self):
        path = os.path.join(self.project, "s1.jsonl")
        with open(path, "w") as handle:
            handle.write(json.dumps({
                "type": "assistant", "timestamp": NOW.isoformat(),
                "message": {"usage": {"cache_read_input_tokens": "lots"}},
            }) + "\n")
        self.assertEqual(self.main()["requests"], 0)

    def test_negative_token_counts_are_skipped(self):
        self.write_session("s1", [turn(600, written=-5_000), turn(500, written=1_000)])
        stats = self.main()
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["write_tokens"], 1_000)

    def test_a_parent_cycle_does_not_hang(self):
        self.write_session("s1", [turn(600, written=1_000, uuid="a", parent="b"),
                                  turn(500, written=1_000, uuid="b", parent="a")],
                           sidechain=True)
        self.assertEqual(self.other()["requests"], 2)

    def test_missing_projects_dir_reports_rather_than_raises(self):
        report = ttl.measure(projects_dir=os.path.join(self.root, "absent"), days=7, now=NOW)
        self.assertEqual(report["buckets"][ttl.BUCKET_MAIN]["requests"], 0)
        self.assertIsInstance(ttl.render(report, settings={}), str)


class MalformedFieldTest(TTLFixture):
    """Every field is checked where it is read; a surprise must not reach a sum."""

    def raw(self, entry):
        path = os.path.join(self.project, "s1.jsonl")
        with open(path, "a") as handle:
            handle.write(json.dumps(entry) + "\n")

    def test_a_split_counter_that_is_not_a_number_reads_as_zero(self):
        entry = turn(600, written=10_000)
        entry["message"]["usage"]["cache_creation"] = {
            "ephemeral_1h_input_tokens": "lots",
            "ephemeral_5m_input_tokens": None,
        }
        self.raw(entry)
        stats = self.main()
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["write_tokens"], 10_000)
        self.assertEqual(stats["observed_1h_writes"], 0)
        self.assertEqual(stats["observed_5m_writes"], 0)
        self.assertIsNone(stats["observed_1h_share"])

    def test_a_negative_split_counter_reads_as_zero(self):
        self.raw(turn(600, written=10_000, at_1h=-5, at_5m=10_000))
        stats = self.main()
        self.assertEqual(stats["observed_1h_writes"], 0)
        self.assertEqual(stats["observed_5m_writes"], 10_000)

    def test_an_unhashable_uuid_does_not_break_grouping(self):
        entry = turn(600, written=10_000)
        entry["uuid"] = ["not", "hashable"]
        entry["isSidechain"] = True
        self.raw(entry)
        stats = self.other()
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["conversations"], 1)

    def test_a_non_string_parent_link_does_not_break_grouping(self):
        entry = turn(600, written=10_000)
        entry["uuid"] = "a1"
        entry["parentUuid"] = ["not", "hashable"]
        entry["isSidechain"] = True
        self.raw(entry)
        stats = self.other()
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["conversations"], 1)

    def test_a_boolean_priced_count_drops_the_reply(self):
        entry = turn(600, written=10_000)
        entry["message"]["usage"]["cache_creation_input_tokens"] = True
        self.raw(entry)
        self.assertEqual(self.main()["requests"], 0)

    def test_every_field_survives_every_wrong_type(self):
        # The eye misses one of these; the loop does not. Each field in turn is
        # replaced by each hostile value, and the run must not raise.
        hostile = [None, True, -1, "text", [], {}, 1.5, ["not", "hashable"]]
        control = turn(600, written=10_000, uuid="a1", parent="a0")
        control["isSidechain"] = True
        self.raw(control)
        # Control: the fixture is read at all, so "did not raise" below means
        # the hostile values were seen.
        self.assertEqual(self.other()["requests"], 1)
        shutil.rmtree(self.project)
        os.makedirs(self.project)

        fields = [
            ("entry", "uuid"), ("entry", "parentUuid"), ("entry", "type"),
            ("entry", "timestamp"), ("entry", "isSidechain"),
            ("message", "id"), ("message", "model"), ("message", "usage"),
            ("usage", "cache_read_input_tokens"),
            ("usage", "cache_creation_input_tokens"),
            ("usage", "cache_creation"),
            ("split", "ephemeral_1h_input_tokens"),
            ("split", "ephemeral_5m_input_tokens"),
        ]
        for where, field in fields:
            for value in hostile:
                with self.subTest(field=field, value=value):
                    shutil.rmtree(self.project)
                    os.makedirs(self.project)
                    entry = turn(600, written=10_000, uuid="a1", parent="a0")
                    entry["isSidechain"] = True
                    target = {
                        "entry": entry,
                        "message": entry["message"],
                        "usage": entry["message"]["usage"],
                        "split": entry["message"]["usage"]["cache_creation"],
                    }[where]
                    target[field] = value
                    self.raw(entry)
                    # measure() takes self.root because conversations() globs one
                    # level below it.
                    self.measure()

    def test_a_boolean_is_not_accepted_as_a_count(self):
        entry = turn(600, written=10_000)
        entry["message"]["usage"]["cache_creation"]["ephemeral_1h_input_tokens"] = True
        self.raw(entry)
        self.assertEqual(self.main()["observed_1h_writes"], 0)


class PricingTest(unittest.TestCase):
    def test_fable_reads_cheaper_than_the_default(self):
        self.assertEqual(ttl.read_multiplier("claude-fable-5-1"), 0.025)
        self.assertEqual(ttl.read_multiplier("claude-opus-5"), ttl.READ_DEFAULT)
        self.assertEqual(ttl.read_multiplier(None), ttl.READ_DEFAULT)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "settings.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, data):
        with open(self.path, "w") as handle:
            handle.write(data if isinstance(data, str) else json.dumps(data))

    def test_unset_names_the_file_it_was_looked_for_in(self):
        self.write({})
        value, source = ttl.current_settings(self.path, environ={})[ttl.BUCKET_MAIN]
        self.assertIsNone(value)
        # Bare provenance, like every other branch: the renderers word the
        # sentence, so it is written once per renderer and never twice in one.
        self.assertEqual(source, self.path)

    def test_settings_file_value_is_read_and_its_path_named(self):
        self.write({"promptCacheTtl": "1h", "subagentPromptCacheTtl": "5m"})
        found = ttl.current_settings(self.path, environ={})
        self.assertEqual(found[ttl.BUCKET_MAIN][0], "1h")
        self.assertEqual(found[ttl.BUCKET_MAIN][1], self.path)
        self.assertEqual(found[ttl.BUCKET_OTHER][0], "5m")

    def test_environment_variable_outranks_the_settings_file(self):
        self.write({"promptCacheTtl": "1h"})
        env = {"CLAUDE_CODE_PROMPT_CACHE_TTL": "5m"}
        value, source = ttl.current_settings(self.path, environ=env)[ttl.BUCKET_MAIN]
        self.assertEqual(value, "5m")
        self.assertIn("CLAUDE_CODE_PROMPT_CACHE_TTL", source)

    def test_enable_1h_is_outranked_by_the_settings_file(self):
        self.write({"promptCacheTtl": "5m"})
        env = {"ENABLE_PROMPT_CACHING_1H": "1"}
        self.assertEqual(ttl.current_settings(self.path, environ=env)[ttl.BUCKET_MAIN][0], "5m")

    def test_enable_1h_applies_when_nothing_outranks_it(self):
        self.write({})
        env = {"ENABLE_PROMPT_CACHING_1H": "1"}
        self.assertEqual(ttl.current_settings(self.path, environ=env)[ttl.BUCKET_MAIN][0], "1h")

    def test_force_5m_outranks_everything(self):
        self.write({"promptCacheTtl": "1h"})
        env = {"CLAUDE_CODE_PROMPT_CACHE_TTL": "1h", "FORCE_PROMPT_CACHING_5M": "1"}
        for bucket in (ttl.BUCKET_MAIN, ttl.BUCKET_OTHER):
            self.assertEqual(ttl.current_settings(self.path, environ=env)[bucket][0], "5m")

    def test_unparseable_settings_file_is_treated_as_unset(self):
        self.write("{ this is not json")
        value, _ = ttl.current_settings(self.path, environ={})[ttl.BUCKET_MAIN]
        self.assertIsNone(value)

    def test_invalid_value_is_ignored(self):
        self.write({"promptCacheTtl": "30m"})
        value, _ = ttl.current_settings(self.path, environ={})[ttl.BUCKET_MAIN]
        self.assertIsNone(value)


class RenderTest(TTLFixture):
    def test_report_names_the_setting_and_the_verdict(self):
        self.write_session("s1", [turn(600, written=100_000), turn(540, written=100_000)])
        text = ttl.render(self.measure(), settings={})
        self.assertIn("promptCacheTtl", text)
        self.assertIn("subagentPromptCacheTtl", text)
        self.assertIn("USE 5m", text)

    def test_report_renders_the_one_hour_verdict_too(self):
        self.write_session("s1", [turn(1200, written=1_000),
                                  turn(600, written=0, read=200_000)])
        text = ttl.render(self.measure(), settings={})
        self.assertIn("USE 1h", text)
        self.assertIn("write volume", text)

    def test_report_flags_a_setting_that_disagrees_with_the_verdict(self):
        self.write_session("s1", [turn(600, written=100_000), turn(540, written=100_000)])
        settings = {ttl.BUCKET_MAIN: ("1h", "settings.json"),
                    ttl.BUCKET_OTHER: ("5m", "settings.json")}
        self.assertIn("differs from your current setting", ttl.render(self.measure(), settings))

    def test_report_reconciles_a_setting_the_window_predates(self):
        self.write_session("s1", [turn(600, written=80_000, at_1h=80_000, at_5m=0)])
        settings = {ttl.BUCKET_MAIN: ("5m", "settings.json"),
                    ttl.BUCKET_OTHER: (None, "unset")}
        self.assertIn("predates that setting", ttl.render(self.measure(), settings))

    def test_report_says_how_much_of_the_gap_prefix_was_estimated(self):
        self.write_session("s1", [turn(1600, written=5_000),
                                  turn(1500, written=2_000, read=40_000),
                                  turn(800, written=120_000, read=0)])
        text = ttl.render(self.measure(), settings={})
        self.assertIn("had to be estimated", text)
        self.assertNotIn("no sample to go on", text)

    def test_report_says_when_a_gap_had_no_sample_to_go_on(self):
        self.write_session("s1", [turn(1600, written=5_000),
                                  turn(800, written=120_000, read=0)])
        text = ttl.render(self.measure(), settings={})
        self.assertIn("had to be estimated", text)
        self.assertIn("no sample to go on", text)

    def test_report_stays_silent_when_nothing_was_estimated(self):
        self.write_session("s1", [turn(1200, written=5_000),
                                  turn(600, written=1_000, read=40_000)])
        self.assertNotIn("had to be estimated", ttl.render(self.measure(), settings={}))

    def test_json_output_is_serialisable_and_hides_internals(self):
        self.write_session("s1", [turn(600, written=100_000)])
        payload = ttl._jsonable(self.measure())
        json.dumps(payload)
        bucket = payload["buckets"][ttl.BUCKET_MAIN]
        self.assertIn("write_tokens", bucket)
        self.assertNotIn("read_weight_base", bucket)
        self.assertNotIn("read_weighted", bucket)


class HtmlReportTest(TTLFixture):
    """The page has to open from a file:// path on a machine with no network."""

    def page(self, days=14, settings=None):
        return ttl_report.render_html(
            self.measure(days), settings if settings is not None else {}
        )

    def test_the_page_is_a_whole_document_with_a_charset(self):
        # A fragment served from file:// has no Content-Type, so the browser
        # falls back to Latin-1 and every dash renders as mojibake.
        self.write_session("s1", [turn(600, written=10_000)])
        page = self.page()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn('<meta charset="utf-8">', page)
        self.assertIn("</html>", page)

    def test_the_page_references_nothing_outside_itself(self):
        self.write_session("s1", [turn(600, written=10_000)])
        page = self.page()
        for reference in ('src="http', "href=\"http", "@import", 'src="./', "url(http"):
            self.assertNotIn(reference, page)
        self.assertNotIn("<script", page)

    def test_the_page_carries_the_same_figures_as_the_terminal(self):
        self.write_session("s1", [turn(1200, written=23_000),
                                  turn(600, written=0, read=15_000)])
        stats = self.main()
        page = self.page()
        # 23,000 written and 15,000 rescued, both rendered in millions.
        self.assertIn(ttl_report._millions(stats["write_tokens"]), page)
        self.assertIn(ttl_report._millions(stats["gap_tokens"]), page)
        self.assertIn(ttl_report._millions(stats["premium_1h"]), page)
        self.assertIn(ttl_report._millions(stats["penalty_5m"]), page)

    def test_an_empty_bucket_renders_a_placeholder_rather_than_vanishing(self):
        # Every fixture turn is a main-conversation turn, so the other bucket is
        # empty. A dropped section reads as a bug; a stated one reads as quiet.
        self.write_session("s1", [turn(600, written=10_000)])
        page = self.page()
        self.assertIn("No requests in this window", page)
        self.assertIn("Everything else", page)

    def test_the_page_says_when_the_setting_disagrees_with_the_verdict(self):
        self.write_session("s1", [turn(600, written=100_000)])
        settings = {ttl.BUCKET_MAIN: ("1h", "/fake/settings.json"),
                    ttl.BUCKET_OTHER: ("5m", "/fake/settings.json")}
        page = self.page(settings=settings)
        self.assertIn("points the other way", page)

    def test_the_page_says_when_the_setting_already_agrees(self):
        self.write_session("s1", [turn(600, written=100_000)])
        settings = {ttl.BUCKET_MAIN: ("5m", "/fake/settings.json"),
                    ttl.BUCKET_OTHER: ("5m", "/fake/settings.json")}
        self.assertIn("which agrees", self.page(settings=settings))

    def test_an_unset_bucket_names_the_file_it_is_missing_from(self):
        self.write_session("s1", [turn(600, written=100_000)])
        settings = {ttl.BUCKET_MAIN: (None, "/fake/settings.json"),
                    ttl.BUCKET_OTHER: (None, "/fake/settings.json")}
        page = self.page(settings=settings)
        self.assertIn("Not set in /fake/settings.json, so", page)

    def test_the_page_reads_correctly_on_what_current_settings_really_returns(self):
        # The fixtures above inject bare paths; this one goes through the real
        # settings reader, so the sentence is checked as it ships.
        self.write_session("s1", [turn(600, written=100_000)])
        empty = os.path.join(self.root, "settings.json")
        with open(empty, "w") as handle:
            handle.write("{}")
        page = ttl_report.render_html(
            self.measure(), ttl.current_settings(empty, environ={})
        )
        self.assertIn("Not set in %s, so" % empty, page)
        self.assertNotIn("not in", page)

    def test_the_ledger_states_the_read_rate_its_multiplier_comes_from(self):
        # The rates section explains 1.15 at the published 0.10 read rate; the
        # ledger uses the bucket's own blend, so it says which rate it used.
        self.write_session("s1", [turn(1200, written=23_000, model="claude-fable-5-1"),
                                  turn(600, written=0, read=15_000, model="claude-fable-5-1")])
        page = self.page()
        self.assertIn("less your 0.025 read rate", page)
        self.assertIn("&times; 1.23", page)

    def test_a_near_tie_prints_a_flip_factor_that_is_not_one(self):
        # 62 rescued per 100 written is a 1.05x flip; printed to one decimal it
        # read as 1.0x, which says "already at break-even" to a reader.
        self.write_session("s1", [turn(1200, written=100_000),
                                  turn(600, written=0, read=62_000)])
        self.assertIn("1.05&times;", self.page())
        self.assertIn("1.05x", ttl.render(self.measure(), settings={}))

    def test_a_settings_value_is_escaped_into_the_page(self):
        # The value is read off disk, so it is not ours to trust as markup.
        self.write_session("s1", [turn(600, written=100_000)])
        settings = {ttl.BUCKET_MAIN: ("<script>x</script>", "/fake/s.json"),
                    ttl.BUCKET_OTHER: ("5m", "/fake/s.json")}
        page = self.page(settings=settings)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def run_cli(self, argv):
        """Run the CLI with its own output captured, and return the exit code."""
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                return ttl.main(argv)

    def run_cli_capturing(self, argv):
        """Run the CLI and return ``(exit code, stdout, stderr)``."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out):
            with contextlib.redirect_stderr(err):
                code = ttl.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_the_flag_writes_the_file_and_names_it(self):
        self.write_session("s1", [turn(600, written=10_000)])
        target = os.path.join(self.root, "out.html")
        self.assertEqual(
            self.run_cli(["--projects-dir", self.root, "--html", target]), 0
        )
        self.assertTrue(os.path.exists(target))
        with open(target) as handle:
            self.assertTrue(handle.read().startswith("<!doctype html>"))

    def test_a_run_without_the_flag_writes_nothing(self):
        self.write_session("s1", [turn(600, written=10_000)])
        before = sorted(os.listdir(self.root))
        self.assertEqual(self.run_cli(["--projects-dir", self.root]), 0)
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_an_unwritable_path_fails_loudly_instead_of_pretending(self):
        self.write_session("s1", [turn(600, written=10_000)])
        target = os.path.join(self.root, "no-such-dir", "out.html")
        self.assertEqual(
            self.run_cli(["--projects-dir", self.root, "--html", target]), 1
        )

    def test_quiet_with_html_writes_the_page_and_prints_no_report(self):
        # The three output choices are: report (no flags), report and page
        # (--html), page alone (--html --quiet). This is the third.
        self.write_session("s1", [turn(600, written=100_000)])
        target = os.path.join(self.root, "out.html")
        code, out, _ = self.run_cli_capturing(
            ["--projects-dir", self.root, "--html", target, "--quiet"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(target))
        self.assertNotIn("MAIN CONVERSATION", out)
        # The path still prints, or the caller cannot find what they asked for.
        self.assertIn(os.path.abspath(target), out)

    def test_without_quiet_the_report_is_printed_alongside_the_page(self):
        self.write_session("s1", [turn(600, written=100_000)])
        target = os.path.join(self.root, "out.html")
        code, out, _ = self.run_cli_capturing(
            ["--projects-dir", self.root, "--html", target]
        )
        self.assertEqual(code, 0)
        self.assertIn("MAIN CONVERSATION", out)
        self.assertIn(os.path.abspath(target), out)

    def test_quiet_alone_prints_nothing_and_still_writes_nothing(self):
        self.write_session("s1", [turn(600, written=100_000)])
        before = sorted(os.listdir(self.root))
        code, out, _ = self.run_cli_capturing(["--projects-dir", self.root, "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_json_with_html_keeps_stdout_one_parseable_document(self):
        self.write_session("s1", [turn(600, written=100_000)])
        target = os.path.join(self.root, "out.html")
        code, out, err = self.run_cli_capturing(
            ["--projects-dir", self.root, "--json", "--html", target]
        )
        self.assertEqual(code, 0)
        self.assertIn("buckets", json.loads(out))
        self.assertIn(os.path.abspath(target), err)
        self.assertTrue(os.path.exists(target))

    def test_quiet_with_json_is_refused_rather_than_silently_resolved(self):
        self.write_session("s1", [turn(600, written=100_000)])
        target = os.path.join(self.root, "out.html")
        code, out, err = self.run_cli_capturing(
            ["--projects-dir", self.root, "--json", "--quiet", "--html", target]
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("opposite", err)
        # A refused run does its work nowhere: no page either.
        self.assertFalse(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
