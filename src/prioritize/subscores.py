"""Structured sub-score capture.

A second, cheap LLM pass that breaks each candidate article into six universal
strategic dimensions (each 0-10). These are:
  - stored as JSON on the article (column `subscores`), and
  - the raw material for AHP analysis (scripts/ahp.py) and per-executive
    personalization (src/profiles.py).

This module is ADDITIVE: it does not modify store.py or llm_relevance.py.
It ensures its own DB column exists and writes to it directly.
"""
import json
import logging
import sqlite3

from src.llm_client import LLMClient, strip_fences

log = logging.getLogger("prioritize.subscores")

# The six universal dimensions, scored for every article regardless of area.
# Keep this list stable — AHP and profiles depend on the exact keys/order.
DIMENSIONS = [
    "financial_impact",       # effect on reimbursement, margins, revenue, cost
    "strategic_impact",       # effect on growth strategy, positioning, long-term planning
    "competitive_relevance",  # relevance to competitor moves / market position
    "operational_impact",     # effect on operations, capacity, compliance, care delivery
    "time_sensitivity",       # urgency — how soon a response is needed
    "proximity",              # geographic / market closeness to South Florida
]

DIMENSION_DESCRIPTIONS = {
    "financial_impact": "effect on reimbursement, margins, revenue, or costs",
    "strategic_impact": "effect on growth strategy, market positioning, or long-term planning",
    "competitive_relevance": "relevance to competitor moves or the organization's market position",
    "operational_impact": "effect on operations, capacity, compliance, or care delivery",
    "time_sensitivity": "urgency — how soon the organization should respond",
    "proximity": "geographic or market closeness to South Florida and the organization",
}

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
    """Add the `subscores` column if an older DB doesn't have it (safe migration)."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(articles)").fetchall()]
    if "subscores" not in cols:
        con.execute("ALTER TABLE articles ADD COLUMN subscores TEXT")
        con.commit()
        log.info("Added 'subscores' column to articles table")


def score_batch(client: LLMClient, model: str, org: dict,
                articles: list[dict], batch_size: int = 10) -> list[dict]:
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
    con.execute("UPDATE articles SET subscores=? WHERE id=?",
                (json.dumps(subscores), article_id))


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
