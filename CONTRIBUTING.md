# Contributing to llmeter

Thanks for helping out. llmeter is deliberately tiny — a status-line command
that prints usage and harvests it to disk. Contributions that keep it small,
private, and unbreakable are very welcome.

## Ground rules (non-negotiable)

These are what make llmeter safe to run on every message of every session:

1. **Zero dependencies.** Python **stdlib only** — no `pip install`, ever. If a
   change needs a third-party package, it doesn't belong here.
2. **Fail-soft, always.** The status-line command must **never** break the host
   tool's prompt. Any error must still print *something* and exit 0. Every new
   code path that touches a payload gets a hostile-shape test (see below).
3. **Zero network, zero secrets.** llmeter reads a local payload and writes two
   local files. It must never make a network call, and must never persist
   anything beyond the allowlisted usage fields (model, context %, cap %,
   `resets_at`, session id). If you add a field to the snapshot, it must be on
   an explicit allowlist — never pass a raw vendor payload through to disk.
4. **The installer must not be able to lose a user's settings.** Any change to
   `install.sh` / `uninstall.sh` must keep: back-up-before-write, refuse-on-
   unparseable, atomic write + re-validate, and touch only the `statusLine`
   key.

## Dev setup

No setup — it's stdlib Python + zsh. Run the tests:

```bash
python3 -m unittest discover -s tests
```

CI runs the same on every push/PR.

## Adding an adapter (the main way to extend llmeter)

llmeter's roadmap is "every AI CLI" (see [docs/ROADMAP.md](docs/ROADMAP.md)).
Each tool is one self-contained adapter:

1. Create `llmeter/adapters/<tool>.py` with a `parse(payload) -> Reading`
   function. A **Reading** is the vendor-neutral dict documented at the top of
   [`llmeter/core.py`](llmeter/core.py): `source`, `model`, `context_pct`,
   `caps` (cap-metered tools) and/or `cost` (pay-per-token tools), `session_id`.
2. **Allowlist** the fields you extract — never copy a raw vendor dict into the
   Reading (rule 3).
3. Feed it whatever the tool surfaces. Claude Code *pushes* a payload to a
   status-line command; most others are *pull* (tail a log, run a status
   command, call an API) — the ROADMAP records the mechanics per tool.
4. Add tests. You don't need the real tool installed — construct a
   representative payload dict and assert on `parse()` + `core.format_line()`,
   the way `tests/test_llmeter.py` does for Claude Code. Include at least one
   hostile-shape case (`{}`, a string where a dict is expected, a non-dict
   input) proving it never raises.

Core (persistence, cross-window fallback, rendering, fail-soft) is shared and
already done — an adapter should only need `parse()`.

## Pull requests

- Keep one concern per PR; keep it small.
- `python3 -m unittest discover -s tests` must pass (CI enforces it).
- Describe what changed and why. If it touches the installer or the harvested
  data, say how you verified the ground rules above still hold.

## Reporting issues

Include your OS, `python3 --version`, whether the status line renders at all,
and (if safe to share) the contents of `~/.claude/llmeter/usage-snapshot.json`.
Never paste anything you consider sensitive — though by design that file holds
only usage numbers.
