# llmeter backlog

Living to-do list. `[ ]` todo · `[~]` in progress · `[x]` done · `[?]` needs Lynn's decision.
Adapter work per CLI is tracked in `docs/ROADMAP.md`; this file holds defects and fixes.

## Defects

- [ ] **An idle pane renders its own stale caps after another pane has landed a reset
  (`render_prefers_stale_caps_after_reset`).** `format_line` prefers the reading's own `rate_limits`
  over the snapshot's. After the weekly reset (88 → 8) a pane that has had no API response since
  still reads `wk 88%` while the file and the returned snapshot hold 8, so two panes disagree and one
  is stale. Found by the codex vendor leg on `fix/cap-reset-freshness` (2026-09-02); the behaviour is
  on `main` and that branch does not touch the render path. Candidate fix: a republish that
  `write_snapshot` filtered as a fingerprint match is positive evidence that this reading's caps are
  a replay, so render from the returned snapshot's caps in that case only. Needs a test with two
  sessions, one reset, one idle. · source: session 2026-09-02 · small

- [x] **A cap reset inside a window is discarded, so the snapshot and history hold the old
  percentage until the window's reset time changes.** Seen 2026-09-02: Anthropic reset the weekly
  allowance in the morning (a launch promotion; `/usage` read 8% at 11:12 AEST, reset time unchanged
  at Sat 04:00), the status line showed the live 8%, but `usage-snapshot.json` and every
  `usage-history.jsonl` row from 00:23 onward held 88%. The day's weekly series is unrecoverable; the
  only readings are three taken by eye (2% about 09:10, 6% about 10:00, 8% at 11:12).

  **Cause.** `_merge_caps` in `llmeter/core.py`: for the same `resets_at`, the stored and incoming
  percentages are merged by max, because an idle session re-publishes its last-known caps on every
  refresh and last-writer-wins made the meter flap (69→82→69 within a minute, 2026-07-06). A reset
  arrives as a lower value with the same `resets_at`, which is exactly the shape the max rule exists
  to reject. The display is unaffected because `format_line` prefers the reading's own live caps and
  only falls back to the snapshot.

  **Why a value-based rule cannot fix it.** A stale republish (82 then 58) and a reset (88 then 8)
  are the same shape: a lower number in the same window. "A value never seen in this window" fails on
  real data, because the week climbs through 8% on its way up, so 8% was seen days earlier. "Confirmed
  by later readings" fails in the other direction: after a genuine reset, an idle session republishing
  its stale 88 would re-poison the max, so the ambiguity is symmetric and no threshold or persistence
  rule on values alone separates the two.

  **Fix: decide by freshness of the reading, not by its value.** An idle session's statusline payload
  is byte-identical between refreshes (same `context_tokens`, same `rate_limits`); a session that just
  received an API response has a changed payload. So:
  1. Keep a per-session fingerprint in the snapshot: `sessions: {session_id: {fp, at}}` where `fp`
     hashes `(context_tokens, caps)`. Prune entries older than 24h on each write. `_SNAPSHOT_FIELDS`
     is an allowlist, so `sessions` is added there deliberately and holds nothing from the raw payload
     but a hash and a timestamp.
  2. In `write_snapshot`: if the reading's fingerprint equals the stored one for its session, it is a
     republish. Skip the merge and the history append; return the stored snapshot.
  3. A fresh reading's caps replace the stored caps outright for the same or a newer `resets_at`
     (an older `resets_at` still loses, as today). The max rule goes; the reset case and the
     ordinary climb are then the same path.
  4. History appends on any change, as now, so a reset logs as its own row.

  **What this does and does not guarantee.** The flap is bounded to one occurrence per idle session
  for as long as that session keeps refreshing: its first write has no stored fingerprint and is
  accepted once, then every identical republish is ignored, including a stale high value after a
  reset, which the max rule can never reject. A republish renews the entry's timestamp, so the 24 h
  prune forgets a session that has stopped refreshing, never a quiet one; a session silent for 24 h
  and then back is accepted once more. A session whose payload changes but whose `rate_limits` are stale (Claude
  Code only refreshes them on an API response, so this should not happen) would still be accepted.
  The fingerprint must cover every persisted field a session can change by itself, so it is derived
  from `_SNAPSHOT_FIELDS` rather than listed by hand: a hand list missed `cost` (a pay-per-token
  session's spend froze on disk), then `model`/`context_window_size` (a `/model` switch) and
  `context_pct`, one per review round. And `captured_at` no longer advances on a republish, so a snapshot
  every session has left idle for 6 h expires from `read_snapshot` until one of them gets an API
  response: a 7-hour-old value is exactly what today's record shows to be untrustworthy.

  **Tests to add or change** in `tests/test_llmeter.py`, `MergeTests`: (a) same window, fresh lower
  reading is accepted and logged (the reset); (b) identical republish of a lower value from a second
  session is ignored and logs nothing (rewrite of `test_same_window_keeps_max`, which currently
  asserts the defect); (c) identical republish of a stale higher value after a reset is ignored; (d)
  `test_new_window_wins_even_if_lower` unchanged; (e) `sessions` prunes entries older than 24h; (f)
  a fingerprint reaches disk as a hash only.

  Sizing: `core.py` merge and write paths, about 30 lines net; one reading of `adapters/claude_code.py`
  to confirm `session_id` and `context_tokens` are always on the Reading (they are today).

  Fixed 2026-09-02 on fix/cap-reset-freshness: fingerprint per session over every `_SNAPSHOT_FIELDS`
  value, replace-on-fresh; tests (a)–(g) plus (h) a cost-only change is fresh and (i) every persisted
  field moving alone is fresh.
