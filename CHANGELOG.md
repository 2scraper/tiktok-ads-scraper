# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and SemVer as closely as a CLI toolkit can. A patch means **fixes**; where
a default changes in one, the note leads with it.

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
