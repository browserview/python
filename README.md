# browserview

Python client for the [browserview.io](https://browserview.io) API. browserview.io runs disposable cloud Chromium sessions that humans can watch and control in a live viewer while agents drive the same browser over the Chrome DevTools Protocol.

Requires Python 3.9+. Single dependency: `httpx`.

## Install

```sh
pip install browserview
```

## Configuration

| Setting | Constructor arg | Env var | Default |
| --- | --- | --- | --- |
| API key | `api_key` | `BROWSERVIEW_API_KEY` | required |
| Base URL | `base_url` | `BROWSERVIEW_BASE_URL` | `https://sessions.browserview.io` |
| Timeout | `timeout` | — | `90.0` seconds |
| Retries | `max_retries` | — | `3` (`0` disables) |

Precedence: explicit constructor arg > environment variable > default.

**API keys** are minted in the [browserview.io](https://browserview.io) console, format `bv_live_` + 40 hex chars, and are scoped to your own sessions.

The client authenticates with `Authorization: Bearer <key>`; the server equally accepts `x-api-key: <key>` if you call it directly.

## Quickstart

```python
from browserview import BrowserView

with BrowserView() as bv:  # reads BROWSERVIEW_API_KEY (and BROWSERVIEW_BASE_URL)
    session = bv.create_session(start_url="https://example.com")

    print(session.viewer_url)  # live viewer, control access
    print(session.watch_url)   # live viewer, view-only

    bv.destroy_session(session.id)
```

`create_session` only sends the fields you set; server defaults are `start_url="about:blank"`, `1280x800`, and `wait=True` — with `wait` the call blocks until the browser is ready, typically ~5 seconds and up to ~60s server-side (the default 90s timeout leaves headroom). Bounds: width 320-3840, height 240-2160.

Other operations:

```python
sessions = bv.list_sessions()        # no URLs/tokens in list responses
fresh = bv.get_session(session.id)   # fresh URLs/tokens + health fields
token = bv.mint_token(session.id, scope="view", ttl_seconds=3600)
# scope: "view" | "control" | "cdp"; ttl_seconds: 1..604800 (7 days), default 3600
```

Session fields include `mem_limit_bytes` (container memory limit), and — on `get_session` only — `restarts` (browser restart count, `None` if the session's status server is unreachable) and `degraded` (`True` once it has restarted).

The server returns `viewer_url` / `watch_url` / `cdp_url` as *relative* paths; the client resolves them to absolute URLs against your base URL for you.

## Drive the session with Playwright

Connect an agent to the same browser a human is watching in the viewer:

```python
from browserview import BrowserView
from playwright.sync_api import sync_playwright

with BrowserView() as bv, sync_playwright() as p:
    session = bv.create_session(start_url="https://example.com")

    browser = p.chromium.connect_over_cdp(
        session.cdp_url,
        headers={"x-session-token": session.cdp_token},
    )

    page = browser.contexts[0].pages[0]
    page.goto("https://news.ycombinator.com")
```

The token can also be passed as `?token=` on the CDP URL. Session-scoped tokens (from `mint_token`) authenticate viewer/CDP/signal endpoints the same two ways.

## Per-session configuration

All create-time knobs are keyword arguments to `create_session`; only the ones you set are sent:

```python
session = bv.create_session(
    start_url="about:blank",
    stealth=True,                              # anti-automation-detection tweaks
    user_agent="MyAgent/1.0",
    locale="en-GB",
    timezone="America/New_York",
    geolocation={"lat": 40.7, "lon": -74.0},
    proxy={"server": "http://proxy:8080", "username": "u", "password": "p"},
    context_id="my-login",                     # cookies persist across runs under this id
    downloads=True,                            # persisted /downloads mount + file APIs
    metadata={"job": "crawl-1"},               # searchable labels
    max_lifetime_seconds=600,
)
```

Overrides apply once the session is healthy — the very first `start_url` navigation can go out with stock headers, so navigate over CDP from `about:blank` when the first request matters. The applied config (secrets redacted) is echoed on `session.config`; labels on `session.metadata`.

```python
bv.list_sessions(metadata={"job": "crawl-1"})            # filter by labels

image = bv.screenshot(session.id, format="png")          # current-viewport bytes

files = bv.list_downloads(session.id)                    # [{name, size_bytes, modified_ms}]
data = bv.download_file(session.id, files[0]["name"])    # bytes; works after the session ends
bv.upload_file(session.id, b"col1,col2", "input.csv")    # into /downloads for file-input flows

token = bv.solve_captcha(                                # 501 unless the host has a solver
    session.id, "turnstile", sitekey="0x...", url="https://target.example",
)
```

## Session replay

Create the session with `record=True` and BrowserView captures everything server-side — a video of the display plus structured streams of actions, console output, network requests, and errors:

```python
with BrowserView() as bv:
    session = bv.create_session(start_url="https://example.com", record=True)

    # ... drive the session ...
    bv.destroy_session(session.id)

    # Ready seconds after the session ends; polls up to 2 minutes.
    replay = bv.wait_for_replay(session.id)
    print(replay.video["url"])              # seekable WebM
    print(replay.pages)                     # main-frame navigation timeline
    print(replay.events["console"]["url"])  # JSONL: {"ts": ..., "level": ...}
```

`get_replay(session_id)` fetches the manifest without polling: it returns `Replay(status="recording")` while the session is alive and raises a 404 `BrowserViewError` while the recording finalizes. If the live session was created without `record=True`, `wait_for_replay` fails immediately instead of polling. Every event line carries an absolute epoch-ms `ts`; align it with the video via `(ts - replay.video["start_time_ms"]) / 1000` seconds. Artifact URLs expire at `urls_expire_at_ms` — call `get_replay` again for fresh ones.

## Errors and retries

Non-2xx responses raise `BrowserViewError` with `status_code` and `retry_after` (parsed from the `Retry-After` header on any status, in seconds):

- `401` — invalid or missing key (repeated failures escalate to 429 per-IP)
- `404` — unknown session, or one you don't own
- `429` + `Retry-After: 30` — session/capacity limits on create (session limits, memory, ports)
- `429` + `Retry-After: 60` — request/create rate limits (per IP or per owner) and repeated auth failures
- `502` — session create/backend failure
- `503` + `Retry-After: 10` — auth backend temporarily unavailable
- `503` + `Retry-After: 60` — recording backend unavailable (create with `record=True`)

The client retries automatically: HTTP `429`/`503` on **all** methods (creating a session is safe to retry on these — the session was not created), and transport/network errors on idempotent `GET`/`DELETE`. It honors `Retry-After` when present (each wait capped at 30s), otherwise backs off 1s, 2s, 4s. Up to `max_retries` retries (default 3); set `max_retries=0` to disable.

```python
from browserview import BrowserView, BrowserViewError

bv = BrowserView(max_retries=0, timeout=10.0)
try:
    bv.get_session("nope")
except BrowserViewError as err:
    print(err.status_code, err.retry_after, err)
```

## Health checks

`GET /healthz` and `GET /readyz` on the base URL are unauthenticated and return `{"status": "ok"}` — handy for probes and smoke tests; no client method needed.
