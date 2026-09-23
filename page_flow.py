"""page_flow.py — the retry / solve / blocked decision, as DATA.

Shared by all three browser engines and the HTTP path so they cannot
quietly disagree about whether a response is worth retrying, worth paying
a solver for, or worth reporting as a block. Three copies of that triage
drift, and the drift is silent: one engine reporting exit 3 where its twin
reports exit 0 on the same response (CLAUDE.md §1).

Policy and pure algorithms only. No JavaScript crosses this boundary —
Selenium's `execute_script` takes a function BODY with an explicit
`return` while Playwright and pyppeteer take `() => expr`, so a shared
module that carried a snippet would acquire one driver's dialect. What the
engines share here is a NAME for an operation and a decision about it.

TikTok answers a request six ways
---------------------------------
and five of them want a different response, which is why this module
exists on this site at all:

    content            a search response with ads in it
    no_ads             the query was answered and matched nothing
    token_rejected     HTTP 421 — the x-ccl-str header was missing or stale
    empty_success      HTTP 200, content-length 0 — a REFUSAL
    challenge          the slide-puzzle interstitial
    error              an HTTP status the site gave us
    parse_error        a page the site served that we failed to read — OUR bug

`empty_success` is the one a naive engine gets wrong, and it is the
defining shape of this site. Measured 2026-09-22 on the video-feed route
next door, every way it was asked — headless Chromium, headful Chromium, a
Windows user agent, after accepting the EU cookie consent, and through a
residential exit in Peru — TikTok answered:

    HTTP 200   content-type: application/json   content-length: 0

No status to key on, no body to parse, no marker to match. Folded into
"empty" it reads as a region with no ads and the run reports exit 0;
named separately it rotates an exit and reports exit 3. This repo's own
route has not produced it, and it is carried anyway, because the cost of
being wrong about it is a silent wrong answer.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from product_parser import (STATE_CHALLENGE, STATE_CONTENT,
                            STATE_EMPTY_SUCCESS, STATE_ERROR, STATE_NO_ADS,
                            STATE_PARSE_ERROR, STATE_TOKEN_REJECTED,
                            STATE_UNKNOWN, STATE_WAF_CHALLENGE,
                            detect_page_state)

# ---------------------------------------------------------------------------
# Readiness — for the browser engines only
# ---------------------------------------------------------------------------
#
# The browser engines do not read ads out of the DOM — they come from the
# search endpoint's JSON. The browser is there to let the Ad Library's own
# search run once, so its app mints the `x-ccl-str` token, and what the
# engines wait for is a page with a button on it to press (the Search
# button). That is a much weaker requirement than most repos in this family
# have, and stating it here keeps an engine from growing a tile-counting
# wait that measures the wrong thing.
#
# `body` is the floor that always matches, which is why the minimum is 1
# rather than the >1 CLAUDE.md §5 requires of a LISTING: nothing on this
# page is a grid to count.
READY_SELECTOR = 'button, body'
MIN_CARD_MATCHES = 1
CONTENT_TIMEOUT_MS = 30_000
READY_POLL_MS = 500


def ready_selector(mode: str = "ads") -> str:
    return READY_SELECTOR


def min_matches(mode: str = "ads") -> int:
    return MIN_CARD_MATCHES


def content_timeout_ms(mode: str = "ads") -> int:
    return CONTENT_TIMEOUT_MS


def wait_for_count(count: Callable[[str], int], selector: str, minimum: int,
                   timeout_ms: int = CONTENT_TIMEOUT_MS,
                   poll_ms: int = READY_POLL_MS) -> int:
    """Poll `count(selector)` until it reaches `minimum`, or time out.

    The caller passes a counting primitive rather than a snippet, and the
    primitive must be a protocol call — `querySelectorAll` through the
    driver — never an evaluated STRING. CLAUDE.md §18: a site whose
    Content-Security-Policy lacks `unsafe-eval` kills
    `wait_for_function`-style string evaluation. TikTok's CSP does carry
    `unsafe-eval` today (measured 2026-09-22), so this is insurance rather
    than a workaround — and it is also the only spelling all three drivers
    share, since Selenium takes a function BODY where the other two take
    `() => expr`.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    seen = 0
    while True:
        try:
            seen = count(selector)
        except Exception:
            seen = 0
        if seen >= minimum or time.time() >= deadline:
            return seen
        time.sleep(poll_ms / 1000.0)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(html, status: Optional[int] = None, url: str = "",
             mode: str = "ads") -> str:
    """Name what TikTok answered with. See product_parser.detect_page_state.

    The argument ORDER is the contract: every caller writes
    `classify(html, status, url)`. CLAUDE.md §17 records a sibling repo
    whose two engines called `classify(html, url=...)` against a callee
    taking `status` second, and both crashed on their FIRST fetch —
    invisible to import, `--help`, `compileall` and four hundred green
    offline assertions, because none of those calls a function the way a
    live run does. `smoke_test.py` binds every call site against this
    signature for exactly that reason.

    `status` is threaded through rather than dropped. It is the only
    signal a 503 or a 403 gives, and a classifier that never receives it
    has to guess from a body that may not exist — which, on this site, is
    literally the case: the refusal that matters here HAS no body.
    """
    return detect_page_state(html, status, url)


STATE_POLICY = {
    # A search response with ads in it.
    STATE_CONTENT: {"retry": False, "solve": False, "blocked": False,
                    "parse": True},
    # The query was answered and matched nothing. A real, complete answer
    # — EXIT_NO_PRODUCTS, never EXIT_BLOCKED. Retrying re-asks a question
    # the library has answered.
    STATE_NO_ADS: {"retry": False, "solve": False, "blocked": False,
                   "parse": False},
    # HTTP 421: the request arrived without a usable `x-ccl-str`. Its own
    # state because its remedy is its own — mint a fresh token, which the
    # engine does by re-priming. `blocked` is FALSE deliberately: rotating
    # an exit does nothing about a stale token, and counting it as blocked
    # would spend the proxy budget on the wrong problem and report exit 3
    # for something entirely under our control.
    STATE_TOKEN_REJECTED: {"retry": True, "solve": False, "blocked": False,
                           "parse": False},
    # HTTP 200 with a zero-length body — TikTok's silent refusal.
    #
    # `retry` True and `blocked` True: a different exit is the only thing
    # that has ever been worth trying against it. `solve` is False, and
    # that is the measured part rather than a default — this shape carries
    # no widget, no sitekey and no challenge page, so there is nothing for
    # a solver to solve and paying for one would be buying a request the
    # API will reject (CLAUDE.md §19: detected != paying).
    STATE_EMPTY_SUCCESS: {"retry": True, "solve": False, "blocked": True,
                          "parse": False},
    # The slide-puzzle interstitial. `solve` is False: this repo implements
    # no solver for ByteDance's puzzle, so a solve on this state would be a
    # promise with nothing behind it. `retry` and `blocked` are True: a
    # different profile is what has actually worked (tiktok-shop-scraper).
    STATE_CHALLENGE: {"retry": True, "solve": False, "blocked": True,
                      "parse": False},
    # TikTok's WAF interstitial: HTTP 200, 1,462 bytes, "Please wait...",
    # carrying a JavaScript challenge.
    #
    # `blocked` True is what makes `--transport auto` do the right thing:
    # the engines switch to a browser for any state that counts as
    # blocked, and a browser is the measured remedy — 3 of 3 cleared on
    # the very exits that refused a plain HTTP client.
    #
    # `solve` False, and that is measured rather than defaulted: the page
    # carries no widget, no sitekey and no captcha of any kind, so paying
    # a solver would buy a request the API cannot fulfil (CLAUDE.md §19:
    # detected != paying).
    STATE_WAF_CHALLENGE: {"retry": True, "solve": False, "blocked": True,
                          "parse": False},
    # An HTTP error that is not a recognised refusal — a 500, a gateway's
    # own page, a truncated body. A wait, not a spend.
    STATE_ERROR: {"retry": True, "solve": False, "blocked": False,
                  "parse": False},
    # A page the site plainly served, with its own assets all over it, that
    # this parser failed to read. OUR bug, and it gets its own name so it
    # cannot be reported as "no ads" — which would send the reader to
    # check the query instead of the parser (CLAUDE.md §20).
    # One retry in case a response was truncated, and always worth a dump.
    STATE_PARSE_ERROR: {"retry": True, "solve": False, "blocked": False,
                        "parse": False},
    # Neither the site nor a recognised refusal — a proxy's own response,
    # Chromium's network-error page (which carries the site's hostname in
    # its title and would fool a title check), an upstream error.
    STATE_UNKNOWN: {"retry": True, "solve": False, "blocked": False,
                    "parse": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["parse"]


# Whether a blocked page is worth re-fetching at all. CONSULTED by the
# engines rather than merely documented — setting it False really does stop
# the retry loop. (CLAUDE.md §17: a policy constant nothing reads is the
# same defect as dead code, and this family shipped one for months.)
RETRY_ON_BLOCKED = True

# Retries to spend on a refusal when there is no pool to rotate through.
# Without a pool every retry leaves from the same address that was just
# refused, so more than one is repetition rather than a second attempt.
BLOCK_RETRIES_WITHOUT_POOL = 1

# ---------------------------------------------------------------------------
# The solve budget
# ---------------------------------------------------------------------------
#
# One paid solve per page. CLAUDE.md §23 records that this constant has
# read like an enforced limit in every repo in this family and was not one:
# every engine calls the captcha handler TWICE per attempt — once before
# the response is classified, so a challenge is cleared before anything is
# judged, and once after, for the state that says the page really is gated
# — and only the second call was counted. Measured from an address where a
# challenge rendered on every fetch, one page bought THREE solves.
#
# So the budget is not a number engines are trusted to respect. It is a
# function both call sites go through, and `smoke_test.py` asserts that the
# number of call sites equals the number of guards equals the number of
# increments.
SOLVES_PER_PAGE = 1


class SolveBudget:
    """One page's paid-solve allowance, shared by both call sites.

    `spend()` returns True at most SOLVES_PER_PAGE times and counts the
    spend itself, so neither caller can forget to.
    """

    def __init__(self, limit: int = SOLVES_PER_PAGE):
        self.limit = max(0, int(limit))
        self.spent = 0

    def may_spend(self) -> bool:
        return self.spent < self.limit

    def spend(self) -> bool:
        if not self.may_spend():
            return False
        self.spent += 1
        return True

    def __repr__(self) -> str:                # pragma: no cover - debugging
        return f"SolveBudget(spent={self.spent}/{self.limit})"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def pagination_is_addressable(url: str = "", mode: str = "ads") -> bool:
    """Never, on this route, and the reason is a decoy.

    The Ad Library's request body carries an `offset`, the site's own UI
    increments it, and it does nothing: offsets 0, 12 and 24 returned the
    identical twelve ads in the identical order (measured 2026-09-22).
    Pages come from a `search_id` cursor the previous response hands back,
    so page N is unknowable until page N-1 has been read.

    CLAUDE.md §18 says to ask this per ROUTE rather than per site. The
    answer here is False, and it is False because of a parameter that
    LOOKS like the answer is True — which is why the measurement is in
    this docstring rather than the assumption.
    """
    return False


def pages_to_plan(pages_requested: int, pages_available: Optional[int]) -> int:
    """How many pages a run may ask for, given what is known to exist.

    Where a caller knows how many pages exist, capping at it matters:
    asking past the end is not an empty page, it is a request the site
    will answer with something else. The Ad Library states a TOTAL of ads
    per region, not a page count, and its cursor is what ends a run.
    """
    wanted = max(1, int(pages_requested or 1))
    if pages_available and pages_available > 0:
        return min(wanted, int(pages_available))
    return wanted


def concurrency_limit(cdp_endpoint: Optional[str]) -> Optional[int]:
    """1 when workers would collide, else None for "no limit imposed here".

    The Scraping Browser API allows ONE live connection per profile, so N
    workers sharing a `pid` collide with `profile_locked`. Several `pid`s,
    one run each, is the way to parallelise that path (CLAUDE.md §7).
    """
    return 1 if cdp_endpoint else None


def concurrency_for_mode(mode: str, concurrency: int) -> int:
    """One worker, always.

    A second worker would have no request to make: the cursor for page 2
    arrives inside page 1's response. Clamping here rather than starting
    idle threads keeps the run's own log honest about what it did.
    """
    return 1


def sample_share(collected: int, total: Optional[int]) -> Optional[float]:
    """What fraction of a region's ads a run actually holds.

    CLAUDE.md §21: "complete" and "exhaustive" are different words, and on
    THIS route the gap is the largest in the family. TikTok's Ad Library
    reports 17,343,646 ads for Germany alone and serves TWELVE per
    request, so a thorough hundred-page run holds 1,200 of them —
    0.0069%. A sidecar that says only "complete" is lying by omission, so
    every run records this figure per region and the closing log prints
    it.
    """
    if not total or total <= 0 or collected < 0:
        return None
    return round(100.0 * collected / total, 4)
