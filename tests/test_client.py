"""Offline tests for the browserview client (httpx.MockTransport, no network)."""

from __future__ import annotations

import json as jsonlib

import httpx
import pytest

import browserview.client as client_module
from browserview import BrowserView, BrowserViewError

BASE = "https://sessions.test.invalid"

SESSION_BODY = {
    "id": "3f9c62d81b4a",
    "status": "running",
    "health": "healthy",
    "start_url": "about:blank",
    "width": 1280,
    "height": 800,
    "created_at": "2026-08-01T00:00:00Z",
    "owner": "",
    "mem_limit_bytes": 1342177280,
    "viewer_url": "/sessions/3f9c62d81b4a/viewer?token=ctl",
    "watch_url": "/sessions/3f9c62d81b4a/viewer?token=view",
    "cdp_url": "/sessions/3f9c62d81b4a/cdp",
    "cdp_token": "tok",
    "token_ttl_seconds": 3600,
}


@pytest.fixture
def sleeps(monkeypatch):
    """Stub out retry sleeps and record the requested delays."""
    recorded = []
    monkeypatch.setattr(client_module, "_sleep", recorded.append)
    return recorded


def make_client(handler, **kwargs):
    kwargs.setdefault("api_key", "bv_live_" + "0" * 40)
    kwargs.setdefault("base_url", BASE)
    return BrowserView(transport=httpx.MockTransport(handler), **kwargs)


# -- retries ---------------------------------------------------------------


def test_retry_on_429_then_success(sleeps):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, json={"detail": "session limit reached"})
        return httpx.Response(201, json=SESSION_BODY)

    with make_client(handler) as bv:
        session = bv.create_session()

    assert session.id == "3f9c62d81b4a"
    assert len(calls) == 3
    # No Retry-After header -> exponential backoff 1s, 2s.
    assert sleeps == [1.0, 2.0]


def test_retry_after_header_honored_and_capped(sleeps):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429, headers={"Retry-After": "7"}, json={"detail": "busy"}
            )
        if len(calls) == 2:
            return httpx.Response(
                503, headers={"Retry-After": "999"}, json={"detail": "auth down"}
            )
        return httpx.Response(200, json=[])

    with make_client(handler) as bv:
        assert bv.list_sessions() == []

    assert sleeps == [7.0, 30.0]  # honored, then capped at 30s


def test_retries_exhausted_raises_with_status_and_retry_after(sleeps):
    def handler(request):
        return httpx.Response(
            429, headers={"Retry-After": "30"}, json={"detail": "capacity"}
        )

    with make_client(handler, max_retries=2) as bv:
        with pytest.raises(BrowserViewError) as excinfo:
            bv.get_session("abc")

    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after == 30.0
    assert str(excinfo.value) == "capacity"
    assert len(sleeps) == 2


def test_max_retries_zero_disables_retries(sleeps):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"detail": "nope"})

    with make_client(handler, max_retries=0) as bv:
        with pytest.raises(BrowserViewError):
            bv.create_session()

    assert len(calls) == 1
    assert sleeps == []


def test_transport_error_retried_for_get(sleeps):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=dict(SESSION_BODY, restarts=0, degraded=False))

    with make_client(handler) as bv:
        session = bv.get_session("3f9c62d81b4a")

    assert session.id == "3f9c62d81b4a"
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_transport_error_not_retried_for_post(sleeps):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("boom", request=request)

    with make_client(handler) as bv:
        with pytest.raises(httpx.ConnectError):
            bv.create_session()

    assert len(calls) == 1
    assert sleeps == []


# -- request shaping -------------------------------------------------------


def test_session_id_is_url_escaped():
    seen = {}

    def handler(request):
        seen[request.method] = request.url.raw_path.decode()
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path.endswith("/tokens"):
            return httpx.Response(
                200, json={"token": "t", "scope": "view", "ttl_seconds": 60}
            )
        return httpx.Response(200, json=SESSION_BODY)

    weird_id = "../etc/passwd?x=1#frag"
    escaped = "..%2Fetc%2Fpasswd%3Fx%3D1%23frag"
    with make_client(handler) as bv:
        bv.get_session(weird_id)
        assert seen["GET"] == f"/sessions/{escaped}"
        bv.destroy_session(weird_id)
        assert seen["DELETE"] == f"/sessions/{escaped}"
        bv.mint_token(weird_id, scope="view")
        assert seen["POST"] == f"/sessions/{escaped}/tokens"


def test_create_session_omits_unset_fields():
    bodies = []

    def handler(request):
        bodies.append(jsonlib.loads(request.content))
        return httpx.Response(201, json=SESSION_BODY)

    with make_client(handler) as bv:
        bv.create_session()
        bv.create_session(start_url="https://example.com", wait=False)

    assert bodies[0] == {}  # notably: no "wait" key by default
    assert bodies[1] == {"start_url": "https://example.com", "wait": False}


# -- response parsing ------------------------------------------------------


def test_new_fields_parsed_and_urls_absolutized():
    body = dict(SESSION_BODY, restarts=2, degraded=True, some_future_field="x")

    def handler(request):
        return httpx.Response(200, json=body)

    with make_client(handler) as bv:
        session = bv.get_session("3f9c62d81b4a")

    assert session.mem_limit_bytes == 1342177280
    assert session.restarts == 2
    assert session.degraded is True
    assert session.viewer_url == f"{BASE}/sessions/3f9c62d81b4a/viewer?token=ctl"
    assert session.watch_url == f"{BASE}/sessions/3f9c62d81b4a/viewer?token=view"
    assert session.cdp_url == f"{BASE}/sessions/3f9c62d81b4a/cdp"


def test_missing_optional_fields_do_not_raise():
    def handler(request):
        return httpx.Response(200, json=[{"id": "abc", "status": "running"}])

    with make_client(handler) as bv:
        sessions = bv.list_sessions()

    assert sessions[0].id == "abc"
    assert sessions[0].mem_limit_bytes is None
    assert sessions[0].restarts is None
    assert sessions[0].degraded is False
    assert sessions[0].viewer_url is None


def test_mint_token_bad_shape_raises_browserview_error():
    def handler(request):
        return httpx.Response(200, json={"unexpected": "shape"})

    with make_client(handler) as bv:
        with pytest.raises(BrowserViewError) as excinfo:
            bv.mint_token("abc", scope="cdp")

    assert "token" in str(excinfo.value)


# -- replay ----------------------------------------------------------------

REPLAY_BODY = {
    "status": "ready",
    "session_id": "3f9c62d81b4a",
    "start_url": "https://example.com",
    "started_at_ms": 1000,
    "ended_at_ms": 2000,
    "end_reason": "api",
    "video": {
        "url": "/sessions/3f9c62d81b4a/replay/files/video.webm?token=r",
        "format": "webm",
        "codec": "vp8",
        "start_time_ms": 1000,
        "duration_ms": 1000,
        "size_bytes": 42,
    },
    "page_count": 1,
    "pages": [
        {"page_id": "p_000", "url": "https://example.com",
         "start_time_ms": 1000, "end_time_ms": 2000}
    ],
    "events": {
        "console": {
            "url": "https://bucket.s3.invalid/console.jsonl.gz?sig=1",
            "count": 2,
            "size_bytes": 10,
        }
    },
    "urls_expire_at_ms": 99999,
}


def test_create_session_record_flag():
    def handler(request):
        assert jsonlib.loads(request.content) == {"record": True}
        return httpx.Response(201, json=SESSION_BODY)

    with make_client(handler) as bv:
        bv.create_session(record=True)


def test_get_replay_absolutizes_relative_urls():
    def handler(request):
        assert request.url.path == "/sessions/3f9c62d81b4a/replay"
        return httpx.Response(200, json=REPLAY_BODY)

    with make_client(handler) as bv:
        replay = bv.get_replay("3f9c62d81b4a")
    assert replay.status == "ready"
    assert replay.video["url"] == (
        BASE + "/sessions/3f9c62d81b4a/replay/files/video.webm?token=r"
    )
    # Presigned (already absolute) URLs are untouched.
    assert replay.events["console"]["url"].startswith("https://bucket.s3.invalid/")
    assert replay.pages[0]["page_id"] == "p_000"


def test_wait_for_replay_polls_through_recording_and_404(sleeps):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"status": "recording", "session_id": "x"})
        if calls["n"] == 2:
            return httpx.Response(404, json={"detail": "replay not found"})
        return httpx.Response(200, json=REPLAY_BODY)

    with make_client(handler) as bv:
        replay = bv.wait_for_replay("3f9c62d81b4a", timeout=60, interval=1)
    assert replay.status == "ready"
    assert calls["n"] == 3


def test_wait_for_replay_propagates_non_404():
    def handler(request):
        return httpx.Response(403, json={"detail": "nope"})

    with make_client(handler) as bv:
        with pytest.raises(BrowserViewError) as excinfo:
            bv.wait_for_replay("x", timeout=5, interval=1)
    assert excinfo.value.status_code == 403


def test_wait_for_replay_times_out(monkeypatch):
    # Fake clock: each stubbed sleep advances monotonic time, so the
    # deadline is reached without real waiting.
    clock = {"now": 0.0}

    def fake_sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(client_module, "_sleep", fake_sleep)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: clock["now"])

    def handler(request):
        return httpx.Response(404, json={"detail": "replay not found"})

    with make_client(handler) as bv:
        with pytest.raises(BrowserViewError) as excinfo:
            bv.wait_for_replay("x", timeout=3, interval=2)
    assert "not ready" in str(excinfo.value)


# -- configuration ---------------------------------------------------------


def test_base_url_env_var(monkeypatch):
    monkeypatch.setenv("BROWSERVIEW_BASE_URL", "https://shard-7.example/")
    bv = BrowserView(api_key="k", transport=httpx.MockTransport(lambda r: None))
    assert bv.base_url == "https://shard-7.example"
    bv.close()


def test_default_base_url(monkeypatch):
    monkeypatch.delenv("BROWSERVIEW_BASE_URL", raising=False)
    bv = BrowserView(api_key="k", transport=httpx.MockTransport(lambda r: None))
    assert bv.base_url == "https://sessions.browserview.io"
    bv.close()
