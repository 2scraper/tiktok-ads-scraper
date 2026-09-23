#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two output files from this project (JSON, as written by
output_writer.save) and reports what changed between them, keyed on
`row_key` (`{region}:{ad_id}`), because an ad id is not unique across
regions on this site.

    python3 diff_runs.py --old ads_de.2026-09-01.json \\
                          --new ads_de.2026-09-07.json

Typical use is a scheduled re-run kept under a dated filename, diffed against
the previous one:

    python3 playwright_scraper.py --region DE --out "ads_de_$(date +%F)"
    python3 diff_runs.py --old "$(ls -t ads_de_*.json | grep -v meta | sed -n 2p)" \\
                          --new "ads_de_$(date +%F).json" --out diff.json

Four buckets, each keyed on row_key:

  added          — present in --new, absent from --old
  removed        — present in --old, absent from --new: usually NOT an ad
                   that stopped running (see below)
  changed        — present in both, with a different advertiser, run dates,
                   creative, review status or audience bucket. See
                   TRACKED_FIELDS.
  source_changed — kept for the family's output shape. Every row here comes
                   from one source, the library's search endpoint, so it is
                   always empty.

ONE THING TO KNOW BEFORE READING A DIFF OF THIS SITE
----------------------------------------------------
**`removed` usually does not mean the ad stopped.** The library reports
millions of ads per region and serves twelve per request, so any run is a
tiny slice of it (the README's "Complete is not exhaustive"). An ad leaving
that slice between two runs is the slice moving. The sidecar records
`site_total_per_region` and `ads_per_region`, and this tool prints the
share each run covered.

A row this project's parser could not recover a key for (None) cannot be
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

# What is worth watching on an ad, and nothing else.
#
# EVERY NAME HERE MUST EXIST ON THE ROW CLASS, and that is not a style
# rule. This tuple used to be tiktok-profile-scraper's, arriving with the
# copied core and naming 26 account fields `Advertisement` does not have —
# so the diff compared the title and `video_count` and nothing else, and
# reported "0 changed" on runs where a tracked value had changed. A monitor
# that cannot see the thing it monitors is worse than no monitor, because
# it reports success. `smoke_test.py` pins every name here against the
# dataclass.
#
# DELIBERATELY NOT TRACKED:
#
#   `video_urls`, `cover_urls`, `image_urls` — the library's own proxied
#     media links; the creative counts beside them are tracked instead.
#
#   `ad_id`, `region` and `row_key` — together they ARE the join key.
#
#   `position` and `page` — where the library happened to list the ad in
#     this run, not a property of the ad.

TRACKED_FIELDS = (
    "title",
    "advertiser_name",
    # when it ran
    "first_shown_at",
    "last_shown_at",
    "days_shown",
    # what it is
    "creative_type",
    "video_count",
    "image_count",
    "ad_type",
    "show_mode",
    # the platform's verdict on it
    "audit_status",
    "sor_audit_status",
    "rejection_info",
    # who saw it, in TikTok's own bucket
    "estimated_audience",
)

# The column that says which source a row was read from, and the tracked
# columns only ONE of those sources fills. None and empty on this repo:
# every row comes from the same search endpoint, so no column can be
# present in one run's rows and structurally absent from another's. Kept
# rather than deleted because the comparison below is shared with the
# tiktok-* siblings, one of which reads two sources.
SOURCE_COLUMN = None
SOURCE_ONLY_FIELDS = ()


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

        # A row whose SOURCE_COLUMN differs between runs is not comparable
        # on SOURCE_ONLY_FIELDS: inert on this repo, which has one source. Reporting it as a change would be a
        # false alarm about the site; the other columns still compare fine.
        sources = ((before.get(SOURCE_COLUMN), after.get(SOURCE_COLUMN))
                   if SOURCE_COLUMN else (None, None))
        if sources[0] != sources[1] and any(f in field_changes
                                            for f in SOURCE_ONLY_FIELDS):
            source_part = {f: v for f, v in field_changes.items()
                           if f in SOURCE_ONLY_FIELDS}
            other_part = {f: v for f, v in field_changes.items()
                          if f not in SOURCE_ONLY_FIELDS}
            source_changed.append({
                "sku": sku, "title": after.get("title"),
                SOURCE_COLUMN: {"old": sources[0], "new": sources[1]},
                "changes": source_part,
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


def _describe(p: dict) -> str:
    """The one or two columns that make an added/removed line readable."""
    return f"{p.get('region') or '?'}  {p.get('advertiser_name') or '?'}"


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed, "
          f"{len(result['source_changed'])} not comparable across run kinds.")
    for p in result["added"]:
        print(f"  + {p.get('sku')}  {p.get('title')}  {_describe(p)}")
    for p in result["removed"]:
        print(f"  - {p.get('sku')}  {p.get('title')}  {_describe(p)}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result["source_changed"]:
        src = c[SOURCE_COLUMN]
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[{SOURCE_COLUMN} {src['old']!r} -> {src['new']!r}: "
              f"the two rows were read from different sources, so this is not a change in the ad]")
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
    The rows both runs DID see are still comparable, which is why
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
            # TRACKED_FIELDS. A mode that produces many rows per sku would
            # give a diff whose every line is an artefact of two rows
            # sharing an id, so it is refused outright rather than answered.
            # Every mode this repo has qualifies; the check is here so that
            # adding one that does not is caught rather than discovered.
            problems.append(
                f"{label} ({path}) is a {mode!r} run, which is not one row "
                f"per sku. This tool diffs one row per sku, so there "
                f"is nothing here it can compare.")
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(set(modes.values())) > 1:
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

    # And what share of the library each run covered, which changes what
    # `removed` means: a run is a slice of millions of ads per region.
    for label, path in (("--old", args.old), ("--new", args.new)):
        _, meta = _run_status(path)
        meta = meta or {}
        totals = meta.get("site_total_per_region") or {}
        got = meta.get("ads_per_region") or {}
        for region, total in sorted(totals.items()):
            if total and got.get(region) is not None and got[region] < total:
                print(f"[i] {label} ({path}) holds {got[region]:,} of the "
                      f"{total:,} ads the library reports for {region} — a "
                      f"complete run, and a sample. A `removed` line may mean "
                      f"the sample moved rather than that the ad stopped "
                      f"running. CLAUDE.md §21: complete and exhaustive are "
                      f"different words.")

    if not problems:
        return True

    # A generic headline, because the reasons below are not only about
    # completeness: a mode mismatch and an ordering mismatch are refused too, and a
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

    # `source_changed` is not a reason to fail, and on this repo it is
    # always empty: every row comes from one source.
    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
