"""Python client for the browserview.io API.

browserview.io provides disposable cloud Chromium sessions that humans can
watch and control in a live viewer while agents drive the same browser via
the Chrome DevTools Protocol.

    from browserview import BrowserView

    with BrowserView() as bv:  # reads BROWSERVIEW_API_KEY
        session = bv.create_session(start_url="https://example.com")
        print(session.viewer_url)
"""

from .client import (
    BrowserView,
    BrowserViewError,
    Replay,
    Session,
    SessionToken,
)

__all__ = [
    "BrowserView",
    "BrowserViewError",
    "Replay",
    "Session",
    "SessionToken",
]

__version__ = "0.4.0"
