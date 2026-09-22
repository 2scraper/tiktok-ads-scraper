"""
smoke_test.py — the offline suite for tiktok-profile-scraper.

One file of plain functions with fixtures loaded from
`fixtures_generated.json`. No pytest, no conftest, no fixtures directory
(CLAUDE.md §10); `tests/test_smoke.py` wraps this as a single pytest test so
`pytest` works as an entry point without a second copy of the checks.

    python3 smoke_test.py            run everything
    python3 smoke_test.py -v         print every check as it passes

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is RECORDED, because "skipped, engine absent" reads
identically to a real import error. CI installs each engine in its own venv
and fails if that engine's group reports a skip.

The fixtures are cut from real captures taken 2026-09-22 by
`make_fixtures.py`, which also PROVES each trimmed fixture parses
identically to its untrimmed original — every column of every kept row, and
the same page state — before writing anything.

What the fixtures deliberately reproduce
----------------------------------------
Each is a trap this repo measured, and the fixture exists so that fixing it
stays fixed:

  * `stats` against `statsV2` — TikTok publishes every count twice and the
    two disagree. The fixtures span the whole measured range of that
    disagreement, from @zachking's 407 to @tiktok's 43,287, and the check
    below pins BOTH numbers for each account. A suite that only ever saw
    @nasa's 14,710 would pass a parser that read the wrong object and
    happened to be tested on a 3-significant-figure account.
  * an account with 163,000,000 followers and 2,680,875,280 likes, where an
    int that should have been a string breaks;
  * a small account, 429,675 followers, which is where a parser that only
    ever sees abbreviated magnitudes gets caught;
  * a handle with NO account behind it, which TikTok answers with HTTP 200,
    370 KB and a complete app shell — `statusCode: 10221`,
    `statusMsg: "user banned"`, `userInfo: null`. That message is TikTok's
    word for a nonexistent handle as well as a banned one, and one of the
    checks below pins that this repo does not claim to tell them apart;
  * a `?lang=ja` page, to pin that a locale flag moves the CHROME and not
    the DATA.

What is NOT in them, and why that needed no scrubbing step
----------------------------------------------------------
A fixture here is the `webapp.user-detail` scope alone, trimmed out of a
~370 KB page. The session material a capture carries — the csrf token, the
odinId, the ttwid — all lives OUTSIDE that scope, so it is absent as a
consequence of keeping only what is under test rather than as a redaction
someone has to remember to perform. That is the version of scrubbing that
cannot rot.

A TikTok profile is also not a comment thread: the account, its bio, its
counts and its picture are the site's own catalogue entry for a public
creator, not a private individual's words. So nothing here is replaced with
a placeholder, and the checks test real values.
"""

import argparse
import ast
import csv
import hashlib
import inspect
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import types
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict, fields

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
# `HERE` is what the family core calls this. Both names are bound because
# the shared checks below were lifted verbatim from a sibling repo, and
# renaming inside them would stop the two copies being comparable — which
# is the whole point of sharing them.
HERE = REPO_ROOT
sys.path.insert(0, REPO_ROOT)

import product_parser                                        # noqa: E402
import page_flow                                             # noqa: E402
import output_writer                                         # noqa: E402
import proxy_pool                                            # noqa: E402
import env_config                                            # noqa: E402
import tiktok_payload                                        # noqa: E402
from output_writer import Advertisement                      # noqa: E402

FAILURES = []
PASSED = 0
SKIPS = []
VERBOSE = False

ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")

DRIVER_IMPORTS = {
    "playwright_scraper": "playwright",
    "selenium_scraper": "selenium",
    "puppeteer_scraper": "pyppeteer",
}

# The family's CLI contract (CLAUDE.md §9). Re-derived rather than copied:
#
#   grep -ohE '"--[a-z0-9-]+"' */playwright_scraper.py | sort | uniq -c
#
# across the sibling repos in ~/2scraper on 2026-09-22.
CONTRACT_FLAGS = {
    "--url", "--pages", "--category", "--format", "--out", "--delay",
    "--retries", "--retry-delay", "--concurrency", "--proxy", "--proxy-file",
    "--proxy-rotate", "--proxy-shuffle", "--proxy-block-retries",
    "--twocaptcha-key", "--captcha-api", "--solve-captcha", "--min-score",
    "--cdp-endpoint", "--allow-empty", "--dump-html", "--headless",
    "--headful", "--mode", "--fingerprint", "--fp-country", "--fp-tags",
    "--locale",
}

# This repo's own addition. `--transport` exists because the profile page is
# server-rendered and a browser buys nothing on it, so the default is plain
# HTTP and the browser is a fallback rather than the engine.
SITE_FLAGS = {"--transport", "--region", "--query", "--days",
              "--ad-type"}

# CLAUDE.md §12, and ASSEMBLED from pieces rather than written out — which
# is what lets the scan cover this file too. Three sibling repos exempted
# their own suite from the scan, so the one file most likely to acquire a
# stray phrase or a pasted credential was the one file nobody scanned
# (§22).
BANNED_WORDING = (
    "cloud " + "browser", "anti" + "detect browser",
    "2scraper Anti" + "detect Browser",
    "gate." + "2prx.com", "ANTI" + "DETECT_LOCAL_API",
)

BANNED_FLAGS = ("--anti" + "detect", "--country-code", "--country")

_FIXTURE_PATH = os.path.join(REPO_ROOT, "fixtures_generated.json")


def _load_fixtures():
    if not os.path.exists(_FIXTURE_PATH):
        raise SystemExit(
            f"fixtures_generated.json is missing from {REPO_ROOT}.\n"
            f"Regenerate it with `python3 make_fixtures.py`, which needs "
            f"your own captures — see that file's docstring. If it is "
            f"present but ignored, check .gitignore's "
            f"`!fixtures_generated.json` exception.")
    with open(_FIXTURE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


FIX = _load_fixtures()
RESPONSES = FIX["responses"]

SCRAPED_AT = "2026-09-22T12:00:00Z"
FIXTURE_REGION = "DE"


def payload(name):
    """One captured search response, as the endpoint returned it."""
    return RESPONSES[name]["payload"]


def rows(name, region=FIXTURE_REGION, page=1):
    parsed, _diag = product_parser.parse_search(payload(name), region,
                                                SCRAPED_AT, page,
                                                Advertisement)
    return parsed


def diag(name, region=FIXTURE_REGION, page=1):
    _rows, d = product_parser.parse_search(payload(name), region, SCRAPED_AT,
                                           page, Advertisement)
    return d


def raw_ads(name):
    return payload(name)["data"]


WAF_FIXTURE = (
    '<!DOCTYPE html> <html lang="en"> <head>'
    '<script id="slardar-config" type="application/json">'
    '{ "slardarClient": "SlardarWAF", "bid": "slardar_us_waf", "pid": "js-44" }'
    '</script></head><body> Please wait... '
    '<p id="wci" class="_wafchallengeid"></p>'
    '<p id="cs" class="ZXhwaXJlZC1jaGFsbGVuZ2UtYmxvYg"></p>'
    '<p id="rci" class="waforiginalreid"></p>'
    '<script src="https://sf16-website-login.neutral.ttwstatic.com/obj/'
    'tiktok_web_login_static/obj/waf-aiso/dd9808.js"></script>'
    '</body></html>'
)

def _SERVED_PAGES():
    """(name, text) for every capture this repo knows is good.

    JSON here rather than HTML, because this repo's route answers with
    JSON — which is itself worth pinning: a marker set written for markup
    must still score zero against a body that has none.
    """
    return [(n, json.dumps(payload(n))) for n in RESPONSES]


# Everything the 2Captcha Scraping Browser's auto-solve extension injects
# into a page, captured verbatim on 2026-09-22 from a real
# `--cdp-endpoint` fetch of a page TikTok SERVED.
#
# This is the fixture CLAUDE.md §24 asks every repo in this family to hold,
# and it was a recorded SKIP here until a live Scraping Browser profile
# turned up. It is 1,882 bytes of script tags and one custom element; the
# page they came from was 453 KB of real content.
#
# Counted on it: 16 `chrome-extension://` tags, 4 `hunter.js`,
# `cf-turnstile` once, `data-ts-input` once and `captcha-widgets` twice.
# Every one of those is a marker some repo in this family has carried at
# some point, and `cf-turnstile` is the one CLAUDE.md §19 singles out —
# it fires on GOOD pages over CDP and was measured ABSENT from a real
# Cloudflare challenge on another site.
#
# Also measured, and worth recording because it is the reason
# tiktok-shop-scraper cannot use this path: the same extension, on TikTok
# SHOP's challenge page, injected all sixteen hunters and did not touch
# ByteDance's own captcha. `Captcha.setAutoSolve` returned {} and no
# `Captcha.solveFinished` event ever fired, across three shop routes with
# waits up to 35 seconds. The extension has no hunter for `oec-ttweb-captcha`.
CDP_INJECTED = (
    '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/conte'
    'nt/captcha/captchafox/interceptor.js"></script><script src="chrome-ext'
    'ension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/mt_captcha/i'
    'nterceptor.js"></script><script src="chrome-extension://kjmkgkdkpedkej'
    'edfhmfcenooemhbpbo/content/captcha/turnstile/interceptor.js"></script>'
    '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/conte'
    'nt/captcha/turnstile/hunter.js" data-ts-input="cf-turnstile-response">'
    '</script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhb'
    'pbo/content/captcha/amazon_waf/interceptor.js"></script><script src="c'
    'hrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/yan'
    'dex/interceptor.js"></script><script src="chrome-extension://kjmkgkdkp'
    'edkejedfhmfcenooemhbpbo/content/captcha/lemin/interceptor.js"></script'
    '><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/cont'
    'ent/captcha/arkoselabs/hunter.js"></script><script src="chrome-extensi'
    'on://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/arkoselabs/inter'
    'ceptor.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfh'
    'mfcenooemhbpbo/content/captcha/recaptcha/interceptor.js"></script><scr'
    'ipt src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/c'
    'aptcha/recaptcha/hunter.js"></script><script src="chrome-extension://k'
    'jmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/keycaptcha/hunter.js">'
    '</script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhb'
    'pbo/content/captcha/geetest_v4/interceptor.js"></script><script src="c'
    'hrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/gee'
    'test/interceptor.js"></script><script src="chrome-extension://kjmkgkdk'
    'pedkejedfhmfcenooemhbpbo/content/communication_helpers.js"></script><s'
    'cript src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content'
    '/core_helpers.js"></script><captcha-widgets></captcha-widgets>')


class _NullContext:
    """Stands in for a driver's lifetime while the browser is stubbed.

    Each engine wraps its driver in `_driver_context`; Playwright's opens a
    real `sync_playwright()`, so the concurrency checks below swap in this
    no-op to drive the queue with no browser anywhere (CLAUDE.md §10).
    """

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAILURES.append("%s%s" % (name, (" — " + detail) if detail else ""))
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))


def equal(name, got, want):
    check(name, got == want, "got %r, want %r" % (got, want))


def skip(group, reason):
    SKIPS.append("%s: %s" % (group, reason))
    print("  SKIP %s — %s" % (group, reason))


def _argparse_flags(module_name):
    """Every --flag a module's parser defines, without running the CLI."""
    path = os.path.join(HERE, module_name + ".py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    # Only calls on the argparse parser itself. A browser's option object
    # also has `add_argument`, and counting Chrome's own switches
    # (`--no-sandbox`, `--window-size=…`) as CLI flags made this check
    # compare nonsense.
    parsers = {"p"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in ("add_argument_group",
                                             "add_mutually_exclusive_group")):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    parsers.add(target.id)
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in parsers):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and arg.value.startswith("--"):
                    flags.add(arg.value)
    return flags


def _import_engine(name):
    try:
        return __import__(name)
    except ImportError as e:
        skip(name, "engine library absent (%s)" % e)
        return None


def _import_graph(entrypoint):
    """Every local module an entrypoint reaches, transitively."""
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    seen, queue = set(), [entrypoint]
    while queue:
        name = queue.pop()
        if name in seen or name not in local:
            continue
        seen.add(name)
        tree = ast.parse(open(os.path.join(HERE, name + ".py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                queue.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                queue.append(node.module.split(".")[0])
    return seen


def _tree_state():
    result = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return sorted(line for line in result.stdout.splitlines()
                  if not line.endswith(".pyc"))


def _fault_args(engine, **overrides):
    """Arguments for a fault-injection run, with no network in them."""
    base = dict(url=None, region="DE", mode="ads", query="", days=30,
                ad_type=1, pages=1, locale="en", delay=0,
                retries=0, retry_delay=0, solve_captcha="never",
                dump_html=False, out="unused", concurrency=1,
                cdp_endpoint=None, proxy=None, proxy_file=None,
                proxy_rotate="per-run", fingerprint=False, twocaptcha_key=None,
                fp_tags=None, fp_country=None, headless=True, captcha_api="v2",
                min_score=0.3, category=None, allow_empty=False, format="json",
                proxy_block_retries=1, transport="browser")
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _with_stubs(engine, fetch, body):
    """Run `body` with the engine's transport and session stubbed out."""
    class FakeSession:
        client_version = ""
        proxy_url = None

        def close(self):
            pass

    original = (engine._open_session, engine._prime_session,
                engine._fetch_with_policy)
    engine._open_session = lambda pw, args, pool: FakeSession()
    engine._prime_session = lambda session, args, url: 200
    engine._fetch_with_policy = fetch
    try:
        return body(FakeSession)
    finally:
        (engine._open_session, engine._prime_session,
         engine._fetch_with_policy) = original


# ---------------------------------------------------------------------------
# The measurement this whole repo turns on
# ---------------------------------------------------------------------------


def check_offset_is_a_decoy_and_the_cursor_is_not():
    """The trap this repo exists to not fall into.

    The request body carries an `offset`, the library's own UI increments
    it, and it does NOTHING. Measured 2026-09-22: offsets 0, 12 and 24
    returned the identical twelve ads in the identical order. Pagination
    is by `search_id`, a base64 cursor the response hands back.

    A run that trusted `offset` would re-collect page one for as long as
    it was asked to and report a complete run of duplicates — CLAUDE.md
    §23's trap, and the loudest decoy this family has met, because here
    the useless parameter is one the site's own front end sends.
    """
    body = product_parser.search_body()
    equal("the body still sends offset, as the endpoint expects",
          body["offset"], 0)
    check("but the parser exposes no way to advance it",
          "offset" not in inspect.signature(product_parser.search_body).parameters,
          "a caller must not be able to ask for offset N and believe it")
    equal("the cursor is what search_body takes", body["search_id"], "")
    equal("and a cursor goes in it",
          product_parser.search_body(cursor="abc")["search_id"], "abc")

    # The cursor is readable, and reading it is what makes the end of the
    # listing unambiguous.
    d1, d2 = diag("de_page1"), diag("de_page2")
    for label, d in (("page 1", d1), ("page 2", d2)):
        check("%s hands back a cursor" % label, bool(d["cursor"]))
        decoded = d["cursor_decoded"]
        check("%s cursor decodes" % label, isinstance(decoded, dict),
              "got %r" % decoded)
        check("%s cursor states its position" % label,
              isinstance(decoded.get("next_cursor"), int))
    check("and page 2's cursor is ahead of page 1's",
          d2["cursor_decoded"]["next_cursor"]
          > d1["cursor_decoded"]["next_cursor"],
          "%r then %r" % (d1["cursor_decoded"], d2["cursor_decoded"]))

    # The two pages really are different ads — which is what proves the
    # cursor works and `offset` was never needed.
    a = {r.ad_id for r in rows("de_page1")}
    b = {r.ad_id for r in rows("de_page2")}
    check("the two cursor pages hold different ads (%d shared of %d)"
          % (len(a & b), len(a)), len(a & b) < len(a),
          "if they were identical the fixture would be demonstrating the "
          "bug rather than the fix")

    # And no engine may construct a page URL.
    try:
        product_parser.page_url("u", 2)
        check("page 2 has no address", False, "it returned a URL")
    except product_parser.NotAnAdsQuery as exc:
        check("page 2 is refused, with the measurement in the reason",
              "decoy" in str(exc) and "offset" in str(exc),
              "got %r" % str(exc))
    check("the shared policy says this route is not addressable",
          not page_flow.pagination_is_addressable("", "ads"))
    equal("and clamps concurrency to one",
          page_flow.concurrency_for_mode("ads", 8), 1)


def check_limit_is_a_decoy_too():
    """12 per response, whatever is asked for.

    Measured 2026-09-22: `limit` 12, 50 and 100 all returned twelve. A
    repo that believed `limit` would report a third of the pages it
    thinks it fetched.
    """
    equal("the body asks for the measured ceiling",
          product_parser.search_body()["limit"], product_parser.ADS_PER_PAGE)
    equal("which is 12", product_parser.ADS_PER_PAGE, 12)
    for name in RESPONSES:
        equal("%s really holds %d ads" % (name, product_parser.ADS_PER_PAGE),
              len(raw_ads(name)), product_parser.ADS_PER_PAGE)


def check_an_ad_id_is_not_unique_across_regions():
    """The bug this check was written after finding.

    An EU ad buy commonly runs in several member states at once and the
    library lists it under each. Measured 2026-09-22, page one of five
    regions within the same few seconds:

        DE vs IT   11 of 12 ad ids shared
        DE vs FR    9 of 12
        DE vs PL    9 of 12
        DE vs GB    1 of 12      <- outside the EU, bought separately

    So deduping on `sku` across regions DESTROYS data, and it did: a
    two-region run collapsed France's twelve ads to two before `row_key`
    existed. CLAUDE.md §9: dedupe on whatever is actually unique.
    """
    de = rows("de_page1", region="DE")
    fr = rows("de_page1", region="FR")     # same payload, different region
    equal("the same ad under two regions keeps one ad_id",
          de[0].ad_id, fr[0].ad_id)
    check("but gets two different row_keys",
          de[0].row_key != fr[0].row_key,
          "%r vs %r" % (de[0].row_key, fr[0].row_key))
    equal("which name the region", de[0].row_key,
          "DE:" + str(de[0].ad_id))
    equal("and the region is a column too", de[0].region, "DE")

    # The engines must dedupe on it, and diff_runs must join on it.
    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        check("%s dedupes on row_key, not sku" % module,
              'dedupe_by_key(page_rows, seen, key="row_key")' in src,
              "deduping on sku collapses an ad that runs in several "
              "countries down to one country")
    diff_src = open(os.path.join(HERE, "diff_runs.py"), encoding="utf-8").read()
    check("diff_runs joins on row_key where a row has one",
          'p.get("row_key")' in diff_src,
          "joined on the ad id alone it would compare Germany's copy of "
          "an ad against France's and call the difference a change")


def check_a_nested_column_is_canonical():
    """`rejection_info` is JSON inside a column, and it is SORTED.

    Found by `make_fixtures.py --verify`: a capture and its own fixture
    held byte-equal payloads (`o == f` was True) and parsed to different
    rows, because the fixture had been written with sorted keys and the
    capture had not, and this column re-serialises a nested object.

    Same shape as CLAUDE.md §24's float32 round-trip making two modes
    disagree on every row — a serialisation detail leaking into a value.
    """
    rejected = [r for r in rows("rejected") if r.rejection_info]
    check("the fixture holds a rejected ad", bool(rejected),
          "2 of 216 ads measured carry one; recapture if this goes stale")
    text = rejected[0].rejection_info
    parsed = json.loads(text)
    equal("it round-trips to the same string",
          json.dumps(parsed, ensure_ascii=False, sort_keys=True,
                     separators=(", ", ": ")), text)
    # And the same object arriving in a different key order must give the
    # same column.
    shuffled = dict(reversed(list(parsed.items())))
    same = product_parser.parse_search(
        {"data": [{"id": "1", "rejection_info": shuffled}], "code": 0},
        "DE", SCRAPED_AT, 1, Advertisement)[0][0].rejection_info
    equal("key order in the source does not change the column",
          same, text)


def check_timestamps_are_milliseconds():
    """TikTok publishes these in MILLISECONDS.

    Read as seconds, 1790112836000 lands in the year 58,700 — which is
    the kind of wrong that looks like a parser working until somebody
    sorts by it.
    """
    for name in RESPONSES:
        for row in rows(name):
            if row.first_shown_at:
                check("%s: %s is a plausible year" % (name, row.ad_id),
                      row.first_shown_at.startswith("20"),
                      "got %r" % row.first_shown_at)
                break
    raw = raw_ads("de_page1")[0]["first_shown_date"]
    check("the source really is 13 digits (milliseconds)",
          len(str(int(raw))) == 13, "got %r" % raw)
    # A 10-digit value is absorbed as seconds rather than mis-dated, so a
    # future change in the site is a smaller surprise.
    equal("a 10-digit value is read as seconds",
          product_parser._ms_to_iso(1790112836)[:4], "2026")
    equal("and 0 means never, not 1970",
          product_parser._ms_to_iso(0), None)


def check_image_ads_are_not_broken_video_ads():
    """69 of 216 ads measured carry images and a title; the rest carry video.

    An ad with no `videos` is not a failure — it is an image ad, and its
    `title` is the one place its text appears.
    """
    all_rows = [r for name in RESPONSES for r in rows(name)]
    kinds = {r.creative_type for r in all_rows}
    check("both creative kinds appear in the fixtures", 
          {"video", "image"} & kinds, "got %r" % kinds)
    images = [r for r in all_rows if r.image_urls]
    check("image ads carry their urls", bool(images))
    for row in images[:5]:
        equal("%s: image_count matches the url list" % row.ad_id,
              row.image_count, len(row.image_urls))
        check("%s: and it is named an image or both" % row.ad_id,
              "image" in (row.creative_type or ""))
    videos = [r for r in all_rows if r.video_urls]
    for row in videos[:5]:
        equal("%s: video_count matches the url list" % row.ad_id,
              row.video_count, len(row.video_urls))
        check("%s: the media url is the library's own proxy" % row.ad_id,
              "library.tiktok.com/api/v1/cdn/" in row.video_urls[0],
              "the proxied form is what answers; the CDN address inside it "
              "is base64 and its signature is bound to the proxy")


def check_an_anonymous_advertiser_is_not_a_parse_failure():
    """211 of 216 ads measured name an advertiser. Five do not.

    That is the library's own gap, and a row for one must still carry its
    id, its dates and its creatives rather than being dropped.
    """
    all_rows = [r for name in RESPONSES for r in rows(name)]
    equal("every row has an id", [r for r in all_rows if not r.ad_id], [])
    equal("every row has a row_key", [r for r in all_rows if not r.row_key], [])
    nameless = [r for r in all_rows if not r.advertiser_name]
    if nameless:
        for row in nameless[:3]:
            check("%s: a nameless advertiser still has a title" % row.ad_id,
                  row.title is not None or row.creative_type is not None,
                  "the row is still worth having")
    check("the parser drops no ad that has an id (%d rows from %d ads)"
          % (len(all_rows), sum(len(raw_ads(n)) for n in RESPONSES)),
          len(all_rows) == sum(len(raw_ads(n)) for n in RESPONSES))


def check_the_row_schema():
    """The family prefix is byte-identical and in order (§9)."""
    names = [f.name for f in fields(Advertisement)]
    equal("family prefix first and in order", names[:5],
          ["source", "scraped_at", "url", "sku", "title"])
    equal("source names the host this repo actually reads",
          Advertisement().source, "library.tiktok.com")
    check("Product is bound to the row class",
          output_writer.Product is Advertisement)
    equal("sku is the ad id", rows("de_page1")[0].sku,
          raw_ads("de_page1")[0]["id"])

    all_rows = [r for name in RESPONSES for r in rows(name)]
    always_null = [n for n in names
                   if all(getattr(r, n) is None for r in all_rows)]
    check("no column is null on every fixture row (%d rows)" % len(all_rows),
          not always_null,
          "always null: %s — either measure them on more ads or drop them"
          % always_null)
    # The two that WERE dropped, and the measurement that justified it.
    for gone in ("impression", "spent"):
        check("%r is not a column" % gone, gone not in names,
              "it was empty on all 216 ads measured across six regions")


def check_no_ads_is_not_a_block():
    """A query that matched nothing is a real answer."""
    empty = {"data": [], "code": 0, "total": 0, "has_more": False}
    equal("an empty result has its own state",
          product_parser.detect_page_state(empty, 200),
          product_parser.STATE_NO_ADS)
    for probe in (page_flow.counts_as_blocked, page_flow.should_retry,
                  page_flow.should_solve, page_flow.should_parse):
        check("no_ads: %s is False" % probe.__name__,
              not probe(product_parser.STATE_NO_ADS))
    check("`no_ads_found` is a COMPLETE stop reason",
          "no_ads_found" in output_writer.COMPLETE_STOP_REASONS,
          "a zero-row run would otherwise report 5, claiming we never "
          "reached the site")


def check_a_stale_token_is_not_a_block():
    """HTTP 421 means the header went stale, and rotating an exit is the
    wrong response.

    Measured: the endpoint answers 421 to a request without a valid
    `x-ccl-str`, and 200 to the same request with one. The remedy is to
    mint another, which the engines do by re-priming — so this state is
    retried and is NOT counted as blocked. Counting it as blocked would
    spend a proxy budget on the wrong problem and report exit 3 for
    something entirely under our own control.
    """
    state = product_parser.STATE_TOKEN_REJECTED
    equal("421 is recognised without needing a body",
          product_parser.detect_page_state("", 421), state)
    equal("...and with one", product_parser.detect_page_state(
        {"msg": "system busy"}, 421), state)
    check("it is retried", page_flow.should_retry(state))
    check("it is NOT blocked", not page_flow.counts_as_blocked(state),
          "a different exit does nothing about a stale token")
    check("and pays no solver", not page_flow.should_solve(state))
    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        check("%s mints a fresh token on 421" % module,
              "STATE_TOKEN_REJECTED" in src and "_prime_session" in src,
              "the remedy has to be wired, not merely named")


def check_a_browser_is_a_prerequisite_and_says_so():
    """This repo is the reverse of its siblings, and that has to be stated.

    The search endpoint answers HTTP 421 without an `x-ccl-str` that only
    the page's own JavaScript mints — there is no global to read it from,
    and a plain `fetch` from inside the page gets 421 too. So `http` is
    refused up front rather than failing later on a status nobody can
    interpret.
    """
    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        check("%s refuses --transport http with the reason" % module,
              "421" in src and "cannot work" in src)
        check("%s names the header in one place" % module,
              "TOKEN_HEADER" in src,
              "spelling it twice is how two call sites come to disagree")
        check("%s mints the token by watching the app's own request"
              % module, "capture_search_token" in src)
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    check("the README says a browser is required here",
          "421" in readme and "x-ccl-str" in readme,
          "a user coming from the sibling repos will expect --transport "
          "http to work")


def check_regions_are_refused_with_the_reason():
    """CLAUDE.md §5: a false refusal sends the reader hunting for a typo."""
    equal("a known region normalises", product_parser.normalise_region("de"),
          "DE")
    equal("33 regions are known", len(product_parser.KNOWN_REGIONS), 33)
    for code, want in (("US", "does not cover"), ("BR", "does not cover"),
                       ("XX", "not one of the 33"), ("", "no region")):
        try:
            product_parser.normalise_region(code)
            check("%r is refused" % code, False, "it was accepted")
        except product_parser.NotAnAdsQuery as exc:
            check("%r is refused, naming %r" % (code, want),
                  want.lower() in str(exc).lower(), "got %r" % str(exc))
    # US must NOT be called a typo — it is a real country the library does
    # not serve, and saying otherwise sends the reader looking for one.
    try:
        product_parser.normalise_region("US")
    except product_parser.NotAnAdsQuery as exc:
        check("a real country is not called a typo",
              "real country" in str(exc), "got %r" % str(exc))


def check_the_run_says_how_small_a_sample_it_is():
    """CLAUDE.md §21, and here the gap is the largest in the family.

    The library reports 17,343,646 ads for Germany alone and serves
    TWELVE per request. A thorough hundred-page run holds 1,200 of them —
    0.0069%. A sidecar that says only "complete" is lying by omission.
    """
    equal("the arithmetic is right", page_flow.sample_share(1200, 17343646),
          round(100.0 * 1200 / 17343646, 4))
    equal("and absent when the total is unknown",
          page_flow.sample_share(12, None), None)
    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        for field_name in ("site_total_per_region", "sample_share_pct",
                           "ads_per_region", "why_each_region_stopped"):
            check("%s records %s in the sidecar" % (module, field_name),
                  field_name in src)
        check("%s says it in the closing log too" % module,
              "not exhaustive" in src,
              "the sidecar is read by machines; the log is read by people")
    # The site's own total must reach the sidecar unchanged.
    d = diag("de_page1")
    check("the response states a total", isinstance(d["total"], int)
          and d["total"] > 1000,
          "got %r" % d["total"])


def check_the_state_policy_is_read_and_not_hardcoded():
    """Every state has a policy, and the engines consult it."""
    states = [v for k, v in vars(product_parser).items()
              if k.startswith("STATE_") and isinstance(v, str)]
    for state in states:
        check("%s has a policy entry" % state, state in page_flow.STATE_POLICY)
    for state, policy in page_flow.STATE_POLICY.items():
        equal("%s policy has all four keys" % state,
              sorted(policy), ["blocked", "parse", "retry", "solve"])
    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        check("%s calls should_parse rather than testing the string" % module,
              "page_flow.should_parse(" in src)
        check("%s does not compare a state to a literal" % module,
              'state == "content"' not in src and "state == 'content'" not in src)


def check_the_shared_payload_module_has_not_drifted():
    """`tiktok_payload.py` is byte-identical across the tiktok-* repos."""
    path = os.path.join(HERE, "tiktok_payload.py")
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    expected = open(os.path.join(HERE, "tiktok_payload.sha256"),
                    encoding="utf-8").read().strip()
    check("tiktok_payload.py matches its recorded digest", digest == expected,
          "got %s, recorded %s — if you changed it on purpose, update the "
          "digest in EVERY tiktok-* repo in the same commit"
          % (digest[:16], expected[:16]))


def check_no_marker_matches_a_response_the_library_serves():
    """CLAUDE.md §18, against a JSON body rather than markup.

    Worth doing precisely because the marker set was written for HTML: a
    set that scored zero on pages and non-zero on payloads would report a
    blocked run on a perfectly good search response.
    """
    for name, text in _SERVED_PAGES():
        hits = product_parser.challenge_markers_present(text)
        check("no challenge marker in the %s response" % name, not hits,
              "fired: %s" % hits)
        waf = tiktok_payload.waf_markers_present(text)
        check("no WAF marker in the %s response" % name, not waf,
              "fired: %s" % waf)


def check_policy_constants_have_a_consumer():
    """§17: a policy constant nothing reads is the same defect as dead code.

    `RETRY_ON_BLOCKED` carried a paragraph of justification in a sibling repo
    and no engine consulted it, so setting it False changed nothing.
    """
    import page_flow
    sources = []
    for name in ("playwright_scraper.py", "selenium_scraper.py",
                 "puppeteer_scraper.py", "scraper_api_client.py"):
        path = os.path.join(HERE, name)
        if os.path.exists(path):
            sources.append(open(path, encoding="utf-8").read())
    joined = "\n".join(sources)
    for constant in ("RETRY_ON_BLOCKED", "BLOCK_RETRIES_WITHOUT_POOL",
                     "SOLVES_PER_PAGE"):
        check("page_flow.%s is CONSULTED by an engine" % constant,
              constant in joined,
              "defined in page_flow and read by nothing")
    for fn in ("pages_to_plan", "ready_selector", "min_matches",
               "content_timeout_ms", "wait_for_count", "classify",
               "should_retry", "should_solve", "counts_as_blocked",
               "should_parse", "concurrency_limit",
               "pagination_is_addressable"):
        check("page_flow.%s has a caller outside its own module" % fn,
              fn in joined, "unused policy")


def check_csv_and_json_writers():
    """JSON and CSV carry the same columns in the same order, and an empty
    CSV still carries its header (CLAUDE.md §9)."""
    from output_writer import write_csv, write_json
    written = rows("de_page1")[:2]
    expected = [f.name for f in fields(Advertisement)]
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "out.csv")
        write_csv(written, csv_path, row_cls=Advertisement)
        with open(csv_path, encoding="utf-8") as f:
            reader = list(csv.reader(f))
        equal("csv header is the dataclass, in order", reader[0], expected)
        equal("csv has one line per row", len(reader), len(written) + 1)
        equal("and the id column is the ad id",
              reader[1][expected.index("sku")], written[0].sku)

        json_path = os.path.join(tmp, "out.json")
        write_json(written, json_path)
        data = json.load(open(json_path, encoding="utf-8"))
        equal("json columns match the dataclass, in order",
              list(data[0].keys()), expected)
        equal("json and csv agree on the region",
              str(data[0]["region"]), reader[1][expected.index("region")])
        # `hashtags` is this repo's only list column, and a CSV has to
        # flatten it. Pinned because the flattening is where a list column
        # silently becomes "['a', 'b']" in somebody's spreadsheet.
        check("a list column survives the CSV round trip",
              any(isinstance(r.get("video_urls"), list) for r in data),
              "video_urls is this repo's list column")

        # An empty CSV still carries its header, so a consumer reads a
        # table with no rows rather than failing on a zero-byte file.
        empty_path = os.path.join(tmp, "empty.csv")
        write_csv([], empty_path, row_cls=Advertisement)
        with open(empty_path, encoding="utf-8") as f:
            empty = list(csv.reader(f))
        equal("an empty CSV still has its header", empty, [expected])


def check_exit_codes():
    import output_writer as O
    equal("0 ok / 1 crash / 2 usage / 3 blocked / 4 empty / 5 api / 6 partial",
          (O.EXIT_BLOCKED, O.EXIT_NO_PRODUCTS, O.EXIT_API_ERROR, O.EXIT_PARTIAL),
          (3, 4, 5, 6))
    check("page_cap_reached is a COMPLETE stop reason",
          "page_cap_reached" in O.COMPLETE_STOP_REASONS)
    # `/explore` and `/careers` are each served at ONE address holding
    # their whole result set — measured, not assumed: every pagination
    # parameter tried returned a byte-identical payload — so a run that
    # stopped after one fetch fetched the whole route.
    check("single_page_route is complete by construction AND by measurement",
          "single_page_route" in O.COMPLETE_STOP_REASONS)
    # Carried for the family's shared vocabulary and unreachable here: this
    # site cannot clamp an out-of-range page back, having only one.
    check("page_echo_mismatch is complete",
          "page_echo_mismatch" in O.COMPLETE_STOP_REASONS)
    check("...and an enumeration that yielded nothing is NOT complete",
          "enumeration_empty" not in O.COMPLETE_STOP_REASONS)
    check("no_new_products is complete",
          "no_new_products" in O.COMPLETE_STOP_REASONS)


def check_a_run_that_finds_nothing_writes_nothing():
    """Never replace last night's good output with []."""
    from output_writer import save
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        with open(prefix + ".json", "w", encoding="utf-8") as f:
            f.write('[{"sku": "yesterday"}]')
        code = save([], prefix, "json", allow_empty=False)
        equal("an empty run exits 4", code, 4)
        equal("...and leaves the previous good file alone",
              open(prefix + ".json", encoding="utf-8").read(),
              '[{"sku": "yesterday"}]')
        code = save([], prefix, "json", allow_empty=True)
        equal("--allow-empty WRITES the empty file...", 
              json.load(open(prefix + ".json", encoding="utf-8")), [])
        # ...and still reports exit 4. Pinned deliberately (§10: pin a known
        # behaviour rather than half-guarding it): "zero businesses" is true
        # whether or not the file was written, and a caller that wanted the
        # file still wants to know the result was empty.
        equal("...and still reports exit 4, because it IS empty", code, 4)


def check_sidecar_shape():
    from output_writer import run_meta
    meta = run_meta(status="complete", stop_reason="single_page_route",
                    pages_requested=1, pages_completed=1, pages_failed=[],
                    products=390, mode="listings", source="mercor.com",
                    start_url="https://work.mercor.com/explore",
                    final_url="https://work.mercor.com/explore",
                    extra={"records_in_payload": 390, "urls_in_itemlist": 326,
                           "pages_available": 1, "route_is_paginated": False})
    for key in ("status", "stop_reason", "pages_requested", "pages_completed",
                "pages_failed", "mode", "source"):
        check("the sidecar records %r" % key, key in meta)
    equal("the sidecar carries how many records the payload held",
          meta["records_in_payload"], 390)
    # Both views, because the response has two and they disagree. Without
    # the second number a reader cannot tell that the site's own structured
    # index is 64 entries short of its own payload — which is the whole
    # reason this scraper does not read that index.
    equal("...and how many the site's own ItemList indexed",
          meta["urls_in_itemlist"], 326)
    equal("...and whether this route is addressable page by page",
          meta["route_is_paginated"], False)
    equal("pages_failed is a LIST of numbers, not a count",
          isinstance(meta["pages_failed"], list), True)


def check_engines_import_their_driver_at_module_level():
    """For the guarded imports above to MEAN anything.

    A sibling repo imported `launch`/`connect` inside the launch path, so the
    module imported cleanly with no pyppeteer installed: the group never
    skipped, and the CI job that exists to fail on unexpected skips could not
    have caught a broken import. It also let CI run against a stub version
    for a while without anything noticing. This drifts back silently, so it
    is asserted with an `ast` walk rather than trusted.
    """
    for module, driver in DRIVER_IMPORTS.items():
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            check("%s exists" % module, False)
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        top_level = set()
        for node in tree.body:          # module level ONLY
            if isinstance(node, ast.Import):
                top_level.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])
        check("%s imports %s at MODULE level" % (module, driver),
              driver in top_level,
              "top-level imports: %s" % sorted(top_level))


def check_shared_calls_bind_against_the_real_signature():
    """§17's check #1, and the one that earns its keep.

    A sibling repo shipped `classify(html, url=…)` in two of three engines
    against a callee taking `status` second, and BOTH crashed on their first
    fetch — invisible to import, --help, compileall, the undefined-name walk
    and 400+ green assertions, because none of those calls a function the way
    a live run does.

    This walks every engine's AST for calls into the shared modules and binds
    each one against the callee's real signature.
    """
    # EVERY shared module, not the three that are easy. A sibling repo
    # widened this after a key arrived and five calls into functions that
    # never existed came out of the credential-gated paths — the ones
    # nobody runs, for the obvious reason (CLAUDE.md §16). Two of the
    # modules below are reachable only with a key.
    import captcha_solver
    import diff_runs
    import env_config
    import fingerprint_client
    import output_writer
    import page_flow
    import product_parser
    import proxy_pool
    targets = {"page_flow": page_flow, "product_parser": product_parser,
               "output_writer": output_writer, "proxy_pool": proxy_pool,
               "fingerprint_client": fingerprint_client,
               "captcha_solver": captcha_solver, "env_config": env_config,
               "diff_runs": diff_runs}
    bound = 0
    for module in ENGINES + ("scraper_api_client",):
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        tree = ast.parse(source)
        # Which shared names this file imported directly (`from x import y`).
        direct = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in targets:
                for alias in node.names:
                    direct[alias.asname or alias.name] = (
                        targets[node.module], alias.name)

        # A name bound ANYWHERE in this file shadows a same-named module
        # (CLAUDE.md §22). An engine that takes `proxy_pool` as a parameter
        # is calling a method on an object, not a module attribute, and
        # without this rule that reported twenty-one false positives on a
        # clean repo in a sibling. Parameters count whether or not they
        # carry a type annotation — an annotation is not what makes a name
        # a local.
        shadowed = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                spec = node.args
                for arg in (list(spec.args) + list(spec.posonlyargs)
                            + list(spec.kwonlyargs)
                            + [a for a in (spec.vararg, spec.kwarg) if a]):
                    shadowed.add(arg.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                shadowed.add(node.id)
        local_targets = {name: mod for name, mod in targets.items()
                         if name not in shadowed}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            owner = attr = None
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id in local_targets:
                    owner, attr = local_targets[func.value.id], func.attr
            elif isinstance(func, ast.Name) and func.id in direct:
                owner, attr = direct[func.id]
            if owner is None:
                continue
            # A name that is NOT THERE is the loudest possible failure and
            # this check used to swallow it: `getattr(..., None)` returned
            # None, `not callable(None)` was true, and the call was skipped.
            # Three calls into a page_flow API that does not exist in this
            # repo -- comparable(), next_page_selector(),
            # next_page_candidates(), all of them Tokopedia's, all arriving
            # with copied code -- sat in two engines under a green run of
            # this very function. Absent is not "nothing to bind".
            if not hasattr(owner, attr):
                check("%s.%s exists (called from %s:%d)"
                      % (getattr(owner, "__name__", owner), attr,
                         module + ".py", node.lineno),
                      False,
                      "the engine calls a name the shared module does not "
                      "define; a live run reaches this as AttributeError")
                continue
            callee = getattr(owner, attr)
            if not callable(callee):
                continue
            if inspect.isclass(callee):
                # A CONSTRUCTOR is a call like any other, and skipping it
                # is how `SolveBudget(limit=…)` or `ProxyPool(rotate=…)`
                # with a wrong keyword reaches a live run untested. Bind
                # against `__init__` with `self` already supplied.
                try:
                    signature = inspect.signature(callee.__init__)
                    signature = signature.replace(
                        parameters=list(signature.parameters.values())[1:])
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    signature = inspect.signature(callee)
                except (TypeError, ValueError):
                    continue
            positional = [inspect.Parameter.empty] * len(node.args)
            keywords = {}
            for kw in node.keywords:
                if kw.arg is None:
                    break
                keywords[kw.arg] = inspect.Parameter.empty
            else:
                try:
                    signature.bind(*positional, **keywords)
                    bound += 1
                except TypeError as exc:
                    check("%s:%d %s.%s(...) binds against its real signature"
                          % (module, node.lineno,
                             getattr(owner, "__name__", owner), attr),
                          False,
                          "%s; signature is %s" % (exc, signature))
    check("every shared-module call in every engine binds (%d checked)" % bound,
          bound > 40, "only %d calls were checked — is the walk finding them?"
          % bound)


def check_engine_flag_sets():
    """§17's check #2: against the contract AND against each other, both ways.

    A missing flag fails; so does closing a difference the README documents.
    """
    sets = {}
    for module in ENGINES:
        if not os.path.exists(os.path.join(HERE, module + ".py")):
            continue
        sets[module] = _argparse_flags(module)
    for module, flags in sets.items():
        missing = (CONTRACT_FLAGS | SITE_FLAGS) - flags
        check("%s defines every contract flag" % module, not missing,
              "missing %s" % sorted(missing))
    # NO documented differences on this site, and that is a stronger
    # statement than a list: the three engines were generated from one
    # file with only their driver layer swapped, so their flag sets are
    # identical by construction. Growing a difference — in either
    # direction — has to be a decision, and this empty map is what makes
    # it fail the build until someone writes down why.
    DOCUMENTED_DIFFERENCES = {}
    names = sorted(sets)
    for i in range(len(names) - 1):
        a, b = names[i], names[i + 1]
        only_a = sets[a] - sets[b] - DOCUMENTED_DIFFERENCES.get(a, set())
        only_b = sets[b] - sets[a] - DOCUMENTED_DIFFERENCES.get(b, set())
        check("%s and %s define the same flags" % (a, b),
              not only_a and not only_b,
              "only in %s: %s; only in %s: %s"
              % (a, sorted(only_a), b, sorted(only_b)))


def check_banned_and_removed_flags():
    """Scoped to the ENGINES.

    `--country` is banned on the engines and there is no exception here:
    YouTube serves one site, and a country flag on a scraper could only
    contradict what the URL already says. The InnerTube `gl` this repo
    does take is `--region`, which is a rendering hint to the site rather
    than a claim about where the request comes from, and it is named
    differently for exactly that reason. On `fingerprint_client.py` the
    name `--country` is legitimate — there it picks a fingerprint locale,
    not a target — which is why this check is scoped to the engines rather
    than to the tree (CLAUDE.md §10).

    What the rule is really about is an option that can disagree with
    reality, and the three that CAN on this site are all refused with
    their reason rather than silently absorbed:

      * a URL that is not a profile — a hashtag feed or a video page has
        no account object on it, and "not a TikTok URL" would be a lie
        about a TikTok URL (CLAUDE.md §5);
      * `--pages` above 1, because a profile has exactly one page and a
        silent cap would report a complete run of duplicates;
      * `--proxy` with `--cdp-endpoint`, because the Scraping Browser
        already proxies and stacking two exits is not better cover;
      * more workers than there are accounts to fetch, which would leave
        idle threads and a log that overstates what the run did.
    """
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        for flag in BANNED_FLAGS:
            check("%s does not define %s" % (module, flag),
                  '"%s"' % flag not in source)
        check("%s refuses a region the library does not serve" % module,
              "NotAnAdsQuery" in source and "p.error" in source,
              "region=US would otherwise return a body that is not JSON")
        check("%s refuses --transport http with the reason" % module,
              "421" in source and "cannot work" in source,
              "failing later on a 421 is a status nobody can interpret")
        check("%s refuses --proxy with --cdp-endpoint" % module,
              "already proxies" in source,
              "the Scraping Browser already proxies; stacking two exits is "
              "not better cover")
        check("%s forces --concurrency to 1, and says why" % module,
              "cursor" in source and "concurrency" in source,
              "a second worker would have no request to make: the cursor "
              "for page 2 arrives inside page 1's response")


def check_undefined_names_in_every_module():
    """§10: compileall proves a file PARSES, not that its names RESOLVE.

    A live run of a sibling repo's pyppeteer engine died with NameError on a
    line reached only while fetching, after an import had been removed — the
    module imported cleanly, --help worked, compileall passed and CI was
    green. Kept COARSE (pooled bindings, no scope tracking) so it
    under-reports rather than inventing problems.
    """
    import builtins
    modules = [f for f in sorted(os.listdir(HERE))
               if f.endswith(".py") and f != "smoke_test.py"]
    for filename in modules:
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        # Module-level dunders exist without being assigned anywhere.
        defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__",
                                        "__package__", "__spec__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, ast.alias) and node.asname:
                defined.add(node.asname)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unresolved = sorted(used - defined)
        check("%s: every name resolves" % filename, not unresolved,
              "%s" % unresolved)


def check_dockerfile_copies_everything_the_entrypoint_imports():
    """§10: all three repos in this family shipped an image that died with
    ModuleNotFoundError on every invocation, --help included, because
    proxy_pool.py was missing from the COPY list. CI never built the image;
    this check needs no Docker."""
    path = os.path.join(HERE, "Dockerfile")
    if not os.path.exists(path):
        check("Dockerfile exists", False)
        return
    dockerfile = open(path, encoding="utf-8").read()
    # Only the COPY instructions, continuations included — a comment above
    # them naming a file is not a file the image carries.
    copy_lines, joining = [], False
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if joining or stripped.upper().startswith("COPY "):
            copy_lines.append(stripped)
            joining = stripped.endswith("\\")
    copied = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.py", " ".join(copy_lines)))
    entry = re.search(r'(?:CMD|ENTRYPOINT)\s*\[?\s*"?(?:python3?"?,\s*"?)?'
                      r'([A-Za-z_][A-Za-z0-9_]*)\.py', dockerfile)
    entrypoint = entry.group(1) if entry else "playwright_scraper"
    needed = _import_graph(entrypoint)
    missing = sorted(needed - copied)
    check("the Dockerfile COPYs every module %s.py imports" % entrypoint,
          not missing, "missing %s" % missing)
    for unwanted in ("smoke_test", "test_smoke"):
        check("the image does not carry %s.py" % unwanted,
              unwanted not in copied)


def check_env_example_documents_exactly_what_the_loader_reads():
    import env_config
    path = os.path.join(HERE, ".env.example")
    if not os.path.exists(path):
        check(".env.example exists", False)
        return
    documented = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+)\s*=", 
                                open(path, encoding="utf-8").read(), re.M))
    read = set(env_config.ENV_KEYS)
    check("every variable the loader reads is documented",
          not (read - documented), "undocumented: %s" % sorted(read - documented))
    check("every documented variable is actually read",
          not (documented - read), "unread: %s" % sorted(documented - read))


def check_a_copied_env_example_reads_as_UNSET():
    """§17: `cp .env.example .env` followed by a run must not connect.

    The placeholder check was a literal set in a sibling repo, and the two
    credentialled URLs are documented the way the vendor documents them —
    `ws://{login}-zone-…:{password}@cb.2captcha.com:9222` — so neither
    literal matched, the run connected with the string `{login}-zone-…` as
    its username, and got a 401 a long way from its cause.
    """
    import env_config
    example = os.path.join(HERE, ".env.example")
    if not os.path.exists(example):
        check(".env.example exists", False)
        return
    text = open(example, encoding="utf-8").read()
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, re.M))
    check("the example actually sets every variable",
          set(values) == set(env_config.ENV_KEYS),
          "example has %s, loader reads %s"
          % (sorted(values), sorted(env_config.ENV_KEYS)))
    # Every CREDENTIAL must read as unset. The default TARGET must not: it is
    # a real, usable URL, and blanking it would remove the one setting this
    # file exists to make convenient (§17's check #3 says exactly this — the
    # credentials unset, the non-credential default still usable).
    CREDENTIALS = {"TWOCAPTCHA_KEY", "TIKTOK_CDP_ENDPOINT", "TIKTOK_PROXY"}
    before = dict(os.environ)
    try:
        for name, raw in values.items():
            os.environ[name] = raw
            got = env_config.env_value(name)
            if name in CREDENTIALS:
                check("a copied .env.example leaves %s unset" % name,
                      got is None, "got %r" % got)
            else:
                check("...while %s stays a usable default" % name,
                      got == raw.strip(), "got %r" % got)
    finally:
        os.environ.clear()
        os.environ.update(before)
    # And the counter-check: a real credential must still come through, or
    # the placeholder rule would have made the loader useless. Deliberately
    # NOT 32 hex characters — that is the shape of a real 2captcha key, and
    # this repo's own credential scan (rightly) fails on one.
    try:
        os.environ["TWOCAPTCHA_KEY"] = "not-a-real-key-but-a-real-value"
        equal("a real value is still read",
              env_config.env_value("TWOCAPTCHA_KEY"),
              "not-a-real-key-but-a-real-value")
    finally:
        os.environ.clear()
        os.environ.update(before)


def check_credential_scan_is_one_implementation_invoked_from_both():
    """§17: two sources of truth, one dead and one holed.

    `.github/ci_checks.py` sat in three repos invoked by NOTHING, while
    tests.yml carried an inline grep doing a narrower version of the same job
    — one that matched only ws:// and wss://, so an http://user:pass@
    credential would have sailed past CI.
    """
    script = os.path.join(HERE, ".github", "ci_checks.py")
    check("the credential scan exists as a script", os.path.exists(script))
    if not os.path.exists(script):
        return
    workflow_dir = os.path.join(HERE, ".github", "workflows")
    workflow = os.path.join(workflow_dir, "tests.yml")
    # Triggered on the whole .github directory being absent, never on this one
    # file being missing: two suites in this family run INSIDE the Docker
    # image, which deliberately COPYs no .github/, and a check that quietly
    # starts passing once its input disappears is the same failure this
    # function is about (CLAUDE.md §22).
    if not os.path.isdir(workflow_dir):
        skip("ci-wiring", "no .github/ in this tree (the Docker image)")
    elif os.path.exists(workflow):
        text = open(workflow, encoding="utf-8").read()
        check("CI INVOKES the script rather than reimplementing it",
              "ci_checks.py" in text)
        # ...and does not ALSO reimplement it. The original version of this
        # check asserted only the first half, and the workflow carried inline
        # `python - <<EOF` copies of the --help and sample checks alongside
        # the call — justified in a comment as keeping the two from drifting
        # apart. They drifted: the inline sample copy still imported the row
        # dataclass under a name this repo renamed, and it failed on the
        # repo's FIRST push while the script it duplicated passed.
        #
        # Scoped to the OFFLINE job, because the docker job legitimately
        # names `sample_output.json` for a different purpose — asserting the
        # image does NOT contain it. A guard that fired there would be wrong,
        # and a guard people have to argue with is one they learn to
        # suppress.
        offline = text.split("  engine-smoke:", 1)[0]
        for marker, what in (("from output_writer import", "the row schema"),
                             ("sample_output.json", "the sample output"),
                             ("subprocess.run([sys.executable", "the --help contract")):
            check("the offline job does not reimplement the check for %s" % what,
                  marker not in offline,
                  "tests.yml's offline job mentions %r — one implementation, "
                  "in ci_checks.py, invoked from both" % marker)
        # And the guard must have had something to read, or it passed for the
        # wrong reason (CLAUDE.md §22).
        check("...and the offline job was actually found to scan",
              "ci_checks.py" in offline, "no offline job in tests.yml")
    result = subprocess.run([sys.executable, script, "--all"], cwd=HERE,
                            capture_output=True, text=True)
    check("the credential scan passes on this repo's own tree",
          result.returncode == 0,
          (result.stdout + result.stderr)[-600:])


def check_the_credential_scan_survives_a_venv_in_the_tree():
    """A guard people have to argue with is one they learn to suppress.

    Found by cloning this repo the way a stranger does and following the
    README: `python3 -m venv` puts a virtualenv in the working tree, and the
    credential scan walked into pip's vendored code and flagged a 32-hex
    string in `_elffile.py` as key-shaped. Correct about the string, wrong
    about the file, and the first thing a new user would have seen.

    The fix is structural rather than a longer list of names — a directory
    holding `pyvenv.cfg` is a virtualenv whatever it is called — and this
    pins BOTH halves, because narrowing a credential scan is exactly how one
    stops catching things. CLAUDE.md §22 records a sibling repo whose scan
    caught an UNTRACKED `.env.bak` holding a live key, so scanning must not
    be reduced to tracked files.
    """
    import importlib.util
    script = os.path.join(HERE, ".github", "ci_checks.py")
    if not os.path.exists(script):
        skip("credential-scan", "no .github/ in this tree (the Docker image)")
        return
    spec = importlib.util.spec_from_file_location("_ci_checks", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    check("the scan knows a virtualenv structurally, not by name",
          hasattr(mod, "_is_virtualenv"))
    if not hasattr(mod, "_is_virtualenv"):
        return

    with tempfile.TemporaryDirectory() as tmp:
        odd = os.path.join(tmp, "whatever-i-called-it")
        os.makedirs(os.path.join(odd, "lib"))
        open(os.path.join(odd, "pyvenv.cfg"), "w").write("home = /usr\n")
        check("...so a venv under any name is recognised",
              mod._is_virtualenv(pathlib.Path(odd)))
        plain = os.path.join(tmp, "src")
        os.makedirs(plain)
        check("...and an ordinary directory is not",
              not mod._is_virtualenv(pathlib.Path(plain)))

    # The other half: it must still walk files git does not track, because a
    # key pasted into a scratch file is the case this scan exists for.
    scanned = [str(p) for p in mod.scanned_files()]
    check("the scan still reads this repo's own files", len(scanned) > 20,
          "%d file(s)" % len(scanned))
    check("...and is not limited to git's index",
          "git ls-files" not in open(script, encoding="utf-8").read())


def check_banned_wording():
    """§12: enforced by this test rather than by review."""
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "__pycache__", ".pytest_cache", "node_modules")]
        for filename in files:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt",
                                      ".toml", ".html", ".example")):
                continue
            path = os.path.join(root, filename)
            text = open(path, encoding="utf-8", errors="replace").read().lower()
            for phrase in BANNED_WORDING:
                if phrase.lower() in text and filename != "smoke_test.py":
                    check("%s contains no %r" % (
                        os.path.relpath(path, HERE), phrase), False)
    check("banned-wording scan ran", True)


def check_worker_pools_start_on_different_exits():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    from proxy_pool import ProxyPool
    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"], rotate="per-run")
    firsts = [engine._worker_pool(pool, i).current for i in range(3)]
    equal("three workers start on three different exits",
          len(set(firsts)), 3)
    equal("a missing pool stays missing", engine._worker_pool(None, 0), None)


def check_fingerprint_kwargs_are_ones_the_driver_accepts():
    """§10: an unknown key in new_context(**kwargs) is a TypeError at launch,
    on the PAID path, at runtime."""
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    try:
        from fingerprint_client import playwright_context_kwargs
    except ImportError as e:
        skip("fingerprint", str(e))
        return
    sample = {"id": "x", "country": "US",
              "userAgent": "Mozilla/5.0 Chrome/140.0.0.0",
              "screen": {"width": 1920, "height": 1080},
              "timezone": "America/New_York", "language": "en-US",
              "devicePixelRatio": 2}
    kwargs = playwright_context_kwargs(sample)
    from playwright.sync_api import sync_playwright  # noqa: F401
    import playwright.sync_api as pw_api
    signature = inspect.signature(pw_api.Browser.new_context)
    unknown = [k for k in kwargs if k not in signature.parameters]
    check("every fingerprint kwarg is one new_context accepts", not unknown,
          "unknown: %s" % unknown)


def check_every_engine_exposes_the_same_public_surface():
    """The three engines are one file with three driver layers.

    Read from the SOURCE rather than from imported modules, and that is
    the fix rather than a style choice. The first version compared the
    engines that happened to import, so in a single-engine virtualenv —
    the only configuration this repo's README supports (§6: install
    exactly one) — it compared ONE engine against nothing and reported
    itself passed. Measured: 0 pairs compared, suite green.

    A third-party audit found the divergence it was supposed to find, by
    installing all three engines in one environment: a configuration the
    README tells people not to create. A check that only runs in an
    unsupported setup is a check nobody runs.

    An `ast` walk needs no driver installed, so the comparison now happens
    in every environment including one with no engine at all.
    """
    surfaces = {}
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        names = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target,
                                                                ast.Name):
                names.add(node.target.id)
        surfaces[module] = names

    equal("all three engines are present to compare", len(surfaces), 3)

    # What each engine legitimately holds that its twins do not: the names
    # its own driver layer needs. Everything else must match.
    DRIVER_LOCAL = {
        "playwright_scraper": {"sync_playwright", "PWError", "PWTimeout"},
        "puppeteer_scraper": {"launch", "connect", "asyncio", "concurrent",
                              "PyppeteerError", "NetworkError", "PPTimeout",
                              "_Loop", "_FETCH_JS", "RemoteBrowserError",
                              "CDP_CONNECT_TIMEOUT"},
        "selenium_scraper": {"webdriver", "WebDriverException", "SETimeout",
                             "ChromeOptions", "By", "_FETCH_JS",
                             "RemoteBrowserError", "_apply_fingerprint"},
    }
    names = sorted(surfaces)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            only_a = surfaces[a] - surfaces[b] - DRIVER_LOCAL.get(a, set())
            only_b = surfaces[b] - surfaces[a] - DRIVER_LOCAL.get(b, set())
            check("%s and %s expose the same names" % (a, b),
                  not only_a and not only_b,
                  "only in %s: %s; only in %s: %s"
                  % (a, sorted(only_a), b, sorted(only_b)))

    # And the shared half must really be shared: the names the runner
    # needs, in every engine, whatever its driver.
    for module, names_in in surfaces.items():
        for name in ("scrape", "parse_args", "PageOutcome", "MODES",
                     "_targets", "_window", "_run_ads", "_worker_pool",
                     "_open_session", "_prime_session",
                     "_fetch_with_policy", "handle_captcha_if_present",
                     "_proxy_failure", "_mask_credentials", "_driver_context",
                     "_rotate_if_per_page", "_open_http", "_call", "_dump",
                     "ENGINE_NAME"):
            check("%s defines %s" % (module, name), name in names_in)

    # The runtime shape of PageOutcome, for whichever engines DO import.
    for module in ENGINES:
        engine = _import_engine(module)
        if engine is None:
            continue
        outcome = engine.PageOutcome(number=1)
        for field_name in ("number", "url", "rows", "state", "status",
                           "blocked", "error", "diagnostics", "attempted"):
            check("%s.PageOutcome carries %r" % (module, field_name),
                  hasattr(outcome, field_name))
        equal("%s names the same modes" % module, tuple(engine.MODES),
              ("ads",))


def check_every_solve_is_counted_against_the_budget():
    """`SOLVES_PER_PAGE` is a MONEY limit, so every call that can buy must
    be counted — CLAUDE.md §23.

    `handle_captcha_if_present` is called TWICE per attempt in every engine
    in this family: once before the response is classified (so a challenge
    is cleared before anything is judged) and once after, for the state
    that says the page really is gated. In every sibling repo only the
    SECOND was counted, so the first bought a solve on every block attempt,
    for free and silently. Measured in a sibling on 2026-09-17 from an
    address where a real Cloudflare challenge rendered on every fetch: one
    page bought THREE Turnstile solves with the cap set to 1.

    This repo fixes it by construction rather than by discipline. The
    budget is an OBJECT created once per page and handed to both call
    sites, and it counts the spend inside itself — so a caller cannot
    forget to, which is the failure mode the sibling had. What this check
    pins is that the object is still the only way to spend.

    Inherited rather than measured here: this site has refused nothing, so
    no run of this repo has ever bought a solve.
    """
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        calls = source.count("handle_captcha_if_present(session, args, budget)")
        check("%s routes the solver through one entry point" % module,
              source.count("def _handle_captcha_in_browser") == 1,
              "the HTTP transport has no page to inject a token into, so "
              "the split is what keeps that answer in one place")
        check("%s calls the solver from two places, as designed" % module,
              calls == 2, "found %d call site(s)" % calls)
        check("%s creates exactly one budget per attempt loop" % module,
              source.count("budget = SolveBudget()") == 1,
              "%d budget object(s)" % source.count("budget = SolveBudget()"))
        check("%s never counts a spend by hand" % module,
              "solves_bought" not in source,
              "a hand-rolled counter is the thing SolveBudget replaces")

    # The object itself: the spend is counted inside, and the second
    # attempt is refused.
    budget = page_flow.SolveBudget()
    equal("the first spend is allowed", budget.spend(), True)
    equal("the second is not", budget.spend(), False)
    equal("and the count is kept by the object", budget.spent, 1)
    check("may_spend agrees with spend", not budget.may_spend())
    equal("a zero budget buys nothing", page_flow.SolveBudget(0).spend(), False)

    # And a solver call must sit behind it, not beside it.
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        # The browser handler, not the router. `handle_captcha_if_present`
        # now decides which transport it is on and returns early for the
        # HTTP one, so the budget lives one function further in — and this
        # check must follow it rather than reading a function that no
        # longer spends anything.
        handler = source[source.index("def _handle_captcha_in_browser"):]
        handler = handler[:handler.index("\n\n\n")]
        check("%s checks the budget before paying" % module,
              handler.index("budget.spend()") < handler.index("solve_recaptcha("),
              "the solver is reached before the budget is charged")

    equal("at most one purchase per page", page_flow.SOLVES_PER_PAGE, 1)


def check_a_dead_proxy_is_reported_as_a_proxy_failure():
    """CLAUDE.md §8: a proxy failure is not a timeout, and the two want
    opposite responses — another try at the same exit versus a different one.

    The engines all compute the reason (`_proxy_failure`) and, WITH a pool,
    log it on rotation. Without a pool — a single `--proxy`, which is the
    common case — an earlier version dropped it and reported only "gave up
    loading", so a refused proxy read exactly like a slow site. Found by
    running it rather than by reading it: `--proxy http://127.0.0.1:9`
    printed the generic message while `_proxy_failure()` had already
    identified ERR_PROXY_CONNECTION_FAILED.

    Asserted on the SOURCE rather than by launching a browser, because the
    branch only runs when a navigation fails and the suite must pass with no
    engine library installed at all.
    """
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        # Anchored on the FUNCTION that carries the decision, not on the
        # first `except _TransportError` in the file — which is the
        # client-version fallback in `_prime_session` and comes earlier.
        # The first version of this check read that block in all three
        # engines and failed on code that was correct. Reading the wrong
        # block is how a check like this passes, or fails, for the wrong
        # reason (CLAUDE.md §22).
        anchor = "def _fetch_with_policy("
        if anchor not in source:
            check("%s has a give-up branch to check" % module, False)
            continue
        start = source.index(anchor)
        end = source.find("\ndef ", start + 1)
        branch = source[start:end if end > 0 else len(source)]
        check("%s handles a transport failure at all" % module,
              "except (_TransportError, TransportError) as exc:" in branch,
              "both transports raise, and both must be caught: the browser "
              "sessions raise _TransportError and the HTTP one raises "
              "http_transport.TransportError")
        check("%s names the proxy when the proxy was the fault" % module,
              "exit_failed = _proxy_failure(exc)" in branch
              and "if exit_failed:" in branch,
              "the failure branch does not distinguish a dead exit")
        check("%s ROTATES on a dead exit rather than retrying it" % module,
              "pool.advance(exit_failed)" in branch,
              "a retry through the same dead exit is repetition, not a "
              "second attempt")
        check("%s rebuilds the browser when it rotates" % module,
              "_open_session(pw, args, pool)" in branch,
              "a rotation is a fresh browser, never a proxy swapped under "
              "a live session")
        check("%s still has a plain message for a non-proxy failure" % module,
              "failed after %d attempt(s)" in branch,
              "the non-proxy branch was lost")
        check("%s masks the exit it names" % module,
              "mask(session.proxy_url)" in branch,
              "host and port are the point of the log; the password is not")
    # ...and the detector the branch depends on must actually match the
    # string Chromium produces. Measured on a SIBLING repo 2026-09-17
    # against a dead
    # local port: `net::ERR_PROXY_CONNECTION_FAILED`.
    engine = _import_engine("playwright_scraper")
    if engine is None:
        skip("proxy-failure", "playwright_scraper not importable here")
    else:
        class _E(Exception):
            pass
        got = engine._proxy_failure(
            _E("Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://x/"))
        equal("the marker list matches what Chromium really raises", got,
              "ERR_PROXY_CONNECTION_FAILED")
        equal("...and a plain timeout is NOT read as a proxy failure",
              engine._proxy_failure(_E("Page.goto: Timeout 25000ms exceeded.")),
              "")


def check_engines_do_not_evaluate_a_string_in_the_browser():
    """§18: a site whose CSP omits `unsafe-eval` kills wait_for_function with
    an EvalError and takes the run down with exit 1, on the site's most
    obvious URL. Mercor has not been measured for that, and the cheap habit
    costs nothing on a site that would have allowed it."""
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)}
        for banned in ("wait_for_function", "waitForFunction", "waitFor"):
            check("%s never CALLS %s" % (module, banned), banned not in called,
                  "poll through page_flow.wait_for_count instead")


def check_credentials_never_reach_a_log():
    """§8: an EXCEPTION MESSAGE is a log, and the masker must be GLOBAL.

    A Playwright connection error repeats the endpoint five times (the
    message plus a four-line call log), so a masker handling only the first
    occurrence prints the password four times and looks like it is working.
    """
    for module in ENGINES:
        engine = _import_engine(module)
        if engine is None:
            continue
        masked = engine._mask_credentials(
            "tried ws://u:supersecret@h1:9222 and ws://u:supersecret@h2:9222 "
            "and again ws://u:supersecret@h1:9222")
        check("%s masks EVERY occurrence" % module,
              "supersecret" not in masked, masked)
        check("%s keeps the host and port, which are the useful half" % module,
              "h1:9222" in masked and "h2:9222" in masked, masked)
    from proxy_pool import mask
    masked = mask("http://user:secret@exit.example.com:2334")
    check("proxy_pool.mask hides the password", "secret" not in masked)
    check("proxy_pool.mask keeps the exit", "exit.example.com:2334" in masked)


def check_sample_output_matches_the_schema():
    from output_writer import Advertisement as RowClass
    expected = [f.name for f in fields(RowClass)]
    json_path = os.path.join(HERE, "sample_output.json")
    csv_path = os.path.join(HERE, "sample_output.csv")
    if not os.path.exists(json_path):
        check("sample_output.json exists", False)
        return
    rows = json.load(open(json_path, encoding="utf-8"))
    check("sample_output.json holds rows", bool(rows))
    equal("sample_output.json keys match the schema, in order",
          list(rows[0].keys()), expected)
    check("sample_output.json is from a real run (library.tiktok.com rows)",
          all(r["source"] == "library.tiktok.com" for r in rows))
    check("...and carries no fabrication markers",
          not any("lorem" in (r.get("title") or "").lower() or
                  "example.com" in (r.get("url") or "").lower()
                  for r in rows))
    # The sample is cut from a real run and NOT anonymised. An
    # advertiser in a statutory transparency library is a company that has
    # published its advertising by law, not a private individual — which
    # is the distinction CLAUDE.md §10 draws.
    check("the sample's rows point at the library",
          all("library.tiktok.com" in str(r.get("url", "")) for r in rows))
    check("...with numeric ad ids",
          all(str(r.get("sku", "")).isdigit() for r in rows))
    check("...each keyed by region and ad",
          all(r.get("row_key") == f"{r.get('region')}:{r.get('sku')}"
              for r in rows))
    check("...and row_keys are unique",
          len({r.get("row_key") for r in rows}) == len(rows),
          "an ad id is not unique across regions; the key is")
    check("...with a first-shown date in this century",
          all(str(r.get("first_shown_at", "")).startswith("20") for r in rows))
    if os.path.exists(csv_path):
        header = next(csv.reader(open(csv_path, encoding="utf-8")))
        equal("sample_output.csv header matches the schema", header, expected)


def check_a_failed_page_outranks_a_complete_stop_reason():
    """`finish_run` decides completeness from the reason AND the evidence.

    A named list of stop reasons cannot cover a failure recorded anywhere
    else, which is the same hole the exit-code unification closed one
    level up. Pinned in BOTH directions: a clean run must still be
    complete, or this rule would make every run partial and someone would
    turn it off.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for label, failed, want_status, want_rc in (
                ("with a failed page", [2], "partial", 6),
                ("with none", [], "complete", 0)):
            out = os.path.join(tmp, label.replace(" ", "_"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = output_writer.finish_run(
                    [Advertisement(sku="a"), Advertisement(sku="b")], out, "json", False,
                    blocked=False, stop_reason="page_cap_reached",
                    pages_requested=2, pages_completed=2, pages_failed=failed,
                    start_url="u", final_url="u", mode="ads")
            meta = json.load(open(out + ".meta.json"))
            equal("%s: status" % label, meta["status"], want_status)
            equal("%s: exit code" % label, rc, want_rc)


def check_per_page_rotation_actually_rotates_per_page():
    """The mode is named for what it does, which in this family it did not.

    Rotation here is BETWEEN REGIONS, because that is what one iteration
    of this run is: cursor pages within a region cannot be rotated
    between, since the cursor belongs to the session that received it.
    """
    engine = _import_engine("playwright_scraper")
    if engine is None:
        skip("playwright_scraper", "engine library absent")
        return

    served = payload("de_page1")

    def fetch(box, pw, args, pool, url, body, label):
        return 200, served, product_parser.STATE_CONTENT, False

    def run(rotate, regions):
        # Assembled from pieces, not written out: the credential scan reads
        # this file (CLAUDE.md §22).
        exits = ["http:" + "//" + "u" + ":" + "p" + "@one.example:1",
                 "http:" + "//" + "u" + ":" + "p" + "@two.example:2"]
        pool = proxy_pool.ProxyPool(exits, rotate=rotate)

        def body(FakeSession):
            args = _fault_args(engine, region=",".join(regions),
                               proxy_rotate=rotate, pages=1)
            box = {"session": FakeSession(), "prime_url": "u"}
            engine._run_ads(box, None, args, pool)
            return pool.rotations

        return _with_stubs(engine, fetch, body)

    for regions in (["DE"], ["DE", "FR", "GB"]):
        equal("per-run takes no exit over %d region(s)" % len(regions),
              run("per-run", regions), 0)
        equal("per-page takes one exit BETWEEN each of %d region(s)"
              % len(regions), run("per-page", regions), len(regions) - 1)

    check("a single-exit pool does not thrash",
          engine._rotate_if_per_page(
              {"session": None, "prime_url": "u"}, None,
              _fault_args(engine, proxy_rotate="per-page"),
              proxy_pool.ProxyPool(
                  ["http:" + "//" + "u" + ":" + "p" + "@only.example:1"],
                  rotate="per-page"), "why") is False,
          "rebuilding a browser to arrive at the same address buys nothing")


def check_a_site_that_answered_is_not_a_run_that_failed():
    """Exit 4 and exit 5 answer different questions, and two states sat on
    the wrong side of the line for a day.

    `comments_disabled` and `video_unavailable` are the site ANSWERING:
    this video takes no comments, this video is not there. Both arrive as
    HTTP 200 with a large, healthy payload. The honest code for a zero-row
    run is 4 — "we asked, and the answer was nothing" — and not 5, which
    means the content was never obtained at all and sends a reader to
    check a proxy that is working fine.

    They regressed to 5 when the family unified its exit codes: that rule
    keys on "did the run complete" rather than on a list of failure names,
    which is the right shape and stays. What was wrong was the COMPLETE
    set, which enumerated only the ways a pagination LOOP can end and not
    the ways a SITE can answer. Measured the day after: both URLs returned
    5 where they had returned 4.

    Pinned in both directions, because a rule that made everything
    complete would pass the first half of this check and be worse than the
    bug.
    """
    import tempfile
    answered = ("no_ads_found", "no_new_products")
    failed = ("page_load_timeout", "page_challenge", "page_error",
              "parse_error")

    for reason in answered:
        check("%r is a COMPLETE stop reason — the site answered" % reason,
              reason in output_writer.COMPLETE_STOP_REASONS,
              "a zero-row run reports 5 otherwise, which claims we never "
              "reached the site")
    for reason in failed:
        check("%r is NOT complete — we never got the content" % reason,
              reason not in output_writer.COMPLETE_STOP_REASONS)

    with tempfile.TemporaryDirectory() as tmp:
        for reason, want in ([(r, 4) for r in answered]
                             + [(r, 5) for r in failed]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = output_writer.finish_run(
                    [], os.path.join(tmp, reason), "json", False,
                    blocked=False, stop_reason=reason, pages_requested=1,
                    pages_completed=0, start_url="u", final_url="u",
                    mode="ads")
            equal("zero rows + %s -> exit %d" % (reason, want), rc, want)

        # And a blocked run outranks both: something stood between the run
        # and the content, which is neither "no answer" nor "no content".
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = output_writer.finish_run(
                [], os.path.join(tmp, "blocked"), "json", False,
                blocked=True, stop_reason="page_challenge", pages_requested=1,
                pages_completed=0, start_url="u", final_url="u",
                mode="ads")
        equal("a blocked run reports 3 whatever it stopped for", rc, 3)

    # The states themselves must still classify the way the parser says,
    # or the stop reasons above would never be reached.
    equal("an empty result still classifies as such",
          product_parser.detect_page_state({"data": [], "code": 0}, 200),
          product_parser.STATE_NO_ADS)
    equal("and the zero-byte 200 does not",
          product_parser.detect_page_state(b"", 200),
          product_parser.STATE_EMPTY_SUCCESS)


def check_a_fingerprint_is_applied_whole_or_not_at_all():
    """CLAUDE.md §24: a HALF identity is measured worse than none.

    Four defects lived on this path, every one of them a SILENT success —
    the call was accepted, the log said nothing, and the page disagreed
    with the fingerprint. All four were found by reading the values back
    out of a live page on 2026-09-21, which is what §24 tells you to do
    and which no amount of reading this code would have produced:

      1. `navigator.languages` reported `["en-US"]` against the
         fingerprint's `["en-US", "en"]`, because Playwright's `locale=`
         sets the PRIMARY language only.
      2. `navigator.userAgentData.brands` reported `HeadlessChrome/153`
         while the user agent claimed `Chrome/150` — the client hints are
         the half a `user_agent=` option leaves behind.
      3. Detaching the CDP session REVERTED the override, and the protocol
         reported success either way.
      4. In pyppeteer the init script never ran: `evaluateOnNewDocument`
         wraps its argument as a function expression, and
         `Page.addScriptToEvaluateOnNewDocument` silently does nothing
         until `Page.enable` has been sent — it answers
         `{"identifier": "1"}` regardless.

    These are asserted on the SOURCE and on the pure functions, because
    the branch needs a live browser and a paid key, and the suite must
    pass with neither.
    """
    import fingerprint_client as F

    # A fingerprint shaped like the ones the live API returns.
    fp = {
        "userAgent": {
            "value": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/150.0.0.0 Safari/537.36",
            "brandVersionList": [{"brand": "Not;A=Brand", "version": "8"},
                                 {"brand": "Chromium", "version": "150"},
                                 {"brand": "Google Chrome", "version": "150"}],
            "brandFullVersionList": [{"brand": "Chromium",
                                      "version": "150.0.0.0"}],
            "platform": "Windows", "platformVersion": "19.0.0",
            "architecture": "x86", "bitness": "64", "model": "",
            "mobile": False, "fullVersion": "150.0.0.0",
        },
        "navigator": {"platform": "Win32", "hardwareConcurrency": 32,
                      "deviceMemory": 32},
        "intl": {"languages": ["en-US", "en"], "contentLocale": "en-US",
                 "timeZone": "America/New_York"},
        "screen": {"width": 2560, "height": 1440, "deviceScaleFactor": 1.5},
        "webgl": {"vendor": "Google Inc.", "renderer": "ANGLE (NVIDIA)"},
    }

    metadata = F.user_agent_metadata(fp)
    check("a complete fingerprint yields client hints", bool(metadata))
    equal("the brands come from the fingerprint, not the browser",
          [b["brand"] for b in metadata["brands"]],
          ["Not;A=Brand", "Chromium", "Google Chrome"])
    equal("platform", metadata["platform"], "Windows")
    equal("platformVersion", metadata["platformVersion"], "19.0.0")
    equal("bitness", metadata["bitness"], "64")
    equal("mobile is a real bool", metadata["mobile"], False)

    # The refusal half: no brand list means no metadata, so the caller
    # leaves the hints alone rather than applying a fragment of one.
    equal("an incomplete fingerprint yields NO metadata",
          F.user_agent_metadata({"userAgent": {"platform": "Windows"}}), None)
    equal("...and neither does an empty one",
          F.user_agent_metadata({}), None)

    # Accept-Language carries no q-values, and the reason is written down.
    equal("Accept-Language is built without q-values",
          F.accept_language(fp), "en-US,en")
    check("...and why is recorded beside it",
          "q-value" in inspect.getdoc(F.accept_language),
          "Chromium derives navigator.languages from this string and keeps "
          "the qualifier, which no real browser reports")
    equal("no languages, no header", F.accept_language({}), None)

    # The init script carries the language list, which `locale=` cannot.
    script = F.playwright_init_script(fp)
    check("the init script carries navigator.languages",
          "'languages'" in script and "en-US" in script)
    check("...and the platform and WebGL strings with it",
          "'platform'" in script and "37446" in script)

    # And every engine applies the two TOGETHER.
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        check("%s applies the client hints beside the user agent" % module,
              '"userAgent": user_agent, "userAgentMetadata": metadata'
              in source,
              "a bare override is the thing §24 measured being refused")
        check("%s refuses to apply a partial identity" % module,
              "HALF identity is worse than" in source,
              "no brand list must mean no override at all")
        check("%s never detaches the session that carries it" % module,
              ".detach()" not in source,
              "detaching reverts the override, and the call succeeds anyway")
        # The timezone reaches the browser by a DIFFERENT route in each
        # engine, and that is legitimate rather than drift: Playwright
        # takes `timezone_id` as a context option, which is what
        # `playwright_context_kwargs` sets; the other two have no such
        # option and send `Emulation.setTimezoneOverride`. Naming the
        # route per engine is what keeps a missing one visible — all three
        # were verified against a live page reporting America/New_York on
        # 2026-09-21.
        route = ("playwright_context_kwargs" if module == "playwright_scraper"
                 else "Emulation.setTimezoneOverride")
        check("%s applies the fingerprint's timezone (via %s)"
              % (module, route), route in source,
              "a browser reporting UTC under a New York fingerprint "
              "contradicts itself on an axis any script reads")

    puppeteer = os.path.join(HERE, "puppeteer_scraper.py")
    if os.path.exists(puppeteer):
        source = open(puppeteer, encoding="utf-8").read()
        check("pyppeteer enables the Page domain before adding the script",
              'send("Page.enable"' in source,
              "without it the protocol answers success and runs nothing")
        check("...and does not use the wrapper that mangles the source",
              "evaluateOnNewDocument(\n" not in source
              and "page.evaluateOnNewDocument(" not in source,
              "that wrapper emits `(<source>)()` and Chromium drops the "
              "syntax error in silence")
        check("...and installs it through the raw protocol command instead",
              "Page.addScriptToEvaluateOnNewDocument" in source)


def check_the_credential_scan_covers_the_files_it_most_needs_to():
    """The repo's own guard was blind to its biggest files, twice over.

    Both were found by PLANTING a real-shaped key and running the scan
    rather than by reading it (CLAUDE.md §23), and both reported
    "nothing credential-shaped" over a file that held one:

      1. `.json` and `.csv` were not in `SCANNED_SUFFIXES` at all, so
         `fixtures_generated.json` — 500-odd KB of captured page payload,
         which is precisely where a front-end key or a session token
         arrives — was never opened.
      2. With the suffixes added it STILL passed, because the allowlists
         were applied per LINE and that fixture is a single line. It
         contains "sha" 69 times and "hash" 36 times, so one allowlisted
         token anywhere in it exempted every match in the whole file. A
         line-scoped allowlist becomes a FILE-scoped one the moment a file
         is one line.

    Added with no new allowlist entries, which is the point: the real
    fixtures and sample carry zero 32-hex strings and zero credentialled
    URLs, so the strictest rule now covers the largest files instead of
    acquiring an exception a real key could hide behind (CLAUDE.md §24).
    """
    sys.path.insert(0, os.path.join(HERE, ".github"))
    import ci_checks

    for suffix in (".json", ".csv"):
        check("the scan opens %s files" % suffix,
              suffix in ci_checks.SCANNED_SUFFIXES,
              "the generated fixtures and the committed sample are these")

    scanned = {str(p.relative_to(ci_checks.REPO))
               for p in ci_checks.scanned_files()}
    for name in ("fixtures_generated.json", "sample_output.json",
                 "sample_output.csv"):
        check("the scan reaches %s" % name, name in scanned,
              "it is committed, and it is captured payload")

    # The window, and that it is narrower than a one-line fixture.
    check("the allowlist is scoped to a window, not to a line",
          hasattr(ci_checks, "_allowed_near"),
          "a per-line allowlist exempts a whole one-line file")
    equal("a token beside the match still excuses it",
          ci_checks._allowed_near("md5 " + "a" * 32, 4, 36,
                                  ci_checks.HEX32_ALLOWED, lower=True), True)
    far = "md5" + " " * 400 + "b" * 32
    equal("a token 400 characters away does not",
          ci_checks._allowed_near(far, len(far) - 32, len(far),
                                  ci_checks.HEX32_ALLOWED, lower=True), False)
    check("the window is narrower than the fixture is long",
          ci_checks.ALLOWLIST_WINDOW * 2 <
          len(json.dumps(FIX, ensure_ascii=False)),
          "otherwise the fixture is one window and nothing is scoped")

    # And the escaped-quote half: a fixture stored as JSON escapes every
    # quote inside it, so a pattern with plain quotes matches nothing.
    #
    # The sample is ASSEMBLED from pieces rather than written out, and that
    # is not fussiness — the scan now reads this file, and a literal
    # credentialled URL here would make the check fail on its own test
    # data. CLAUDE.md §22: a note about a banned string is a use of it, and
    # assembling is what lets the scan cover the suite instead of
    # exempting the one file most likely to acquire a pasted secret.
    sample = "ws:" + "//" + "acct7" + ":" + "s3cr3tpw" + "@" + "host:9222"
    check("the credentialled-URL pattern tolerates an escaped quote",
          ci_checks.CREDENTIALLED_URL.search('{"e": \\"%s\\"}' % sample)
          is not None)
    check("...and the bare form too",
          ci_checks.CREDENTIALLED_URL.search('{"e": "%s"}' % sample)
          is not None)
    # A documented placeholder must still be allowed, or the check becomes
    # one people switch off.
    placeholder = "ws:" + "//" + "user" + ":" + "pass" + "@" + "host:9222"
    check("a documented placeholder is still allowed",
          ci_checks._allowed_near(placeholder, 0, len(placeholder),
                                  ci_checks.CREDENTIAL_ALLOWED))


def check_ci_greps_for_a_sentinel_this_suite_can_actually_emit():
    """The `engine-smoke` job must be ABLE to fail.

    That job exists for one reason (CLAUDE.md §10): "skipped, engine absent"
    reads identically to a real import error, so CI installs each engine and
    fails if THAT engine's group still reports a skip. It works by grepping
    the suite's own output for a sentinel.

    The inherited version grepped for `"<engine>_scraper could not be
    imported"` — a string no suite in this family emits. Checked 2026-09-18,
    seven sibling repos carry the same dead grep, so in none of them could
    the job ever have failed. It passed for the wrong reason, which is
    §22's "a check that swallows the loudest failure it could report".

    This check is the guard against that coming back: whatever sentinel the
    workflow looks for, this suite has to be capable of printing it. It
    verifies the sentinel against `skip()`'s real output format rather than
    against a copy of the string, so changing either one without the other
    fails here.
    """
    workflow = os.path.join(HERE, ".github", "workflows", "tests.yml")
    if not os.path.isdir(os.path.join(HERE, ".github")):
        # §22: trigger on the WHOLE .github directory being absent — which
        # is the Docker image, where it is deliberately not COPYed — and
        # never on a file inside it going missing, because a check that
        # quietly starts passing once its input disappears is the failure
        # mode this whole function is about.
        skip("ci-sentinel", "no .github/ in this tree (the Docker image)")
        return
    check("tests.yml exists", os.path.exists(workflow))
    if not os.path.exists(workflow):
        return
    text = open(workflow, encoding="utf-8").read()

    # What `skip()` actually prints, derived rather than quoted.
    import io as _io, contextlib as _contextlib
    buf = _io.StringIO()
    before = len(SKIPS)
    with _contextlib.redirect_stdout(buf):
        skip("playwright_scraper", "engine library absent (probe)")
    del SKIPS[before:]          # leave the run's real skip list untouched
    printed = buf.getvalue()
    check("skip() prints a line naming the engine", "playwright_scraper" in printed,
          repr(printed))

    # The sentinel the workflow greps for, with the matrix placeholder
    # resolved the way Actions would resolve it.
    greps = re.findall(r'grep -q(?:E)? "([^"]*matrix\.engine[^"]*)"', text)
    check("the engine-smoke step greps for something", bool(greps),
          "no grep against ${{ matrix.engine }} found in tests.yml")
    for pattern in greps:
        resolved = pattern.replace("${{ matrix.engine }}", "playwright")
        check("the CI sentinel %r is a string this suite can emit" % resolved,
              resolved in printed,
              "the workflow greps for %r but skip() prints %r — the job "
              "cannot fail" % (resolved, printed.strip()))

    # And the other half: the step must confirm the suite RAN, or a crash on
    # line one sails past a grep for an absent string.
    check("the engine-smoke step also asserts the suite ran to completion",
          "checks passed" in text,
          "nothing in tests.yml checks for the suite's summary line")


def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description="tiktok-profile-scraper offline suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    VERBOSE = args.verbose

    global _TREE_BEFORE
    _TREE_BEFORE = _tree_state()

    for fn in CHECKS:
        if VERBOSE:
            print("\n== %s" % fn.__name__)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — a broken check is a failure
            import traceback
            FAILURES.append("%s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            print("  ERROR %s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            if VERBOSE:
                traceback.print_exc()

    print("\n%d checks passed, %d failed, %d group(s) skipped."
          % (PASSED, len(FAILURES), len(SKIPS)))
    for line in SKIPS:
        print("  skipped: %s" % line)
    if FAILURES:
        print("\nFailures:")
        for line in FAILURES:
            print("  - %s" % line)
        return 1
    return 0


def check_no_module_defines_a_name_twice():
    """A second `def` of the same name silently replaces the first.

    Found in this family by the offline suite rather than by reading: an
    assembled engine carried TWO copies of its whole shared layer —
    `_open_http`, `_prime_session`, `handle_captcha_if_present`, `_call`,
    `_fetch_with_policy`, `_rotate_if_per_page` and `_worker_pool`, ten
    functions, four hundred lines. Python bound the second copy and threw
    the first away.

    Nothing else could see it. The module imported, `--help` worked,
    `compileall` passed, the undefined-name walk was clean, every engine
    produced correct rows, and all three agreed with each other — because
    the two copies were IDENTICAL. What gave it away was a check counting
    solver call sites, which found four where the design has two.

    That is the dangerous shape: a duplicate that is currently harmless
    and becomes a silent wrong answer the moment somebody edits one copy.
    CLAUDE.md §22's statement-after-return check is the same family of
    defect — something the parser accepts and a reader never sees.
    """
    import collections
    for name in ENGINES + ("product_parser", "page_flow", "output_writer",
                           "proxy_pool", "env_config", "diff_runs",
                           "captcha_solver", "fingerprint_client",
                           "http_transport", "scraper_api_client",
                           "tiktok_payload", "smoke_test"):
        path = os.path.join(HERE, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        seen = collections.Counter(
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)))
        duplicates = {k: v for k, v in seen.items() if v > 1}
        check("%s.py defines no name twice" % name, not duplicates,
              "%r — the later definition silently replaces the earlier, and "
              "nothing else in this repo can see that" % duplicates)


def check_no_statement_follows_a_return_in_the_same_block():
    """CLAUDE.md §22: found the same fifteen dead lines in six repos.

    A function whose `def` line had been lost, leaving its docstring and
    body absorbed into the end of the function above it. It parses, it
    imports, `--help` works, `compileall` passes — and the
    undefined-name walk cannot see it and SHOULD not, because it pools
    every binding in a file rather than tracking scopes.

    Measured across eighteen repos when it was written: 6 hits, 0 false
    positives.
    """
    terminators = (ast.Return, ast.Raise, ast.Break, ast.Continue)
    hits = []
    for path in sorted(pathlib.Path(HERE).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, terminators):
                        nxt = block[i + 1]
                        hits.append("%s:%d (after %s on line %d)"
                                    % (path.name, nxt.lineno,
                                       type(stmt).__name__.lower(),
                                       stmt.lineno))
    check("no statement follows a return/raise/break/continue", not hits,
          "; ".join(hits[:6]))


def check_the_waf_interstitial_is_recognised_and_curable():
    """The THIRD shape of refusal on this site, and the only curable one.

    Measured 2026-09-22, `GET /@nasa` with a plain HTTP client:

        from this datacentre address (Hetzner, Helsinki)   3 of 3 served
        from a residential pool, nine exits                2 of 9 served

    The other seven answered HTTP 200 with 1,462 bytes whose visible text
    is "Please wait..." and whose body carries TikTok's WAF challenge. So
    a residential proxy is MEASURABLY WORSE than no proxy on this route —
    22% against 100% — which inverts this family's usual instinct and is
    why the README says so.

    And a browser CLEARS it: driving Chromium through the very exits that
    refused a plain HTTP client, 3 of 3 served and 0 still challenged. It
    is a JavaScript challenge, not a captcha.
    """
    page_html = WAF_FIXTURE
    equal("the interstitial gets its own state",
          product_parser.detect_page_state(page_html, 200),
          product_parser.STATE_WAF_CHALLENGE)
    state = product_parser.STATE_WAF_CHALLENGE
    check("it counts as blocked, which is what makes --transport auto "
          "switch to a browser", page_flow.counts_as_blocked(state),
          "a browser is the measured remedy; the engines reach for one on "
          "any blocked state")
    check("it is retried", page_flow.should_retry(state))
    check("it pays NO solver", not page_flow.should_solve(state),
          "the page carries no widget and no sitekey — paying would buy a "
          "request the API cannot fulfil")
    check("and is never parsed", not page_flow.should_parse(state))

    # It must NOT be confused with the two other HTTP-200 refusals, nor
    # with a page this parser simply failed to read — that last one would
    # point a reader at the parser instead of at their exit.
    check("it is not read as a parse error",
          product_parser.detect_page_state(page_html, 200)
          != product_parser.STATE_PARSE_ERROR)
    check("it is not read as the shop's captcha",
          product_parser.detect_page_state(page_html, 200)
          != product_parser.STATE_CHALLENGE)

    # CLAUDE.md §18: count every marker on pages you KNOW are good.
    for marker in tiktok_payload.WAF_CHALLENGE_MARKERS:
        for name, served in _SERVED_PAGES():
            check("%r does not appear on served page %s" % (marker, name),
                  marker not in served,
                  "a marker that fires on a good page is worse than none")
    check("'Please wait' is deliberately NOT a marker",
          not any("please wait" in m.lower()
                  for m in tiktok_payload.WAF_CHALLENGE_MARKERS),
          "ordinary English that a caption or a bio can contain")


def check_the_scraping_browsers_own_extension_does_not_read_as_a_challenge():
    """The marker check, against a page fetched the way a PAID run fetches.

    CLAUDE.md §21 records a guard that passed for the WRONG REASON: it ran
    only against captures taken with a plain HTTP client, which carry no
    extension injection at all. Every other fixture in this repo is such a
    capture. This one is the material a real `--cdp-endpoint` fetch adds.

    It was a recorded SKIP until a live Scraping Browser profile turned up
    on 2026-09-22 — and the reason it was a skip, rather than an omission,
    is that "not known to be injected" is not "measured not to be".
    """
    served = _SERVED_PAGES()
    check("there are served fixtures to test against", bool(served))

    # The marker set must score zero on a served page WITH the injection
    # spliced into it — which is what a paid run actually receives.
    for name, html in served:
        with_injection = html.replace("<body>", "<body>" + CDP_INJECTED)
        hits = product_parser.challenge_markers_present(with_injection)
        check("no marker fires on %s fetched over --cdp-endpoint" % name,
              not hits,
              "fired: %s — the Scraping Browser's own extension is being "
              "read as the site's challenge" % hits)
        waf = tiktok_payload.waf_markers_present(with_injection)
        check("...and no WAF marker either, on %s" % name, not waf,
              "fired: %s" % waf)

    # And the set must score zero WITHOUT an extension strip, or the strip
    # becomes load-bearing and the next marker added inherits the hole
    # (CLAUDE.md §24).
    for marker in tiktok_payload.BOT_CHALLENGE_MARKERS:
        check("marker %r is absent from the injected material" % marker,
              marker not in CDP_INJECTED,
              "a marker the extension injects would report every paid run "
              "as blocked")

    # The specific names this family has been burned by, pinned as
    # PRESENT in the injection — so that if a future edit adds one to the
    # marker set, the check above fails loudly rather than the repo
    # shipping a scraper that reports exit 3 on a full catalogue.
    for burned in ("cf-turnstile", "captcha-widgets", "hunter.js",
                   "data-ts-input"):
        check("the extension really does inject %r" % burned,
              burned in CDP_INJECTED,
              "if this stops being true the fixture is stale — recapture "
              "it over --cdp-endpoint")


CHECKS = [v for k, v in sorted(globals().items()) if k.startswith("check_")
          and callable(v) and k != "check"]

if __name__ == "__main__":
    sys.exit(main())
