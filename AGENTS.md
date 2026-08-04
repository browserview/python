# AGENTS.md — browserview (Python)

Python client for the browserview.io API. Sync-only, one runtime dependency (`httpx`).

## Commands

- Test: `uv run --with pytest --with httpx pytest` (or `pip install -e .[dev]` then `pytest`) — tests are fully offline via `httpx.MockTransport`
- Build: `uv build` (hatchling; produces sdist + wheel in `dist/`)
- Publish: `uv publish` (needs a PyPI token)

## Layout

- `src/browserview/client.py` — entire SDK: `BrowserView` client, dataclasses (`Session`, `SessionToken`, `Replay`), `BrowserViewError`.
- `src/browserview/__init__.py` — public exports + `__version__` (keep in sync with `pyproject.toml`).
- `tests/test_client.py` — offline behavioral tests; `tests/conftest.py` puts `src/` on `sys.path`.

## Conventions

- Field names mirror the API's snake_case exactly. Dataclasses tolerate unknown/missing response fields (filter through `_*_FIELDS` sets) — never let a new server field break parsing.
- Only send request fields the caller explicitly set (server defaults win).
- 429/503 retried for every method; transport errors retried only for GET/DELETE. `Retry-After` honored, capped at 30s per wait.
- PEP 561 typed (`py.typed` ships in the wheel).
- The API contract source of truth is the browserview.io orchestrator; docs: https://browserview.io/docs/api and https://browserview.io/llms-full.txt

## Release

1. Bump version in **both** `pyproject.toml` and `src/browserview/__init__.py`.
2. Run tests.
3. Commit, tag `v<version>`, push with tags.
4. `uv build && uv publish`
