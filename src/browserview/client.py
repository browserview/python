"""Client, models, and errors for the browserview.io API."""

from __future__ import annotations

import os
import time
import urllib.parse
from dataclasses import dataclass, fields
from typing import Any, Optional

import httpx

DEFAULT_BASE_URL = "https://sessions.browserview.io"

#: Statuses that are always safe to retry (the server did not act on the
#: request: rate/capacity limits and temporary auth-backend outages).
_RETRYABLE_STATUSES = frozenset({429, 503})

#: Upper bound on a single ``Retry-After``-driven sleep, in seconds.
_MAX_RETRY_AFTER_WAIT = 30.0

# Module-level indirection so tests can stub out sleeping.
_sleep = time.sleep


class BrowserViewError(Exception):
    """Raised for any non-2xx API response (or a malformed 2xx body).

    Attributes:
        status_code: HTTP status code, or ``None`` when the error did not
            come from an HTTP status (e.g. an unexpected response shape).
        retry_after: Seconds to wait before retrying, from the ``Retry-After``
            header when the server sent one; ``None`` otherwise.
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass
class Session:
    """A browser session.

    The URL and token fields are only present on responses from
    :meth:`BrowserView.create_session` and :meth:`BrowserView.get_session`;
    :meth:`BrowserView.list_sessions` omits them. URL fields are always
    absolute (the server returns them relative; the client resolves them
    against its base URL).
    """

    id: str
    status: str = "unknown"
    health: str = "unknown"
    start_url: str = ""
    width: int = 0
    height: int = 0
    created_at: str = ""
    owner: str = ""
    #: Memory limit applied to the session's container, in bytes.
    mem_limit_bytes: Optional[int] = None
    #: Absolute URL of the live viewer with control access.
    viewer_url: Optional[str] = None
    #: Absolute URL of the live viewer with view-only access.
    watch_url: Optional[str] = None
    #: Absolute Chrome DevTools Protocol endpoint for this session.
    cdp_url: Optional[str] = None
    #: Token authorizing CDP connections; pass as the ``x-session-token``
    #: header or ``?token=`` query parameter.
    cdp_token: Optional[str] = None
    #: Lifetime of the embedded tokens, in seconds.
    token_ttl_seconds: Optional[int] = None
    #: Number of times the in-session browser has been restarted
    #: (:meth:`BrowserView.get_session` only). ``None`` when the session's
    #: status server is unreachable.
    restarts: Optional[int] = None
    #: ``True`` when the session has restarted at least once
    #: (:meth:`BrowserView.get_session` only).
    degraded: bool = False
    #: The applied per-session configuration (proxy/user_agent/locale/
    #: timezone/geolocation/stealth/downloads/context_id/...), echoed back by
    #: the server with proxy credentials redacted. Empty when none were set.
    config: dict[str, Any] = None  # type: ignore[assignment]
    #: Free-form searchable labels attached at create time.
    metadata: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.config is None:
            self.config = {}
        if self.metadata is None:
            self.metadata = {}


@dataclass
class SessionToken:
    """A freshly minted session token."""

    token: str
    scope: str
    ttl_seconds: int


@dataclass
class Replay:
    """A session replay manifest.

    ``status`` is ``"recording"`` while the session is still alive (all
    other fields empty) and ``"ready"`` once artifacts are available.

    ``video`` (when present) is a dict with ``url`` (a seekable WebM),
    ``start_time_ms``, ``duration_ms``, and ``size_bytes``. ``events`` maps
    stream names — ``actions``, ``console``, ``network``, ``errors`` — to
    dicts with ``url`` (newline-delimited JSON, each line carrying an
    epoch-ms ``ts``), ``count``, and ``size_bytes``. ``pages`` is the
    main-frame navigation timeline in absolute epoch milliseconds.

    Artifact URLs expire at ``urls_expire_at_ms``; call
    :meth:`BrowserView.get_replay` again for fresh ones. Align an event with
    the video via ``(event["ts"] - video["start_time_ms"]) / 1000`` seconds.
    """

    status: str
    session_id: str = ""
    start_url: str = ""
    width: int = 0
    height: int = 0
    started_at_ms: int = 0
    ended_at_ms: int = 0
    end_reason: str = ""
    video: Optional[dict[str, Any]] = None
    page_count: int = 0
    pages: list[dict[str, Any]] = None  # type: ignore[assignment]
    events: dict[str, dict[str, Any]] = None  # type: ignore[assignment]
    urls_expire_at_ms: int = 0

    def __post_init__(self) -> None:
        if self.pages is None:
            self.pages = []
        if self.events is None:
            self.events = {}


_SESSION_FIELDS = {f.name for f in fields(Session)}
_REPLAY_FIELDS = {f.name for f in fields(Replay)}
_URL_FIELDS = ("viewer_url", "watch_url", "cdp_url")


class BrowserView:
    """Client for the browserview.io API.

    Args:
        api_key: API key, minted in the browserview.io console
            (``bv_live_...``). Defaults to the ``BROWSERVIEW_API_KEY``
            environment variable.
        base_url: API origin. Defaults to the ``BROWSERVIEW_BASE_URL``
            environment variable, then ``https://sessions.browserview.io``.
        timeout: Request timeout in seconds. Defaults to 90 (session creation
            with ``wait=True`` blocks ~5s, sometimes up to ~60s server-side;
            leave headroom).
        max_retries: Maximum automatic retries per request for retryable
            failures (HTTP 429/503 on any method, plus transport errors on
            idempotent GET/DELETE). ``0`` disables retries. The client honors
            ``Retry-After`` (capped at 30s per wait) and otherwise backs off
            exponentially (1s, 2s, 4s, ...).
        transport: Optional ``httpx`` transport override, mainly for testing
            with ``httpx.MockTransport``.

    Usable as a context manager; otherwise call :meth:`close` when done.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 90.0,
        max_retries: int = 3,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        api_key = api_key or os.environ.get("BROWSERVIEW_API_KEY")
        if not api_key:
            raise ValueError(
                "browserview: missing API key. "
                "Pass api_key= or set BROWSERVIEW_API_KEY."
            )
        base_url = (
            base_url
            or os.environ.get("BROWSERVIEW_BASE_URL")
            or DEFAULT_BASE_URL
        )
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, int(max_retries))
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )

    # -- session lifecycle ------------------------------------------------

    def create_session(
        self,
        start_url: Optional[str] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        wait: Optional[bool] = None,
        record: Optional[bool] = None,
        max_lifetime_seconds: Optional[int] = None,
        proxy: Optional[dict[str, str]] = None,
        user_agent: Optional[str] = None,
        locale: Optional[str] = None,
        timezone: Optional[str] = None,
        geolocation: Optional[dict[str, float]] = None,
        stealth: Optional[bool] = None,
        downloads: Optional[bool] = None,
        context_id: Optional[str] = None,
        solve_captchas: Optional[bool] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> Session:
        """Create a new browser session.

        Only the arguments you set are sent; the server applies its own
        defaults otherwise (``start_url`` "about:blank", 1280x800,
        ``wait`` true — which blocks until the browser is ready, typically
        ~5 seconds).

        Note: ``user_agent``/``locale``/``timezone``/``geolocation``/
        ``stealth``/``proxy`` overrides are fully applied once the session is
        healthy — the very first ``start_url`` navigation may go out with
        stock browser headers. If the first request matters, create with
        ``start_url="about:blank"`` and navigate yourself over CDP.

        Args:
            start_url: Initial page URL.
            width: Viewport width, 320-3840.
            height: Viewport height, 240-2160.
            wait: Block until Chromium's CDP endpoint answers. Server
                default is true.
            record: Record the session for replay (video plus
                action/console/network/error streams, retrievable via
                :meth:`get_replay` after the session ends). Server default
                is false.
            max_lifetime_seconds: Auto-destroy the session after this many
                seconds (clamped to the plan/global caps).
            proxy: ``{"server": "scheme://host:port", "username": ...,
                "password": ..., "bypass": ...}`` — egress through a proxy.
                Credentials never reach the browser's command line and are
                redacted in API responses.
            user_agent: Override the browser's User-Agent string.
            locale: e.g. ``"en-US"`` — sets ``navigator.language`` and
                ``Accept-Language``.
            timezone: IANA name, e.g. ``"America/New_York"``.
            geolocation: ``{"lat": ..., "lon": ..., "accuracy": ...}``.
            stealth: Standard-tier anti-automation-detection tweaks.
            downloads: Mount a writable, persisted ``/downloads`` directory
                and enable :meth:`list_downloads` / :meth:`download_file` /
                :meth:`upload_file` for this session.
            context_id: Owner-scoped persistent context — cookies from a
                previous session with the same id are restored before you
                start driving, and archived back on end.
            solve_captchas: Record intent for captcha solving (the
                :meth:`solve_captcha` endpoint is separately gated on a
                server-configured provider).
            metadata: ``{key: value}`` string labels, searchable via
                ``list_sessions(metadata=...)``.
        """
        body: dict[str, Any] = {}
        optional: dict[str, Any] = {
            "start_url": start_url,
            "width": width,
            "height": height,
            "wait": wait,
            "record": record,
            "max_lifetime_seconds": max_lifetime_seconds,
            "proxy": proxy,
            "user_agent": user_agent,
            "locale": locale,
            "timezone": timezone,
            "geolocation": geolocation,
            "stealth": stealth,
            "downloads": downloads,
            "context_id": context_id,
            "solve_captchas": solve_captchas,
            "metadata": metadata,
        }
        for key, value in optional.items():
            if value is not None:
                body[key] = value
        data = self._request("POST", "/sessions", json=body)
        return self._session_from(data)

    def list_sessions(
        self, metadata: Optional[dict[str, str]] = None
    ) -> list[Session]:
        """List all sessions. URL and token fields are omitted; use
        :meth:`get_session` for fresh ones.

        Args:
            metadata: Only return sessions whose metadata labels contain
                every given ``{key: value}`` pair.
        """
        params = None
        if metadata:
            params = {f"metadata.{k}": v for k, v in metadata.items()}
        data = self._request("GET", "/sessions", params=params)
        return [self._session_from(item) for item in data]

    def get_session(self, session_id: str) -> Session:
        """Fetch a session by id, including fresh URLs and tokens plus the
        ``restarts`` / ``degraded`` health fields."""
        data = self._request("GET", f"/sessions/{self._quote(session_id)}")
        return self._session_from(data)

    def destroy_session(self, session_id: str) -> None:
        """Destroy a session and its browser."""
        self._request("DELETE", f"/sessions/{self._quote(session_id)}")

    def mint_token(
        self,
        session_id: str,
        scope: str,
        ttl_seconds: int = 3600,
    ) -> SessionToken:
        """Mint a new token for a session.

        Args:
            session_id: Target session id.
            scope: One of ``"view"``, ``"control"``, or ``"cdp"``.
            ttl_seconds: Token lifetime in seconds, 1..604800 (7 days).
        """
        data = self._request(
            "POST",
            f"/sessions/{self._quote(session_id)}/tokens",
            json={"scope": scope, "ttl_seconds": ttl_seconds},
        )
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise BrowserViewError(
                "browserview: unexpected token response from the server "
                "(missing 'token' field)"
            )
        return SessionToken(
            token=token,
            scope=data.get("scope", scope),
            ttl_seconds=data.get("ttl_seconds", ttl_seconds),
        )

    # -- screenshot, downloads, captcha -------------------------------------

    def screenshot(
        self,
        session_id: str,
        format: str = "png",
        quality: int = 80,
    ) -> bytes:
        """Capture a current-viewport image of a live session.

        Args:
            session_id: Target session id.
            format: ``"png"``, ``"jpeg"``, or ``"webp"``.
            quality: 1-100, for lossy formats.

        Returns:
            The raw image bytes.
        """
        return self._request(
            "GET",
            f"/sessions/{self._quote(session_id)}/screenshot",
            params={"format": format, "quality": quality},
            raw=True,
        )

    def list_downloads(self, session_id: str) -> list[dict[str, Any]]:
        """List a downloads-enabled session's files (works during and after
        the session).

        Returns:
            ``[{"name", "size_bytes", "modified_ms"}]``.
        """
        data = self._request(
            "GET", f"/sessions/{self._quote(session_id)}/downloads"
        )
        return data.get("files", []) if isinstance(data, dict) else []

    def download_file(self, session_id: str, name: str) -> bytes:
        """Fetch one downloaded file's contents (follows the presigned-S3
        redirect the server issues after the session ends)."""
        return self._request(
            "GET",
            f"/sessions/{self._quote(session_id)}/downloads/"
            f"{urllib.parse.quote(name, safe='')}",
            raw=True,
            follow_redirects=True,
        )

    def upload_file(
        self,
        session_id: str,
        content: bytes,
        filename: str,
    ) -> dict[str, Any]:
        """Place a file into a live session's ``/downloads`` mount (for
        ``input[type=file]`` flows — attach it via CDP
        ``DOM.setFileInputFiles`` with the returned container ``path``).

        The session must have been created with ``downloads=True``.

        Returns:
            ``{"name", "path", "size_bytes"}`` — ``path`` is the file's
            location inside the session container.
        """
        return self._request(
            "POST",
            f"/sessions/{self._quote(session_id)}/files",
            files={"file": (filename, content)},
        )

    def solve_captcha(
        self,
        session_id: str,
        type: str,
        sitekey: str,
        url: str,
        action: Optional[str] = None,
    ) -> str:
        """Relay a captcha challenge to the server-configured solver and
        return the solution token for your agent to inject.

        Raises a 501 :class:`BrowserViewError` when solving is not enabled
        on the host.

        Args:
            session_id: Target session id.
            type: ``"recaptcha_v2"``, ``"recaptcha_v3"``, ``"hcaptcha"``,
                or ``"turnstile"``.
            sitekey: The site key from the challenge on the page.
            url: The page URL hosting the challenge.
            action: reCAPTCHA v3 action, when applicable.
        """
        body: dict[str, Any] = {"type": type, "sitekey": sitekey, "url": url}
        if action is not None:
            body["action"] = action
        data = self._request(
            "POST",
            f"/sessions/{self._quote(session_id)}/captcha/solve",
            json=body,
        )
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise BrowserViewError(
                "browserview: unexpected captcha response from the server "
                "(missing 'token' field)"
            )
        return token

    # -- replay ------------------------------------------------------------

    def get_replay(self, session_id: str) -> Replay:
        """Fetch the replay manifest for a recorded session.

        Works after the session is destroyed. Returns
        ``Replay(status="recording")`` while the session is still alive, and
        raises a 404 :class:`BrowserViewError` while the recording finalizes
        (typically <30s after the session ends — use :meth:`wait_for_replay`
        to poll through that window).
        """
        data = self._request(
            "GET", f"/sessions/{self._quote(session_id)}/replay"
        )
        return self._replay_from(data)

    def wait_for_replay(
        self,
        session_id: str,
        timeout: float = 120.0,
        interval: float = 5.0,
    ) -> Replay:
        """Poll until a session's replay is ready and return its manifest.

        Polls through the live (``status="recording"``) and finalizing (404)
        states. Raises :class:`BrowserViewError` on timeout; any non-404 API
        error propagates immediately, as does a 404 reporting that the live
        session is not being recorded (one created without ``record=True``).
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                replay = self.get_replay(session_id)
                if replay.status == "ready":
                    return replay
            except BrowserViewError as e:
                if e.status_code != 404:
                    raise
                if "not being recorded" in str(e):
                    raise BrowserViewError(
                        f"browserview: session {session_id} was not created "
                        "with record=True, so it has no replay",
                        e.status_code,
                    ) from e
            if time.monotonic() + interval > deadline:
                raise BrowserViewError(
                    f"browserview: replay for session {session_id} was not "
                    f"ready within {timeout:g}s"
                )
            _sleep(interval)

    def _replay_from(self, data: dict[str, Any]) -> Replay:
        known = {k: v for k, v in data.items() if k in _REPLAY_FIELDS}
        video = known.get("video")
        if isinstance(video, dict) and isinstance(video.get("url"), str):
            video["url"] = self._absolutize(video["url"])
        for stream in (known.get("events") or {}).values():
            if isinstance(stream, dict) and isinstance(stream.get("url"), str):
                stream["url"] = self._absolutize(stream["url"])
        return Replay(**known)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> "BrowserView":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _quote(session_id: str) -> str:
        return urllib.parse.quote(session_id, safe="")

    def _request(
        self,
        method: str,
        path: str,
        json: Any = None,
        params: Any = None,
        files: Any = None,
        raw: bool = False,
        follow_redirects: bool = False,
    ) -> Any:
        retryable_transport = method in ("GET", "DELETE")
        attempt = 0
        while True:
            try:
                response = self._http.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    files=files,
                    follow_redirects=follow_redirects,
                )
            except httpx.TransportError:
                if retryable_transport and attempt < self.max_retries:
                    _sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                raise

            if response.is_success:
                if raw:
                    return response.content
                return None if response.status_code == 204 else response.json()

            retry_after = self._parse_retry_after(response)
            if (
                response.status_code in _RETRYABLE_STATUSES
                and attempt < self.max_retries
            ):
                if retry_after is not None and retry_after >= 0:
                    delay = min(retry_after, _MAX_RETRY_AFTER_WAIT)
                else:
                    delay = self._backoff(attempt)
                _sleep(delay)
                attempt += 1
                continue

            raise BrowserViewError(
                self._error_detail(response), response.status_code, retry_after
            )

    @staticmethod
    def _backoff(attempt: int) -> float:
        return float(2**attempt)  # 1s, 2s, 4s, ...

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> Optional[float]:
        header = response.headers.get("retry-after")
        if header is None:
            return None
        try:
            return float(header)
        except ValueError:
            return None

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        detail = f"HTTP {response.status_code}"
        try:
            payload = response.json()
            value = payload.get("detail")
            if isinstance(value, str) and value:
                detail = value
            elif value is not None:
                detail = str(value)
        except Exception:
            pass
        return detail

    def _absolutize(self, value: str) -> str:
        parts = urllib.parse.urlsplit(value)
        if parts.scheme or parts.netloc:
            return value  # already absolute
        if value.startswith("/"):
            return self.base_url + value
        return urllib.parse.urljoin(self.base_url + "/", value)

    def _session_from(self, data: dict[str, Any]) -> Session:
        known = {k: v for k, v in data.items() if k in _SESSION_FIELDS}
        for field in _URL_FIELDS:
            value = known.get(field)
            if isinstance(value, str) and value:
                known[field] = self._absolutize(value)
        if known.get("degraded") is None:
            known.pop("degraded", None)
        return Session(**known)
