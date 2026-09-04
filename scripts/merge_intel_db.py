"""Union-merge a conflicted data/intel.db during a git merge.

data/intel.db is a binary SQLite file, so git cannot merge it. Any pull where the
local machine and the automated run BOTH ingested rows leaves a conflict, and
resolving it by picking a side destroys ingested articles the rolling feeds
cannot return. This script resolves the conflict by UNION instead.

It reads the two conflict stages git already holds in the index:
    :2 = ours   (HEAD / the local branch)
    :3 = theirs (the incoming commit)
and writes a database containing every article present in EITHER side, matched on
url_hash -- the articles table's real identity key (UNIQUE), unlike id.

Rows unique to `theirs` are inserted WITHOUT their original ids. Once both sides
ingest independently their autoincrement counters drift, so the same id can refer
to different articles on each side; reusing ids would silently clobber unrelated
rows. SQLite assigns fresh ids instead.

Only `articles` is unioned. source_health / profile_scores / scouts are
operational or recomputable state and keep the `ours` value.

Usage, from the repo root, while the conflict is open:
    python3 scripts/merge_intel_db.py
    git add data/intel.db
    git commit --no-edit
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

DB = Path("data/intel.db")


def stage(num: int, dest: Path) -> bool:
    """Extract conflict stage `num` of DB to `dest`. False if that stage is absent."""
    try:
        blob = subprocess.run(["git", "show", f":{num}:{DB.as_posix()}"],
                              capture_output=True, check=True).stdout
    except subprocess.CalledProcessError:
        return False
    dest.write_bytes(blob)
    return True


def counts(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT count(*) FROM articles").fetchone()[0]
    finally:
        con.close()


def main() -> int:
    if not DB.exists():
        sys.exit(f"{DB} not found -- run this from the repo root.")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ours, theirs, merged = tmp / "ours.db", tmp / "theirs.db", tmp / "merged.db"

        if not stage(2, ours) or not stage(3, theirs):
            sys.exit("No merge conflict staged for data/intel.db -- nothing to do.\n"
                     "(This script is only for resolving an open conflict.)")

        n_ours, n_theirs = counts(ours), counts(theirs)
        shutil.copy(ours, merged)

        con = sqlite3.connect(merged)
        con.execute("ATTACH DATABASE ? AS incoming", (str(theirs),))
        cols = [r[1] for r in con.execute("PRAGMA table_info(articles)") if r[1] != "id"]
        col_list = ", ".join(cols)
        con.execute(f"INSERT INTO main.articles ({col_list}) "
                    f"SELECT {col_list} FROM incoming.articles "
                    f"WHERE url_hash NOT IN (SELECT url_hash FROM main.articles)")
        con.commit()

        n_merged = con.execute("SELECT count(*) FROM articles").fetchone()[0]
        dupes = con.execute("SELECT count(*) FROM "
                            "(SELECT url_hash FROM articles GROUP BY url_hash HAVING count(*) > 1)"
                            ).fetchone()[0]
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()

        if dupes or integrity != "ok":
            sys.exit(f"ABORT -- not writing: duplicate url_hash groups={dupes}, "
                     f"integrity_check={integrity}")

        shutil.copy(merged, DB)

    print(f"ours={n_ours}  theirs={n_theirs}  merged={n_merged} "
          f"(recovered {n_merged - n_ours} row(s) only present upstream)")
    print("duplicate url_hash groups: 0    integrity_check: ok")
    print("\nNow run:\n  git add data/intel.db\n  git commit --no-edit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
