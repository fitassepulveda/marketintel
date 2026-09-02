"""One-off: re-insert articles that were lost when data/intel.db was reverted.

Context (2026-09-02): a local test run on 09-01 ingested several stories that the
CI run had not yet seen. Reverting the DB to protect briefed_on stamps also threw
away those ingested rows, and the Google News search feeds had rotated the items
out by the next morning — so they could never be re-ingested.

Inserted rows are normal candidates: briefed_on is NULL and `fetched` is now, so
the next briefing scores them like any other article, provided `published` is
still inside briefing.lookback_hours (72h) at run time.

Usage (on the Mac, venv active):
    python3 scripts/seed_missing_articles.py            # insert
    python3 scripts/seed_missing_articles.py --dry-run  # show what would happen
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import store  # noqa: E402

# Fill in `url` for any entry you want inserted. Entries with an empty url are skipped.
ARTICLES = [
    {
        "url": "https://therealdeal.com/miami/2026/09/01/dfre-starts-live-local-act-project-in-miami-health-district/",
        "title": "Developer plans Live Local Act project in Miami's Health District",
        "summary": ("DFRE is developing a 112-unit workforce housing project under Florida's "
                    "Live Local Act in Miami's Health District, a roughly $35M development "
                    "adjacent to the hospital district."),
        "source": "The Real Deal (Miami)",
        "area": "south_florida_competitive",
        "published": "2026-09-01T12:00:00+00:00",
    },
    {
        "url": ("https://www.bizjournals.com/southflorida/news/2026/09/01/"
                "hca-palm-west-hospital-plans-121-bed-expansion.html"),
        "title": "HCA seeks major expansion for South Florida hospital",
        "summary": ("HCA Florida Palms West Hospital plans a 121-bed expansion at its "
                    "Palm Beach County campus."),
        "source": "South Florida Business Journal (Health)",
        "area": "south_florida_competitive",
        "published": "2026-09-01T12:00:00+00:00",
    },
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = store.connect()
    now = datetime.now(timezone.utc)
    inserted = skipped = dupes = 0

    for a in ARTICLES:
        if not a["url"].strip():
            print(f"SKIP (no url)  {a['title'][:60]}")
            skipped += 1
            continue
        pub = datetime.fromisoformat(a["published"])
        age_h = (now - pub).total_seconds() / 3600
        warn = "  <-- OUTSIDE 72h WINDOW at next run" if age_h > 60 else ""
        print(f"{'WOULD INSERT' if args.dry_run else 'INSERT'}  [{age_h:.0f}h old]{warn}\n"
              f"    {a['title'][:70]}\n    {a['url'][:90]}")
        if args.dry_run:
            continue
        if store.insert_article(con, a):
            inserted += 1
        else:
            print("    (already present — no change)")
            dupes += 1

    if not args.dry_run:
        con.commit()
    print(f"\ninserted={inserted} duplicate={dupes} skipped={skipped}")
    if not args.dry_run and inserted:
        print("\nNEXT: commit and push data/intel.db, or CI won't see these tomorrow:")
        print('  git add -f data/intel.db && git commit -m "seed: re-add articles lost in DB revert" && git push')


if __name__ == "__main__":
    main()
