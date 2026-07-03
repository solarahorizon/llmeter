"""llmeter adapters — one module per AI CLI.

Each adapter exposes ``parse(payload) -> Reading`` (see ``llmeter.core`` for
the normalized Reading shape). v1 ships ``claude_code`` only; ``codex``,
``antigravity`` and ``deepseek`` are the v2 roadmap (docs/ROADMAP.md).
"""
