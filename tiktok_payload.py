"""tiktok_payload.py — the two structured sources TikTok's web pages carry.

Why this file exists at all
===========================
TikTok serves three page kinds that this family reads, and they belong to
TWO different front-end applications that spell their state differently:

    /@user                 __UNIVERSAL_DATA_FOR_REHYDRATION__   the main webapp
    /@user/video/{id}      __UNIVERSAL_DATA_FOR_REHYDRATION__   same app
    /embed/@user           __FRONTITY_CONNECT_STATE__           the embed app

CLAUDE.md §23 names this shape on another site: two structured sources
belonging to two different applications. Keeping both readers in one
module is what stops a repo growing two half-parsers that disagree about
what "the payload" means.

This file is COPIED BYTE-IDENTICAL into every tiktok-* repo, and each
repo's smoke suite pins its SHA-256 against a recorded digest. CLAUDE.md
§16's rule is that copied core is untested core; the digest turns a silent
divergence into a red test. When this file legitimately changes, the
digest changes in the same commit, in every repo, or those repos go red —
which is the point.

The one measurement that shapes every reader here
=================================================
Measured 2026-09-22 over 15 captured profiles from a datacentre address:
TikTok publishes each count TWICE, and the two disagree.

    account        stats.followerCount   statsV2.followerCount   error
    @tiktok                  95,900,000              95,856,713   +43,287
    @khaby.lame             163,000,000             162,986,107   +13,893
    @nasa                     1,800,000               1,785,290   +14,710
    @charlidamelio          160,200,000             160,206,612    -6,612
    @zachking                86,900,000              86,900,407      -407
    (a small account)           429,700                 429,675      +25

`stats` is rounded to three significant figures and rounds BOTH WAYS, so
it is not a floor, a ceiling, or anything a consumer can correct for.
`statsV2` carries the exact figure, as a STRING. Every reader below
prefers statsV2 and falls back to stats only when statsV2 is absent,
recording which one it used — because a number whose provenance is
unrecorded is a guess presented as a fact (CLAUDE.md §8).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("tiktok_payload")

# ---------------------------------------------------------------------------
# Locating the payloads
# ---------------------------------------------------------------------------
#
# Anchored on the script TAG's id, which is a contract with the page's own
# hydration code, rather than on a CSS class or a surrounding structure.
# Same reasoning as CLAUDE.md §4's "anchor on a URL pattern, never a CSS
# class": the id is what the application itself looks up.
_REHYDRATION_RE = re.compile(
    r'<script\s+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.S,
)
_FRONTITY_RE = re.compile(
    r'<script\s+id="__FRONTITY_CONNECT_STATE__"[^>]*>(.*?)</script>',
    re.S,
)


class PayloadError(ValueError):
    """The page carried no payload, or one that would not parse.

    Distinct from "the payload said no such user", which is a fact about
    the account and is reported through `user_status()` instead. Confusing
    the two is how a scraper reports a parsing bug as an empty result, or
    an empty result as a parsing bug (CLAUDE.md §20).
    """


def decode_page(raw: Any, content_type: Optional[str] = None) -> str:
    """Bytes (or str) to text, honouring a declared charset.

    CLAUDE.md §24: one site in this family serves its detail pages as
    EUC-JP and its listing pages as UTF-8, and a blind
    `bytes.decode("utf-8", "replace")` turned every title into replacement
    characters while the numbers still parsed — a run that reports success
    with a column of garbage. TikTok is UTF-8 everywhere measured, but the
    parser accepts bytes everywhere precisely so that a future charset is
    a one-line fix rather than a silent corruption.
    """
    if isinstance(raw, str):
        return raw
    charset = None
    if content_type:
        m = re.search(r"charset=([\w-]+)", content_type, re.I)
        if m:
            charset = m.group(1)
    if not charset:
        m = re.search(rb'charset=["\']?([\w-]+)', raw[:2048], re.I)
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    for enc in (charset, "utf-8"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def rehydration_scope(html: Any) -> Dict[str, Any]:
    """The main webapp's `__DEFAULT_SCOPE__`, or raise PayloadError."""
    text = decode_page(html)
    m = _REHYDRATION_RE.search(text)
    if not m:
        raise PayloadError(
            "no __UNIVERSAL_DATA_FOR_REHYDRATION__ script in the page "
            f"({len(text)} chars)"
        )
    try:
        data = json.loads(m.group(1))
    except ValueError as exc:
        raise PayloadError(f"rehydration payload did not parse: {exc}") from exc
    scope = data.get("__DEFAULT_SCOPE__")
    if not isinstance(scope, dict):
        raise PayloadError("rehydration payload carried no __DEFAULT_SCOPE__")
    return scope


def frontity_state(html: Any) -> Dict[str, Any]:
    """The embed app's connect state, or raise PayloadError."""
    text = decode_page(html)
    m = _FRONTITY_RE.search(text)
    if not m:
        raise PayloadError(
            f"no __FRONTITY_CONNECT_STATE__ script in the page ({len(text)} chars)"
        )
    try:
        return json.loads(m.group(1))
    except ValueError as exc:
        raise PayloadError(f"embed payload did not parse: {exc}") from exc


def embed_node(html: Any) -> Dict[str, Any]:
    """The embed page's single data node, whatever route key it sits under.

    The state nests the node under its own link (`source.data["/embed/@nasa"]`),
    so the key varies per account and one more key — "strategy" — sits
    beside it and is not a node. Selecting by SHAPE rather than by a
    reconstructed key means a handle containing a character the app escapes
    differently cannot silently miss.
    """
    state = frontity_state(html)
    data = ((state.get("source") or {}).get("data") or {})
    for key, node in data.items():
        if isinstance(node, dict) and ("videoList" in node or "userInfo" in node):
            return node
    raise PayloadError(
        f"embed state carried no data node (keys: {sorted(data)[:6]})"
    )


# ---------------------------------------------------------------------------
# Counts: statsV2 over stats, with provenance
# ---------------------------------------------------------------------------

def _to_int(value: Any) -> Optional[int]:
    """A count to int, or None — never a defaulted zero.

    Zero is a real value on TikTok (an account with no likes), so this
    cannot collapse "absent" onto it. CLAUDE.md §21: zero is not a rating,
    and any numeric field whose absent state is 0 rather than null needs
    the absence recovered from somewhere else.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        # A string that is not a plain integer is not a count. Returning
        # None here rather than guessing is what keeps an abbreviated
        # "1.8M" from being read as 1.
        return None
    return None


def counts(container: Dict[str, Any], keys: Tuple[str, ...]) -> Tuple[Dict[str, Optional[int]], str]:
    """Read `keys` from statsV2 if present, else stats. Returns (values, source).

    The source string goes into a `stats_source` column. Two runs that
    differ only in which object the site happened to publish must be
    distinguishable from two runs where a count really moved — that is the
    same argument CLAUDE.md §8 makes for `price_source`, and
    `diff_runs.py` uses it the same way.
    """
    v2 = container.get("statsV2")
    v1 = container.get("stats")
    if isinstance(v2, dict) and any(k in v2 for k in keys):
        out = {k: _to_int(v2.get(k)) for k in keys}
        # A statsV2 that is present but empty for every key we want is not
        # usable, and falling through to stats is better than a row of
        # nulls beside a rounded number the site did publish.
        if any(v is not None for v in out.values()):
            return out, "statsV2"
    if isinstance(v1, dict):
        return {k: _to_int(v1.get(k)) for k in keys}, "stats"
    return {k: None for k in keys}, "absent"


# ---------------------------------------------------------------------------
# Page states the payload itself declares
# ---------------------------------------------------------------------------
#
# Measured 2026-09-22: a handle that does not exist
# (@thisaccountdoesnotexist999xyz) is served as HTTP 200, 370 KB, with a
# complete app shell and:
#
#     webapp.user-detail = {"statusCode": 10221,
#                           "statusMsg": "user banned",
#                           "needFix": false}          userInfo: null
#
# So "no such account" and "banned account" are ONE state on this site and
# the message is TikTok's own word for both. A scraper that reports "user
# banned" for somebody's typo has invented a fact; a scraper that reports
# "not found" for a genuinely banned account has invented the other one.
# The honest column says what the site said and the honest prose says both
# readings are live — see `USER_STATUS_UNAVAILABLE`.
USER_STATUS_OK = "ok"
USER_STATUS_UNAVAILABLE = "unavailable"   # nonexistent OR banned; TikTok does not distinguish
USER_STATUS_UNKNOWN = "unknown"

# Codes seen on a served page. 0 is a real user. Anything else has so far
# meant "no userInfo", but the set is deliberately not an allowlist of
# failures: a code we have never seen, WITH a userInfo, is treated as a
# user (the data is there), and without one is treated as unavailable.
_STATUS_OK_CODES = (0,)


def user_status(user_detail: Optional[Dict[str, Any]]) -> Tuple[str, Optional[int], Optional[str]]:
    """(status, statusCode, statusMsg) for a `webapp.user-detail` scope."""
    if not isinstance(user_detail, dict):
        return USER_STATUS_UNKNOWN, None, None
    code = user_detail.get("statusCode")
    msg = user_detail.get("statusMsg") or None
    info = user_detail.get("userInfo")
    if isinstance(info, dict) and info.get("user"):
        return USER_STATUS_OK, code if isinstance(code, int) else None, msg
    if isinstance(code, int) and code not in _STATUS_OK_CODES:
        return USER_STATUS_UNAVAILABLE, code, msg
    return USER_STATUS_UNKNOWN, code if isinstance(code, int) else None, msg


# ---------------------------------------------------------------------------
# Refusal detection
# ---------------------------------------------------------------------------
#
# CLAUDE.md §18: count every candidate marker on a page you KNOW is good
# before adding it. Counted 2026-09-22 across the captures in this repo:
#
#     marker              on a served profile   on a shop Security Check
#     "captcha"                          25                  29
#     "slide"                             0                   7
#     "rotate"                            0                  25
#     "secsdk-captcha"                    0                   4
#     "Security Check"                    0                   1
#     "captcha_verify_img_slide"          0                   1
#
# So the bare word `captcha` is a fact about every TikTok page and is
# USELESS as a marker — the exact trap §18 records from another site,
# where `akamai` matched every page the site served. The markers below are
# the ones that scored zero on every served capture.
#
# `rotate` and `slide` are NOT here either, despite scoring zero on the
# profiles measured: they are ordinary English words that a video
# description or a product title can carry, and a marker that a user's own
# text can trip is a marker that will eventually refuse a good page.
#
# The set below is split by what each marker can actually SEE, because the
# challenge reaches a parser two ways and they do not share a vocabulary:
#
#   RAW (an HTTP client, or --dump-html before any script runs)
#       the 5,724-byte interstitial. Counted 2026-09-22:
#           oec-ttweb-captcha     2 on the challenge, 0 on every served page
#           captcha-init          1 on the challenge, 0 on every served page
#       "served page" here is all 15 profile captures, the embed page and
#       the video page.
#
#   RENDERED (a browser, after the widget paints)
#       the slide puzzle itself:
#           secsdk-captcha            0 on served, 4 on the challenge
#           captcha_verify_img_slide  0 on served, 1 on the challenge
#
# A set that carried only the rendered names called the raw interstitial a
# parse error — a served page this parser did not understand — which is
# the wrong advice in a log: it sends the reader to the parser instead of
# to the challenge.
#
# NOT carried: the bare word `captcha`, which appears 25 times on a
# perfectly good profile page and 29 on the challenge. CLAUDE.md §18's
# rule, and the numbers are why: a marker that matches every page is worse
# than no marker.
#
# UNVERIFIED, and stated rather than assumed: CLAUDE.md §24 wants this set
# scored against a page fetched over the 2Captcha Scraping Browser, whose
# auto-solve extension injects captcha hunters into every page it loads.
# That check is written in `smoke_test.py` and SKIPS here, because the CDP
# profiles available while this repo was built had expired (401
# deny_no_user). None of these four names is one the extension is known to
# inject — they are ByteDance's own — but "not known to" is not "measured
# not to", and the skip says so rather than a comment claiming otherwise.
BOT_CHALLENGE_MARKERS = (
    "oec-ttweb-captcha",
    "captcha-init",
    "secsdk-captcha",
    "captcha_verify_img_slide",
)

# TikTok's WAF interstitial — the THIRD shape of refusal on this site, and
# the one that is actually curable.
#
# Measured 2026-09-22 on the profile route, `GET /@nasa` with a plain HTTP
# client:
#
#     from this datacentre address (Hetzner, Helsinki)   3 of 3 served
#     from a residential pool, nine exits                2 of 9 served
#
# The other seven answered HTTP 200 with 1,462 bytes whose visible text is
# "Please wait..." and whose body carries TikTok's WAF challenge —
# `SlardarWAF`, a `_wafchallengeid` element and a `waf-aiso/*.js` script.
#
# Two things follow, and the first inverts this family's usual instinct:
#
#   1. A RESIDENTIAL PROXY IS WORSE THAN NO PROXY HERE. 22% against 100%.
#      CLAUDE.md §24 records a site where the gate was the client rather
#      than the address; this is a site where a "better" address is the
#      worse one, presumably because a shared residential pool has been
#      scraped through before and a clean datacentre IP has not.
#
#   2. A BROWSER CLEARS IT. Driving Chromium through the very exits that
#      refused a plain HTTP client: 3 of 3 served, 0 still challenged. It
#      is a JavaScript challenge, not a captcha — there is no widget and
#      nothing for a solver to solve, so `solve` is False in the policy
#      and the remedy is `--transport browser` or a different exit.
#
# That makes `--transport auto`'s fallback load-bearing on this site
# rather than insurance: the engines already switch to a browser for any
# state that counts as blocked, so naming this one correctly is what makes
# the existing machinery do the right thing.
WAF_CHALLENGE_MARKERS = (
    "_wafchallengeid",
    "waforiginalreid",
    "waf-aiso",
    "slardar_us_waf",
)

# NOT carried: "Please wait...". It is ordinary English that a video
# caption or a bio can contain, and CLAUDE.md §18's rule is that a marker
# a user's own text can trip will eventually refuse a good page. The four
# above are a CSS class, an element id, a script path and a WAF product
# name; none can arrive from user content. Counted 2026-09-22 across 25
# served captures in this family: 0 occurrences each, against 1 each on
# the interstitial.


def waf_markers_present(html) -> List[str]:
    """Which WAF markers a page carries, in the order they are listed."""
    text = decode_page(html)
    return [m for m in WAF_CHALLENGE_MARKERS if m in text]


# The refusal that has no marker at all, and the one this site is really
# built around.
#
# Measured 2026-09-22, every way it was asked — plain curl, headless
# Chromium, headful Chromium, a Windows user agent, after accepting the
# cookie consent, and through a residential exit in Peru — the signed
# request TikTok's own front end makes for a profile's video feed answers:
#
#     HTTP 200   content-type: application/json   content-length: 0
#
# No status to key on, no body to parse, no marker to match. A client that
# checks `response.ok` calls this a success and reports an account with no
# videos. CLAUDE.md §24 records a site whose refusal arrived under HTTP
# 200; this is that, with the body removed as well.
def is_empty_success(status: Optional[int], body: Any) -> bool:
    """True for TikTok's zero-byte HTTP 200 — a refusal wearing a success.

    Deliberately NOT keyed on the endpoint, because the shape is the
    finding: any TikTok JSON route can answer this way, and a new one
    should be recognised without an edit here.
    """
    if status is not None and status != 200:
        return False
    if body is None:
        return True
    if isinstance(body, (bytes, bytearray, str)):
        return len(body.strip()) == 0
    return False
