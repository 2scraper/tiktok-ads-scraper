# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Mercor changing its markup is the normal way this stops working, and it has
its own issue template. The detail that saves the most time is WHICH anchor
broke — and on this site that is never a CSS selector, because the parser
does not read the DOM at all.

There are **three** structured sources here, and a break is usually in one
of them and not the others:

1. **`<script id="__NEXT_DATA__">` and its `nonce`.** Every page on this
   site carries a nonce on that tag, so a regex written without one matches
   nothing — and the failure reads like the site having stopped
   server-rendering rather than like a typo. `extract_next_data()` returning
   None on a page you can see rows in is the symptom.

2. **The React Query cache path** —
   `props.pageProps.dehydratedState.queries[*].state.data.listings` on
   `/explore`, `props.pageProps.role` on a detail page,
   `props.pageProps.jobs` on `/careers`. Every query is searched rather than
   only `queries[0]`, because a cache's order is an implementation detail of
   whichever component prefetched first. A rename here goes QUIET: rows stop
   appearing rather than appearing wrong. `CORE_FIELDS` in the engines is
   the guard — a 99% floor on the four columns Mercor filled on 390 of 390
   captured records.

3. **The `JobPosting` JSON-LD on a detail page**, which is the only source
   that states a **currency**. Note what it is NOT read for: its
   `baseSalary.value.unitText` said `HOUR` on 3 of 40 sampled pages whose
   own `payRateFrequency` was `per-task`, and it omits `baseSalary`
   entirely for `one-time` pay. Period and amounts come from the record;
   only the currency and the schema.org employment type come from here.

The `ItemList` JSON-LD on `/explore` is deliberately **not** a source. It
indexed 326 URLs against 390 records on 2026-09-18, omitting all 55
`evergreen` listings plus 9 standard. If a future change makes it the
LARGER view, that means the React Query path has started dropping rows —
`listing_meta()` warns on exactly that inversion.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once.
   Unlike most siblings in this family the canary here is **not** gated on a
   secret: every route this scraper reads is served to a bare GitHub runner,
   so it runs a real 3-page scrape daily and is expected to be GREEN. That
   is not a convenience — it is what keeps the README's central claim
   honest. If Mercor ever puts these routes behind a challenge or a key, the
   badge goes red the next morning and the claim is retested without anyone
   having to remember to.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions with inline HTML/JSON fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

Six properties in this repo exist because they were once absent or were
measured against expectation, and cost real time. Tests pin all six, so a PR
that breaks one will fail rather than silently regress:

- **A marker that matches every page is worse than no marker**, and this
  repo made that mistake and caught it within the hour. Mercor is fronted by
  Cloudflare, and `/cdn-cgi/challenge-platform` appears **once on every one
  of seven captures** — `/explore`, `/careers`, the homepage, two detail
  pages and BOTH 404s. It is Cloudflare's ordinary bot-management beacon,
  not a challenge. With it in `BOT_CHALLENGE_MARKERS`, asking for a listing
  that no longer exists reported exit 3 (blocked) instead of `not_found`,
  which sends a reader hunting for a proxy problem that does not exist.

  `challenges.cloudflare.com` was 0 on all seven and is what is carried.
  `cf-turnstile` is NOT, and that is deliberate rather than an oversight:
  2Captcha's Scraping Browser injects it into every page it loads, and two
  sibling repos measured it firing on good pages while being ABSENT from
  real challenges. `smoke_test.py` pins both directions — no marker may fire
  on a page the site served, and the excluded string must really be PRESENT
  on one, or excluding it would be a precaution against nothing.

  Before adding any marker: count it on a page you know is good.

- **Mercor's own captcha IS configured on the marketplace host, and a
  capture cannot see it.** `www.mercor.com` runs none at all — measured in
  a live browser, zero captcha requests on the home page and on `/careers`
  — so this applies to `--mode listings` and `--mode job` only.
  Invisible reCAPTCHA **Enterprise**, sitekey
  `6LcUUCgsAAAAAD_LMM5QDj1qUwsfKYDbNKa0v5wO`, `size=invisible`, found
  through `___grecaptcha_cfg` on the first live run. The sitekey is in no
  served HTML and in none of the eagerly-loaded JS bundles — it arrives in a
  lazily loaded chunk — so grepping a saved page for the site's own captcha
  config finds nothing, and only a real browser reveals it.

  A v3 widget renders no challenge frame and never blocks, so it must never
  be paid for. The `already_rendered` gate is what enforces that, and it is
  only as good as the selector it counts with: the first live run had a
  readiness selector that matched the SERVED markup and not the hydrated
  DOM, so it read 0 anchors on a page holding all 390 rows and went to
  "attempting to solve". With a key set, that is a charge per page for
  nothing.

- **Anything anchored to the DOM must be checked in a real browser.**
  Hydration rewrites every job anchor on this site: the server sends
  `href="/jobs/list_…"` and React replaces it with
  `href="/explore?listingId=…"`. A saved capture is the PRE-hydration
  document, so a selector verified against one can match zero elements
  live — silently, because the rows come from `__NEXT_DATA__` either way.

- **Never write that a captcha cannot be solved.** Write that this repo does
  not implement X. 2Captcha solves enterprise reCAPTCHA
  (`RecaptchaV2EnterpriseTaskProxyless`) and Cloudflare Turnstile
  (`TurnstileTaskProxyless`), and this repo builds both. A detection without
  a sitekey **refuses to build a task** rather than paying for one the API
  will reject.

- **Gating is per ROUTE, not per site.** `/role/…`, `/jobs` and
  `/jobs/{id}-{slug}` are served to a residential exit; `/company/{slug}` is
  refused to one, to plain HTTP and to a real browser alike, 3 of 3 each.
  There is no `--mode company` and adding one would need a measurement, not
  an idea.

- **Walking past the last page does not fail — it repeats.** `?page=48` of a
  47-page listing answers HTTP 200 carrying page 1's rows again. Two
  defences, and the second is the one to keep: runs plan against the site's
  own `pageCount`, and the response states which page the SERVER used inside
  its Apollo cache key, so a mismatch is an unambiguous end-of-listing rather
  than a "no new sku" heuristic.

- **`/jobs` has no addressable pages, and `/role/…` does.** `?page=2` on the
  feed returns the identical 46 job ids as page 1 — it does not fail and does
  not empty. `page_flow.pagination_is_addressable()` answers per URL for
  exactly this reason; a `page_url()` used unconditionally would report a
  complete multi-page run holding one page several times.

- **Pay is a rendered STRING, and three of its shapes break a naive
  parser.** The `•` separates salary from equity and either side may be
  absent (2 of 312 values are an equity range with no salary at all, so
  taking the part before the bullet writes 0.5 into a salary column). `L` is
  the Indian lakh, 1e5 — `₹30L – ₹80L` is 3,000,000 to 8,000,000. And
  `No equity` is a STATEMENT: `has_equity=False` is a fact the listing
  published, `None` is silence. `salary_period` stays null on a listing row
  because the string carries no period; only the detail page states one.

Plus the family's own invariants, which are not negotiable:

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows — including a query that genuinely
  matched nothing, which is a correct answer — `5` remote API error, `6`
  partial. A pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.** A query
  that matched nothing was served exactly as asked.
- **Credentials never reach argv or a log, and an exception message is a
  log.** The masker is global rather than first-occurrence: a Playwright
  connection error repeats the endpoint five times.
- **Merge in page order, not arrival order**, so concurrency cannot change
  the output.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha
classifier and the CLI contract against inline fixtures. If yours genuinely
needs mercor.com, say in the PR what you ran, which mode and URL, from
which exit, and what you got — including the sidecar's `records_in_payload`
and `urls_in_itemlist` (or `jobs_enumerated`/`jobs_fetched` for `--mode
job`) and the coverage lines the run prints.

Four things about running this live that are specific to Mercor:

* **You need nothing.** No key, no proxy, no account. Every route was
  served to a bare Hetzner datacentre address on 2026-09-18, to `curl`, to
  `python-requests` and to an empty User-Agent. If a run is refused, that
  is NEW, and the saved debug HTML is the finding — say so in the issue
  rather than reaching for a proxy.
* **Selenium cannot send proxy credentials.** `--proxy-server` takes an
  address only, so that engine strips them and warns. This matters less
  here than on most sites, because no proxy is needed at all — but if you
  are testing the proxy path, that is the limitation.
* **The slot counters are live and drift within minutes.** Two runs of
  `--mode listings` a few minutes apart differed on exactly one row:
  `remaining_slots` 30 → 29 and `supplied_slots` 0 → 1, because a
  contractor was supplied in between. A small difference between two runs
  is the site, not a regression. The ORDER, by contrast, was measured
  stable — three fetches returned the identical 390 ids in the identical
  order.
* **Be polite about `--mode job`.** It can fetch 462 detail pages. There is
  no rate limiting that we found, which is a reason to be careful rather
  than a licence: use `--delay`, and do not run the full enumeration to
  test a one-line change.

**Run more than the primary engine.** "Mirror them exactly" is a design rule,
not a verification, and running the mirrors for the first release found two
defects that import, `--help`, `compileall` and the whole offline suite all
missed. All three were run live across all three modes and returned
identical rows; that is the bar.

Do not add anything that submits a form. Mercor's pages carry an apply
flow and a sign-up flow, and this project must never touch either — an
application submitted by a scraper is a false record about a real person and
a real company.

## Scope

This repo scrapes **public pages** on mercor.com: the marketplace index, the
jobs feed and individual job pages, exactly as an anonymous visitor is served
them. It reads only routes `robots.txt` allows.

Out of scope: anything behind a login, anything that submits a form
(including the apply and sign-up flows), anything that defeats a protection
rather than passing it the way an ordinary browser does, and candidate
profiles — `/u/` is disallowed by `robots.txt`, there is deliberately no mode
for it, and adding one would be a product decision about personal data rather
than a bug fix.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.

## The `§` references in the comments

Comments and docstrings here cite section numbers — "CLAUDE.md §18", "§24".
That file is the 2scraper family's internal conventions document and is
deliberately **not** in this repository: it applies to every scraper in the
family and is not specific to this one.

Nothing is lost by not having it. The rule a reference points at is always
written out in full beside the reference, because a comment whose reasoning
lives somewhere else is not a comment. Read the `§` as provenance — "this
paragraph exists because it cost another repo real time" — and the sentence
around it as the whole of the argument.

If you find a reference whose reasoning is NOT spelled out beside it, that
is a bug in the comment; please open an issue.
