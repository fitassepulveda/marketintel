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
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src import config, store
from src.ingest import rss, yutori, enrich
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

    ycfg = cfg["settings"]["yutori"]
    yutori_on = use_yutori and bool(os.environ.get("YUTORI_API_KEY"))
    stop_after = ycfg.get("stop_after_first_update", False)
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


def _enrich_undated(con, rows: list[dict], timeout: int) -> None:
    """For candidate rows with no publish date, read it from the article page and
    persist it, so the 72h filter runs on a real date rather than fetch time.
    Best-effort and in-place: failures leave the row undated (fetch-time fallback)."""
    undated = [a for a in rows if not _parse_dt(a.get("published")) and a.get("url")]
    if not undated:
        return
    filled = 0
    for a in undated:
        iso = enrich.fetch_published_date(a["url"], timeout)
        if iso and _parse_dt(iso):
            a["published"] = iso
            store.set_published(con, a["id"], iso)
            filled += 1
    if filled:
        con.commit()
    log.info("Date enrichment: recovered %d/%d undated publish dates", filled, len(undated))


def _is_recent(article: dict, cutoff: datetime) -> bool:
    """Recent = PUBLISHED on/after cutoff. If no usable publish date (even after page
    enrichment), fall back to when we fetched it. With enrichment this fallback is a
    rare exception, not the rule — so almost everything is filtered on a true date."""
    pub = _parse_dt(article.get("published"))
    if pub is not None:
        return pub >= cutoff
    fetched = _parse_dt(article.get("fetched"))
    return fetched is not None and fetched >= cutoff


def prioritize(con, cfg, client, use_llm: bool) -> tuple[list[dict], list[dict]]:
    settings, weights = cfg["settings"], cfg["weights"]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=settings["briefing"]["lookback_hours"])
    # Pull a generous FETCH window, then keep only items PUBLISHED within the cutoff —
    # so an old article surfaced today (by a scout or feed) doesn't sneak in.
    fetch_floor = (now - timedelta(days=14)).isoformat()
    # A briefed story is eligible only for the REST OF THE SAME CALENDAR DAY (org timezone):
    # re-running the briefing later the same day reproduces it, but it NEVER repeats on a later
    # day. (The old rolling-24h window let a story briefed late one day reappear the next morning
    # when two runs landed <24h apart — which is exactly the day-to-day repeat we don't want.)
    org_tz = ZoneInfo(settings["org"].get("timezone", "America/New_York"))
    briefed_after = datetime.now(org_tz).replace(
        hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    rows = [dict(r) for r in store.candidates_recent(con, fetch_floor, briefed_after)]
    # Recover real publish dates for undated items (RSS + scouts) by reading the
    # article page's metadata, so the 72h window filters on a true publish date
    # instead of fetch time. Persisted, so each page is fetched at most once.
    if settings["briefing"].get("enrich_publish_dates", True):
        _enrich_undated(con, rows, settings["briefing"].get("enrich_timeout_seconds", 10))
    rows = [a for a in rows if _is_recent(a, cutoff)]
    if not rows:
        log.info("No articles published within the last %dh.", settings["briefing"]["lookback_hours"])
        return [], []   # (top, runners) — main() unpacks a pair; [] alone would crash the run

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
            guidance=settings["briefing"].get("relevance_guidance", ""),
        )
    else:
        scores = [(5.0, "llm disabled")] * len(to_score)

    floor_rules = settings["briefing"].get("forced_floor_rules", [])
    kept = []
    for art, (llm_score, why) in zip(to_score, scores):
        # Deterministic config-driven floor (e.g. FIU + Baptist in one sentence -> 9),
        # applied after the LLM. Never lowers a higher LLM score.
        text = f'{art.get("title", "")}. {art.get("summary") or ""} {art.get("content") or ""}'
        floor, reason = scoring.forced_floor(text, floor_rules)
        if floor is not None and floor > llm_score:
            why = f"[Auto-floor {floor:g}] {reason}" + (f" (LLM had: {why})" if why else "")
            llm_score = floor
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
    kept.sort(key=lambda a: a["composite_score"], reverse=True)  # dedup may reorder

    # Selection: include EVERY story at/above select_threshold (composite), but never
    # fewer than min_stories nor more than max_stories. Replaces a fixed top-N — a strong
    # news day surfaces more (up to the cap); a quiet day still shows a baseline. If fewer
    # than min_stories clear the bar, pad with the next-highest still-qualified (>= the
    # basic score_threshold floor) items; if none qualify at all, the quiet-day note fires.
    bcfg = settings["briefing"]
    select_threshold = bcfg.get("select_threshold", 90)
    min_stories = bcfg.get("min_stories", 5)
    max_stories = bcfg.get("max_stories", 12)
    strong = [a for a in kept if a["composite_score"] >= select_threshold]
    final = strong[:max_stories] if len(strong) >= min_stories else kept[:min_stories]
    # The next-closest stories that just missed the cut — surfaced as a title+link+score
    # comparison list at the bottom of the briefing. kept is sorted desc and `final` is its
    # prefix, so the runners-up are simply the next slice.
    runners = kept[len(final):len(final) + 5]
    log.info("Prioritization: %d scored, %d above floor(%s), %d after dedup, %d at/above %s "
             "-> %d selected (min %d / max %d), %d runners-up",
             len(to_score), before, weights["score_threshold"], len(kept),
             len(strong), select_threshold, len(final), min_stories, max_stories, len(runners))
    return final, runners


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


def _send_personalized(settings, briefing, date_h, failing, dry_run, run_date, out_dir):
    """Deliver the exec-summary report to each ACTIVE profile with a personal greeting.

    Returns None if no profiles are configured (caller falls back to the shared digest),
    True if at least one real email was sent, or False if profiles exist but nothing was
    sent (dry run / SMTP not ready) — so dedup is only consumed on a real send.
    """
    from src import profiles as profiles_mod
    profs = profiles_mod.active_profiles()
    if not profs:
        return None
    org_name = settings["org"]["name"]
    subject = f'{settings["briefing"]["subject_prefix"]} — {date_h}'
    smtp_ready = all(config.env(k, required=False) for k in
                     ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "EMAIL_FROM"))
    sent_any = False
    for p in profs:
        greeting = p.get("display_name") or p.get("name", "")
        html = emailer.render_html(briefing, date_h, org_name, failing, greeting=greeting)
        tag = (p.get("name", "profile").split() or ["profile"])[0].lower()
        (out_dir / f"{run_date}_{tag}.html").write_text(html, encoding="utf-8")
        if dry_run or not smtp_ready or not p.get("email"):
            log.info("Personalized briefing for %s saved%s.", p.get("name"),
                     " (dry run)" if dry_run else " but NOT sent (SMTP not ready / no email)")
            continue
        emailer.send(html, subject, {
            "host": config.env("SMTP_HOST"), "port": config.env("SMTP_PORT"),
            "user": config.env("SMTP_USER"), "password": config.env("SMTP_PASS"),
            "from": config.env("EMAIL_FROM"), "to": [p["email"]]}, subtype="html")
        log.info("Personalized briefing emailed to %s <%s>", p.get("name"), p["email"])
        sent_any = True
    return sent_any


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
    try:
        top, runners = prioritize(con, cfg, client, use_llm)
    except llm_relevance.ScoringUnavailable as exc:
        # LLM down during scoring — fail (don't send a false quiet-day note) so the watchdog
        # alerts and a re-trigger retries until the LLM is back.
        log.error("Scoring failed (%s) — NOT sending; failing so the run retries.", exc)
        raise SystemExit(1)

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

    def _basic_briefing(items):
        # No-LLM digest (used ONLY with the --no-llm dev flag, never the scheduled path).
        return {"takeaways": [a["title"] for a in items[:5]], "key_question_answers": {},
                "stories": [{"title": a["title"], "area": a["area"], "source": a["source"],
                             "url": a["url"],
                             "what_happened": (a.get("summary") or a.get("content") or "")[:300],
                             "why_it_matters": "", "exposure": "", "watch_next": "",
                             "coverage_label": ""} for a in items],
                "watch": [], "actions": []}

    # Optional per-story deep-dive enrichment via the Yutori Research API. Off unless
    # yutori.deep_dive.enabled is true in settings.yaml. Bounded + fail-safe: it only
    # adds context to the top N stories and can never delay or break the send. Skipped
    # under --no-yutori (and only useful with LLM synthesis).
    if use_llm and not args.no_yutori:
        try:
            from src.ingest import deep_dive
            deep_dive.enrich_stories(top, cfg)
        except Exception as exc:
            log.warning("deep-dive enrichment skipped (%s)", exc)

    if use_llm:
        models = cfg["settings"]["llm"]["models"][cfg["settings"]["llm"]["provider"]]
        try:
            briefing = synthesize.build_briefing(
                client, models["synthesis"],
                cfg["settings"]["llm"]["max_tokens_synthesis"],
                cfg["settings"]["org"], cfg["settings"]["key_questions"], top,
            )
        except Exception as exc:
            # No synthesized/prioritized result -> DO NOT send a degraded digest to leadership.
            # Fail the run (non-zero exit): nothing is emailed, no story is marked briefed (so the
            # next run retries the same stories), and the watchdog flags the missed briefing.
            # (The longer Gemini retry/backoff above already tries hard before we get here.)
            log.error("Synthesis failed (%s) — NOT sending. A briefing without the synthesized "
                      "narrative isn't worth sending; failing so the watchdog alerts and the next "
                      "run retries.", exc)
            raise SystemExit(1)
    else:
        briefing = _basic_briefing(top)

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

    # Attach each story's LLM relevance score (0-10) from the ranked DB rows so the
    # briefing renderer can show a score next to every article. Matched by URL, then title.
    _score_by_url = {emailer._norm_url(a["url"]): a.get("llm_score") for a in top if a.get("url")}
    _score_by_title = {str(a["title"]).strip().lower(): a.get("llm_score") for a in top if a.get("title")}
    for s in briefing["stories"]:
        if s.get("llm_score") is None:
            s["llm_score"] = (_score_by_url.get(emailer._norm_url(s.get("url", "")))
                              or _score_by_title.get(str(s.get("title", "")).strip().lower()))

    # Historical awareness: add an "Additional context" note to any story that has
    # related prior coverage in our database (Gemini-judged). Off unless
    # additional_context.enabled is true in settings.yaml; fail-safe, populates nothing
    # when there's no prior coverage, so it never alters a normal story's layout.
    if use_llm:
        try:
            from src.prioritize import related_context
            related_context.add_context(con, client, cfg, top, briefing)
        except Exception as exc:
            log.warning("additional-context step skipped (%s)", exc)

    failing = store.failing_sources(con)
    html = emailer.render_html(briefing, date_h, settings["org"]["name"], failing)

    # Email digest for the top N stories (HTML, with larger titles). Captured/Published
    # dates are matched from the DB rows (by url, then title). Plain text saved too.
    org_short = settings["org"].get("short_name", settings["org"]["name"])
    top_n = settings["briefing"].get("digest_top_n", 5)
    digest = emailer.render_digest(briefing["stories"], date_h, org_short, articles=top, top_n=top_n,
                                   runners=runners)
    digest_html = emailer.render_digest_html(briefing["stories"], date_h, org_short, articles=top,
                                             top_n=top_n, runners=runners)

    (out_dir / f"{run_date}.html").write_text(html, encoding="utf-8")
    (out_dir / f"{run_date}.json").write_text(json.dumps(briefing, indent=2), encoding="utf-8")
    (out_dir / f"{run_date}_digest.txt").write_text(digest, encoding="utf-8")
    (out_dir / f"{run_date}_digest.html").write_text(digest_html, encoding="utf-8")

    # Delivery: if executive profiles are configured, send each person their own copy
    # (exec-summary format + personal greeting). Otherwise fall back to the single shared
    # digest to digest_recipients (legacy behavior).
    sent = _send_personalized(settings, briefing, date_h, failing, args.dry_run, run_date, out_dir)
    if sent is None:
        sent = _send_html(settings, f'{settings["briefing"]["subject_prefix"]} — {date_h}',
                          digest_html, args.dry_run, run_date, label="Digest")

    # Only consume dedup state when the briefing actually went out — a dry run or a
    # failed/skipped send must not mark stories as already-briefed.
    if sent:
        # Full ISO timestamp (not just the date) so the rebrief window is measured
        # precisely from the first time each story was briefed.
        store.mark_briefed(con, [a["id"] for a in top], datetime.now(timezone.utc).isoformat())
        con.commit()


if __name__ == "__main__":
    main()
