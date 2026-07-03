# llmeter

An ambient **usage meter for AI coding CLIs**. It shows — right under your prompt — which model you're on, how full your context window is, and **how much of your weekly plan you've burned**:

```
Opus 4.8 (1M context) · ctx 30% · wk 37% (resets Tue 10:00)
```

It costs **zero tokens, zero network, ~20 ms** per message, and it quietly saves the real usage numbers to disk so you can chart them later.

> **v1 supports Claude Code.** Codex, Antigravity, and DeepSeek adapters are on the [roadmap](docs/ROADMAP.md) — the code is built around a per-tool adapter seam so they slot in without a rewrite.

---

## Why it exists

Claude Code already knows your real weekly-cap % — the same number the `/usage` panel shows — and it hands that number to your **status line** on every message. But if you don't have a status line configured, it's **discarded**: it's not in the transcripts, not on disk, nowhere. The only way to see "am I about to hit my weekly wall?" is to stop and open `/usage`.

llmeter stands in that status-line slot: it prints the number so it's always in view, and **tees it to disk** before it evaporates — turning a display-only blob into data you can actually track.

## Install

```bash
git clone https://github.com/solarahorizon/llmeter.git
cd llmeter
./install.sh
```

That's it. `install.sh` adds one key to `~/.claude/settings.json` (backing it up first, and validating the result), and Claude Code hot-reloads it. Send a message and the status line appears under your prompt.

- **Idempotent** — safe to re-run.
- **Non-destructive** — backs up your settings, refuses to touch a settings file it can't parse, and only ever adds/removes its own `statusLine` key.
- **No hard-coded paths** — the wrapper locates itself, so the repo works wherever you cloned it.

## Verify it's working

```bash
cat ~/.claude/llmeter/usage-snapshot.json
```

You should see a recent snapshot with `caps` populated.

- The `wk N%` field appears only **after the first API response of a session**, and only for **Pro/Max** accounts (that's when Claude Code includes the rate-limit data). A brand-new window shows the account-level cap from the freshest capture any window made.

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

- **Private & local.** Everything happens inside the Claude Code process on your machine. No network, no credentials, no telemetry. The two files under `~/.claude/llmeter/` never leave your disk.
- **Fail-soft.** If Claude Code ever changes the payload shape, llmeter still prints a line and never breaks your prompt (locked by the test suite).
- **Multi-window safe.** Run many Claude Code panes at once — the snapshot write is atomic and the weekly cap is account-level, so they cooperate rather than collide.

Terminal-independent: works identically under Terminal.app, iTerm2, tmux, VS Code's terminal, or SSH — the terminal is not on the data path.

## Data files

| File | What |
|---|---|
| `~/.claude/llmeter/usage-snapshot.json` | Latest capture (atomic overwrite) — model, context %, cap windows. |
| `~/.claude/llmeter/usage-history.jsonl` | Append-only log, one line whenever a cap % changes — chart your week. |

Override the location with `LLMETER_DIR`.

## Uninstall

```bash
./uninstall.sh            # remove the status line (restores from a backup key)
./uninstall.sh --purge    # also delete ~/.claude/llmeter/
```

It only removes the `statusLine` key if it points at llmeter — a status line you set to something else is left alone.

## Requirements

- **Claude Code** (v1). `wk %` needs a **Pro/Max** subscription.
- **python3** and **zsh** — both ship with macOS. No pip installs, no dependencies (stdlib only).

## Roadmap — every AI CLI

The killer feature (real cap % in your status line) generalizes: any agentic CLI computes an ephemeral usage signal to render it, then throws it away. llmeter's move — stand in the render slot and tee the signal to disk — is vendor-agnostic. Planned adapters: **Codex**, **Google Antigravity**, **DeepSeek**. See [docs/ROADMAP.md](docs/ROADMAP.md) for the per-tool mechanics.

## Development

```bash
python3 -m unittest discover -s tests
```

Zero dependencies. New adapters go in `llmeter/adapters/<tool>.py` and return the normalized `Reading` documented in [`llmeter/core.py`](llmeter/core.py).

## License

[MIT](LICENSE).
