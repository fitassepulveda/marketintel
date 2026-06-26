#!/usr/bin/env python3
"""One-off cleanup: clear contaminated sub-scores.

Sets `subscores = NULL` for any article whose stored sub-scores are all-zero (the
signature of a failed/throttled scoring batch) OR don't cover the current set of
dimensions. Those rows are then treated as un-scored and get cleanly re-scored
(capped per run) on future `scoring_insights` runs — so the averages and influence
numbers stop being polluted by zeros.

  python scripts/clean_zero_subscores.py            # report, then clear
  python scripts/clean_zero_subscores.py --dry-run  # report only, change nothing
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store                       # noqa: E402
from src.prioritize import subscores        # noqa: E402

DIMS = subscores.DIMENSIONS


def _incomplete(subs) -> bool:
    if not subs or (set(DIMS) - set(subs)):
        return True
    try:
        return not any(float(v) > 0 for v in subs.values())
    except (TypeError, ValueError):
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = store.connect()
    subscores.ensure_column(con)
    rows = con.execute("SELECT id, subscores FROM articles WHERE subscores IS NOT NULL").fetchall()

    to_clear = []
    for r in rows:
        try:
            subs = json.loads(r["subscores"])
        except Exception:
            subs = None
        if _incomplete(subs):
            to_clear.append(r["id"])

    print(f"{len(rows)} sub-scored rows found; {len(to_clear)} are all-zero or incomplete.")
    if args.dry_run:
        print("Dry run — nothing changed.")
        return
    if not to_clear:
        print("Nothing to clean.")
        return
    con.executemany("UPDATE articles SET subscores=NULL WHERE id=?", [(i,) for i in to_clear])
    con.commit()
    print(f"Cleared {len(to_clear)} rows. They'll be re-scored (capped per run) on the next "
          "scoring_insights runs, so the numbers will clean up over a few runs "
          "(faster once Gemini billing is enabled).")


if __name__ == "__main__":
    main()
