"""llmeter — an ambient usage meter for AI coding CLIs.

Stands in the host tool's status-line/render slot, prints a one-line
`model · ctx N% · wk N%` readout, and tees the underlying usage signal to
disk before the tool discards it. Zero tokens, zero network, stdlib-only.

v1 ships one adapter — Claude Code (the only tool that *pushes* its real
rate-limit numbers to a status-line command). v2 adds pull-based adapters
(Codex, Antigravity, DeepSeek); see docs/ROADMAP.md. Everything a new adapter
must produce is the normalized Reading defined in `llmeter.core`.
"""

__version__ = "0.1.0"
