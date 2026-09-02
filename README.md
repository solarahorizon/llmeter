# llmeter

An ambient **usage meter for AI coding CLIs**. It shows — right under your prompt — which model you're on, how full your context window is, which prompt-cache TTL bucket (5-minute or 1-hour) the session is currently writing to and how many tokens it holds, and **how much of your session and weekly limits you've burned**:

```
Opus 4.8 (1M context) · ctx 30% (295k/1M) · cache 5m:220k · 5h 22% (resets 14:30) · wk 37% (resets Tue 10:00)
```

`5h` is the rolling 5-hour session window (the one you hit mid-afternoon) and `wk` the weekly plan. The 5-hour reset shows the clock time alone when it lands today, and keeps its weekday when it doesn't — a window opened in the evening resets after midnight, so a bare `01:00` would read as a time that has already gone.

It costs **zero tokens, zero network, ~20 ms** per message, and it quietly saves the real usage numbers to disk so you can chart them later.

> **v1 supports Claude Code.** Codex, Antigravity, and DeepSeek adapters are on the [roadmap](docs/ROADMAP.md) — the code is built around a per-tool adapter seam so they slot in without a rewrite.

---

## Why it exists

Claude Code already knows your real cap percentages — both the 5-hour session window and the weekly one, the same numbers the `/usage` panel shows — and it hands them to your **status line** on every message. But if you don't have a status line configured, they're **discarded**: not in the transcripts, not on disk, nowhere. The only way to see "am I about to hit a wall?" is to stop and open `/usage`.

llmeter stands in that status-line slot: it prints the numbers so they're always in view, and **tees them to disk** before they evaporate — turning a display-only blob into data you can actually track.

## Install

```bash
git clone https://github.com/solarahorizon/llmeter.git
cd llmeter
./install.sh
```

That's it. `install.sh` adds one key to `~/.claude/settings.json` (backing it up first, and validating the result), and Claude Code hot-reloads it. Send a message and the status line appears under your prompt.

- **Idempotent** — safe to re-run.
- **Non-destructive** — backs up your settings before writing, refuses to touch a settings file it can't parse, and notes any prior `statusLine` command it replaces (which stays in the backup).
- **No hard-coded paths** — the wrapper locates itself, so the repo works wherever you cloned it (spaces in the path are fine).

## Verify it's working

```bash
cat ~/.claude/llmeter/usage-snapshot.json
```

You should see a recent snapshot with `caps` populated.

- The `5h N%` and `wk N%` fields appear only **after the first API response of a session**, and only for **Pro/Max** accounts (that's when Claude Code includes the rate-limit data). A brand-new window shows the account-level caps from the freshest capture any window made.
- The `cache 5m:Nk` / `1h:Nk` field shows whichever ephemeral TTL bucket the most recent assistant message wrote to, with that bucket's cumulative `cache_creation_input_tokens` for the session, summed straight from the session's own transcript — not a cap, just what this session has written into the prompt cache so far. It disappears entirely until the first cache write is seen (or the transcript can't be read).

## How it works

```
every message  ·  + every 60s
        │
        ▼
  Claude Code  ──JSON on stdin──►  llmeter-statusline.sh ──► python3 -m llmeter.statusline
        ▲                                                        │ prints one line   │ writes
        └──────────── redraws the line ◄─────────────────────────┘                   ▼
                                                        ~/.claude/llmeter/
                                                          usage-snapshot.json   (latest, atomic)
                                                          usage-history.jsonl   (one line per change)
```

- **Private & local.** Everything happens inside the Claude Code process on your machine. No network, no credentials, no telemetry. The files under `~/.claude/llmeter/` never leave your disk. For the `cache` field, llmeter reads the session's own transcript file (`transcript_path` in the stdin payload) locally, and keeps one small resume file per transcript (`~/.claude/llmeter/cache-ttl-<hash>.json`) holding only five fields — the running 5m/1h totals, a byte offset into the transcript, the last message id seen, and which bucket is currently active — so each render sums just the lines appended since the last one, not the whole transcript again. Nothing else from the transcript is read, kept, or written to disk.
- **Fail-soft.** If Claude Code ever changes the payload shape, llmeter still prints a line and never breaks your prompt (locked by the test suite).
- **Multi-window safe.** Run many Claude Code panes at once — the snapshot write is atomic and both caps are account-level, so they cooperate rather than collide.

Terminal-independent: works identically under Terminal.app, iTerm2, tmux, VS Code's terminal, or SSH — the terminal is not on the data path.

## Data files

| File | What |
|---|---|
| `~/.claude/llmeter/usage-snapshot.json` | Latest capture (atomic overwrite) — model, context %, cap windows. |
| `~/.claude/llmeter/usage-snapshot.<host>.json` | Same, for a session routed at a non-Anthropic endpoint. One file per provider. |
| `~/.claude/llmeter/usage-history.jsonl` | Append-only log, one line whenever a cap % changes — chart your week. |
| `~/.claude/llmeter/usage-history.<host>.jsonl` | Same, per non-Anthropic endpoint. Kept apart so a tool reading the default log never counts another account's rows. |

Override the location with `LLMETER_DIR`.

### Sessions routed elsewhere

If a project points Claude Code at another endpoint with `ANTHROPIC_BASE_URL`
(an LLM gateway, or a vendor serving an Anthropic-compatible API), that session
is spending a **different account's** quota. llmeter keeps each provider's
usage in its own file and never lets one stand in for another.

The visible effect: such a session shows **`model · ctx% · $spend`** rather than
`5h` and `wk`. A cap borrowed from your Anthropic account and displayed under a
third-party model's name is a confident wrong number, and this tool exists to
show you the real one.

**There is no cap % of either kind available for a vendor session, and it is
not a gap llmeter can close.** Measured 2026-08-10 against Claude Code 2.1.226:

- 58 consecutive status-line payloads captured from live Kimi and Qwen sessions
  carried **no `rate_limits` key at all**. Anthropic sessions carry it.
- The vendor returned **no rate-limit response headers**.
- **No usage endpoint** answered on the vendor's API (six candidate paths tried).
- Claude Code's own `/usage` panel calls `GET /api/oauth/usage`, an OAuth call
  scoped to the **Anthropic subscription**. It ignores `ANTHROPIC_BASE_URL`.

That last point is the one that misleads: if you read `/usage` during a Kimi or
Qwen session and think the plan figures are that vendor's, they are not. They
are your Claude plan's.

So llmeter shows the usage signal that *is* real for a metered session — the
running spend Claude Code reports in the payload. On the default provider,
where the cap percentages are the meaningful numbers and a dollar figure would
be noise, no spend is shown.

#### Working examples

Per-project, in that project's `.claude/settings.local.json`. `apiKeyHelper`
names an executable and uses whatever it prints, so the key lives in a
`chmod 700` script outside the repo instead of in a tracked file.

Alibaba Qwen:

```json
{
  "apiKeyHelper": "/Users/you/.claude/qwen-key.sh",
  "env": {
    "ANTHROPIC_BASE_URL": "https://token-plan.<region>.maas.aliyuncs.com/apps/anthropic",
    "ANTHROPIC_MODEL": "qwen3.8-max"
  }
}
```

Moonshot Kimi:

```json
{
  "apiKeyHelper": "/Users/you/.claude/kimi-key.sh",
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.kimi.com/coding",
    "ANTHROPIC_MODEL": "kimi-k3"
  }
}
```

Three things that trip people up:

1. **The endpoint has to match the key type.** Moonshot sells two products with
   two key types and two endpoints, and swapping them returns `401 Invalid
   Authentication` — which reads like a bad key rather than a wrong URL. A
   **Kimi for Coding** subscription key works only against
   `https://api.kimi.com/coding`; a **pay-as-you-go platform** key (from
   `platform.moonshot.ai`) works only against
   `https://api.moonshot.ai/anthropic`. The official Claude Code guide documents
   the pay-as-you-go path only, so a subscription key following it fails.
2. **Set the tier variables too.** With only `ANTHROPIC_MODEL` set, background
   work (title generation, summarization) and subagents still request Claude
   model names the vendor doesn't recognise, and fail quietly. Set
   `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`,
   `ANTHROPIC_DEFAULT_HAIKU_MODEL` and `CLAUDE_CODE_SUBAGENT_MODEL` to the same
   model.
3. **`settings.local.json` is read at startup.** Restart Claude Code in that
   project after editing it.

#### Context window for custom models

Claude Code only knows the window of models in its own table and falls back to
**200k** for everything else, so a 1M-context model reads about 5x too full.
llmeter substitutes the real window and recomputes the percentage. Built in:
`qwen3.8-max`, `kimi-k3`, `kimi-k2.7-code`.

Add your own without editing the source:

```bash
export LLMETER_CONTEXT_WINDOWS="my-model=1048576,other-model=262144"
```

Malformed entries are ignored rather than breaking the status line.

## Uninstall

```bash
./uninstall.sh            # remove llmeter's statusLine key (backs up first)
./uninstall.sh --purge    # also delete ~/.claude/llmeter/
```

It only removes the `statusLine` key if it points at llmeter — a status line you set to something else is left alone. A full-file backup is written before any change.

## Requirements

- **Claude Code** (v1). The `5h %` and `wk %` figures need a **Pro/Max** subscription.
- **python3** and **zsh** — both ship with macOS (on Linux, install zsh; the command uses `/usr/bin/env zsh`). No pip installs, no dependencies (stdlib only).

## Roadmap — every AI CLI

The killer feature (real cap % in your status line) generalizes: any agentic CLI computes an ephemeral usage signal to render it, then throws it away. llmeter's move — stand in the render slot and tee the signal to disk — is vendor-agnostic. Planned adapters: **Codex**, **Google Antigravity**, **DeepSeek**. See [docs/ROADMAP.md](docs/ROADMAP.md) for the per-tool mechanics.

## Development

```bash
python3 -m unittest discover -s tests
```

Zero dependencies. New adapters go in `llmeter/adapters/<tool>.py` and return the normalized `Reading` documented in [`llmeter/core.py`](llmeter/core.py).

## License

[MIT](LICENSE).
