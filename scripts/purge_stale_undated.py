"""Purge stale, undated, bot-blocked Fierce Healthcare rows from the dedup DB.

Background: Fierce Healthcare was briefly ingested via its NATIVE RSS feed, which
returns real fiercehealthcare.com article URLs but NO per-item dates. The source
was then switched to a Google-News proxy (which supplies dates), but the original
undated rows linger in data/intel.db. Because they're undated they fall back to
fetch-time and keep getting surfaced as "recent," while date-enrichment can never
fix them (Fierce returns HTTP 403 to server-side fetches). They also duplicate the
newer, dated Google-News copies of the same stories.

This removes exactly those rows: source = 'Fierce Healthcare', empty publish date,
and a real fiercehealthcare.com URL. Run once on a machine with DB write access
(your Mac), then commit the updated data/intel.db.

    python3 scripts/purge_stale_undated.py            # delete
    python3 scripts/purge_stale_undated.py --dry-run  # just show what would go
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB = "data/intel.db"
WHERE = (
    "source = 'Fierce Healthcare' "
    "AND coalesce(trim(published), '') = '' "
    "AND url LIKE '%fiercehealthcare.com%'"
)


def main() -> None:
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(DB)
    rows = con.execute(f"SELECT title, url FROM articles WHERE {WHERE}").fetchall()
    print(f"Matched {len(rows)} stale undated Fierce row(s):")
    for title, url in rows:
        print(f"  - {(title or '')[:70]}")
    if not rows:
        print("Nothing to purge.")
        return
    if dry:
        print("\n--dry-run: no changes made.")
        return
    con.execute(f"DELETE FROM articles WHERE {WHERE}")
    con.commit()
    remaining = con.execute(
        "SELECT count(*) FROM articles WHERE source='Fierce Healthcare' "
        "AND coalesce(trim(published),'')=''"
    ).fetchone()[0]
    print(f"\nDeleted {len(rows)} row(s). Remaining undated Fierce rows: {remaining}.")
    print("Now commit the DB:  git add -f data/intel.db && git commit -m "
          "'Purge stale undated Fierce rows' && git push")


if __name__ == "__main__":
    main()
