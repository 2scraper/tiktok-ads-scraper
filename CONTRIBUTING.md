# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count and lists any group it skipped because an engine
library is absent.

**The suite must pass with no engine installed at all.** CI installs only the
core requirements, so any import of `playwright_scraper`, `puppeteer_scraper`
or `selenium_scraper` in a check sits inside `try/except ImportError` with the
skip recorded. If the suite fails on a clean clone, that is itself the bug —
say so.

## Never commit a credential

`.env` and every `.env.*` variant except `.env.example` are in `.gitignore`.
Keep them there.

The engines mask `user:pass@` in their own log lines, but three things are
**not** masked: raw page dumps (`--dump-html`), the Scraper API's `x-debug`
response header, and your shell history. Before pasting output into an issue
or a PR, replace keys, proxy passwords and full `ws://user:pass@host:9222`
endpoints with `***`.

CI fails the build if something credential-shaped is committed. That is a
backstop, not a review.

## Reporting a site change

This repo reads TikTok's EU Ad Library from `library.tiktok.com`, which is served to a bare datacentre address with no key and no account — but only through a browser, because one header gates it.

The parser reads one structured source and never the rendered DOM:

    `POST /api/v1/search`, gated by the `x-ccl-str` header the library's own page JavaScript mints

So a site change almost always shows up as that source moving or its shape
changing, and the most useful thing a report can carry is the source itself
from a dump — there is an issue template for exactly that.

## What the checks pin, and why

Each of these cost real time when it was found, and the offline suite pins it
so a PR that undoes one fails rather than silently regressing:

- **One header gates the endpoint, and only the page can mint it.** Without `x-ccl-str` the search answers HTTP 421. No global exposes it and a `fetch` from inside the page gets 421 too, so a browser is a prerequisite and `--transport http` is refused with that reason.

- **`offset` is a decoy, and so is `limit`.** Offsets 0, 12 and 24 returned the identical twelve ads; `limit` 12, 50 and 100 all return twelve. Pagination is the `search_id` cursor, and the end of the listing is read from the cursor rather than from `has_more`.

- **An ad id is not unique.** An EU ad runs in several member states at once — DE and IT shared 11 of 12 ids on one page, DE and GB shared 1. Rows carry `row_key` (`{region}:{ad_id}`), and dedupe and diff join on it; deduping on `sku` once collapsed France's twelve ads to two.

- **Timestamps are milliseconds.** Read as seconds, 1790112836000 lands in the year 58,700.

- **`rejection_info` is stored canonically.** It re-serialises a nested object; without sorted keys two byte-identical responses produced two different strings, which a diff would report as a change.

Before adding a challenge marker, count it on a page you **know** was served.
A marker that matches every page is worse than no marker.

## Before a release

```bash
python3 smoke_test.py
python3 .github/ci_checks.py --history-check
```

The second applies the credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. A commit on top cannot reach what a
published tag already holds.

The canary is **not** gated on a secret: the Ad Library needs no credential, so it scrapes three regions daily from a bare GitHub runner and is expected GREEN.

## Pull requests

Add a check for the behaviour you are changing. `smoke_test.py` is a single
file of plain functions; copy the nearest existing check and edit it. Keep the
three engines identical above their driver layer — a check compares their
public surfaces and flag sets in both directions.
