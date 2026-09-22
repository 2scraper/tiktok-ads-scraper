#!/usr/bin/env python3
"""selenium_scraper.py — TikTok's EU Ad Library.

    python3 selenium_scraper.py --region DE
    python3 selenium_scraper.py --region DE,FR,GB --pages 5 --format both
    python3 selenium_scraper.py --region IT --query nike --days 90

One header gates the whole thing
================================
`library.tiktok.com` is TikTok's advertising transparency library for the
EU/EEA plus GB, CH, NO, IS, LI and TR — 33 regions, taken from the site's
own `/api/v1/support-regions` rather than guessed.

Its search endpoint is a plain POST served to a bare datacentre address
with no key, no account and no cookies. Measured 2026-09-22 from Hetzner,
Helsinki:

    with the browser's headers                         HTTP 200, 12 ads
    the same, minus `x-ccl-str`                        HTTP 421
    content-type + user-agent + referer + x-ccl-str    HTTP 200, 12 ads

So exactly one header gates it, and the page's own JavaScript mints it —
there is no global to read it out of, and a plain `fetch` issued from
inside the page gets 421 too, because the app attaches it in its own HTTP
client.

This engine therefore drives a browser for ONE purpose: open the library,
let it search once, and keep the header it sent. Everything after that is
a cheap POST. `--transport http` is refused with that reason rather than
failing later on a 421 nobody can interpret.

That makes this repo the reverse of its two siblings, whose pages are
server-rendered and who treat the browser as a fallback. CLAUDE.md §21:
gating is per-ROUTE, and so is the remedy.

`offset` is a decoy
===================
The request body carries an `offset`, the site's own UI increments it, and
it does nothing: offsets 0, 12 and 24 returned the identical twelve ads in
the identical order. Pagination is by `search_id`, a base64 cursor the
response hands back, and the end of the listing is read from that cursor
rather than from `has_more`. `limit` is a decoy too — 12, 50 and 100 all
return twelve.

A run that trusted `offset` would re-collect page one for as long as it
was asked to and report a complete run of duplicates.
"""

import argparse
import json
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Imported at MODULE level on purpose — see the note in
# puppeteer_scraper.py and CLAUDE.md §10. smoke_test.py asserts it.
from selenium import webdriver
from selenium.common.exceptions import (WebDriverException,
                                        TimeoutException as SETimeout)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            CaptchaUnsolvable, INJECT_TOKEN_JS,
                            RECAPTCHA_DISCOVERY_JS)
from output_writer import (COMPLETE_STOP_REASONS, Advertisement as Video,
                           dedupe_by_key, finish_run,
                           utc_now, EXIT_API_ERROR, EXIT_NO_PRODUCTS,
                           SOURCE_DEFAULT)
import page_flow
from page_flow import SolveBudget
from http_transport import HttpSession, TransportError
import product_parser as parser
from product_parser import (ADS_PER_PAGE, NotAnAdsQuery, STATE_CONTENT,
                            STATE_EMPTY_SUCCESS, STATE_NO_ADS,
                            STATE_TOKEN_REJECTED, STATE_UNKNOWN,
                            TOKEN_HEADER, detect_page_state,
                            normalise_region, parse_search, search_body,
                            search_url)
from tiktok_payload import PayloadError
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

# The one name the shared logic below uses for "the driver failed". Each
# engine binds it to its own library's exception, so everything from
# `_prime_session` downwards is byte-comparable across the three — which is
# what makes "the engines must agree" checkable rather than aspirational.
DriverError = (WebDriverException, SETimeout)

MODES = ("ads",)
DEFAULT_MODE = "ads"

ENGINE_NAME = "selenium"

# Every remote call is bounded (CLAUDE.md §8).
REQUEST_TIMEOUT_MS = 30_000
NAVIGATION_TIMEOUT_MS = 60_000

MIN_CARD_MATCHES = page_flow.MIN_CARD_MATCHES


@dataclass
class PageOutcome:
    """One fetch attempt's result, in request order rather than arrival order.

    CLAUDE.md §8: merging by arrival order makes the output depend on which
    worker finished first. Workers return these and the caller sorts.
    """
    number: int
    url: str = ""
    rows: List[Any] = field(default_factory=list)
    state: str = STATE_UNKNOWN
    status: Optional[int] = None
    blocked: bool = False
    error: Optional[str] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    attempted: bool = True


def _mask_credentials(text: Any) -> str:
    """Mask every credential in a string, not just the first.

    CLAUDE.md §8: a masker that handles the first occurrence prints the
    password the other four times and looks like it is working — Playwright
    repeats a CDP endpoint five times in one error, once in the message and
    four more in its call log.
    """
    import re
    out = str(text)
    out = re.sub(r"(?i)\b((?:client)?key|token|api[_-]?key|password)=[^&\s\"']+",
                 r"\1=***", out)
    out = re.sub(r"(wss?://)([^:/@\s]+):([^@\s]+)@", r"\1\2:***@", out)
    return out


def _chrome_ua(chromium_version: str) -> str:
    """A user agent built from the Chromium actually installed.

    CLAUDE.md §8: a hardcoded version drifts from whatever is installed,
    and claiming an older Chrome than the JS engine and TLS handshake
    report is itself a mismatch.
    """
    major = (chromium_version or "").split(".")[0] or "140"
    return (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")


def _proxy_failure(exc: Exception) -> str:
    """Name a dead proxy, or "" for anything else.

    CLAUDE.md §8: Chromium reports a dead proxy as a generic error, not a
    timeout, and the two want opposite responses — a timeout deserves
    another try at the SAME exit, a dead proxy a DIFFERENT one. Catching
    only the timeout type let this escape as a traceback in a sibling repo.
    """
    text = str(exc)
    for marker in ("ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
                   "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_UNEXPECTED_PROXY_AUTH",
                   "ERR_PROXY_CERTIFICATE_INVALID"):
        if marker in text:
            return marker
    return ""


# The InnerTube call, made from inside the page.
#
# A `fetch` on the site's own origin, so it carries the same cookies and
# the same proxy the browser has. Note the DIALECT: this is a function
# BODY, taking its argument from `arguments[0]` and signalling completion
# through the callback in `arguments[arguments.length - 1]`. Playwright and
# pyppeteer take `() => expr` for the same job. Spelling it three ways is
# unavoidable; letting either spelling reach a shared module is not
# (CLAUDE.md §1).
_FETCH_JS = """
var spec = arguments[0];
var done = arguments[arguments.length - 1];
var init = {method: spec.method, headers: spec.headers,
            credentials: 'include'};
if (spec.body) { init.body = spec.body; }
fetch(spec.url, init).then(function (response) {
  return response.text().then(function (text) {
    done({status: response.status, text: text});
  });
}).catch(function (err) {
  done({status: null, text: null, error: String(err)});
});
"""


class _BrowserSession:
    """A driver, a page on youtube.com, and a fetch primitive bound to it.

    A rotation is a FRESH BROWSER (CLAUDE.md §8): cookies a bot manager
    issued against exit A and replayed from exit B are a stronger signal
    than either address alone. So this object is torn down and rebuilt
    rather than having its proxy swapped underneath it.
    """

    def __init__(self, driver, proxy_url: Optional[str],
                 client_version: str, user_agent: Optional[str],
                 owns_driver: bool = True):
        # Always True here: this engine cannot connect to a remote browser
        # at all (see the module docstring), so it only ever ends a driver
        # it started. The flag exists so the three sessions carry the same
        # shape and `check_every_engine_exposes_the_same_public_surface`
        # can say so.
        self.owns_driver = owns_driver
        self.driver = driver
        self.browser = driver
        self.context = driver
        self.page = driver
        self.proxy_url = proxy_url
        self.client_version = client_version
        self.user_agent = user_agent

    # -- transport ---------------------------------------------------------

    def _fetch(self, url: str, method: str = "GET",
               headers: Optional[Dict[str, str]] = None,
               body: Optional[str] = None):
        spec = {"url": url, "method": method, "headers": headers or {},
                "body": body}
        try:
            self.driver.set_script_timeout(REQUEST_TIMEOUT_MS / 1000.0)
            return self.driver.execute_async_script(_FETCH_JS, spec)
        except DriverError as exc:
            raise _TransportError(_mask_credentials(exc)) from exc

    def get_text(self, url: str) -> Tuple[Optional[int], Optional[str]]:
        result = self._fetch(url) or {}
        return result.get("status"), result.get("text")

    def post_json(self, url: str, headers: Dict[str, str],
                  body: Dict[str, Any]) -> Tuple[Optional[int], Any]:
        result = self._fetch(url, "POST", headers, json.dumps(body)) or {}
        status, text = result.get("status"), result.get("text")
        if result.get("error"):
            raise _TransportError(_mask_credentials(result["error"]))
        try:
            return status, json.loads(text) if text else None
        except (TypeError, ValueError):
            # A refusal is not JSON. Hand the body back as text so the
            # classifier can name it rather than the run dying on a decode.
            return status, text

    def goto(self, url: str) -> Optional[int]:
        self.driver.set_page_load_timeout(NAVIGATION_TIMEOUT_MS / 1000.0)
        self.driver.get(url)
        # Selenium reports no HTTP status for a navigation. That is a real
        # gap on sites where the status IS the signal — but not here: every
        # response this engine classifies comes back through `_fetch`,
        # which carries the status from the page's own `fetch`. So nothing
        # is discarded; there is simply nothing to discard at this call.
        return None

    def content(self) -> str:
        try:
            return self.driver.page_source or ""
        except DriverError:
            return ""

    def count_selector(self, selector: str) -> int:
        try:
            return len(self.driver.find_elements(By.CSS_SELECTOR, selector))
        except DriverError:
            return 0

    def evaluate(self, js: str, arg: Any = None) -> Any:
        """Run a function EXPRESSION in the page.

        Wrapped in `return (…)(arg)` because `execute_script` takes a
        function BODY, where the shared captcha module hands out `() =>
        expr`. Never an evaluated string on the page's own terms: YouTube's
        Content-Security-Policy has no `unsafe-eval` (CLAUDE.md §18), and
        `execute_script` goes through the WebDriver protocol rather than
        through the page's `eval`.
        """
        try:
            if arg is not None:
                return self.driver.execute_script(
                    f"return ({js})(arguments[0]);", arg)
            return self.driver.execute_script(f"return ({js})();")
        except DriverError:
            return None

    @property
    def url(self) -> str:
        try:
            return self.driver.current_url or ""
        except Exception:
            return ""

    def capture_search_token(self, path_fragment: str, header: str):
        """Selenium's dialect: CDP performance logs.

        Selenium has no request-event API, so this reads Chrome's own
        performance log for `Network.requestWillBeSent`. That needs
        `goog:loggingPrefs` set at launch, which `_launch_local` does —
        and needs the click to go through `execute_script`, because the
        library's Search button is not reliably clickable by the WebDriver
        element click. Measured: an element click captured nothing and a
        scripted click captured the token.
        """
        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
        except Exception:                                  # noqa: BLE001
            pass
        self._click_search()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                entries = self.driver.get_log("performance")
            except Exception:                              # noqa: BLE001
                return None
            for entry in entries:
                try:
                    message = json.loads(entry["message"])["message"]
                except Exception:                          # noqa: BLE001
                    continue
                if message.get("method") != "Network.requestWillBeSent":
                    continue
                request = (message.get("params") or {}).get("request") or {}
                if path_fragment not in request.get("url", ""):
                    continue
                headers = {k.lower(): v
                           for k, v in (request.get("headers") or {}).items()}
                token = headers.get(header.lower())
                if token:
                    return token
            time.sleep(0.5)
        return None

    def _click_search(self):
        try:
            time.sleep(6)
            for button in self.driver.find_elements(By.TAG_NAME, "button"):
                if "search" in (button.text or "").strip().lower():
                    self.driver.execute_script("arguments[0].click();", button)
                    return True
        except Exception:                                  # noqa: BLE001
            pass
        return False

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass


class _TransportError(RuntimeError):
    """A transport-level failure, already masked."""


class RemoteBrowserError(RuntimeError):
    """The remote-browser path is unavailable from this driver."""


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------


def _launch_local(pw, args, pool: Optional[ProxyPool]) -> _BrowserSession:
    """A local Chromium, optionally behind one exit from the pool."""
    from proxy_pool import split_credentials

    proxy_url = pool.current if pool else (args.proxy or None)
    options = ChromeOptions()
    # Chrome's performance log is how this engine captures the one header
    # that gates the Ad Library — Selenium has no request-event API, so it
    # reads `Network.requestWillBeSent` out of the log instead. Without
    # this capability the log is empty and the token mint fails with a
    # message about the Search button, which is the wrong place to look.
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    if args.headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1366,900")

    if proxy_url:
        host_only, username, _password = split_credentials(proxy_url)
        if username:
            # Said out loud rather than silently dropped. Selenium cannot
            # authenticate a proxy at all, and a user who passed a
            # `user:pass` URL must not be left believing it is doing
            # something (CLAUDE.md §6).
            logger.warning("Selenium cannot authenticate a proxy: the "
                           "credentials in --proxy have been STRIPPED and "
                           "only %s is in use. If this exit needs a "
                           "password, use the Playwright or pyppeteer "
                           "engine.", mask(proxy_url))
        options.add_argument(f"--proxy-server={host_only}")

    user_agent = None
    fingerprint = None
    if args.fingerprint:
        from fingerprint_client import get_fingerprint, fingerprint_user_agent
        fingerprint = get_fingerprint(args.twocaptcha_key, tags=args.fp_tags,
                                      country=args.fp_country)
        user_agent = fingerprint_user_agent(fingerprint)
    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")

    driver = webdriver.Chrome(options=options)
    if not user_agent:
        version = (driver.capabilities or {}).get("browserVersion", "")
        user_agent = _chrome_ua(version)
    if fingerprint is not None:
        _apply_fingerprint(driver, fingerprint, user_agent)
    return _BrowserSession(driver, proxy_url, "",
                           user_agent)


def _apply_fingerprint(driver, fingerprint, user_agent) -> None:
    """Give the identity everything the fingerprint states, not just a UA.

    `--user-agent=` on the command line is a BARE override: it changes
    `navigator.userAgent` and leaves `navigator.userAgentData` and the
    `Sec-CH-UA` header reporting the real browser. CLAUDE.md §24 measured
    that half-identity being refused where a complete one was served, so
    this engine applies the same set its twins do.

    Best effort throughout — a fingerprint is cover, and no run should die
    because cover was imperfect.
    """
    from fingerprint_client import (user_agent_metadata, accept_language,
                                    playwright_init_script)

    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": playwright_init_script(fingerprint)})
    except DriverError as exc:
        logger.warning("Could not install the fingerprint's init script: %s",
                       _mask_credentials(exc))

    metadata = user_agent_metadata(fingerprint)
    if not metadata:
        logger.warning("The fingerprint carried no brand list, so its client "
                       "hints are left alone: a HALF identity is worse than "
                       "none (CLAUDE.md §24).")
        return
    payload = {"userAgent": user_agent, "userAgentMetadata": metadata}
    language = accept_language(fingerprint)
    if language:
        payload["acceptLanguage"] = language
    platform = (fingerprint.get("navigator") or {}).get("platform")
    if platform:
        payload["platform"] = platform
    try:
        driver.execute_cdp_cmd("Network.setUserAgentOverride", payload)
        timezone = (fingerprint.get("intl") or {}).get("timeZone")
        if timezone:
            driver.execute_cdp_cmd("Emulation.setTimezoneOverride",
                                   {"timezoneId": timezone})
    except DriverError as exc:
        logger.warning("Could not apply the fingerprint's client hints (%s) — "
                       "the run continues, but navigator.userAgentData will "
                       "disagree with the user agent.",
                       _mask_credentials(exc))


def _connect_remote(pw, args) -> _BrowserSession:
    """Refused, with the reason — see this module's docstring.

    chromedriver's `debuggerAddress` takes a bare `host:port` and has
    nowhere to put a password, so the 2Captcha Scraping Browser endpoint —
    `ws://{login}-zone-…:{password}@cb.2captcha.com:9222` — cannot be used
    from here. Reporting that plainly is the whole point: the alternative
    is an auth failure several steps away from its cause.
    """
    raise RemoteBrowserError(
        "--cdp-endpoint is not usable from the Selenium engine: "
        "chromedriver's debuggerAddress takes a bare host:port and cannot "
        "carry the endpoint's credentials. Use playwright_scraper.py or "
        "puppeteer_scraper.py for the Scraping Browser API.")


def _open_http(args, pool: Optional[ProxyPool]) -> HttpSession:
    """Refused, and the reason is the whole architecture of this repo.

    The search endpoint answers HTTP 421 to any request without a valid
    `x-ccl-str`, and that header is generated by the page's own
    JavaScript — there is no global to read it from, and a plain `fetch`
    issued from inside the page gets 421 too, because the app attaches it
    in its own HTTP client. Measured 2026-09-22.

    So a browser is a PREREQUISITE here, not a fallback. Its two siblings
    are the other way round: their pages are server-rendered and the
    browser is what `--transport auto` reaches for when the site
    challenges. Saying which way round it is, per route, is CLAUDE.md §21.
    """
    raise NotAnAdsQuery(
        "--transport http cannot work against TikTok's Ad Library: the "
        "search endpoint answers HTTP 421 without an `x-ccl-str` header "
        "that only the page's own JavaScript can mint. Use the default "
        "(--transport browser); the browser mints one token and the "
        "requests after it are cheap.")


def _open_session(pw, args, pool: Optional[ProxyPool]):
    if getattr(args, "transport", "browser") == "http":
        return _open_http(args, pool)
    session = (_connect_remote(pw, args) if args.cdp_endpoint
               else _launch_local(pw, args, pool))
    if args.proxy_rotate == "per-run" or not pool:
        logger.info("Browser up%s", f" via {mask(session.proxy_url)}"
                    if session.proxy_url else "")
    return session


# ---------------------------------------------------------------------------
# Minting the one header that gates the endpoint
# ---------------------------------------------------------------------------


def _prime_session(session, args, url: str) -> Optional[int]:
    """Open the Ad Library, let it search once, and keep its token.

    This is the whole reason a browser is here. The app attaches
    `x-ccl-str` to its own requests from inside its HTTP client, so the
    only way to get one is to let the app make a request and watch.

    The token is REUSABLE — verified by replaying a captured one through
    curl and getting HTTP 200 with twelve ads — so one mint serves a whole
    run, and a 421 later in the run means it went stale, which is its own
    state (`token_rejected`) and is cured by minting another rather than
    by rotating an exit.
    """
    status = None
    try:
        status = session.goto(url)
    except DriverError as exc:
        failure = _proxy_failure(exc)
        if failure:
            raise
        logger.warning("Could not open %s (%s)", url, _mask_credentials(exc))

    # Wait for the page to have something to click, through the shared
    # primitive rather than a fixed sleep: a slow load then costs what it
    # costs instead of the run pressing a button that is not there yet.
    page_flow.wait_for_count(session.count_selector,
                             page_flow.ready_selector(args.mode),
                             page_flow.min_matches(args.mode),
                             timeout_ms=page_flow.content_timeout_ms(args.mode))

    token = session.capture_search_token(parser.SEARCH_PATH, TOKEN_HEADER)
    if not token:
        raise _TransportError(
            f"could not mint an `{TOKEN_HEADER}` token from {url}. The Ad "
            "Library's own search has to run once for the app to produce "
            "one; if the page did not load or its Search button moved, "
            "that is what to look at. Re-run with --dump-html to see what "
            "arrived.")
    session.search_token = token
    logger.info("Minted an %s token (%d chars) — the run reuses it.",
                TOKEN_HEADER, len(token))
    return status


def handle_captcha_if_present(session, args, budget: SolveBudget) -> bool:
    """Route to the browser handler, or say why there is nothing to do.

    The annotation on this used to promise a `_BrowserSession`, which
    stopped being true the moment a transport without a page existed. On
    the HTTP transport there is no document to inject a token into and no
    DOM to detect a widget in, so this returns False and says so ONCE per
    run rather than per page — a warning repeated forty times is a warning
    nobody reads.
    """
    if isinstance(session, HttpSession):
        if args.solve_captcha != "never" and not getattr(
                args, "_http_solve_warned", False):
            args._http_solve_warned = True
            logger.warning("A challenge cannot be solved on the HTTP "
                           "transport: there is no page to inject a token "
                           "into. --transport auto (the default) starts a "
                           "browser when the site refuses.")
        return False
    return _handle_captcha_in_browser(session, args, budget)


def _handle_captcha_in_browser(session, args,
                               budget: SolveBudget) -> bool:
    """Detect and, if it is worth paying for, solve a challenge.

    Both call sites — before classification and after — go through the same
    `SolveBudget`, which is the CLAUDE.md §23 fix: `SOLVES_PER_PAGE` read
    like an enforced limit in every repo in this family and was not one,
    because only the second of the two calls was counted. One page bought
    three solves on a site where a challenge rendered on every fetch.

    A missing key or a solver error is a WARNING and the run continues
    (CLAUDE.md §8): detection is not the same as blocking, and a run that
    already has data must not die because a solve failed.
    """
    if args.solve_captcha == "never":
        return False
    html = session.content()
    if not html:
        return False
    static = detect_recaptcha_v3(html, session.url)
    live = None
    try:
        live = detect_recaptcha_in_page(session.evaluate, session.url)
    except Exception:                              # noqa: BLE001
        live = None
    challenge = reconcile_detections(static, live)
    if challenge is None:
        return False
    if not budget.may_spend():
        logger.warning("A challenge is present and this page's solve budget "
                       "(%d) is already spent — not paying twice for one "
                       "page.", budget.limit)
        return False
    if not args.twocaptcha_key:
        logger.warning("A captcha is present and no --twocaptcha-key was "
                       "given; continuing unsolved. The run reports exit 3 "
                       "if it really was blocked.")
        return False
    if not budget.spend():
        return False
    if args.cdp_endpoint:
        # The token is MINTED over plain HTTPS from this machine and then
        # installed into a browser that is somewhere else entirely. A
        # Scraping Browser endpoint carries a `country-` segment, so the
        # solve can be issued on one continent and replayed from another —
        # and a token a challenge issuer binds to the solving address is
        # then worthless on arrival. Said out loud rather than left to be
        # discovered from a bill: nothing here can fix it, and the remedy
        # is the endpoint's own auto-solve (`Captcha.setAutoSolve`), which
        # runs where the browser is.
        logger.warning("Solving over --cdp-endpoint mints the token from "
                       "THIS machine and installs it into a remote browser, "
                       "so it may be issued on a different exit than the one "
                       "that will use it. If the token is refused, that is "
                       "the likeliest reason.")
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                min_score=args.min_score,
                                api_version=args.captcha_api)
    except CaptchaUnsolvable as exc:
        logger.warning("Captcha not solved: %s", _mask_credentials(exc))
        return False
    except Exception as exc:                      # noqa: BLE001
        logger.warning("Captcha solver failed: %s", _mask_credentials(exc))
        return False
    if session.evaluate(INJECT_TOKEN_JS, token) is None:
        logger.warning("Could not inject the solved token into the page.")
        return False
    logger.info("Captcha solved and token injected.")
    return True


# ---------------------------------------------------------------------------
# One InnerTube call, with the family's retry / block policy around it
# ---------------------------------------------------------------------------


def _dump(args, name: str, payload: Any) -> None:
    """Write the exact bytes a call returned.

    On SUCCESS too, not only on failure (CLAUDE.md §9): a run can return
    the right count with a field silently unpopulated, and then the exact
    payload is the only way to tell a parsing bug from a too-early
    snapshot.
    """
    if not args.dump_html:
        return
    path = f"{args.out}_{name}.json"
    try:
        with open(path, "w", encoding="utf-8") as handle:
            if isinstance(payload, (dict, list)):
                json.dump(payload, handle, ensure_ascii=False)
            else:
                handle.write(str(payload))
        logger.info("Wrote %s", path)
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)


def _call(session, args, url: str, body: Dict[str, Any], budget: SolveBudget,
          label: str) -> Tuple[Optional[int], Any, str]:
    """One search POST, with the minted token, and classify the answer."""
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/plain, */*",
        "referer": parser.UI_URL,
        TOKEN_HEADER: getattr(session, "search_token", "") or "",
    }
    status, payload = session.post_json(url, headers, body)
    state = detect_page_state(payload, status, url)
    logger.debug("%s -> http %s, state %s", label, status, state)
    return status, payload, state


def _fetch_with_policy(session_box: Dict[str, Any], pw, args,
                       pool: Optional[ProxyPool], url: str,
                       body: Dict[str, Any], label: str
                       ) -> Tuple[Optional[int], Any, str, bool]:
    """One call plus the retry / rotate / solve policy around it.

    `session_box` holds the live session so a rotation can replace it: a
    rotation is a fresh browser, never a proxy swapped under a live
    session (CLAUDE.md §8).
    """
    budget = SolveBudget()
    attempts = max(1, int(args.retries) + 1)
    blocked_seen = False
    status = payload = None
    state = STATE_UNKNOWN

    for attempt in range(1, attempts + 1):
        session = session_box["session"]
        # First of the two solve call sites: clear a challenge BEFORE the
        # answer is judged, so a gated page is not classified on its
        # interstitial.
        if args.solve_captcha == "always":
            handle_captcha_if_present(session, args, budget)
        try:
            status, payload, state = _call(session, args, url, body,
                                           budget, label)
        except (_TransportError, TransportError) as exc:
            state = parser.STATE_ERROR
            payload = str(exc)
            status = None
            exit_failed = _proxy_failure(exc)
            if exit_failed:
                # A dead proxy is NOT a timeout, and the two want opposite
                # responses: a timeout deserves another try at the SAME
                # exit, a dead proxy a DIFFERENT one. Chromium reports it
                # as a generic error rather than as a timeout, which is how
                # this escaped as a traceback in a sibling repo
                # (CLAUDE.md §8).
                logger.warning("%s failed at the EXIT, not at the site: %s "
                               "via %s. Rotating rather than retrying the "
                               "same address.", label, exit_failed,
                               mask(session.proxy_url))
                if pool:
                    pool.advance(exit_failed)
                    session_box["session"].close()
                    session_box["session"] = _open_session(pw, args, pool)
                    _prime_session(session_box["session"], args,
                                   session_box["prime_url"])
            else:
                logger.warning("%s failed after %d attempt(s): %s",
                               label, attempt, exc)

        if state == STATE_TOKEN_REJECTED:
            # HTTP 421. The remedy is a fresh token, not a fresh exit —
            # so the session is re-primed in place rather than rotated,
            # and this is NOT counted as a block.
            logger.info("%s: the %s token was rejected; minting another.",
                        label, TOKEN_HEADER)
            try:
                _prime_session(session_box["session"], args,
                               session_box["prime_url"])
            except Exception as exc:                       # noqa: BLE001
                logger.error("could not mint a fresh token: %s",
                             _mask_credentials(exc))
                break

        if page_flow.counts_as_blocked(state):
            blocked_seen = True
            # `auto` means HTTP until the site says otherwise, and this is
            # otherwise. An HTTP client has nowhere to put a solved token,
            # no cookie jar a challenge issuer will accept and no DOM to
            # find a widget in, so the only useful response to a refusal is
            # to stop being an HTTP client.
            #
            # Once per run, and then never again: a site that challenged
            # once will challenge again, and flapping between transports
            # would pay the browser's start-up cost on every page while
            # looking like it was trying something new.
            if (getattr(args, "transport", "auto") == "auto"
                    and isinstance(session_box["session"], HttpSession)):
                logger.warning("%s was refused over plain HTTP (%s) — "
                               "starting a browser and retrying. This is "
                               "what --transport auto is for, and it happens "
                               "once per run.", label, state)
                session_box["session"].close()
                args.transport = "browser"
                session_box["session"] = _open_session(pw, args, pool)
                _prime_session(session_box["session"], args,
                               session_box["prime_url"])
                continue
            # Second call site, same budget.
            if page_flow.should_solve(state):
                handle_captcha_if_present(session, args, budget)

        if not page_flow.should_retry(state) or attempt >= attempts:
            break
        if page_flow.counts_as_blocked(state):
            if not page_flow.RETRY_ON_BLOCKED:
                break
            budget_left = (args.proxy_block_retries if pool
                           else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
            if attempt > budget_left:
                break
            if pool and pool.rotates_per_page():
                pool.advance(f"state {state}")
                logger.info("Rotating exit and rebuilding the browser — a "
                            "rotation is a fresh browser, never a proxy "
                            "swapped under a live session.")
                session_box["session"].close()
                session_box["session"] = _open_session(pw, args, pool)
                _prime_session(session_box["session"], args,
                               session_box["prime_url"])
        logger.info("%s: state %s, retrying (%d/%d) in %.1fs",
                    label, state, attempt, attempts - 1, args.retry_delay)
        time.sleep(args.retry_delay)

    return status, payload, state, blocked_seen


# ---------------------------------------------------------------------------
# --mode comments
# ---------------------------------------------------------------------------


def _rotate_if_per_page(session_box, pw, args, pool, why: str) -> bool:
    """Take a new exit between pages, when `--proxy-rotate per-page` asked.

    This is what that mode NAMES and, before this, not what it did:
    `pool.advance()` was reached only from a dead exit or a refusal, so a
    run whose pages all succeeded stayed on one address for its whole
    life. The flag read like a traffic-spreading control and was a
    recovery control — a setting that looks configurable and is not
    (CLAUDE.md §3 says that about `.env`; it is the same defect here).

    A rotation is a FRESH BROWSER (CLAUDE.md §8): cookies a bot manager
    issued against exit A and replayed from exit B are a stronger signal
    than either address alone, so the session is torn down and rebuilt
    rather than having its proxy swapped underneath it.

    Safe to do mid-chain on this site, and that is measured rather than
    assumed: a continuation token fetched by one client was replayed
    successfully by a bare HTTP client with no cookies at all, so the
    token is not bound to the session that received it.
    """
    if not pool or not pool.rotates_per_page() or len(pool) < 2:
        return False
    pool.advance(why)
    session_box["session"].close()
    session_box["session"] = _open_session(pw, args, pool)
    _prime_session(session_box["session"], args, session_box["prime_url"])
    return True


def _worker_pool(pool: Optional[ProxyPool], worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Each worker gets its OWN pool object holding the same exits rotated to
    a different offset, so workers start on distinct addresses and no
    thread needs a lock — the concurrency is safe by construction rather
    than by discipline (CLAUDE.md §7).
    """
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


# ---------------------------------------------------------------------------
# --mode profile
# ---------------------------------------------------------------------------




def _targets(args) -> List[str]:
    """`--url` (or `--region`) to a list of region codes, refusing bad ones."""
    raw = str(args.region or args.url or "")
    out, seen = [], set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        code = normalise_region(part)
        if code in seen:
            logger.info("Region %s named twice; fetching it once.", code)
            continue
        seen.add(code)
        out.append(code)
    if not out:
        raise NotAnAdsQuery("--region named no regions")
    return out


def _window(args) -> Tuple[int, int]:
    """The start/end window, in unix seconds.

    Defaults to the last 30 days, which is what the library's own UI
    defaults to. Stated rather than left implicit because the window is
    part of the QUESTION: two runs over different windows are different
    samples, not a change.
    """
    end = int(time.time())
    start = end - int(args.days) * 86400
    return start, end


def _run_ads(session_box, pw, args, pool) -> Tuple[List[Any], Dict[str, Any]]:
    regions = _targets(args)
    start_time, end_time = _window(args)
    scraped_at = utc_now()

    rows: List[Any] = []
    seen: set = set()
    failed: List[int] = []
    blocked = False
    pages_fetched = 0
    # Adjacent cursor pages DO overlap on this endpoint: a three-page run
    # of one region returned 36 ads of which 33 were distinct, measured
    # 2026-09-22. The listing is a live feed ordered by creation time, so
    # an ad created between two requests shifts the window — the same
    # reason a sibling repo logs its dedupe drops rather than absorbing
    # them silently. Counted here so a reader can see it.
    overlap = 0
    per_region: Dict[str, int] = {}
    totals: Dict[str, Optional[int]] = {}
    empty: List[str] = []
    stopped_early: Dict[str, str] = {}

    for index, region in enumerate(regions, start=1):
        if index > 1 and args.delay:
            time.sleep(args.delay)
        if index > 1 and pool and pool.rotates_per_page():
            _rotate_if_per_page(session_box, pw, args, pool, f"before {region}")

        url = search_url(region, start_time, end_time, args.ad_type)
        cursor = ""
        got_here = 0
        last_cursor_position = -1

        # Through the shared policy rather than recomputed here. The
        # library states no page count of its own — it states a TOTAL,
        # which at twelve per request is not a page count anyone should
        # divide — so `pages_available` is None and the cap is simply
        # what the caller asked for.
        planned = page_flow.pages_to_plan(int(args.pages), None)
        for page in range(1, planned + 1):
            if page > 1 and args.delay:
                time.sleep(args.delay)
            body = search_body(args.query, cursor)
            status, payload, state, was_blocked = _fetch_with_policy(
                session_box, pw, args, pool, url, body,
                f"{region} page {page}")
            blocked = blocked or was_blocked
            _dump(args, f"ads_{region}_p{page}", payload)

            if not page_flow.should_parse(state):
                if state == parser.STATE_NO_ADS:
                    # A real answer: the query matched nothing.
                    stopped_early[region] = "no_ads"
                    break
                if state == parser.STATE_TOKEN_REJECTED:
                    logger.error("%s page %d: HTTP 421 — the %s token was "
                                 "missing or stale. A retry mints a fresh "
                                 "one; rotating exits will not help.",
                                 region, page, TOKEN_HEADER)
                failed.append(index)
                stopped_early[region] = state
                break

            try:
                page_rows, diag = parse_search(payload, region, scraped_at,
                                               page, Video)
            except PayloadError as exc:
                logger.error("%s page %d was served and did not parse: %s",
                             region, page, exc)
                failed.append(index)
                stopped_early[region] = "parse_error"
                break

            totals.setdefault(region, diag.get("total"))
            if not page_rows:
                stopped_early[region] = "no_ads"
                break

            # On `row_key`, not `sku`: the same ad id legitimately
            # appears under several regions, and deduping across them
            # collapsed France's twelve ads to two in testing.
            fresh = dedupe_by_key(page_rows, seen, key="row_key")
            overlap += len(page_rows) - len(fresh)
            pages_fetched += 1
            rows.extend(fresh)
            got_here += len(fresh)

            # The END OF LISTING is read from the site's own cursor, never
            # from `has_more` alone and never from the row count.
            # CLAUDE.md §23 and §24: on three sites now, the response has
            # stated what it actually served, and that statement is the
            # only unambiguous signal.
            cursor = diag.get("cursor") or ""
            decoded = diag.get("cursor_decoded") or {}
            position = decoded.get("next_cursor")
            if not cursor:
                stopped_early[region] = "cursor_exhausted"
                break
            if isinstance(position, int):
                if position <= last_cursor_position:
                    # The cursor stopped advancing while `has_more` still
                    # says True. That is the listing ending, and trusting
                    # `has_more` here would loop on one page forever.
                    logger.info("%s: the cursor stopped advancing at %d — "
                                "end of listing, whatever has_more says.",
                                region, position)
                    stopped_early[region] = "cursor_stalled"
                    break
                last_cursor_position = position
            if not diag.get("has_more"):
                stopped_early[region] = "has_more_false"
                break

        per_region[region] = got_here
        if got_here == 0 and region not in [r for r in failed]:
            empty.append(region)

    if blocked:
        stop_reason = "blocked"
    elif failed:
        stop_reason = "page_failed"
    elif rows:
        stop_reason = "completed"
    else:
        stop_reason = "no_ads_found"

    meta = {
        "stop_reason": stop_reason,
        # A "page" on this route is one CURSOR PAGE, not one region — a
        # run of three regions at three pages each asks for nine.
        "pages_completed": pages_fetched,
        "pages_failed": failed or None,
        "blocked": blocked,
        "regions_requested": regions,
        "pages_per_region_requested": int(args.pages),
        "ads_seen_twice": overlap or None,
        "regions_without_ads": empty or None,
        "ads_per_region": per_region or None,
        "why_each_region_stopped": stopped_early or None,
        # CLAUDE.md §21: "complete" and "exhaustive" are different words,
        # and here the gap is enormous — the library reports millions of
        # ads per region and serves twelve at a time. A run that says only
        # "complete" is lying by omission.
        "site_total_per_region": totals or None,
        "ads_per_page": parser.ADS_PER_PAGE,
        "window_days": int(args.days),
        "window_start": start_time,
        "window_end": end_time,
        "sample_share_pct": {
            r: page_flow.sample_share(per_region.get(r, 0), totals.get(r))
            for r in regions if totals.get(r)
        } or None,
    }
    return rows, meta


_RUNNERS = {"ads": _run_ads}


class _driver_context:
    """The driver's own lifetime, as a context manager.

    Playwright needs one (`sync_playwright()`); Selenium and pyppeteer do
    not, and theirs is a no-op holding the same shape. Keeping it here means
    `scrape()` and the worker loop are identical in all three files.
    """

    def __enter__(self):
        return None            # Selenium needs no driver-level handle

    def __exit__(self, *exc):
        return False


def scrape(args) -> int:
    pool = proxy_pool_from_args(args)
    regions = _targets(args)
    prime_region = regions[0]
    if args.concurrency > 1:
        # The decision is the shared policy's, not this engine's: it
        # answers False for this route because the cursor for page 2
        # arrives inside page 1's response.
        if not page_flow.pagination_is_addressable(prime_region, args.mode):
            logger.info("--concurrency is 1 on this route: the Ad Library "
                        "paginates by a cursor the previous response hands "
                        "back, so page N is unknowable until page N-1 has "
                        "been read.")
        args.concurrency = page_flow.concurrency_for_mode(args.mode,
                                                          args.concurrency)
    limit = page_flow.concurrency_limit(args.cdp_endpoint)
    if limit and args.concurrency > limit:
        args.concurrency = limit

    prime_region = regions[0]
    prime_url = f"{parser.UI_URL}?region={prime_region}"

    rows: List[Any] = []
    meta: Dict[str, Any] = {}
    with _driver_context() as pw:
        session_box = {"session": _open_session(pw, args, pool),
                       "prime_url": prime_url}
        try:
            _prime_session(session_box["session"], args, prime_url)
            rows, meta = _RUNNERS[args.mode](session_box, pw, args, pool)
        finally:
            session_box["session"].close()

    extra = {k: v for k, v in meta.items()
             if k not in ("stop_reason", "pages_completed", "pages_failed",
                          "blocked")}
    extra["engine"] = ENGINE_NAME
    extra["category"] = args.category

    # CLAUDE.md §21: complete and exhaustive are different words, and on
    # this route the gap is the largest in the family. The library reports
    # millions of ads per region and serves twelve at a time.
    totals = meta.get("site_total_per_region") or {}
    shares = meta.get("sample_share_pct") or {}
    for region, total in totals.items():
        got = (meta.get("ads_per_region") or {}).get(region, 0)
        share = shares.get(region)
        logger.info("%s: collected %s of the %s ads the library reports "
                    "(%s%%). A run that fetched every page it asked for is "
                    "COMPLETE; it is not exhaustive.",
                    region, f"{got:,}", f"{total:,}",
                    f"{share:.6f}" if share is not None else "?")

    overlap = meta.get("ads_seen_twice")
    if overlap:
        logger.info("%d ad(s) arrived on more than one cursor page and were "
                    "kept once. The library is a live feed ordered by "
                    "creation time, so a new ad between two requests shifts "
                    "the window — this is the site working, not a fault.",
                    overlap)

    stopped = meta.get("why_each_region_stopped") or {}
    if stopped:
        logger.info("why each region stopped: %s", stopped)

    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=bool(meta.get("blocked")),
        stop_reason=meta.get("stop_reason", "completed"),
        pages_requested=len(regions) * int(args.pages),
        pages_completed=int(meta.get("pages_completed") or 0),
        pages_failed=meta.get("pages_failed") or None,
        start_url=prime_url, final_url=prime_url,
        mode=args.mode, source=SOURCE_DEFAULT, extra=extra)


def parse_args(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(
        description="Scrape TikTok's EU Ad Library — advertisers, creatives "
                    "and the dates an ad ran, from the transparency "
                    "endpoint TikTok serves without a key or an account.")
    p.add_argument("--url", default=None,
                   help="A region code, or a comma-separated list of them. "
                        "Kept under the family's flag name; --region is the "
                        "same thing and reads better. Falls back to "
                        "TIKTOK_URL.")
    p.add_argument("--region", default=None,
                   help="One of the 33 regions the library serves: "
                        + ", ".join(parser.KNOWN_REGIONS) + ". The library "
                        "is an EU/EEA transparency obligation, so US, BR "
                        "and the rest of the world are not covered and are "
                        "refused with that reason rather than returning "
                        "nothing.")
    p.add_argument("--mode", choices=MODES, default=DEFAULT_MODE,
                   help="ads (default, and the only one).")
    p.add_argument("--query", default="",
                   help="Free-text advertiser search. Empty (the default) "
                        "returns everything in the window, newest first.")
    p.add_argument("--days", type=int, default=30,
                   help="How far back the window reaches, in days. Default "
                        "30, which is what the library's own UI defaults "
                        "to. The window is part of the QUESTION: two runs "
                        "over different windows are different samples, not "
                        "a change, so it is recorded in the sidecar.")
    p.add_argument("--ad-type", type=int, default=1,
                   help="TikTok's own `type` parameter. Measured 2026-09-22: "
                        "1, 2 and 3 all return ads and all return the same "
                        "total, so this is passed through rather than "
                        "mapped to words this repo would have to invent.")
    p.add_argument("--pages", type=int, default=3,
                   help="Cursor pages per region, %d ads each (a measured "
                        "ceiling — asking for 50 or 100 still returns %d)."
                        % (parser.ADS_PER_PAGE, parser.ADS_PER_PAGE))
    p.add_argument("--category", default=None,
                   help="Label to tag the run with in the sidecar. Defaults "
                        "to the first region.")
    p.add_argument("--locale", default="en",
                   help="Kept for the family's flag contract. The library's "
                        "API returns advertiser names as the advertiser "
                        "wrote them; this does not translate a row.")
    p.add_argument("--format", choices=("json", "csv", "both"), default="json")
    p.add_argument("--out", default="tiktok_ads", help="Output file prefix.")
    p.add_argument("--delay", type=float, default=0.0,
                   help="Seconds between requests.")
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--retry-delay", type=float, default=2.0)
    p.add_argument("--concurrency", type=int, default=1,
                   help="Forced to 1. The library paginates by a cursor the "
                        "previous response hands back, so a second worker "
                        "would have no request to make.")
    p.add_argument("--proxy", default=None,
                   help="One proxy URL. Credentials go through the driver's "
                        "own fields, never onto a command line.")
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-rotate", choices=ROTATE_MODES, default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2)
    p.add_argument("--twocaptcha-key", default=None,
                   help="2Captcha API key. Also read from TWOCAPTCHA_KEY. "
                        "Not needed for this route.")
    p.add_argument("--captcha-api", choices=("v1", "v2"), default="v2")
    p.add_argument("--solve-captcha", choices=("never", "when-blocked", "always"),
                   default="when-blocked",
                   help="No challenge has been observed on the Ad Library "
                        "from the addresses this repo was built on, so this "
                        "path is readiness rather than routine.")
    p.add_argument("--min-score", type=float, default=0.3)
    p.add_argument("--transport", choices=("auto", "http", "browser"),
                   default="browser",
                   help="browser (the default, and effectively the only "
                        "one). The search endpoint answers HTTP 421 without "
                        "an `x-ccl-str` header that only the page's own "
                        "JavaScript mints, so `http` is refused with that "
                        "reason rather than failing later with a 421 nobody "
                        "can interpret. This is the reverse of the sibling "
                        "repos, whose pages are server-rendered.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="ws:// endpoint of the 2Captcha Scraping Browser API. "
                        "Also read from TIKTOK_CDP_ENDPOINT.")
    p.add_argument("--fingerprint", action="store_true")
    p.add_argument("--fp-country", default=None)
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag. The API rejects a list, and "
                        "rejects 'Chrome' and 'Desktop' — measured.")
    p.add_argument("--dump-html", action="store_true",
                   help="Write the exact payloads a run received, on success "
                        "too.")
    p.add_argument("--allow-empty", action="store_true")
    headless = p.add_mutually_exclusive_group()
    headless.add_argument("--headless", dest="headless", action="store_true",
                          default=True)
    headless.add_argument("--headful", dest="headless", action="store_false")

    args = p.parse_args(argv)
    env_config.apply(args)

    if not (args.region or args.url):
        p.error("no --region given, and TIKTOK_URL is not set in the "
                "environment or .env.")
    try:
        regions = _targets(args)
    except NotAnAdsQuery as exc:
        p.error(str(exc))

    if args.transport == "http":
        p.error("--transport http cannot work here: the search endpoint "
                "answers HTTP 421 without an `x-ccl-str` header that only "
                "the page's own JavaScript can mint. Use the default.")
    if args.pages < 1:
        p.error("--pages must be at least 1.")
    if args.days < 1:
        p.error("--days must be at least 1.")
    if args.cdp_endpoint and (args.proxy or args.proxy_file):
        p.error("--cdp-endpoint already proxies; attaching --proxy stacks a "
                "second exit and creates a mismatch rather than better cover.")
    if args.category is None:
        args.category = regions[0]
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint "
                     "API uses the same key, though it is a separate "
                     "subscription from solving).")
        sys.exit(2)
    try:
        sys.exit(scrape(args))
    except NotAnAdsQuery as exc:
        logger.error("%s", exc)
        sys.exit(2)
    except ProxyError as exc:
        logger.error("%s", exc)
        sys.exit(2)
