"""http_transport.py — a page, fetched without a browser.

Why this exists
===============
Most TikTok routes this family reads are server-rendered: the profile
page, the embed page and the video page all carry their whole payload in
the document, and all three were served to a bare HTTP client from a
datacentre address with no key and no cookies (measured 2026-09-22). A
browser on those routes costs a Chromium start and buys nothing — measured
on the profile route, three accounts end to end: 1.54 s over HTTP against
3.38 s through a browser, identical rows.

What this is NOT
================
Not a replacement for the browser engines. It is the DEFAULT where a route
does not challenge, and the browser is what `--transport auto` falls back
to the moment one does — because an HTTP client has nowhere to put a
solved token, no cookie jar a challenge issuer will accept, and no DOM. On
TikTok that fallback is load-bearing rather than theoretical: the WAF
interstitial a residential exit sometimes gets is cleared by a browser.

The interface is the one `_BrowserSession` already presents, so everything
above the transport in an engine is unchanged: `get_text`, `post_json`,
`goto`, `content`, `count_selector`, `evaluate`, `url`, `close`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

import requests

logger = logging.getLogger("http_transport")

# Every remote call is bounded (CLAUDE.md §8). `requests` imposes no
# timeout of its own, and a hung socket with no timeout is a run that
# never ends and never says why.
REQUEST_TIMEOUT = 30


class TransportError(RuntimeError):
    """A transport-level failure, already masked."""


def _mask(text: Any) -> str:
    """Mask every credential in a string, not just the first.

    `requests` puts the FULL URL — query string included — into the text of
    `HTTPError` and of every connection error, so an endpoint that takes a
    key as a query parameter leaks it the moment anything goes wrong. And
    a masker that handles the first occurrence prints the password the
    other four times while looking like it works (CLAUDE.md §8).
    """
    out = str(text)
    out = re.sub(r"(?i)\b((?:client)?key|token|api[_-]?key|password)=[^&\s\"']+",
                 r"\1=***", out)
    out = re.sub(r"(https?|wss?)://([^:/@\s]+):([^@\s]+)@", r"\1://\2:***@", out)
    return out


class HttpSession:
    """A `requests` session shaped like the browser sessions it replaces."""

    def __init__(self, proxy_url: Optional[str], user_agent: str,
                 client_version: str):
        self.session = requests.Session()
        self.proxy_url = proxy_url
        self.user_agent = user_agent
        self.client_version = client_version
        # Not owned in the browser sense; the attribute exists so the
        # engines' shared teardown does not have to know which transport
        # it is closing.
        self.owns_context = True
        self.browser = None
        self.context = None
        self.page = None
        self._url = ""
        self._content = ""
        if proxy_url:
            # Credentials ride in the session's own proxy field, never on
            # a command line — the same rule the browser engines follow
            # for `--proxy-server` (CLAUDE.md §8). Unlike Selenium, an
            # HTTP client CAN authenticate a proxy, which is worth knowing
            # when choosing a transport.
            self.session.proxies.update({"http": proxy_url,
                                         "https": proxy_url})
        self.session.headers.update({"User-Agent": user_agent,
                                     "Accept-Language": "en-US,en"})

    # -- transport ---------------------------------------------------------

    def get_text(self, url: str) -> Tuple[Optional[int], Optional[str]]:
        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise TransportError(_mask(exc)) from exc
        return response.status_code, response.text

    def post_json(self, url: str, headers: Dict[str, str],
                  body: Dict[str, Any]) -> Tuple[Optional[int], Any]:
        try:
            response = self.session.post(url, headers=headers, json=body,
                                         timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise TransportError(_mask(exc)) from exc
        try:
            return response.status_code, response.json()
        except ValueError:
            # A refusal is not JSON. Hand the body back as text so the
            # classifier can name it rather than the run dying on a decode.
            return response.status_code, response.text

    def goto(self, url: str) -> Optional[int]:
        """Fetch a page and keep it, so `content()` has something to read.

        There is no navigation here — no cookies set by script, no JS. What
        it IS good for is the one thing `_prime_session` needs a page for:
        reading anything the site states in the document.
        """
        status, text = self.get_text(url)
        self._url = url
        self._content = text or ""
        return status

    def content(self) -> str:
        return self._content

    def count_selector(self, selector: str) -> int:
        """Always 0: there is no DOM to count.

        The engines' readiness wait is skipped for this transport rather
        than being allowed to poll this to its timeout — a wait that can
        never be satisfied is a wait that always costs its full budget.
        """
        return 0

    def evaluate(self, js: str, arg: Any = None) -> Any:
        """Always None: there is no page to run JavaScript in.

        Said plainly rather than raising, because the captcha path calls
        this speculatively and a run must not die because a detector could
        not run. The engines report that a challenge cannot be solved on
        this transport and fall back to a browser.
        """
        return None

    @property
    def url(self) -> str:
        return self._url

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
