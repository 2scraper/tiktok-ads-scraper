#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on `sku`.

    python3 diff_runs.py --old restaurants.2026-09-01.json \\
                          --new restaurants.2026-09-07.json

Typical use is a scheduled re-run kept under a dated filename, diffed against
the previous one:

    python3 playwright_scraper.py --text restaurants --location "New York, NY" \\
        --out "restaurants_$(date +%F)"
    python3 diff_runs.py --old "restaurants_$(ls -t restaurants_*.json | sed -n 2p)" \\
                          --new "restaurants_$(date +%F).json" --out diff.json

Four buckets, each keyed on sku:

  added          — sku present in --new, absent from --old
  removed        — sku present in --old, absent from --new: the listing was
                   filled or withdrawn, or simply fell outside the pages this
                   run fetched
  changed        — sku present in both, with a different title, rate range,
                   pay period, commitment, work arrangement, location, or
                   slot count. See TRACKED_FIELDS.
  source_changed — sku present in both, but one row came from the INDEX
                   (`explore`) and the other from a DETAIL page (`detail`),
                   and they differ on a column only one of the two fills.
                   Reported separately because this says something about our
                   own two snapshots rather than about the listing — and
                   --fail-on-change deliberately ignores it.

TWO THINGS TO KNOW BEFORE READING A DIFF OF THIS SITE
-----------------------------------------------------
**`removed` usually does not mean deleted.** A comments run holds the
first N pages of an ordering, and on a video with two and a half million
comments that is a sample rather than a census. Under `--sort top` the
ordering is a live relevance ranking, so a comment drops out of a
five-page file because something above it gained likes — nothing happened
to the comment at all. Under `--sort newest` the window slides forward as
new comments arrive, and the oldest rows fall off the end of it.

Two runs are comparable as a census only when both covered the same
ground, which on this site means the same ordering, the same `--pages`,
and the same answer to `--replies`. The first is refused outright; the
other two are reported as notes, because a smaller run is still worth
diffing as long as the reader knows what `removed` can mean.

Where a route states its own total — the Ad Library does, per region — the
sidecar records it beside what the run collected, and a diff should be read
in that light: a capped run's `removed` can mean "outside this run's slice"
rather than "gone".

**`position` and `page` are deliberately not tracked, and here that is a
necessity rather than a choice.** Under `--sort top` the order IS the
site's live relevance ranking: a comment that gains likes overnight moves,
and every comment below it moves with it. Tracking position would report
most of the file as changed every night, and a diff that is always noisy is
one nobody reads.

Under `--sort newest` the order is chronological and much steadier — but a
new comment at the top still shifts every row beneath it by one, which is
the same noise arriving by a different route. So neither ordering makes
position a useful thing to diff, and both make the SORT itself worth
refusing to compare across, which `_runs_are_comparable` does.

A row this project's parser could not recover a sku for (None) cannot be
matched across runs at all, so it is counted and reported separately rather
than silently folded into "added"/"removed", which would be wrong on its face.
"""

import argparse
import json
import pathlib
import re
import sys
from typing import Dict, List, Optional, Tuple

from output_writer import UNIQUE_BY_SKU_MODES

# What is worth watching on a comment, and nothing else.
#
# EVERY NAME HERE MUST EXIST ON THE ROW CLASS, and that is not a style
# rule. This tuple arrived from the repo this one was ported from naming
# 25 fields the row class does not have — so the diff compared nothing and
# reported "0 changed" with exit 0 on a run where a tracked value had
# changed. A monitor that cannot see the thing it monitors is worse than
# no monitor, because it reports success. `smoke_test.py` pins every name
# here against the dataclass.
#
# A shop's fields are absent because a comment has no price, stock or
# discount — porting them would be dead code that looks load-bearing
# (CLAUDE.md §4). What changes on a comment is its TEXT (it was edited),
# its ENGAGEMENT, and the badges the creator controls.
#
# DELIBERATELY NOT TRACKED, and each is a measurement rather than an
# oversight:
#
#   `published_time_text` and `published_at_approx` — these move ON THEIR
#     OWN. The site renders a relative string, so a comment that said
#     "3 days ago" last night says "4 days ago" tonight without anything
#     having happened. Measured directly: two runs 88 seconds apart
#     disagreed on three of forty rows ("25 minutes ago" -> "27 minutes
#     ago", "7 hours ago" -> "8 hours ago"). Tracking them would make
#     every nightly diff report most of the file as changed, and a diff
#     that is always noisy is one nobody reads.
#
#   `follower_count`, `likes_count`, `video_count` ARE tracked, and they
#     are the point of this repo. Note that they MOVE: nine refetches of
#     one account within an hour on 2026-09-22 returned 1,785,290 …
#     1,785,310 followers, all different, none wrong. So a diff of two
#     runs an hour apart legitimately shows a delta on almost every row,
#     and `--fail-on-change` on this repo means "tell me when a number
#     moved", not "tell me when something broke".
#
#   `avatar_url` is NOT tracked, and this is the one that would otherwise
#     ruin every diff. TikTok serves avatars from a SIGNED CDN URL whose
#     signature is re-minted per request, so the column differs between two
#     runs taken seconds apart, on every row, while the picture is
#     identical. `avatar_id` — the stable content hash out of the same URL
#     — is tracked instead, and it changes when the account actually
#     changes its picture. CLAUDE.md §24 records a float32 round-trip
#     making two modes disagree on every row; this is the same defect
#     wearing a signature.
#
#   `avatar_expires_at` is not tracked for the same reason: it is a
#     property of the link, not of the account.
#
#   `sec_uid` is not tracked. It is an opaque token TikTok re-issues, and
#     a change in it says nothing a consumer can act on.
#
#   `user_id` is not tracked because it IS the join key (`sku`): a change
#     in it would mean the join was wrong rather than that anything
#     happened.
#
#   `position` and `page` — an artefact of the order --url named handles,
#     not a property of the account.
#
#   `scraped_at` and `stats_source` — the latter drives `source_changed`
#     instead, so a run that read `statsV2` and one that fell back to the
#     rounded `stats` do not report every count as having changed.

TRACKED_FIELDS = (
    # the counts this repo exists for
    "follower_count",
    "following_count",
    "likes_count",
    "video_count",
    "friend_count",
    "digg_count",
    "likes_per_video",
    # what the account says about itself. A bio edit and a new bio link
    # are the two most actionable changes on a creator account.
    "title",
    "username",
    "bio",
    "bio_link",
    "avatar_id",
    "language",
    # what kind of account it is. `is_seller` flipping is a TikTok Shop
    # seller appearing — the single column linking this repo to
    # tiktok-shop-scraper.
    "verified",
    "private_account",
    "is_organization",
    "is_seller",
    "commerce_user",
    "secret",
    # the settings an account publishes. A creator closing comments or
    # duets is an editorial act and shows up here.
    "comment_setting",
    "duet_setting",
    "stitch_setting",
    "download_setting",
    "following_visibility",
    "open_favorite",
    "show_playlist_tab",
    # the site's own verdict on the account
    "status",
    "status_code",
)

# The subset that only ONE source populates. (Named
# PROFILE_ONLY_FIELDS because that is what the comparison below
# reads; the donor core spells it that way and a second name would
# be a constant nothing consults — CLAUDE.md §17.)
#
# Empty on this repo, and stated rather than deleted. There is exactly one
# route and one payload here, so there is no second source whose coverage
# differs — nothing can be present in one run's rows and structurally
# absent from another's. `stats_source` still drives `source_changed`,
# because a run read from the rounded `stats` object is not comparable
# count-for-count against one read from `statsV2`.
#
# Its siblings are not like this: tiktok-video-scraper reads an embed
# payload AND a video page, which carry different fields, and that repo's
# copy of this tuple is not empty.
PROFILE_ONLY_FIELDS = ()


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(products: List[dict]) -> Tuple[Dict[str, dict], int]:
    indexed = {}
    unmatchable = 0
    for p in products:
        # Keyed on `row_key` ({region}:{ad_id}) where a row has one:
        # an ad id is not unique across regions on this site, and a
        # diff joined on it alone would compare Germany's copy of an
        # ad against France's and report the difference as a change.
        sku = p.get("row_key") or p.get("sku")
        if sku is None:
            unmatchable += 1
            continue
        # A run's own output can already hold a duplicate sku (two rows in the
        # same category, or a rerun of dedupe_by_sku's job on older output
        # written before it existed) — keep the first and count the rest as
        # unmatchable rather than letting one clobber the other silently.
        if sku in indexed:
            unmatchable += 1
            continue
        indexed[sku] = p
    return indexed, unmatchable


def diff_products(old: List[dict], new: List[dict]) -> dict:
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    changed, source_changed = [], []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in TRACKED_FIELDS
            if before.get(field) != after.get(field)
        }
        if not field_changes:
            continue

        # A row whose `stats_source` differs between runs is not comparable on
        # the profile-only columns: a listing row leaves them null and a
        # profile row fills them, so every one of them would read as a change
        # and none of it would be about the business. Reporting it as a
        # change would be a false alarm about the site; the other columns
        # still compare fine.
        sources = (before.get("stats_source"), after.get("stats_source"))
        if sources[0] != sources[1] and any(f in field_changes
                                            for f in PROFILE_ONLY_FIELDS):
            profile_part = {f: v for f, v in field_changes.items()
                            if f in PROFILE_ONLY_FIELDS}
            other_part = {f: v for f, v in field_changes.items()
                          if f not in PROFILE_ONLY_FIELDS}
            source_changed.append({
                "sku": sku, "title": after.get("title"),
                "stats_source": {"old": sources[0], "new": sources[1]},
                "changes": profile_part,
            })
            field_changes = other_part
            if not field_changes:
                continue

        changed.append({"sku": sku, "title": after.get("title"),
                        "changes": field_changes})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "source_changed": source_changed,
        "unmatchable_old": old_unmatchable,
        "unmatchable_new": new_unmatchable,
    }


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed, "
          f"{len(result['source_changed'])} not comparable across run kinds.")
    for p in result["added"]:
        print(f"  + {p.get('sku')}  {p.get('title')}  "
              f"@ {p.get('company_name') or '?'}  "
              f"{p.get('compensation') or 'pay not stated'}")
    for p in result["removed"]:
        print(f"  - {p.get('sku')}  {p.get('title')}  "
              f"@ {p.get('company_name') or '?'}  "
              f"{p.get('compensation') or 'pay not stated'}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result["source_changed"]:
        src = c["stats_source"]
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[stats_source {src['old']!r} -> {src['new']!r}: a listing row "
              f"leaves these columns null and a profile row fills them, so "
              f"this is not a change in the business]")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str) -> Tuple[Optional[str], Optional[dict]]:
    """Read the `<out>.meta.json` sidecar beside a run's JSON output.

    Returns (status, meta), or (None, None) when there is no sidecar — which
    is the normal case for output written before run metadata existed, or by
    `scraper_api_client.py` (single fetch, no pagination to cut short).
    """
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


def _check_comparable(args) -> bool:
    """Refuse an assortment diff between runs that are not both complete.

    This is the failure mode the sidecar exists for: a run cut short on page
    3 of 10 is missing every product on pages 4-10, and diffing it against
    yesterday's full run reports all of them as `removed` — reading as "these
    products were delisted" when in fact they were simply never fetched.
    Prices of the SKUs both runs DID see are still comparable, which is why
    this is a refusal with a --force escape hatch rather than a hard error.
    """
    problems = []
    modes = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        if status is None:
            continue  # no sidecar: nothing to check, see _run_status
        mode = (meta or {}).get("mode")
        if mode:
            modes[label] = mode
        if mode and mode not in UNIQUE_BY_SKU_MODES:
            # This tool's whole premise is one row per `sku`, diffed on
            # price. A mode that produces many rows per sku would give a diff
            # whose every line is an artefact of two rows sharing an id, so
            # it is refused outright rather than answered. Both of this
            # repo's current modes qualify; the check is here so that adding
            # one that does not is caught rather than discovered.
            problems.append(
                f"{label} ({path}) is a {mode!r} run, which is not one row "
                f"per sku. This tool diffs one row per sku on price, so there "
                f"is nothing here it can compare.")
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(set(modes.values())) > 1:
        kinds = set(modes.values())
        # `careers` against either marketplace mode is the severe case and
        # deserves its own sentence: the two share NO ids at all, so every
        # row would be reported as both added and removed. `listings`
        # against `job` is milder — same id space, different columns — but
        # still describes the mode change rather than the catalogue.
        if "comments" in kinds and kinds - {"comments"}:
            problems.append(
                f"the two runs are different POPULATIONS ({modes}). A "
                f"comments run is keyed by comment id and a video or "
                f"search run by 11-character video id; the two sets have "
                f"no id in common, so every row would be reported as both "
                f"added and removed.")
        else:
            problems.append(
                f"the two runs are different modes ({modes}). Rows from "
                f"different modes carry different columns, so "
                f"`added`/`removed` would describe the mode change rather "
                f"than the site.")

    # A SORT GUARD, family core. Where rows carry a `sort` column, two runs
    # under different orderings are different SAMPLES of a capped listing,
    # and diffing them reports the sampling as though the site had
    # changed. No tiktok-* row carries one today, so this guard is inert
    # here — kept rather than deleted so a route that gains an ordering is
    # guarded from its first run.
    #
    # `sort` is therefore a column rather than a sidecar field, so this
    # guard reads what is actually in the rows rather than trusting
    # metadata that a hand-edited file could contradict.
    sorts = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        rows, meta = _load(path), (_run_status(path)[1] or {})
        seen = {r.get("sort") for r in rows if isinstance(r, dict)}
        seen.discard(None)
        sorts[label] = (seen.pop() if len(seen) == 1
                        else meta.get("sort") or "?")
    if len(set(sorts.values())) > 1 and "?" not in sorts.values():
        problems.append(
            f"the two runs used different orderings ({sorts}). A run "
            f"that stops after N pages holds a different SET of rows under "
            f"each, so the diff would report the sampling "
            f"rather than the site.")

    # And whether either run was CAPPED, which changes what `removed` means.
    for label, path in (("--old", args.old), ("--new", args.new)):
        _, meta = _run_status(path)
        meta = meta or {}
        total = meta.get("total_comments")
        collected = meta.get("comments_collected")
        share = meta.get("sample_share_pct")
        if total and collected and collected < total:
            # The caveat is spelled per ORDERING, because it is much
            # stronger under one than the other: `top` is a live ranking,
            # so a comment leaving a five-page sample usually means the
            # ranking moved, while under `newest` a sample slides forward
            # in time and the older rows fall off the end.
            ordering = meta.get("sort")
            why = {
                "top": "which on a `top` ordering it usually does: that "
                       "ordering is a live ranking",
                "newest": "which on a `newest` ordering it usually does: "
                          "the window slides forward as comments arrive",
            }.get(ordering, "which is the usual explanation")
            print(f"[i] {label} ({path}) holds {collected:,} of the "
                  f"video's {total:,} comments"
                  + (f" ({share}%)" if share else "") + " — a complete run, "
                  f"and a sample. A `removed` line may mean the sample "
                  f"moved rather than that a comment was deleted, "
                  f"{why}. CLAUDE.md §21: complete and exhaustive are "
                  f"different words.")
        if meta.get("replies") is False and meta.get("mode") == "comments":
            print(f"[i] {label} ({path}) did not expand replies, so it "
                  f"holds top-level comments only. A diff against a run "
                  f"that did would report every reply as added.")

    if not problems:
        return True

    # A generic headline, because the reasons below are no longer only about
    # completeness: a mode mismatch and a reviews run are refused too, and a
    # message naming the wrong reason sends the reader looking in the wrong
    # place.
    print("[!] Refusing to diff these two runs:")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the incomplete side, or pass --force to compare anyway "
          "(added/removed will include rows that were simply never "
          "fetched).")
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two tiktok-ads-scraper JSON outputs by sku.")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when a run's .meta.json says it was partial "
                        "or failed. Products never fetched by the short run will "
                        "appear as added/removed.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2

    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2

    result = diff_products(old, new)
    _print_summary(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    # `source_changed` is not a reason to fail: it means one row came from a
    # listing run and the other from a profile run, so the columns only a
    # profile fills differ. That says something about our own two snapshots
    # rather than about the business, and alerting on it would train whoever
    # reads the alert to ignore it.
    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
