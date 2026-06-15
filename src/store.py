"""SQLite storage: article archive, dedup memory, source health log."""
from __future__ import annotations
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from .config import DATA_DIR

DB_PATH = DATA_DIR / "intel.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    url_hash TEXT UNIQUE NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    content TEXT,
    source TEXT NOT NULL,
    area TEXT NOT NULL,
    published TEXT,
    fetched TEXT NOT NULL,
    enrichment TEXT,           -- JSON from Yutori (entities, sentiment, ...)
    llm_score REAL,            -- 0-10 relevance vs the area's key question
    llm_rationale TEXT,
    composite_score REAL,      -- 0-100 final score
    briefed_on TEXT            -- date it appeared in a briefing (dedup across days)
);
CREATE TABLE IF NOT EXISTS source_health (
    run_date TEXT NOT NULL,
    source TEXT NOT NULL,
    area TEXT NOT NULL,
    items_fetched INTEGER NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS scouts (
    source TEXT PRIMARY KEY,        -- source name from sources.yaml
    area TEXT NOT NULL,
    scout_id TEXT NOT NULL,         -- Yutori scout UUID
    query TEXT,
    created_at TEXT NOT NULL,
    last_update_ts INTEGER NOT NULL DEFAULT 0,  -- newest update timestamp already ingested
    active INTEGER NOT NULL DEFAULT 1           -- 0 once archived (stopped scouting)
);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # Migration: add scouts.active to DBs created before the one-shot feature.
    cols = [r[1] for r in con.execute("PRAGMA table_info(scouts)").fetchall()]
    if cols and "active" not in cols:
        con.execute("ALTER TABLE scouts ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        con.commit()
    return con


def url_hash(url: str) -> str:
    return hashlib.sha256(url.strip().lower().encode()).hexdigest()[:24]


def insert_article(con, item: dict) -> bool:
    """Insert if new. Returns True if inserted, False if duplicate."""
    try:
        con.execute(
            """INSERT INTO articles (url_hash, url, title, summary, content, source,
               area, published, fetched, enrichment)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                url_hash(item["url"]), item["url"], item["title"],
                item.get("summary", ""), item.get("content", ""),
                item["source"], item["area"], item.get("published", ""),
                datetime.now(timezone.utc).isoformat(),
                json.dumps(item.get("enrichment")) if item.get("enrichment") else None,
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def log_source_health(con, run_date: str, source: str, area: str, count: int, error: str | None):
    con.execute(
        "INSERT INTO source_health (run_date, source, area, items_fetched, error) VALUES (?,?,?,?,?)",
        (run_date, source, area, count, error),
    )


def unbriefed_recent(con, since_iso: str) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM articles WHERE briefed_on IS NULL AND fetched >= ?", (since_iso,)
    ).fetchall()


def save_scores(con, article_id: int, llm_score: float, rationale: str, composite: float):
    con.execute(
        "UPDATE articles SET llm_score=?, llm_rationale=?, composite_score=? WHERE id=?",
        (llm_score, rationale, composite, article_id),
    )


def mark_briefed(con, ids: list[int], date_str: str):
    con.executemany("UPDATE articles SET briefed_on=? WHERE id=?", [(date_str, i) for i in ids])


def upsert_scout(con, source: str, area: str, scout_id: str, query: str):
    """Record (or replace) the Yutori scout backing a source. Resets the cursor."""
    con.execute(
        """INSERT INTO scouts (source, area, scout_id, query, created_at, last_update_ts, active)
           VALUES (?,?,?,?,?,0,1)
           ON CONFLICT(source) DO UPDATE SET
             area=excluded.area, scout_id=excluded.scout_id,
             query=excluded.query, created_at=excluded.created_at,
             last_update_ts=0, active=1""",
        (source, area, scout_id, query, datetime.now(timezone.utc).isoformat()),
    )


def get_scout(con, source: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM scouts WHERE source=?", (source,)).fetchone()


def all_scouts(con) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM scouts").fetchall()


def set_scout_cursor(con, source: str, last_update_ts: int):
    con.execute("UPDATE scouts SET last_update_ts=? WHERE source=?", (last_update_ts, source))


def set_scout_active(con, source: str, active: bool):
    con.execute("UPDATE scouts SET active=? WHERE source=?", (1 if active else 0, source))


def failing_sources(con, days: int = 2) -> list[str]:
    """Sources that returned 0 items (or errored) in every run over the last N distinct run dates."""
    rows = con.execute(
        """SELECT source FROM source_health
           WHERE run_date IN (SELECT DISTINCT run_date FROM source_health ORDER BY run_date DESC LIMIT ?)
           GROUP BY source HAVING MAX(items_fetched) = 0""",
        (days,),
    ).fetchall()
    return [r["source"] for r in rows]
