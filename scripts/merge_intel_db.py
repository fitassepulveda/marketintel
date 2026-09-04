"""Union-merge a conflicted data/intel.db during a git merge.

data/intel.db is a binary SQLite file, so git cannot merge it. Any pull where the local
machine and the automated run BOTH wrote rows leaves a conflict, and resolving it by
picking a side destroys data the pipeline cannot regenerate. This script resolves the
conflict by UNION instead.

It reads the two conflict stages git already holds in the index:
    :2 = ours   (HEAD / the local branch)
    :3 = theirs (the incoming commit)

WHAT IT MERGES, and why each one matters:
  * articles       -- unioned on url_hash (the table's real identity key; id is NOT stable).
                      Rows unique to theirs are inserted WITHOUT their original ids: once
                      both sides ingest independently their autoincrement counters drift,
                      so the same id can refer to different articles on each side.
  * briefed_on     -- a row present on BOTH sides keeps ours, so a stamp that only theirs
                      recorded would be silently dropped. Any stamp missing on our side is
                      copied across. Losing one makes an already-sent story resurface as a
                      duplicate in a later briefing.
  * profile_scores -- unioned, REMAPPED to our ids via url_hash (see above: raw article_id
                      does not survive the articles union). These are LLM-scored rows that
                      cost real API spend to produce and are the only record that the
                      semantic-profile pass ran.
  * source_health  -- unioned on the whole row; the table has no primary key.

An earlier version of this script merged ONLY `articles`, and on 2026-09-04 that silently
destroyed 100 profile_scores rows, 44 source_health rows and one briefed_on stamp from the
automated run. That is the reason for everything above: anything this script does not
explicitly union is DATA LOSS, not a no-op.

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


def main() -> int:
    if not DB.exists():
        sys.exit(f"{DB} not found -- run this from the repo root.")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ours, theirs, merged = tmp / "ours.db", tmp / "theirs.db", tmp / "merged.db"

        if not stage(2, ours) or not stage(3, theirs):
            sys.exit("No merge conflict staged for data/intel.db -- nothing to do.\n"
                     "(This script is only for resolving an open conflict.)")

        shutil.copy(ours, merged)
        con = sqlite3.connect(merged)
        con.execute("ATTACH DATABASE ? AS incoming", (str(theirs),))

        stats = {}
        stats["articles_before"] = con.execute("SELECT count(*) FROM main.articles").fetchone()[0]

        # --- articles: union on url_hash, fresh ids ---
        cols = [r[1] for r in con.execute("PRAGMA table_info(articles)") if r[1] != "id"]
        col_list = ", ".join(cols)
        con.execute(f"INSERT INTO main.articles ({col_list}) "
                    f"SELECT {col_list} FROM incoming.articles "
                    f"WHERE url_hash NOT IN (SELECT url_hash FROM main.articles)")
        stats["articles_after"] = con.execute("SELECT count(*) FROM main.articles").fetchone()[0]

        # id maps, built AFTER the articles union so both sides resolve to final ids
        ours_ids = {h: i for h, i in con.execute("SELECT url_hash, id FROM main.articles")}
        theirs_ids = {i: h for i, h in con.execute("SELECT id, url_hash FROM incoming.articles")}

        # --- briefed_on: fill in any stamp only theirs recorded, never overwrite ours ---
        stamps = 0
        for h, b in con.execute("SELECT url_hash, briefed_on FROM incoming.articles "
                                "WHERE briefed_on IS NOT NULL"):
            nid = ours_ids.get(h)
            if nid is None:
                continue
            if not con.execute("SELECT briefed_on FROM main.articles WHERE id=?", (nid,)).fetchone()[0]:
                con.execute("UPDATE main.articles SET briefed_on=? WHERE id=?", (b, nid))
                stamps += 1
        stats["briefed_on_restored"] = stamps

        # --- profile_scores: union, remapped to final ids ---
        ps_before = con.execute("SELECT count(*) FROM main.profile_scores").fetchone()[0]
        added = 0
        for aid, prof, score, rat, ver, at in con.execute(
                "SELECT article_id, profile, score, rationale, version, scored_at "
                "FROM incoming.profile_scores").fetchall():
            h = theirs_ids.get(aid)
            nid = ours_ids.get(h) if h else None
            if nid is None:
                continue
            if con.execute("SELECT 1 FROM main.profile_scores WHERE article_id=? AND profile=?",
                           (nid, prof)).fetchone():
                continue
            con.execute("INSERT INTO main.profile_scores "
                        "(article_id, profile, score, rationale, version, scored_at) "
                        "VALUES (?,?,?,?,?,?)", (nid, prof, score, rat, ver, at))
            added += 1
        stats["profile_scores"] = (ps_before, ps_before + added)

        # --- source_health: whole-row union (no primary key) ---
        sh_before = con.execute("SELECT count(*) FROM main.source_health").fetchone()[0]
        con.execute("""INSERT INTO main.source_health (run_date, source, area, items_fetched, error)
                       SELECT run_date, source, area, items_fetched, error FROM incoming.source_health
                       EXCEPT
                       SELECT run_date, source, area, items_fetched, error FROM main.source_health""")
        sh_after = con.execute("SELECT count(*) FROM main.source_health").fetchone()[0]
        stats["source_health"] = (sh_before, sh_after)

        con.commit()

        dupes = con.execute("SELECT count(*) FROM "
                            "(SELECT url_hash FROM main.articles GROUP BY url_hash HAVING count(*) > 1)"
                            ).fetchone()[0]
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()

        if dupes or integrity != "ok":
            sys.exit(f"ABORT -- not writing: duplicate url_hash groups={dupes}, "
                     f"integrity_check={integrity}")

        shutil.copy(merged, DB)

    a0, a1 = stats["articles_before"], stats["articles_after"]
    p0, p1 = stats["profile_scores"]
    s0, s1 = stats["source_health"]
    print(f"articles       {a0} -> {a1}   (+{a1 - a0} only upstream)")
    print(f"profile_scores {p0} -> {p1}   (+{p1 - p0})")
    print(f"source_health  {s0} -> {s1}   (+{s1 - s0})")
    print(f"briefed_on stamps restored: {stats['briefed_on_restored']}")
    print("duplicate url_hash groups: 0    integrity_check: ok")
    print("\nNow run:\n  git add data/intel.db\n  git commit --no-edit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
