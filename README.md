# tiktok-ads-scraper

Extract advertisements from **TikTok's EU Ad Library** — the advertiser,
the creatives, the dates an ad ran, TikTok's own audience bucket, and the
policy reason where an ad was rejected. 33 regions.

Playwright, Selenium or Puppeteer. No key, no account, no proxy.

---

## You do not need a key, a proxy, or an account

`library.tiktok.com` is TikTok's advertising transparency library, run to
satisfy EU/EEA obligations. Its search endpoint is a plain POST, served to
a bare datacentre address with no credentials of any kind. Measured
**2026-09-22** from Hetzner, Helsinki:

| request | result |
|---|---|
| the browser's headers | HTTP 200, 12 ads |
| the same, **minus `x-ccl-str`** | **HTTP 421** |
| `content-type` + `user-agent` + `referer` + `x-ccl-str` only | HTTP 200, 12 ads |

So exactly **one header** gates it — and that header is minted by the
library's own JavaScript. There is no global to read it from, and a plain
`fetch` issued from inside the page gets 421 too, because the app attaches
it in its own HTTP client.

**That makes a browser a prerequisite here, not a fallback** — the reverse
of this repo's two siblings, whose pages are server-rendered. The engine
opens the library, lets it search once, keeps the header it sent, and every
request after that is a cheap POST. `--transport http` is refused up front
with that reason rather than failing later on a 421 nobody can interpret.

---

## Quick start

```bash
git clone https://github.com/2scraper/tiktok-ads-scraper
cd tiktok-ads-scraper
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-playwright.txt
./venv/bin/python -m playwright install chromium
./venv/bin/python playwright_scraper.py --region DE
```

Several regions, deeper, both formats:

```bash
./venv/bin/python playwright_scraper.py --region DE,FR,GB,IT \
    --pages 10 --days 90 --format both
```

A named advertiser:

```bash
./venv/bin/python playwright_scraper.py --region DE --query nike
```

---

## Read this before you trust a page count

**`offset` is a decoy.** The request body carries one, the library's own UI
increments it, and it does nothing: measured, offsets 0, 12 and 24 returned
the **identical twelve ads in the identical order**. Pagination is by
`search_id`, a base64 cursor the response hands back:

```json
{"last_sort": [1790109992000], "next_cursor": 12}
```

A scraper that trusted `offset` would re-collect page one for as long as it
was asked to and report a complete run of duplicates. This repo reads the
cursor, and reads the END of the listing out of it too — a cursor that
stops advancing ends the run whatever `has_more` claims.

**`limit` is a decoy too.** 12, 50 and 100 all return twelve.

**And an ad id is not unique.** An EU ad buy commonly runs in several
member states at once and the library lists it under each. Measured, page
one of five regions fetched within the same few seconds:

| pair | ad ids shared |
|---|---|
| DE vs IT | 11 of 12 |
| DE vs FR | 9 of 12 |
| DE vs PL | 9 of 12 |
| **DE vs GB** | **1 of 12** |

GB is outside the EU and is bought separately, which is exactly the shape
you would expect. So every row carries `row_key` (`{region}:{ad_id}`) and
that is what deduping and diffing join on. Before it existed, a two-region
run collapsed France's twelve ads to two.

---

## Complete is not exhaustive, and here the gap is enormous

The library reports **17,343,646 ads for Germany alone** and serves
**twelve** per request. A thorough hundred-page run holds 1,200 of them —
0.0069%.

Every run records `site_total_per_region`, `ads_per_region` and
`sample_share_pct` in its sidecar, and the closing log prints the fraction
in words. A run that fetched every page it asked for is **complete**; it is
not exhaustive, and the two are different sentences.

The sidecar also records `why_each_region_stopped` — `cursor_exhausted`,
`cursor_stalled`, `has_more_false` or `no_ads` — so "we stopped" is never
just a number.

---

## What you get

25 columns per ad. The ones worth naming:

| column | note |
|---|---|
| `sku`, `ad_id` | the ad id — **not unique across regions** |
| `row_key` | `{region}:{ad_id}`, which is |
| `advertiser_name` | 211 of 216 measured carry one; five are published anonymously |
| `region` | which of the 33 the ad was found in — a column, because the same ad runs in several |
| `first_shown_at`, `last_shown_at`, `days_shown` | TikTok publishes these in **milliseconds**; read as seconds they land in the year 58,700 |
| `creative_type`, `video_count`, `image_count` | derived from the creatives the ad carries, not from `show_mode`, whose meaning TikTok documents nowhere |
| `video_urls`, `cover_urls`, `image_urls` | the library's own proxied media URLs, which answer HTTP 302 to the real files |
| `estimated_audience` | TikTok's bucket, e.g. `"0-1K"` — a **string**, because it is a range and turning it into a number would be a guess |
| `rejection_info` | TikTok's own policy reason on a rejected ad. 2 of 216 measured, and stored in a canonical (sorted-key) form so two byte-equal responses cannot diff against each other |
| `audit_status`, `sor_audit_status`, `show_mode`, `ad_type` | TikTok's own values, passed through unmapped |

**`impression` and `spent` are not columns.** TikTok publishes both fields
on every ad and both were empty on **all 216 measured** across six regions
and three pages each. The likely reason is that the EU rules attach spend
and reach to *political* advertising and TikTok prohibits political
advertising outright — so they may be structurally empty rather than
sparse. The measurement is recorded in `output_writer.py` so anyone can add
them back with a better one.

---

## Scope: 33 regions, and the rest of the world is not one of them

```
AT BE BG CH CY CZ DE DK EE ES FI FR GB GR HR HU IE IS IT LI LT
LU LV MT NL NO PL PT RO SE SI SK TR
```

`--region US` is refused **with that reason** — it is a real country the
library does not cover, not a typo. Measured: `region=US` and `region=BR`
return a body that is not JSON at all, while DE, FR and GB return ads.

---

## Exit codes

| code | meaning |
|---|---|
| 0 | ok |
| 1 | crash |
| 2 | bad usage |
| 3 | blocked |
| 4 | zero ads — including "the query matched nothing", which is a real answer |
| 5 | the content was never obtained |
| 6 | partial |

**A run that finds nothing writes nothing.** `--allow-empty` is the opt-out.

An HTTP 421 mid-run is **not** a block: it means the token went stale, and
the remedy is to mint another, which the run does by re-priming. Rotating
an exit would spend a proxy budget on the wrong problem.

---

## Configuration

```bash
cp .env.example .env
python3 env_config.py      # prints what was picked up, WITHOUT printing secrets
```

Variables: `TWOCAPTCHA_KEY`, `TIKTOK_CDP_ENDPOINT`, `TIKTOK_PROXY`,
`TIKTOK_URL`. None of them is needed for this route.

The 2Captcha **Scraper API** path is deliberately **not implemented** here,
and the distinction matters: a service that fetches a URL cannot mint a
header for a request it is not making. That is a feature request, not a
limitation of the product — and the three browser engines do the job with
no key at all.

---

## The daily canary

`.github/workflows/canary.yml` runs a real scrape every morning **from a
bare GitHub runner with no secrets**, and is expected green — the README's
central claim under test daily.

---

## Contributing

```bash
python3 smoke_test.py       # the offline suite — no network, no browser needed
python3 -m pytest
```

---

## Licence

MIT. See `LICENSE`.

Captcha solving, the Scraping Browser API, proxies and fingerprints are
four separately-billed [2Captcha](https://2captcha.com) products behind one
key. This repo needs none of them, and says so above with the measurement.
