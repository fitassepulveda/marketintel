#!/usr/bin/env python3
"""Daily Market Intelligence Briefing pipeline.

Usage:
  python run_briefing.py                 # full run: ingest -> score -> email
  python run_briefing.py --dry-run       # everything except sending; saves HTML to data/briefings/
  python run_briefing.py --no-yutori     # skip Yutori sources (e.g., before key is procured)
  python run_briefing.py --no-llm        # skip LLM scoring/synthesis (ingestion test only)
"""
from __future__ import annotations
import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import config, store
from src.ingest import rss, yutori
from src.llm_client import LLMClient
from src.output import emailer, synthesize
from src.prioritize import llm_relevance, scoring

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("run")


def ingest(con, cfg, run_date: str, use_yutori: bool) -> int:
    new_count = 0
    lookback = cfg["settings"]["briefing"]["lookback_hours"]

    for source, area, items, error in rss.fetch_all(cfg["sources"], lookback):
        inserted = sum(store.insert_article(con, it) for it in items)
        store.log_source_health(con, run_date, source["name"], area, len(items), error)
        new_count += inserted

    import os
    yutori_on = use_yutori and bool(os.environ.get("YUTORI_API_KEY"))
    for source, area, items, error in yutori.fetch_all(cfg["sources"], cfg["settings"]["yutori"], yutori_on):
        inserted = sum(store.insert_article(con, it) for it in items)
        store.log_source_health(con, run_date, source["name"], area, len(items), error)
        new_count += inserted

    con.commit()
    log.info("Ingestion complete: %d new articles", new_count)
    return new_count


def prioritize(con, cfg, client, use_llm: bool) -> list[dict]:
    settings, weights = cfg["settings"], cfg["weights"]
    since = (datetime.now(timezone.utc) - timedelta(hours=settings["briefing"]["lookback_hours"])).isoformat()
    rows = [dict(r) for r in store.unbriefed_recent(con, since)]
    if not rows:
        return []

    # Cheap pre-rank, then LLM-score only the top slice (cost control)
    rows.sort(key=lambda a: scoring.pre_rank(weights, a), reverse=True)
    to_score = rows[: weights.get("max_llm_scored_items", 60)]

    if use_llm:
        models = settings["llm"]["models"][settings["llm"]["provider"]]
        scores = llm_relevance.score_batch(
            client, models["scoring"], settings["org"],
            settings["key_questions"], to_score,
        )
    else:
        scores = [(5.0, "llm disabled")] * len(to_score)

    kept = []
    for art, (llm_score, why) in zip(to_score, scores):
        comp = scoring.composite(weights, art, llm_score)
        art.update(llm_score=llm_score, llm_rationale=why, composite_score=comp)
        store.save_scores(con, art["id"], llm_score, why, comp)
        if comp >= weights["score_threshold"]:
            kept.append(art)
    con.commit()

    kept.sort(key=lambda a: a["composite_score"], reverse=True)
    kept = kept[: settings["briefing"]["max_stories"]]
    log.info("Prioritization: %d items above threshold (of %d scored)", len(kept), len(to_score))
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't send email; save HTML locally")
    ap.add_argument("--no-yutori", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    cfg = config.load_all()
    con = store.connect()
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    use_llm = not args.no_llm

    ingest(con, cfg, run_date, use_yutori=not args.no_yutori)

    client = LLMClient(cfg["settings"]["llm"]["provider"]) if use_llm else None
    top = prioritize(con, cfg, client, use_llm)
    if not top:
        log.warning("No items above threshold today — no briefing sent.")
        return

    if use_llm:
        models = cfg["settings"]["llm"]["models"][cfg["settings"]["llm"]["provider"]]
        briefing = synthesize.build_briefing(
            client, models["synthesis"],
            cfg["settings"]["llm"]["max_tokens_synthesis"],
            cfg["settings"]["org"], cfg["settings"]["key_questions"], top,
        )
    else:
        briefing = {"takeaways": [a["title"] for a in top[:5]], "key_question_answers": {},
                    "stories": [{"title": a["title"], "area": a["area"], "source": a["source"],
                                 "url": a["url"], "what_happened": a["summary"][:200],
                                 "why_it_matters": "", "exposure": ""} for a in top],
                    "watch": [], "actions": []}

    date_h = datetime.now().strftime("%A, %B %d, %Y")
    html = emailer.render_html(briefing, date_h, cfg["settings"]["org"]["name"],
                               store.failing_sources(con))

    out_dir = config.DATA_DIR / "briefings"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{run_date}.html").write_text(html, encoding="utf-8")
    (out_dir / f"{run_date}.json").write_text(json.dumps(briefing, indent=2), encoding="utf-8")

    if args.dry_run:
        log.info("Dry run: briefing saved to data/briefings/%s.html (not sent)", run_date)
    else:
        emailer.send(html, f'{cfg["settings"]["briefing"]["subject_prefix"]} — {date_h}', {
            "host": config.env("SMTP_HOST"), "port": config.env("SMTP_PORT"),
            "user": config.env("SMTP_USER"), "password": config.env("SMTP_PASS"),
            "from": config.env("EMAIL_FROM"),
            "to": [e.strip() for e in config.env("EMAIL_TO").split(",") if e.strip()],
        })
        log.info("Briefing emailed.")

    store.mark_briefed(con, [a["id"] for a in top], run_date)
    con.commit()


if __name__ == "__main__":
    main()
