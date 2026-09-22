"""http_transport.py — the InnerTube endpoint without a browser.

Why this exists
===============
This repo's own README says the thing that makes this module obvious: the
endpoint YouTube's front end calls answers a bare HTTP POST with no key,
no cookies, no account and no browser. Measured 2026-09-21 from a
datacentre address, 60 consecutive pages with no refusal of any kind.

Everything else here then drove a browser anyway, because that is what
this family of scrapers does. Measured on the same machine, one page of
twenty comments:

    browser (Playwright, Chromium)   3.1 s
    plain HTTP, identical work       0.9 s

So the browser cost 3.5x and bought nothing on the path that matters. A
third-party audit put it more sharply than the code did and it was right.

What this is NOT
================
Not a replacement for the browser engines. It is the DEFAULT for a site
that does not challenge, and the browser is what `--transport auto` falls
back to the moment one does — because an HTTP client has nowhere to put a
solved token, no cookie jar a challenge issuer will accept, and no DOM.
That fallback is the whole reason the engines stay.

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
        reading the live InnerTube client version out of the document.
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
