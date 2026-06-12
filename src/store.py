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
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
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


def failing_sources(con, days: int = 2) -> list[str]:
    """Sources that returned 0 items (or errored) in every run over the last N distinct run dates."""
    rows = con.execute(
        """SELECT source FROM source_health
           WHERE run_date IN (SELECT DISTINCT run_date FROM source_health ORDER BY run_date DESC LIMIT ?)
           GROUP BY source HAVING MAX(items_fetched) = 0""",
        (days,),
    ).fetchall()
    return [r["source"] for r in rows]
