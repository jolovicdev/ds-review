# AGENTS.md

Repository instructions for AI coding agents. Keep this file short; README is for product docs.

## Project

DS-Review is a Python 3.12+ GitHub PR reviewer built on Blackgeorge and DeepSeek.
It runs either as a composite `uv` GitHub Action or as a self-hosted FastAPI GitHub App webhook server.

## Commands

Run before finishing code changes:

```bash
uv run ruff check src/ tests/
uv run mypy src/
uv run pytest tests/ -v
```

Format only when needed:

```bash
uv run ruff format src/ tests/
```

## Conventions

- Use `uv` for Python workflows.
- Use `str | None` style unions.
- Keep GitHub, HTTP, and LLM work async.
- Keep LLM boundary schemas in `src/models.py`.
- Render review Markdown through `src/review_markdown.py`; do not hand-roll review bodies elsewhere.
- Keep review publishing strict: P0/P1/P2 only by default, no P3/style noise.

## Safety

- Never commit `.secrets.toml`, private keys, `.env`, `.blackgeorge/`, local DBs, pycache, or virtualenv files.
- Do not weaken webhook HMAC verification.
- Do not raise `max_evaluators` above 15 without explicit maintainer approval.
- Comment-triggered reviews should remain gated by trusted GitHub author associations unless the maintainer explicitly opts out.
