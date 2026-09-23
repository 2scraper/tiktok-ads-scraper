# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and SemVer as closely as a CLI toolkit can. A patch means **fixes**; where
a default changes in one, the note leads with it.

## [Unreleased]

### Fixed

> **`diff_runs.py` compared almost nothing.** Its `TRACKED_FIELDS` were
> tiktok-profile-scraper's account columns, 26 of which `Advertisement`
> does not have, so a diff of two runs reported "0 changed" whenever only
> an ad's run dates, review status or audience changed. It now tracks this
> repo's own columns and prints each run's share of the library's
> per-region total. `smoke_test.py` pins every tracked name against the
> dataclass and checks that a changed column is actually reported.

> **A copied `.env.example` set `TIKTOK_URL=nasa`**, a TikTok handle, which
> is not a region. It is now `DE`.

- **Donor prose removed from the shared core.** `output_writer.py`,
  `diff_runs.py`, the engines, `page_flow.py`, `smoke_test.py`,
  `.github/ci_checks.py` and the `Dockerfile` carried text from the repos
  this core was copied from — YouTube comment threads, `--sort top`,
  reply threads, job listings, "the business", `--mode comments --out
  software-engineer` — describing those sites as if they were this one.
  Rewritten from this repo's own README, code and fixtures, or deleted
  where there was no measured equivalent. Explicit sibling provenance
  ("measured on tiktok-profile-scraper's route", "a sibling repo
  (youtube-scraper) had…") is kept and now says whose it is.
- `output_writer.py`'s docstring described `--mode profile` and account
  statistics; `page_flow.py`, the engines and `smoke_test.py` described a
  profile page, an account, `video_unavailable` and the embed window. All
  now describe the Ad Library (or are gone).
- The engines' `_rotate_if_per_page` said nothing one fetch receives feeds
  the next; on this route the `search_id` cursor does, and it now says so.
- `smoke_test.py`'s `run_meta` test data is an `ads` run on
  `library.tiktok.com` rather than a `profile` run on `@nasa`.
- `.github/ci_checks.py` no longer exempts an `avatar_id` column this repo
  does not have from the credential scan.

- `captcha_solver.py`'s docstring pointed at a "No DataDome solver" section
  that does not exist in this repo (it came with the copied core). Removed.
- **The Scraper API engine failed on every `--wait-text` / `--wait-element` /
  `--wait-state` call, and was billed for it.** It sent `waitFor` as a
  JSON-encoded string; measured 2026-09-23 the live API answers that with
  HTTP 422 "params.waitFor must be an object" and still charges $0.0005,
  while the same request with an object is answered 200. It is now sent as
  an object. (The target status was already read from `http_code`; the new
  regression check pins that too, driving the real `fetch_html` with
  `requests.post` stubbed.)
- **`--wait-state networkidle` is refused by the Scraper API** (HTTP 422
  "params.waitFor.state must be one of: load, domcontentloaded", still
  billed — measured 2026-09-23 on a sibling repo with the object-shaped
  `waitFor`). The choice is removed.

## [0.1.1] — 2026-09-23

> **Correction to v0.1.0.** Its `captcha_solver.py` docstring described a
> 2Captcha captcha-solving method for TikTok as available. That method is
> deprecated, and the text no longer offers it. The challenge policy for
> TikTok's slide puzzle now says `solve: False`, which matches what the
> code does: no solver for it is implemented.

## [0.1.0] — 2026-09-22

First release. Reads TikTok's EU Ad Library — the transparency endpoint
TikTok serves without a key, an account or a proxy.

### One header gates the whole thing

Measured 2026-09-22 from a bare datacentre address (Hetzner, Helsinki):

    with the browser's headers                         HTTP 200, 12 ads
    the same, minus `x-ccl-str`                        HTTP 421
    content-type + user-agent + referer + x-ccl-str    HTTP 200, 12 ads

That header is minted by the library's own JavaScript — there is no global
to read it from, and a plain `fetch` issued from inside the page gets 421
too. So a browser is a PREREQUISITE here rather than a fallback, which is
the reverse of this repo's two siblings, and `--transport http` is refused
up front with that reason.

### The decoys

- **`offset` does nothing.** The body carries one, the library's own UI
  increments it, and offsets 0, 12 and 24 returned the identical twelve
  ads in the identical order. Pagination is by a base64 `search_id`
  cursor, and the END of the listing is read from that cursor rather than
  from `has_more` — a cursor that stops advancing ends the run.
- **`limit` does nothing.** 12, 50 and 100 all return twelve.

A scraper that trusted either would report a complete run of duplicates.

### The bug this release found in itself

**An ad id is not unique.** An EU ad buy commonly runs in several member
states at once and the library lists it under each. Measured, page one of
five regions within the same few seconds:

    DE vs IT   11 of 12 ad ids shared
    DE vs FR    9 of 12
    DE vs PL    9 of 12
    DE vs GB    1 of 12      <- outside the EU, bought separately

Deduping on `sku` across regions therefore DESTROYS data, and it did: a
two-region run collapsed France's twelve ads to two. Every row now carries
`row_key` (`{region}:{ad_id}`), the engines dedupe on it and `diff_runs.py`
joins on it.

### Complete is not exhaustive, and here the gap is the largest in the family

The library reports 17,343,646 ads for Germany alone and serves twelve per
request — a hundred-page run holds 0.0069%. Every sidecar records
`site_total_per_region`, `ads_per_region`, `sample_share_pct` and
`why_each_region_stopped`, and the closing log prints the fraction in
words.

### Also found while building

- **`rejection_info` was order-dependent.** It re-serialises a nested
  object, and without `sort_keys` two byte-identical responses produced
  two different strings — so a diff would report the rejection as having
  changed. Found by `make_fixtures.py --verify`, which parsed a capture
  and its own fixture (`o == f` was True) and got different rows. Same
  shape as CLAUDE.md §24's float32 round-trip.
- **Timestamps are milliseconds.** Read as seconds, 1790112836000 lands in
  the year 58,700.
- **Selenium needed `goog:loggingPrefs` at launch.** It has no
  request-event API, so it captures the header from Chrome's performance
  log — and without that capability the log is empty and the failure
  message points at the Search button, which is the wrong place to look.

### Deliberately not here

- **`impression` and `spent`.** TikTok publishes both on every ad and both
  were empty on all 216 measured across six regions. The measurement is
  recorded in `output_writer.py`.
- **The 2Captcha Scraper API path.** That service fetches a URL and hands
  back what the site returned; it cannot mint a header for a request it is
  not making. Refused with that reason — a feature request, not a
  limitation of the product (CLAUDE.md §19's wording rule).
- **Any region outside the 33 the library serves.** `--region US` is
  refused as a real country the library does not cover, not as a typo.
