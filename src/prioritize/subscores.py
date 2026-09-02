"""Structured sub-score capture.

A second, cheap LLM pass that breaks each candidate article into the universal
strategic dimensions (each 0-10) listed in DIMENSIONS below. These are:
  - stored as JSON on the article (column `subscores`), stamped with
    SUBSCORE_VERSION so a vector from an older dimension list / prompt is never
    silently reused, and
  - the raw material for AHP analysis (scripts/ahp.py) and per-executive
    personalization (src/profiles.py).

This module is ADDITIVE: it does not modify store.py or llm_relevance.py.
It ensures its own DB column exists and writes to it directly.
"""
from __future__ import annotations
import json
import logging
import sqlite3
from datetime import datetime, timezone

from src.llm_client import LLMClient, strip_fences

log = logging.getLogger("prioritize.subscores")

# The universal dimensions, scored for every article regardless of area.
# Keep this list stable — AHP and profiles depend on the exact keys/order.
# CHANGING THIS LIST (or the prompt below) REQUIRES BUMPING SUBSCORE_VERSION,
# otherwise stored vectors missing the new key silently score 0 on it.
DIMENSIONS = [
    "financial_impact",       # effect on reimbursement, margins, revenue, cost
    "strategic_impact",       # effect on growth strategy, positioning, long-term planning
    "competitive_relevance",  # relevance to competitor moves / market position
    "operational_impact",     # effect on operations, capacity, compliance, care delivery
    "time_sensitivity",       # urgency — how soon a response is needed
    "proximity",              # geographic / market closeness to South Florida
    "actionability",          # does it imply a concrete decision/response for leadership
    "direct_relevance",       # how directly it affects UHealth specifically vs general news
    "magnitude",              # size/materiality of the development (dollars, scale, scope)
]

DIMENSION_DESCRIPTIONS = {
    "financial_impact": "effect on reimbursement, margins, revenue, or costs",
    "strategic_impact": "effect on growth strategy, market positioning, or long-term planning",
    "competitive_relevance": "relevance to competitor moves or the organization's market position",
    "operational_impact": "effect on operations, capacity, compliance, or care delivery",
    "time_sensitivity": "urgency — how soon the organization should respond",
    "proximity": "geographic or market closeness to South Florida and the organization",
    "actionability": "whether it implies a concrete decision, response, or plan for leadership (vs. passive/informational)",
    "direct_relevance": "how directly it affects the organization specifically, vs. general industry news",
    "magnitude": "the size or materiality of the development — dollar amounts, scale, scope",
}

# Bump whenever DIMENSIONS or SYSTEM changes. Stored sub-scores carrying an older
# version are treated as absent and re-scored (mirrors store.SCORE_VERSION).
SUBSCORE_VERSION = 1

SYSTEM = (
    "You analyze news items for a healthcare system's strategic intelligence. "
    "For each item, rate it 0-10 on EACH of these independent dimensions:\n"
    + "\n".join(f"- {d}: {DIMENSION_DESCRIPTIONS[d]}" for d in DIMENSIONS)
    + "\n\nRespond ONLY with a JSON array, one object per item, same order:\n"
      '[{"i": <index>, '
    + ", ".join(f'"{d}": <0-10>' for d in DIMENSIONS)
    + "}]"
)


def ensure_column(con: sqlite3.Connection):
    """Safe migrations: the `subscores` column, its version stamp, and the
    per-profile sub-score table. Idempotent — call on every run."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(articles)").fetchall()]
    if "subscores" not in cols:
        con.execute("ALTER TABLE articles ADD COLUMN subscores TEXT")
        log.info("Added 'subscores' column to articles table")
    if "subscores_version" not in cols:
        con.execute("ALTER TABLE articles ADD COLUMN subscores_version INTEGER")
        log.info("Added 'subscores_version' column to articles table")
    con.execute("""
        CREATE TABLE IF NOT EXISTS profile_subscores (
            article_id INTEGER NOT NULL,
            profile    TEXT NOT NULL,        -- profiles.yaml `name`
            subscores  TEXT NOT NULL,        -- JSON {dimension: 0-10}
            version    INTEGER NOT NULL,     -- SUBSCORE_VERSION at write time
            scored_at  TEXT NOT NULL,
            PRIMARY KEY (article_id, profile)
        )""")
    con.commit()


def score_batch(client: LLMClient, model: str, org: dict,
                articles: list[dict], batch_size: int = 15) -> list[dict]:
    """Return a list of {dimension: score} dicts aligned with `articles`."""
    results: list[dict] = [dict.fromkeys(DIMENSIONS, 0.0) for _ in articles]
    for start in range(0, len(articles), batch_size):
        batch = articles[start:start + batch_size]
        items_txt = "\n".join(
            f'[{i}] area={a["area"]} | source={a["source"]}\n'
            f'    title: {a["title"]}\n    summary: {a["summary"][:400]}'
            for i, a in enumerate(batch)
        )
        prompt = (
            f"Organization: {org['name']} — {org['description']} Region: {org['region']}\n\n"
            f"Items:\n{items_txt}"
        )
        try:
            text = strip_fences(client.complete(model, SYSTEM, prompt, max_tokens=2000))
            for obj in json.loads(text):
                idx = start + int(obj["i"])
                if start <= idx < start + len(batch):
                    results[idx] = {d: float(obj.get(d, 0)) for d in DIMENSIONS}
        except Exception as exc:
            log.warning("Sub-score batch failed (%s); items keep zeros", exc)
    return results


def save(con: sqlite3.Connection, article_id: int, subscores: dict):
    """Save the shared (org-level) vector, stamped with the current version."""
    con.execute("UPDATE articles SET subscores=?, subscores_version=? WHERE id=?",
                (json.dumps(subscores), SUBSCORE_VERSION, article_id))


def save_for_profile(con: sqlite3.Connection, article_id: int, profile: str,
                     subscores: dict):
    """Save a PROFILE-SPECIFIC vector (from a per-profile scoring pass)."""
    con.execute(
        "INSERT INTO profile_subscores (article_id, profile, subscores, version, scored_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(article_id, profile) DO UPDATE SET "
        "subscores=excluded.subscores, version=excluded.version, scored_at=excluded.scored_at",
        (article_id, profile, json.dumps(subscores), SUBSCORE_VERSION,
         datetime.now(timezone.utc).isoformat()))


def load_for_profile(con: sqlite3.Connection, article_id: int, profile: str) -> dict | None:
    """Current-version profile vector, or None if absent/stale (=> re-score)."""
    row = con.execute(
        "SELECT subscores FROM profile_subscores "
        "WHERE article_id=? AND profile=? AND version=?",
        (article_id, profile, SUBSCORE_VERSION)).fetchone()
    return json.loads(row[0]) if row else None


def is_current(article: dict) -> bool:
    """True when the article's stored shared vector is usable as-is."""
    return bool(article.get("subscores")) and \
        article.get("subscores_version") == SUBSCORE_VERSION


def load_scored(con: sqlite3.Connection) -> list[dict]:
    """All articles that have sub-scores, as dicts with a parsed `subscores` field."""
    rows = con.execute(
        "SELECT * FROM articles WHERE subscores IS NOT NULL ORDER BY fetched DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["subscores"] = json.loads(d["subscores"])
            out.append(d)
        except (TypeError, json.JSONDecodeError):
            continue
    return out
