"""
youtube-scraper — 2captcha Scraper API edition (fourth engine)

Fetches one page through the 2captcha Scraper API, which renders it on
2captcha's infrastructure and returns the HTML over plain HTTPS.

What this path can and cannot do HERE, measured
-----------------------------------------------
This is the one engine in this repo that cannot read comments, and the
reason is a property of the SITE rather than of the service — so it is
stated with the measurement rather than as an opinion (CLAUDE.md §19).

YouTube puts no comments in its HTML. The document ships a placeholder and
the browser then asks for them over a POST to `/youtubei/v1/next`. A
service that fetches a URL and returns the rendered document therefore
returns a page whose comment section is still empty. Measured 2026-09-21
against `/watch?v=dQw4w9WgXcQ`:

    no waitFor                      HTTP 200,  1,354,420 bytes,  0 comments
    waitFor {"state":"networkidle"} HTTP 200,  1,386,808 bytes,  0 comments
    waitFor {"element":"ytd-comment-thread-renderer","checkVisible":true}
                                    HTTP 408 timeout — it never appears
    waitFor {"text":"Top comments"} HTTP 408 timeout

Control, per CLAUDE.md §20: a deliberately wrong key answered HTTP 401 in
0.1 s where the real one answered 200 in 6.7 s, so the endpoint is
evaluating credentials and the 200s above are real work.

So `--mode video` is what this client does, and it does it well: the watch
document carries the video's whole metadata inline, which is exactly the
shape a rendered-HTML service is good for. For comments, use one of the
three browser engines — or nothing at all, because the endpoint they call
is ungated and free.

And the honest sentence about when to pay for this at all: on this site,
not for access. Measured the same day from a bare Finnish datacentre
address with no key and no proxy, 60 consecutive InnerTube pages returned
1,200 comments with no refusal of any kind. What this client buys is
somebody else's browser infrastructure and a specific exit country.

    python3 scraper_api_client.py --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    # TWOCAPTCHA_KEY and YOUTUBE_URL are read from .env, so neither needs
    # to be typed — a secret in argv is readable by anything that can run
    # `ps` (CLAUDE.md §3).
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from typing import Optional

import requests

from product_parser import normalise_region, NotAnAdsQuery
from tiktok_payload import PayloadError
from output_writer import (EXIT_NO_PRODUCTS, Video, finish_run, utc_now)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s and rejects bodies over 10,000 bytes.
MAX_API_TIMEOUT = 120

# Exit codes. Kept distinct from 2 (bad usage) on purpose: a remote API
# failing is not the operator passing wrong arguments, and a harness that
# lumps them together sends you looking in the wrong place. An early run
# reported `exit=2` for an HTTP 422 from the API — which reads as "you called
# it wrong".
#
# Imported rather than redefined: the browser engines return the same code for
# a Scraping Browser that will not accept a connection, and two definitions
# of one exit code is how a family's contract drifts.
from output_writer import EXIT_API_ERROR  # noqa: E402

def _mask_credentials(url: str) -> str:
    """Never print a username:password embedded in a ws://... or http://... URL."""
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme, rest = url[:scheme_sep + 3], url[scheme_sep + 3:]
    _, _, host_part = rest.partition("@")
    return f"{scheme}***:***@{host_part}"


# Credentials embedded ANYWHERE in a blob of text, not just in a string that
# is entirely a URL — and every occurrence, not the first. A masker that
# handles one occurrence prints the password the other four times and looks
# like it is working.
_CREDS_IN_TEXT_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s'\"@]+@", re.IGNORECASE)
# Same shape as captcha_solver's and fingerprint_client's. A third copy is
# one too many and they should be unified in a family pass; reaching into
# another module's private name to avoid it would be worse.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)


def _redact_debug_header(value: str) -> str:
    """The x-debug header, safe to log.

    SECURITY.md names this header as one of three places credentials reach a
    log unmasked, and it was logged verbatim: the API echoes back the task it
    ran, so a run driven through a credentialed CDP endpoint put that
    endpoint's username and password into the log, and a key passed as a
    query parameter would go the same way.

    Redaction rather than an allowlist of fields, deliberately: the header is
    the API's own metadata and its shape is not ours to pin, so an allowlist
    would silently drop the cost and timing figures this is logged FOR the
    first time the API adds a field.
    """
    return _KEY_IN_TEXT_RE.sub(r"\1=***",
                               _CREDS_IN_TEXT_RE.sub(r"\1***:***@", value))


def _build_wait_for(args) -> Optional[str]:
    """`waitFor` must be a JSON STRING (double-encoded), per the API docs.
    Passing a nested object is silently wrong.

    Default (no flag): wait for the DOM. On a challenge-protected page
    that resolves instantly against the challenge page itself — which is
    exactly the trap documented in this module's docstring, so
    --wait-text/--wait-element exist to wait on something only the real
    page can contain."""
    if args.wait_text:
        return json.dumps({"text": args.wait_text})
    if args.wait_element:
        return json.dumps({"element": args.wait_element, "checkVisible": True})
    if args.wait_state:
        return json.dumps({"state": args.wait_state})
    return None


def fetch_html(args) -> str:
    payload = {
        "task_type": "scrape",
        "url": args.url,
        "data_format": "raw",   # we want HTML; product_parser does the rest
        "format": "json",       # so we get {"status", "headers", "body"}
        "timeout": min(args.timeout, MAX_API_TIMEOUT),
    }

    wait_for = _build_wait_for(args)
    if wait_for:
        payload["waitFor"] = wait_for
        logger.info("waitFor: %s", wait_for)

    if args.cdp_url:
        payload["cdpurl"] = args.cdp_url
        logger.info("Routing through an existing browser session: %s",
                    _mask_credentials(args.cdp_url))

    logger.info("POST %s (url=%s)", SYNC_ENDPOINT, args.url)
    resp = requests.post(
        SYNC_ENDPOINT,
        headers={"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"},
        json=payload,
        # Give the HTTP call more headroom than the API-side task timeout,
        # otherwise a task that legitimately runs the full 120s looks like
        # a client-side network failure.
        timeout=min(args.timeout, MAX_API_TIMEOUT) + 30,
    )

    # The API returns its own per-task metadata (price, timings, status)
    # in an x-debug header — worth logging, it's the only place the real
    # cost of the call shows up.
    debug = resp.headers.get("x-debug")
    if debug:
        logger.info("x-debug: %s", _redact_debug_header(debug))

    if resp.status_code != 200:
        # 422 = task ran but errored (this is what a bad/unreachable
        # cdpurl produces: "CDP connect failed (user cdpurl) after N
        # attempts"); 402 = out of balance; 408 = sync wait exceeded.
        raise RuntimeError(
            f"Scraper API returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    html = body.get("body") or ""
    upstream_status = body.get("status")
    logger.info("Upstream page status %s, %d bytes of HTML.", upstream_status, len(html))
    # The STATUS is returned alongside the HTML, not thrown away. It used to
    # be, and that cost this engine the family's central distinction. On this
    # site a refusal carries no markup at all — nothing a challenge check
    # on it, so the challenge check below finds nothing and the run fell
    # through to "0 products" and exit 4. A pipeline branching on the exit
    # code then reads a block as an empty category. See detect_page_state,
    # which the three browser engines already reach through page_flow.
    return html, upstream_status


def main() -> int:
    args = parse_args()

    if not args.key:
        logger.error("No 2captcha API key. Pass --key, or better, export "
                     "TWOCAPTCHA_KEY.")
        return 2

    # A challenge page is not necessarily final, so a single attempt is not
    # evidence. Each retry is a fresh billable task, so the default is
    # deliberately low.
    attempts = max(1, args.retries + 1)
    rc = 1
    for attempt in range(1, attempts + 1):
        rc = _run_once(args, attempt, attempts)
        if rc != 3 or attempt == attempts:
            return rc
        logger.info("Challenge page on attempt %d/%d — retrying in %ds.",
                    attempt, attempts, args.retry_delay)
        time.sleep(args.retry_delay)
    return rc


def _run_once(args, attempt: int = 1, attempts: int = 1) -> int:
    """Refused, with the reason.

    The 2Captcha Scraper API fetches a URL and hands back what the site
    returned. TikTok's Ad Library does not answer a fetch: its search
    endpoint is a POST that needs an `x-ccl-str` header which only the
    library's own page JavaScript mints, and without it the endpoint
    answers HTTP 421 (measured 2026-09-22). A service that renders a URL
    cannot produce that header for a request it is not making.

    So this path is refused up front rather than shipped as a client that
    returns 421 forever. CLAUDE.md §19's rule about wording applies here
    too: this is "THIS REPO does not implement it", not "it cannot be
    done" — a Scraper API session that ran the library's own search and
    returned the request headers would work, and that is a feature
    request rather than a limitation.

    The three browser engines need no key and do this in one page load.
    """
    logger.error(
        "The Scraper API path is not implemented for TikTok's Ad Library. "
        "Its search endpoint is a POST gated by an `x-ccl-str` header that "
        "only the library's own page JavaScript mints; a service that "
        "fetches a URL cannot produce one, and the endpoint answers HTTP "
        "421 without it. Use any of the three browser engines — they need "
        "no key: `python3 playwright_scraper.py --region DE`.")
    return EXIT_API_ERROR


def parse_args():
    p = argparse.ArgumentParser(
        description="YouTube scraper — 2captcha Scraper API edition (no "
                    "local browser). Reads a video's METADATA from the "
                    "watch document. It cannot read comments: YouTube "
                    "renders none into its HTML and fetches them over a "
                    "POST this service does not make — measured, see the "
                    "module docstring. Use a browser engine for comments; "
                    "the endpoint they call needs no key at all.")
    # NOT required: prefer the TWOCAPTCHA_KEY env var. A key on the command
    # line is visible to anyone who can run `ps`, and it lands in shell
    # history and in any log that echoes the command line.
    p.add_argument("--key", default=os.environ.get("TWOCAPTCHA_KEY"),
                   help="2captcha.com API key (sent as a Bearer token). "
                        "Defaults to $TWOCAPTCHA_KEY, which is the safer "
                        "way to pass it.")
    p.add_argument("--url", default=None,
                   help="A YouTube video URL or a bare 11-character video "
                        "id. Required, unless YOUTUBE_URL is set in the "
                        "environment or .env.")
    p.add_argument("--mode", choices=("video",), default="video",
                   help="Only `video` exists on this path, and the reason "
                        "is measured rather than a limitation of the "
                        "service — see the module docstring.")
    p.add_argument("--category", default=None,
                   help="Label to tag the run with in the sidecar. Defaults "
                        "to the video id.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="youtube_video_scraperapi",
                   help="Output file prefix")
    p.add_argument("--timeout", type=int, default=60,
                   help=f"API-side task timeout in seconds "
                        f"(1-{MAX_API_TIMEOUT}, default 60)")
    p.add_argument("--cdp-url", default=None,
                   help="Route the fetch through an existing browser "
                        "session over CDP (sent as the API's `cdpurl` "
                        "param), e.g. ws://user:pass@host:port")
    wait = p.add_mutually_exclusive_group()
    wait.add_argument("--wait-text", default=None,
                      help="Wait until this string appears on the page. Note "
                           "that waiting for anything in the COMMENT section "
                           "times out on this site: it never renders here.")
    wait.add_argument("--wait-element", default=None,
                      help="Wait until this CSS selector is visible.")
    wait.add_argument("--wait-state", default=None,
                      choices=("load", "domcontentloaded", "networkidle"),
                      help="Wait for a page lifecycle state.")
    p.add_argument("--retries", type=int, default=1,
                   help="Retries when the response is a challenge page. Each "
                        "one is a fresh billable task.")
    p.add_argument("--retry-delay", type=int, default=5)
    p.add_argument("--dump-html", default=None,
                   help="Write the exact HTML the service returned.")
    p.add_argument("--allow-empty", action="store_true")

    args = p.parse_args()
    env_config.apply(args, keys={"TWOCAPTCHA_KEY": "key",
                                 "YOUTUBE_URL": "url"})
    if not args.url:
        p.error("no --url given, and YOUTUBE_URL is not set in the "
                "environment or .env.")
    try:
        normalise_region(args.region or args.url or "")
        ok, reason = True, ""
    except NotAnAdsQuery as exc:
        ok, reason = False, str(exc)
    if not ok:
        p.error(reason)
    if not args.url.startswith("http"):
        pass
    if args.category is None:
        args.category = args.region or args.url
    return args


if __name__ == "__main__":
    sys.exit(main())
