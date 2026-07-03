# llmeter roadmap — every AI CLI

v1 ships **one** adapter, Claude Code, because it is the only tool that
*pushes* its usage signal to you (a status-line command fed a JSON payload
that includes the real rate-limit numbers). Every other tool is *pull* — you
have to read a log, run a status command, or call an API. This doc records
the mechanics per tool so the adapters can be built on evidence, not guesses.

## The architecture (already in place)

```
host tool payload ──► adapters/<tool>.py::parse() ──► normalized Reading ──► core (persist + render)
```

A **Reading** (defined in `llmeter/core.py`) is vendor-neutral:

```
{ source, model, context_pct, caps: {window: {used_percentage, resets_at}}, cost: {session_usd, tokens}, session_id }
```

- **Cap-metered tools** (Claude Code, Codex, Antigravity) fill `caps`.
- **Pay-per-token tools** (DeepSeek) fill `cost` and leave `caps` empty.

Adding a tool = one new `adapters/<tool>.py` returning a Reading, plus a way
to feed it the tool's payload. The core (persistence, cross-window fallback,
rendering, fail-soft) is shared and already done.

## Two axes that shape every adapter

1. **Data shape** — a *cap %* ("% of allowance used, resets at T") vs *cost*
   ("tokens + $ this session"). llmeter renders either.
2. **Collection** — *push* (the tool hands you the data, like Claude Code's
   status line) vs *pull* (you tail a log / run a command / call an API).

## Per-tool plan (as of July 2026 — re-verify before building)

| Tool | Cap %? | Where the signal surfaces | Collection | Status |
|---|---|---|---|---|
| **Claude Code** | Yes (5h + 7d) | `statusLine` command, JSON on stdin incl. `rate_limits` | push | **shipped (v1)** |
| **Codex CLI** | Yes (5h + 7d, token-based since Apr 9 2026) | `/status` in-CLI; session/rollout logs under `~/.codex/`; platform usage page | pull | adapter #2 — research the exact `~/.codex/` fields |
| **Antigravity** | Yes (weekly; **separate quota pool per model**) | Go CLI + SDK (I/O 2026); IDE state. 3rd-party "Cockpit" ext already reads it, so it's reachable | pull | adapter #3 — research the CLI/IDE surface |
| **DeepSeek** | **No cap** (pay-per-token API) | usage fields on API responses; community CLIs/TUIs show per-turn token+cost | pull | adapter #4 — `cost`, not `caps` |

### Codex (adapter #2)
Codex flipped to token-based 5-hour + weekly limits on 2026-04-09. `/status`
prints them live; the numbers presumably also land in `~/.codex/` session
logs. **Task:** confirm whether the cap % is in the logs (harvestable) or only
computed in the TUI (would need to shell out to `/status`). If log-based, a
periodic tail → `parse()` → `caps` is the clean path.

### Antigravity (adapter #3)
IDE-hosted (VS Code fork) with a new Go CLI + SDK. Quotas are **per model
pool** (Gemini Flash / Pro / Claude each separate), weekly refresh. The
existing third-party "Cockpit" VS Code extension proves the quota data is
reachable. **Task:** find the CLI/SDK/IDE surface that exposes remaining
quota; map each model pool into a `caps` entry (keyed by model).

### DeepSeek (adapter #4)
No subscription cap — it's a pay-per-token API. "Usage" here is **cost**:
tokens + dollars, with a cache-hit/miss split. Community CLIs/TUIs already
show per-turn + session cost. **Task:** read the usage fields off the API
responses (or the CLI's own accounting) into the `cost` field. This is where
the `cost`-not-`caps` half of the Reading earns its place.

## Other things on the list

- **Menu-bar / live surface** — a small always-on indicator reading
  `usage-snapshot.json` (the data is already being captured).
- **`llmeter doctor`** — verify the install (settings key present, snapshot
  fresh, python3/zsh ok) in one command.
- **Config file** — opt into showing the 5-hour window, cost, or per-model
  pools; choose separators/emoji.

## Non-goals

- Not a billing tool — the cap % / cost are the vendors' numbers surfaced,
  not a reconciliation of your invoice.
- Not a proxy or a network service — everything stays local and passive.
