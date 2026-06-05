"""Eval: can we pull the listing data we need from nehnutelnosti.sk?

This is the MEASURABLE definition of the scraping objective — run it to get an
objective PASS/FAIL, independent of *how* the fetch is implemented. It exercises
the real production path (`realestate.scraper.sweep_unit`, capped to one page) for
BOTH deal types on one small locality, then checks that the rows carry the fields
the medallion pipeline actually needs, at sensible coverage thresholds.

    cd backend
    .venv\\Scripts\\python.exe eval_scrape.py            # default locality
    .venv\\Scripts\\python.exe eval_scrape.py --locality stupava

Exit code 0 = PASS (we can pull the data), 1 = FAIL (with a diagnostic). As the
fetch layer changes (e.g. to a tokenized API call or a headless browser), this eval
is the unchanged yardstick that says whether the change actually works.

Success criteria (one page is enough to prove the capability):
  * the sweep completes (status == "done", not blocked/error),
  * it returns at least MIN_ROWS listings for each deal,
  * required fields meet their per-field non-null coverage threshold.
"""

from __future__ import annotations

import argparse
import sys

from realestate.scraper import sweep_unit

# One small area is enough to prove the capability; default is a borough, but any
# locality slug works (e.g. a tiny corridor town like "stupava").
DEFAULT_LOCALITY = "bratislava-ruzinov"
DEALS = ("predaj", "prenajom")  # sell + rent — both must work
MAX_PAGES = 1                   # one page proves the pull; no need to sweep the area
MIN_ROWS = 1                    # >=1 parsed listing per deal to count as a pull

# Fields the pipeline needs, with the minimum fraction of rows that must be non-null.
# detail_id is the join key (must always be present); price can legitimately be missing
# (price_on_request), so its bar is lower; area/category/city are needed for gold.
REQUIRED_COVERAGE = {
    "detail_id":     1.00,
    "city":          1.00,   # derived from the slug, so should always be set
    "category":      0.90,
    "area_m2":       0.85,
    "price_eur":     0.70,   # some listings are "cena dohodou" (price_on_request)
    "price_per_m2":  0.60,   # needs both price and area
}
NICE_TO_HAVE = ("rooms", "title", "detail_url", "valid_from")


def _coverage(rows: list[dict], field: str) -> float:
    if not rows:
        return 0.0
    return sum(1 for r in rows if r.get(field) not in (None, "")) / len(rows)


def eval_deal(locality: str, deal: str, max_pages: int = MAX_PAGES) -> dict:
    """Run one capped sweep and score it. Returns a result dict (does not write)."""
    res = sweep_unit(locality, deal, max_pages=max_pages)
    rows = res["df"].to_dicts()
    checks = {f: (_coverage(rows, f), thr) for f, thr in REQUIRED_COVERAGE.items()}

    failures = []
    if res["status"] != "done":
        failures.append(f"sweep status={res['status']} (error: {res.get('error')})")
    if len(rows) < MIN_ROWS:
        failures.append(f"got {len(rows)} rows, need >= {MIN_ROWS}")
    for f, (cov, thr) in checks.items():
        if cov < thr:
            failures.append(f"{f} coverage {cov:.0%} < {thr:.0%}")

    return {
        "deal": deal, "status": res["status"], "n_rows": len(rows),
        "n_pages": res["n_pages"], "error": res.get("error"),
        "coverage": checks, "rows": rows, "passed": not failures, "failures": failures,
    }


def _print_deal(r: dict) -> None:
    mark = "PASS" if r["passed"] else "FAIL"
    print(f"\n[{mark}] deal={r['deal']}  status={r['status']}  rows={r['n_rows']}  pages={r['n_pages']}")
    if r["error"]:
        print(f"       note: {r['error']}")
    for f, (cov, thr) in r["coverage"].items():
        flag = "ok " if cov >= thr else "LOW"
        print(f"       {flag} {f:<14} {cov:5.0%}  (need {thr:.0%})")
    if r["rows"]:
        s = r["rows"][0]
        print("       sample:", {k: s.get(k) for k in
              ("detail_id", "category", "rooms", "area_m2", "price_eur", "price_per_m2", "city", "deal")})
        nice = {k: _coverage(r["rows"], k) for k in NICE_TO_HAVE}
        print("       nice-to-have coverage:", {k: f"{v:.0%}" for k, v in nice.items()})
    for f in r["failures"]:
        print(f"       - {f}")


def _write_csv(results: list[dict], path: str) -> int:
    """Dump all scraped rows (both deals) to a CSV for inspection. Returns row count."""
    import csv
    rows = [r for res in results for r in res["rows"]]
    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Eval: can we pull the listing data we need?")
    ap.add_argument("--locality", default=DEFAULT_LOCALITY, help="locality slug (default: %(default)s)")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES, help="pages per deal (default: %(default)s)")
    ap.add_argument("--csv", default=None, help="optional path to dump scraped rows for inspection")
    args = ap.parse_args()

    print(f"=== Scrape-capability eval -- locality={args.locality}, "
          f"deals={DEALS}, max_pages={args.max_pages} ===")
    results = [eval_deal(args.locality, deal, args.max_pages) for deal in DEALS]
    for r in results:
        _print_deal(r)

    if args.csv:
        n = _write_csv(results, args.csv)
        print(f"\nwrote {n} rows -> {args.csv}")

    overall = all(r["passed"] for r in results)
    print("\n" + ("=" * 60))
    print("OVERALL:", "PASS -- we can pull the data we need" if overall
          else "FAIL -- see diagnostics above")
    print("=" * 60)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
