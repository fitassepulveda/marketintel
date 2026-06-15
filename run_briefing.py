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
from email.utils import parsedate_to_datetime
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
    ycfg = cfg["settings"]["yutori"]
    yutori_on = use_yutori and bool(os.environ.get("YUTORI_API_KEY"))
    stop_after = ycfg.get("stop_after_first_update", True)
    for source, area, items, error in yutori.fetch_all(con, cfg["sources"], ycfg, yutori_on):
        inserted = sum(store.insert_article(con, it) for it in items)
        store.log_source_health(con, run_date, source["name"], area, len(items), error)
        new_count += inserted
        # One-shot mode: once we've pulled a scout's first results, archive it so
        # it never runs (and bills) a second time.
        if yutori_on and stop_after and items:
            try:
                yutori.stop_scout(con, ycfg, source["name"])
            except Exception as exc:
                log.warning("Could not archive scout '%s': %s", source["name"], exc)

    con.commit()
    log.info("Ingestion complete: %d new articles", new_count)
    return new_count


def _parse_dt(s) -> datetime | None:
    """Parse an ISO or RFC-822 date string to an aware datetime (UTC), else None."""
    if not s:
        return None
    s = str(s).strip()
    dt = None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(s)
        except (TypeError, ValueError, IndexError):
            return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_recent(article: dict, cutoff: datetime) -> bool:
    """Recent = PUBLISHED on/after cutoff. If no usable publish date, fall back to
    when we fetched it (so undated items can't be older than the window either)."""
    pub = _parse_dt(article.get("published"))
    if pub is not None:
        return pub >= cutoff
    fetched = _parse_dt(article.get("fetched"))
    return fetched is not None and fetched >= cutoff


def prioritize(con, cfg, client, use_llm: bool) -> list[dict]:
    settings, weights = cfg["settings"], cfg["weights"]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=settings["briefing"]["lookback_hours"])
    # Pull a generous FETCH window, then keep only items PUBLISHED within the cutoff —
    # so an old article surfaced today (by a scout or feed) doesn't sneak in.
    fetch_floor = (now - timedelta(days=14)).isoformat()
    rows = [dict(r) for r in store.unbriefed_recent(con, fetch_floor)]
    rows = [a for a in rows if _is_recent(a, cutoff)]
    if not rows:
        log.info("No articles published within the last %dh.", settings["briefing"]["lookback_hours"])
        return []

    # No keyword influence anywhere. The LLM scores every recent article for relevance
    # to the org (against its area's key question). Order by recency (neutral), then
    # cap per source so one high-volume feed can't flood the pool, then a global cap.
    _epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    rows.sort(key=lambda a: (_parse_dt(a.get("published")) or _parse_dt(a.get("fetched")) or _epoch),
              reverse=True)
    max_per_source = weights.get("max_per_source", 15)
    uncapped = set(weights.get("uncapped_sources", []))
    per_source: dict = {}
    capped = []
    for a in rows:
        if a["source"] in uncapped:   # high-value competitor feed — never trimmed
            capped.append(a)
            continue
        n = per_source.get(a["source"], 0) + 1
        per_source[a["source"]] = n
        if n <= max_per_source:
            capped.append(a)
    to_score = capped[: weights.get("max_llm_scored_items", 150)]

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
        comp = scoring.composite(weights, art, llm_score)  # area-light, LLM-led
        art.update(llm_score=llm_score, llm_rationale=why, composite_score=comp)
        store.save_scores(con, art["id"], llm_score, why, comp)
        if comp >= weights["score_threshold"]:
            kept.append(art)
    con.commit()

    kept.sort(key=lambda a: a["composite_score"], reverse=True)
    # Collapse same-event duplicates. Primary: semantic (embedding) similarity, which
    # understands meaning regardless of wording. Fallback: keyword rules if embeddings
    # are unavailable (no key / API error).
    before = len(kept)
    deduped = None
    if use_llm and client and len(kept) > 1:
        try:
            vecs = client.embed([f'{a["title"]} {(a.get("summary") or "")[:400]}' for a in kept])
            deduped = scoring.semantic_dedupe(kept, vecs, weights.get("dedup_cosine_similarity", 0.85))
            log.info("Dedup: semantic (embeddings)")
        except Exception as exc:
            log.warning("Embedding dedup failed (%s); falling back to keyword dedup", exc)
    if deduped is None:
        deduped = scoring.dedupe_by_title(kept, weights.get("dedup_title_similarity", 0.90),
                                          weights.get("dedup_token_overlap", 0.6))
        log.info("Dedup: keyword fallback")
    kept = deduped
    final = kept[: settings["briefing"]["max_stories"]]
    log.info("Prioritization: %d scored, %d above threshold, %d after dedup -> top %d",
             len(to_score), before, len(kept), len(final))
    return final


def _send_html(settings, subject: str, body_html: str, dry_run: bool,
               run_date: str, label: str = "Digest") -> bool:
    """Send an HTML email, respecting --dry-run and SMTP readiness. Returns True if sent."""
    recipients = settings["briefing"].get("digest_recipients", [])
    smtp_ready = all(config.env(k, required=False) for k in
                     ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "EMAIL_FROM"))
    if dry_run:
        log.info("Dry run: %s saved to data/briefings/ (not sent).", label)
        return False
    if not (recipients and smtp_ready):
        log.warning("%s saved but NOT sent (set SMTP_* + EMAIL_FROM in .env and "
                    "digest_recipients in settings.yaml).", label)
        return False
    emailer.send(
        body_html, subject,
        {"host": config.env("SMTP_HOST"), "port": config.env("SMTP_PORT"),
         "user": config.env("SMTP_USER"), "password": config.env("SMTP_PASS"),
         "from": config.env("EMAIL_FROM"), "to": recipients},
        subtype="html",
    )
    log.info("%s emailed to %s", label, ", ".join(recipients))
    return True


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

    settings = cfg["settings"]
    date_h = datetime.now().strftime("%A, %B %d, %Y")
    out_dir = config.DATA_DIR / "briefings"
    out_dir.mkdir(exist_ok=True)

    # Quiet day: nothing cleared the threshold. Still send a short note so an empty
    # news day is never indistinguishable from a broken pipeline.
    if not top:
        log.info("No stories cleared the threshold — sending a quiet-day note.")
        quiet_html = emailer.render_quiet_html(
            date_h, settings["org"]["name"], settings["briefing"]["lookback_hours"],
            store.failing_sources(con),
        )
        (out_dir / f"{run_date}_digest.html").write_text(quiet_html, encoding="utf-8")
        _send_html(settings, f'{settings["briefing"]["subject_prefix"]} — {date_h} (quiet day)',
                   quiet_html, args.dry_run, run_date, label="Quiet-day note")
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
                                 "why_it_matters": "", "exposure": "", "watch_next": "",
                                 "coverage_label": ""} for a in top],
                    "watch": [], "actions": []}

    # Safety net: guarantee every ranked story appears, even if synthesis dropped one.
    # Append a basic entry (from the DB row) for any top item the LLM didn't emit.
    have = {emailer._norm_url(s.get("url", "")) for s in briefing["stories"]}
    have |= {str(s.get("title", "")).strip().lower() for s in briefing["stories"]}
    for a in top:
        if emailer._norm_url(a["url"]) in have or str(a["title"]).strip().lower() in have:
            continue
        briefing["stories"].append({
            "title": a["title"], "area": a["area"], "source": a["source"], "url": a["url"],
            "what_happened": (a.get("summary") or a.get("content") or "")[:300],
            "why_it_matters": "", "exposure": "", "watch_next": "",
            "coverage_label": f'{a["source"]} coverage',
        })

    html = emailer.render_html(briefing, date_h, settings["org"]["name"], store.failing_sources(con))

    # Email digest for the top N stories (HTML, with larger titles). Captured/Published
    # dates are matched from the DB rows (by url, then title). Plain text saved too.
    org_short = settings["org"].get("short_name", settings["org"]["name"])
    top_n = settings["briefing"].get("digest_top_n", 5)
    digest = emailer.render_digest(briefing["stories"], date_h, org_short, articles=top, top_n=top_n)
    digest_html = emailer.render_digest_html(briefing["stories"], date_h, org_short, articles=top, top_n=top_n)

    (out_dir / f"{run_date}.html").write_text(html, encoding="utf-8")
    (out_dir / f"{run_date}.json").write_text(json.dumps(briefing, indent=2), encoding="utf-8")
    (out_dir / f"{run_date}_digest.txt").write_text(digest, encoding="utf-8")
    (out_dir / f"{run_date}_digest.html").write_text(digest_html, encoding="utf-8")

    sent = _send_html(settings, f'{settings["briefing"]["subject_prefix"]} — {date_h}',
                      digest_html, args.dry_run, run_date, label="Digest")

    # Only consume dedup state when the briefing actually went out — a dry run or a
    # failed/skipped send must not mark stories as already-briefed.
    if sent:
        store.mark_briefed(con, [a["id"] for a in top], run_date)
        con.commit()


if __name__ == "__main__":
    main()
