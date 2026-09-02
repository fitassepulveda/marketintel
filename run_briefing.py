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
from src import profiles as profiles_mod
from src.ingest import rss, yutori, enrich
from src.llm_client import LLMClient
from src.output import emailer, synthesize
from src.prioritize import llm_relevance, scoring
from src.prioritize import profile_relevance as PR

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


def _force_competitor_area(con, rows: list, bcfg: dict) -> int:
    """Re-tag stories that name a South Florida competitor into the competitive-intel area,
    whatever feed carried them.

    The strategy team flagged this in review: "HCA cuts 2026 earnings forecast" landed on a
    national feed and was filed under national_policy — but HCA is a competitor, so it is
    competitive intelligence. Ambiguous names (Mount Sinai, Jackson, Baptist, Cleveland
    Clinic, Memorial) only match when a South Florida geography term appears alongside them,
    so the New York / Alabama / Ohio / Houston namesakes are not swept in. Runs BEFORE
    scoring, so the area's key question is the competitive one too. Config-driven; a missing
    term list simply disables it. Never raises into the run.
    """
    area = bcfg.get("competitor_area") or ""
    strong_terms = [t.lower() for t in (bcfg.get("competitor_terms") or [])]
    weak_terms = [t.lower() for t in (bcfg.get("competitor_terms_regional") or [])]
    region_terms = [t.lower() for t in (bcfg.get("competitor_region_terms") or [])]
    if not area or not (strong_terms or weak_terms):
        return 0
    moved = 0
    for a in rows:
        if a.get("area") == area:
            continue
        hay = f'{a.get("title") or ""} {a.get("summary") or ""}'.lower()
        hit = next((t for t in strong_terms if t in hay), None)
        if not hit:
            weak = next((t for t in weak_terms if t in hay), None)
            if weak and any(r in hay for r in region_terms):
                hit = weak
        if not hit:
            continue
        log.info("Area re-tag: %r %s -> %s (matched %r)",
                 str(a.get("title", ""))[:60], a.get("area"), area, hit)
        a["area"] = area
        try:
            store.set_area(con, a["id"], area)
        except Exception as exc:          # never let a re-tag break the run
            log.warning("Could not persist area re-tag for %s (%s)", a.get("id"), exc)
        moved += 1
    if moved:
        con.commit()
    return moved


def prioritize(con, cfg, client, use_llm: bool) -> tuple[list[dict], list[dict], dict, list[dict]]:
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
    # A named South Florida competitor makes a story competitive intel regardless of the feed
    # it arrived on. Done before scoring so the area's key question is the competitive one.
    _force_competitor_area(con, rows, settings["briefing"])
    if not rows:
        log.info("No articles published within the last %dh.", settings["briefing"]["lookback_hours"])
        return [], [], {}, []   # (top, runners, dup_map, scored_pool) — main() unpacks a quadruple

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

        # REUSE scores saved by an earlier run today (temperature-0 scoring is
        # deterministic, so a saved score equals what a re-score would produce).
        # A retry after a partial failure then only pays the LLM for what is
        # still missing, instead of re-scoring the whole pool on every attempt
        # (which is what turned the 2026-07-15 quota outage into a $7+ morning).
        # llm_score 0 with rationale "not scored" is a failed batch, not a score.
        # A score is reusable only if it came from the CURRENT scoring version — a score
        # saved under the old whole-number scale is not comparable with a one-decimal one,
        # and reusing it would freeze that article on the coarse scale forever.
        def _has_saved_score(a) -> bool:
            return (a.get("llm_score") is not None
                    and (a.get("llm_rationale") or "") not in ("", "not scored")
                    and a.get("score_version") == store.SCORE_VERSION)

        unscored = [a for a in to_score if not _has_saved_score(a)]
        if len(unscored) < len(to_score):
            log.info("Reusing %d saved scores; %d items still need the LLM.",
                     len(to_score) - len(unscored), len(unscored))
        fresh: dict[int, tuple[float, str]] = {}
        if unscored:
            try:
                got = llm_relevance.score_batch(
                    client, models["scoring"], settings["org"],
                    settings["key_questions"], unscored,
                    guidance=settings["briefing"].get("relevance_guidance", ""),
                )
            except llm_relevance.ScoringUnavailable as exc:
                # Persist the batches that DID succeed before failing the run, so
                # the retry reuses them instead of paying for them again.
                saved = 0
                for art, (s, why) in zip(unscored, getattr(exc, "partial", None) or []):
                    if why != "not scored":
                        store.save_scores(con, art["id"], s, why,
                                          scoring.composite(weights, art, s))
                        saved += 1
                con.commit()
                if saved:
                    log.warning("Persisted %d partial scores for the retry to reuse.", saved)
                raise
            fresh = {id(a): sc for a, sc in zip(unscored, got)}
        scores = [fresh[id(a)] if id(a) in fresh
                  else (float(a["llm_score"]), a["llm_rationale"])
                  for a in to_score]
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
    # Collapse same-event duplicates — against BOTH today's pool and recently-BRIEFED history.
    # Primary: semantic (embedding) similarity; fallback: keyword rules if embeddings are
    # unavailable (no key / API error). Two sent-briefing repeats drove the design (2026-07-24):
    #  * HISTORY SEED — Baptist's $100M gift (briefed 6/15) was re-reported by another outlet
    #    5 weeks later under a fresh URL + publish date and re-briefed. URL-hash dedup can't
    #    see that, so candidates are also matched against everything briefed in the last
    #    dedup_history_days; a match means "already shown" and the candidate is dropped.
    #  * ABSORPTION TRACKING — Cleveland Clinic's $25M gift arrived twice on 7/23 from two
    #    sources; dedup dropped one copy but only the SURVIVOR was marked briefed, so the
    #    dropped copy resurfaced solo the next morning. The absorbed map lets main() stamp
    #    dropped duplicates as briefed together with their surviving copy.
    hist_days = settings["briefing"].get("dedup_history_days", 60)
    pool_ids = {a["id"] for a in kept}
    history = []
    if hist_days:
        hist_floor = (now - timedelta(days=hist_days)).isoformat()
        # Exclude the current pool itself: on a same-day re-run, today's already-briefed
        # stories are BOTH candidates and history — they must not suppress themselves.
        history = [dict(r) for r in store.briefed_recent(con, hist_floor)
                   if r["id"] not in pool_ids]
    before = len(kept)
    deduped = absorbed = None
    if use_llm and client and kept and (len(kept) > 1 or history):
        try:
            texts = [f'{a["title"]} {(a.get("summary") or "")[:400]}' for a in kept + history]
            vecs = client.embed(texts)
            deduped, absorbed = scoring.semantic_dedupe_track(
                kept, vecs[:len(kept)], weights.get("dedup_cosine_similarity", 0.85),
                seed=history, seed_vectors=vecs[len(kept):])
            log.info("Dedup: semantic (embeddings), vs %d briefed history stories", len(history))
        except Exception as exc:
            log.warning("Embedding dedup failed (%s); falling back to keyword dedup", exc)
    if deduped is None:
        deduped, absorbed = scoring.dedupe_by_title_track(
            kept, weights.get("dedup_title_similarity", 0.90),
            weights.get("dedup_token_overlap", 0.6), seed=history)
        log.info("Dedup: keyword fallback, vs %d briefed history stories", len(history))
    else:
        # UNION, NOT FALLBACK (2026-09-01). The keyword rules were only ever reached when
        # embeddings failed — so a pair the cosine threshold missed shipped twice even
        # though the cheap local test would have caught it. Two outlets covering one policy
        # story share few words but the same named measure, and the two Amendment 3 stories
        # of 2026-08-28 rode that gap into both tiers of the same briefing.
        # Running the keyword pass over the semantic SURVIVORS costs nothing (local, no API)
        # and can only remove duplicates the primary pass already kept.
        kw_kept, kw_absorbed = scoring.dedupe_by_title_track(
            deduped, weights.get("dedup_title_similarity", 0.90),
            weights.get("dedup_token_overlap", 0.6), seed=history)
        if kw_absorbed:
            for dropped, absorber in kw_absorbed:
                log.info("Dedup: keyword pass caught %r as a duplicate of %r",
                         str(dropped.get("title", ""))[:70], str(absorber.get("title", ""))[:70])
        deduped, absorbed = kw_kept, list(absorbed) + kw_absorbed
    kept = deduped
    kept.sort(key=lambda a: a["composite_score"], reverse=True)  # dedup may reorder

    # Absorption map for mark_briefed: survivor article id -> ids of duplicates it absorbed.
    # Duplicates absorbed by a HISTORY story go under key None — the event was already
    # briefed, so they're stamped on any successful send regardless of today's selection.
    dup_map: dict = {}
    for dropped, absorber in absorbed:
        key = absorber["id"] if absorber["id"] in pool_ids else None
        dup_map.setdefault(key, []).append(dropped["id"])
        if key is None:
            log.info("Dedup: %r duplicates already-briefed story %s (%r)",
                     str(dropped.get("title", ""))[:70], absorber["id"],
                     str(absorber.get("title", ""))[:70])

    # TWO-TIER SELECTION (2026-08-25).
    #   Tier 1 (full story cards): EVERY story at/above select_threshold, capped at
    #     max_stories. NO PADDING — with min_stories 0 a quiet day simply yields a shorter
    #     briefing. The old behaviour (pad to min_stories from the score_threshold floor)
    #     discarded the bar entirely on quiet days and shipped 5.5/10 filler that rendered
    #     identically to a real top story. Padding is still available by raising
    #     min_stories, but it is off by default.
    #   Tier 2 ("Also worth noting", compact headline+link+score): the band between
    #     secondary_floor and select_threshold — the "fine on a slow news day" class.
    #     secondary_floor is a HARD floor: below it nothing ships in any section.
    # kept is sorted desc and `final` is its prefix, so tier 2 is simply the next slice.
    bcfg = settings["briefing"]
    select_threshold = bcfg.get("select_threshold", 90)
    min_stories = bcfg.get("min_stories", 0)
    max_stories = bcfg.get("max_stories", 12)
    secondary_floor = bcfg.get("secondary_floor", 60)
    floor_by_area = bcfg.get("secondary_floor_by_area") or {}
    secondary_max = bcfg.get("secondary_max", 6)
    strong = [a for a in kept if a["composite_score"] >= select_threshold]
    final = strong[:max_stories]
    if len(final) < min_stories:          # opt-in padding; min_stories 0 == never pads
        final = kept[:min_stories]

    # Tier 2 admission is PER AREA. A competitor hire or promotion earns a glance at 6.0;
    # a national/payer/AI/reputation item has to beat 7.0 for the same space. One flat floor
    # filled the section with the general trend pieces the strategy team consistently
    # rejects, while the local competitor items they explicitly wanted sat at the same score.
    def _sec_floor(a) -> float:
        return float(floor_by_area.get(a.get("area"), secondary_floor))
    runners = [a for a in kept[len(final):]
               if a["composite_score"] >= _sec_floor(a)][:secondary_max]
    log.info("Prioritization: %d scored, %d above floor(%s), %d after dedup, %d at/above %s "
             "-> %d selected (min %d / max %d), %d also-worth-noting (floor %s)",
             len(to_score), before, weights["score_threshold"], len(kept),
             len(strong), select_threshold, len(final), min_stories, max_stories,
             len(runners), floor_by_area or secondary_floor)
    return final, runners, dup_map, to_score


def _flag_broken_links(stories, timeout: int = 5, max_workers: int = 8) -> None:
    """Best-effort link health check: set story['link_ok']=False for URLs that don't resolve
    (so the renderer can leave a note). Fail-safe — transient/unknown errors leave it unset
    (treated as OK, no false 'broken' notes), and this never raises into the send path."""
    import concurrent.futures
    import requests

    def check(u):
        if not u or u == "#":
            return False
        headers = {"User-Agent": "Mozilla/5.0 (MarketIntel link check)"}
        try:
            r = requests.head(u, timeout=timeout, allow_redirects=True, headers=headers)
            if r.status_code in (403, 405, 429) or r.status_code >= 500:
                # Bot-block / HEAD-not-allowed: confirm with a light GET before judging.
                r = requests.get(u, timeout=timeout, allow_redirects=True, stream=True,
                                 headers=headers)
            # ONLY flag links that are DEFINITIVELY gone. 403/429/5xx are usually bot
            # protection on a perfectly good page (false positives erode trust), and
            # timeouts are transient — never flag those.
            if r.status_code in (404, 410):
                return False
            return True
        except Exception:
            return None  # unknown (timeout, transient, bot-block) — do NOT flag as broken

    urls = list({s.get("url", "") for s in stories if s.get("url")})
    results = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(check, u): u for u in urls}
            for f in concurrent.futures.as_completed(futs, timeout=timeout * 2 + 5):
                results[futs[f]] = f.result()
    except Exception:
        pass
    for s in stories:
        ok = results.get(s.get("url", ""))
        if ok is False:
            s["link_ok"] = False
        elif ok is True:
            s["link_ok"] = True


def _valid_story(s: dict) -> bool:
    """A synthesized story is usable only if every exec-facing section is present.
    A story missing 'why it matters' / 'what to consider' must NEVER reach the email.

    what_happened is a LIST of bullets since 2026-08-25 (a string in older briefings), so it
    is judged on its content: a list of blank strings is empty, however truthy str() makes it.
    """
    from src.output.emailer import _bullets
    if not _bullets(s.get("what_happened")):
        return False
    return all(str(s.get(k) or "").strip()
               for k in ("why_it_matters", "watch_next"))


def _reconcile_stories(briefing: dict, top: list[dict]) -> list[dict]:
    """Deterministically match every synthesized story back to its source article,
    heal it, and drop anything unmatchable — IN PLACE on briefing["stories"].

    Why: the model must echo url/title, but it mangles them (Google-News URLs are
    ~500-char base64 blobs; titles get rewritten). Matching on url/title alone once
    produced a duplicate, half-finished story card in a sent briefing (2026-07-09):
    the real synthesized story failed the match (so it lost its score badge and sank
    to the bottom), while a bare fallback stub for the "missing" article was appended
    (and, carrying the article's 9/10 score, sorted to the TOP of the email).

    Match order: the [n] id the model now echoes back -> normalized URL -> exact title.
    For each match: restore the canonical DB url (never trust a model-echoed link),
    fill area/source, and attach llm_score/published/fetched so the badge, date, and
    score-ordering always work. A story that matches nothing, duplicates an
    already-matched article, or has blank required sections is removed.

    Returns the articles from `top` left with NO valid story (caller re-synthesizes
    those, and drops them if that fails — a missing story may cost us one item, but a
    half-baked or duplicated card cost trust, which is worse).
    """
    by_url = {emailer._norm_url(a["url"]): i for i, a in enumerate(top) if a.get("url")}
    by_title = {str(a["title"]).strip().lower(): i for i, a in enumerate(top) if a.get("title")}
    matched: set[int] = set()
    healed: list[dict] = []
    for s in briefing.get("stories", []):
        idx = None
        try:
            i = int(s.get("id"))
            if 0 <= i < len(top):
                idx = i
        except (TypeError, ValueError):
            pass
        if idx is None:
            idx = by_url.get(emailer._norm_url(s.get("url", "")))
        if idx is None:
            idx = by_title.get(str(s.get("title", "")).strip().lower())
        if idx is None:
            log.warning("Reconcile: dropping unmatchable story %r", str(s.get("title", ""))[:90])
            continue
        if idx in matched:
            log.warning("Reconcile: dropping duplicate story for article %s", top[idx]["id"])
            continue
        if not _valid_story(s):
            log.warning("Reconcile: story for article %s has blank required sections",
                        top[idx]["id"])
            continue
        a = top[idx]
        s["id"] = idx
        s["url"] = a["url"]                      # canonical link, never the model's echo
        s["area"] = a.get("area") or s.get("area", "")
        s["source"] = a.get("source") or s.get("source", "")
        s["llm_score"] = a.get("llm_score")
        s["published"] = a.get("published")
        s["fetched"] = a.get("fetched")
        matched.add(idx)
        healed.append(s)
    briefing["stories"] = healed
    return [a for i, a in enumerate(top) if i not in matched]


def _resolve_recipients(settings, spec: str) -> list[str]:
    """Resolve --recipients: a named list from briefing.recipient_lists (e.g. 'test'),
    or a comma-separated list of email addresses."""
    lists = settings["briefing"].get("recipient_lists", {})
    if spec in lists:
        return list(lists[spec])
    return [e.strip() for e in spec.split(",") if e.strip()]


def _send_html(settings, subject: str, body_html: str, dry_run: bool,
               run_date: str, label: str = "Digest",
               recipients_override: list[str] | None = None) -> bool:
    """Send an HTML email, respecting --dry-run and SMTP readiness. Returns True if sent."""
    recipients = recipients_override or settings["briefing"].get("digest_recipients", [])
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


def _send_personalized(settings, briefing, date_h, failing, dry_run, run_date, out_dir,
                       runners=None):
    """Deliver the exec-summary report to each ACTIVE profile with a personal greeting.

    Returns None if no profiles are configured (caller falls back to the shared digest),
    True if at least one real email was sent, or False if profiles exist but nothing was
    sent (dry run / SMTP not ready) — so dedup is only consumed on a real send.
    """
    # Semantic (role_description) profiles are scored + sent separately by
    # _send_semantic_profiles -- exclude them here so they don't ALSO get this
    # shared-content, name-only copy.
    profs = [p for p in profiles_mod.active_profiles() if not profiles_mod.uses_semantic_scoring(p)]
    if not profs:
        return None
    org_name = settings["org"]["name"]
    subject = f'{settings["briefing"]["subject_prefix"]} — {date_h}'
    smtp_ready = all(config.env(k, required=False) for k in
                     ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "EMAIL_FROM"))
    sent_any = False
    for p in profs:
        greeting = p.get("display_name") or p.get("name", "")
        html = emailer.render_html(briefing, date_h, org_name, failing, greeting=greeting,
                                   runners=runners,
                                   show_consider=settings["briefing"].get("show_consider_section", True))
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


def prioritize_for_profile(con, cfg, client, profile: dict, scored_pool: list[dict]
                           ) -> tuple[list[dict], dict]:
    """A SECOND, independent prioritization over the SAME already-ingested,
    already-house-scored candidate pool (`scored_pool`, straight from `prioritize()`,
    so ingestion and house scoring never run twice) — re-scored against `profile`'s
    role_description instead of the house composite.

    Mirrors `prioritize()`'s ordering (score -> threshold -> dedup -> cap, so a
    duplicate dropped by dedup can be backfilled from the rest of the pool) but is
    otherwise independent: a story can land in one list, both, or neither.

    Returns (final, dup_map): `final` ranked desc by personal_composite and capped to
    the profile's max_stories; `dup_map` is prioritize()'s absorbed-duplicate map,
    shaped for mark_briefed.
    """
    settings, weights = cfg["settings"], cfg["weights"]
    models = settings["llm"]["models"][settings["llm"]["provider"]]
    # active_profiles() normally sets this; these dicts come from load_profiles() so that
    # identically-configured profiles can be grouped BEFORE per-profile setup. Only used
    # by personal_composite()'s fallback path, but cheap insurance against a KeyError.
    profile.setdefault("_weights", profiles_mod.dimension_weights(profile))

    # 1. Relevance judged against THIS role. Cached in profile_scores by
    #    (article, profile, version), so a same-day re-run only pays for new articles.
    PR.ensure_table(con)
    cached = PR.load_all(con, profile["name"])
    todo = [a for a in scored_pool if a["id"] not in cached]
    if todo:
        results = PR.score_batch(client, models["scoring"], settings["org"], profile, todo)
        for art, (score, why) in zip(todo, results):
            PR.save(con, art["id"], profile["name"], score, why)
            cached[art["id"]] = (score, why)
        con.commit()

    # Work on COPIES: these dicts are the same objects the house pipeline holds in `top`,
    # and this pass's scores must never leak into the shared briefing.
    pool = []
    for a in scored_pool:
        hit = cached.get(a["id"])
        if not hit:
            continue
        b = dict(a)
        b["profile_score"], b["profile_why"] = hit
        b["personal_composite"] = profiles_mod.personal_composite(profile, b)
        pool.append(b)

    # 2. Threshold on this profile's own scale — semantic relevance x10, the same scale as
    #    the house composite, so a shared threshold keeps its meaning (docs/PROFILES.md).
    threshold = float(profile.get("threshold", weights.get("score_threshold", 55)))
    above = sorted((a for a in pool if a["personal_composite"] >= threshold),
                   key=lambda a: a["personal_composite"], reverse=True)
    if not above:
        log.info("%s: %d candidates scored, none at/above %s.",
                 profile["name"], len(pool), threshold)
        return [], {}

    # 3. Dedup against today's own duplicates AND recently-briefed history — same
    #    embeddings-primary / keyword-union approach as prioritize().
    now = datetime.now(timezone.utc)
    hist_days = settings["briefing"].get("dedup_history_days", 60)
    pool_ids = {a["id"] for a in above}
    history = []
    if hist_days:
        hist_floor = (now - timedelta(days=hist_days)).isoformat()
        history = [dict(r) for r in store.briefed_recent(con, hist_floor)
                   if r["id"] not in pool_ids]
    deduped, absorbed = None, []
    if client and (len(above) > 1 or history):
        try:
            texts = [f'{a["title"]} {(a.get("summary") or "")[:400]}' for a in above + history]
            vecs = client.embed(texts)
            deduped, absorbed = scoring.semantic_dedupe_track(
                above, vecs[:len(above)], weights.get("dedup_cosine_similarity", 0.85),
                seed=history, seed_vectors=vecs[len(above):])
        except Exception as exc:
            log.warning("%s: embedding dedup failed (%s); falling back to keyword dedup",
                        profile["name"], exc)
    if deduped is None:
        deduped, absorbed = scoring.dedupe_by_title_track(
            above, weights.get("dedup_title_similarity", 0.90),
            weights.get("dedup_token_overlap", 0.6), seed=history)
    else:
        kw_kept, kw_absorbed = scoring.dedupe_by_title_track(
            deduped, weights.get("dedup_title_similarity", 0.90),
            weights.get("dedup_token_overlap", 0.6), seed=history)
        deduped, absorbed = kw_kept, list(absorbed) + kw_absorbed
    deduped.sort(key=lambda a: a["personal_composite"], reverse=True)

    # 4. Cap AFTER dedup, so a dropped duplicate is backfilled rather than leaving a hole.
    max_stories = int(profile.get("max_stories", 12))
    final = deduped[:max_stories]
    log.info("%s: %d candidates scored, %d at/above %s, %d after dedup -> %d selected "
             "(max %d).", profile["name"], len(pool), len(above), threshold,
             len(deduped), len(final), max_stories)

    final_ids = {a["id"] for a in final}
    dup_map: dict = {}
    for dropped, absorber in absorbed:
        key = absorber["id"] if absorber["id"] in final_ids else None
        dup_map.setdefault(key, []).append(dropped["id"])
    return final, dup_map


def _synthesize_for_profile(client, cfg, models, label: str, final: list[dict]):
    """Synthesize + heal a briefing for one profile group, mirroring main()'s chain:
    enrich -> synthesize -> reconcile (-> one retry) -> link check -> prior-coverage note.

    `_reconcile_stories` is the important one: it restores canonical URLs and score/date
    meta and drops unmatchable, duplicate or blank cards. Skipping it is what once put a
    half-finished stub card at the top of a sent briefing (2026-07-09).

    Returns (briefing, final) with `final` trimmed to the articles that actually have a
    publishable story, or (None, []) if nothing survived.
    """
    settings = cfg["settings"]
    # Deep-dive enrichment for stories THIS pass selected that the shared pass did not
    # already enrich. Stories shared with the house briefing are the same DB rows, so they
    # arrive enriched for free; only profile-ONLY stories would cost anything, and at
    # ~$0.05-0.18 browsing (plus ~$0.35 research) per article that is real money against
    # the Yutori budget — so it is OPT-IN (yutori.deep_dive.profiles), default off.
    # With it off, profile-only stories are written from title + summary.
    if (cfg["settings"].get("yutori", {}).get("deep_dive", {}) or {}).get("profiles", False):
        unenriched = [a for a in final if not a.get("full_text")]
        if unenriched:
            try:
                from src.ingest import deep_dive
                deep_dive.enrich_stories(unenriched, cfg)
            except Exception as exc:
                log.warning("%s: deep-dive enrichment skipped (%s)", label, exc)

    def _build(items):
        return synthesize.build_briefing(
            client, models["synthesis"], settings["llm"]["max_tokens_synthesis"],
            settings["org"], settings["key_questions"], items,
            style=settings["briefing"].get("synthesis_style", ""))

    try:
        briefing = _build(final)
    except Exception as exc:
        log.error("%s: synthesis failed (%s) — skipping this group.", label, exc)
        return None, []

    missing = _reconcile_stories(briefing, final)
    if missing:
        log.warning("%s: %d item(s) came back without a valid story — retrying those.",
                    label, len(missing))
        try:
            extra = _build(missing)
            still_missing = _reconcile_stories(extra, missing)
            briefing["stories"].extend(extra["stories"])
        except Exception as exc:
            log.warning("%s: retry synthesis failed (%s)", label, exc)
            still_missing = missing
        if still_missing:
            gone = {a["id"] for a in still_missing}
            log.error("%s: dropping %d story(ies) that could not be synthesized.",
                      label, len(still_missing))
            final = [a for a in final if a["id"] not in gone]
    if not final or not briefing.get("stories"):
        log.error("%s: nothing publishable after reconciliation — not sending.", label)
        return None, []

    try:
        _flag_broken_links(briefing["stories"])
    except Exception as exc:
        log.warning("%s: link-health check skipped (%s)", label, exc)
    return briefing, final


def _send_semantic_profiles(con, cfg, client, use_llm, scored_pool, date_h, dry_run,
                            run_date, out_dir) -> bool:
    """Ambulatory Leadership Prioritization (2026-09-02): the delivery half of the second
    prioritization pass, for every ACTIVE profile that defines a role_description.

    Profiles whose scoring config is IDENTICAL (role_description, relevance_guidance,
    keyword lists, threshold, max_stories) are grouped, so the LLM scoring and synthesis
    passes run ONCE per distinct configuration rather than once per person — the three
    "Ambulatory-*" profiles share one pass and differ only in the greeting. Note the
    profile_scores cache is keyed by the GROUP REPRESENTATIVE's name, so removing that
    one profile makes the next run re-score the pool once under the new representative.

    Returns True if at least one real email was sent. mark_briefed for this pass's own
    stories is handled here, independently of the shared pipeline's own stamping.
    """
    if not use_llm or client is None:
        return False
    settings = cfg["settings"]
    all_profiles, defaults = profiles_mod.load_profiles()
    semantic = []
    for raw in all_profiles:
        p = {**defaults, **raw}
        if p.get("active") and profiles_mod.uses_semantic_scoring(p):
            semantic.append(p)
    if not semantic:
        return False

    def _sig(p):
        return (p.get("role_description", ""), p.get("relevance_guidance", ""),
                tuple(p.get("keyword_interests") or []), tuple(p.get("keyword_avoid") or []),
                p.get("threshold"), p.get("max_stories"))

    groups: dict = {}
    for p in semantic:
        groups.setdefault(_sig(p), []).append(p)

    models = settings["llm"]["models"][settings["llm"]["provider"]]
    org_name = settings["org"]["name"]
    subject = f'{settings["briefing"]["subject_prefix"]} — {date_h}'
    failing = store.failing_sources(con)
    show_consider = settings["briefing"].get("show_consider_section", True)
    smtp_ready = all(config.env(k, required=False) for k in
                     ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "EMAIL_FROM"))
    sent_any = False

    for members in groups.values():
        rep = members[0]     # scoring config is identical across the group
        label = "/".join(p["name"] for p in members)
        try:
            final, dup_map = prioritize_for_profile(con, cfg, client, rep, scored_pool)
        except Exception as exc:
            log.error("%s: profile scoring failed (%s) — skipping this group.", label, exc)
            continue
        if not final:
            continue        # prioritize_for_profile already logged why

        briefing, final = _synthesize_for_profile(client, cfg, models, label, final)
        if briefing is None:
            continue
        try:
            from src.prioritize import related_context
            related_context.add_context(con, client, cfg, final, briefing)
        except Exception as exc:
            log.warning("%s: additional-context step skipped (%s)", label, exc)

        group_sent = False
        for p in members:
            greeting = p.get("display_name") or p.get("name", "")
            html = emailer.render_html(briefing, date_h, org_name, failing,
                                       greeting=greeting, show_consider=show_consider)
            tag = (p.get("name", "profile").split() or ["profile"])[0].lower()
            (out_dir / f"{run_date}_{tag}.html").write_text(html, encoding="utf-8")
            if dry_run or not smtp_ready or not p.get("email"):
                log.info("Profile briefing for %s saved (%d stories)%s.", p.get("name"),
                         len(briefing.get("stories", [])),
                         " (dry run)" if dry_run else " but NOT sent (SMTP not ready / no email)")
                continue
            emailer.send(html, subject, {
                "host": config.env("SMTP_HOST"), "port": config.env("SMTP_PORT"),
                "user": config.env("SMTP_USER"), "password": config.env("SMTP_PASS"),
                "from": config.env("EMAIL_FROM"), "to": [p["email"]]}, subtype="html")
            log.info("Profile briefing (%d stories) emailed to %s <%s>",
                     len(briefing.get("stories", [])), p.get("name"), p["email"])
            group_sent = sent_any = True

        # Consume dedup state for THIS group's stories only once it really went out —
        # same principle as the shared pipeline.
        if group_sent:
            ids = [a["id"] for a in final]
            extra = list(dup_map.get(None, []))
            for i in ids:
                extra.extend(dup_map.get(i, []))
            store.mark_briefed(con, ids + extra, datetime.now(timezone.utc).isoformat())
            con.commit()

    return sent_any


def _run_semantic_pass(con, cfg, client, use_llm, scored_pool, date_h, dry_run,
                       run_date, out_dir, recipients_override) -> None:
    """Fail-safe entry point for the semantic-profile pass.

    Called from BOTH delivery paths — the normal one and the quiet-day one — so these
    profiles are still scored on their own merits on a day when nothing clears the house
    bar (select_threshold is 80, so that is a routine occurrence, not an edge case).
    Any failure is logged and swallowed: this pass must never take the shared briefing
    down with it.
    """
    if recipients_override:      # --recipients is a limited TEST send; skip real profiles
        return
    try:
        _send_semantic_profiles(con, cfg, client, use_llm, scored_pool, date_h,
                                dry_run, run_date, out_dir)
    except Exception as exc:
        log.error("Semantic-profile pass failed (%s) — the shared briefing is unaffected.",
                  exc)


def main():

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't send email; save HTML locally")
    ap.add_argument("--no-yutori", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--recipients", default=None,
                    help="Override delivery for this run: a named list from "
                         "briefing.recipient_lists (e.g. 'test') OR comma-separated emails. "
                         "Sends ONE shared briefing to exactly these addresses and SKIPS the "
                         "per-profile send — use for test sends to a limited group.")
    args = ap.parse_args()

    cfg = config.load_all()
    con = store.connect()
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    use_llm = not args.no_llm

    ingest(con, cfg, run_date, use_yutori=not args.no_yutori)

    client = LLMClient(cfg["settings"]["llm"]["provider"]) if use_llm else None
    try:
        top, runners, dup_map, scored_pool = prioritize(con, cfg, client, use_llm)
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
    # news day is never indistinguishable from a broken pipeline. Since 2026-08-25 the
    # note also carries the "Also worth noting" tier, so a day with no headline story
    # still shows the team what moved — without dressing any of it up as a top story.
    if not top:
        log.info("No stories cleared the threshold — sending a quiet-day note (%d lighter "
                 "item(s) attached).", len(runners or []))
        quiet_html = emailer.render_quiet_html(
            date_h, settings["org"]["name"], settings["briefing"]["lookback_hours"],
            store.failing_sources(con), runners=runners,
        )
        (out_dir / f"{run_date}_digest.html").write_text(quiet_html, encoding="utf-8")
        _send_html(settings, f'{settings["briefing"]["subject_prefix"]} — {date_h} (quiet day)',
                   quiet_html, args.dry_run, run_date, label="Quiet-day note")
        # Quiet for the house bar is not necessarily quiet for a specific role, so the
        # semantic profiles still get their own prioritization before we bail out.
        _run_semantic_pass(con, cfg, client, use_llm, scored_pool, date_h, args.dry_run,
                           run_date, out_dir, args.recipients)
        return

    def _basic_briefing(items):
        # No-LLM digest (used ONLY with the --no-llm dev flag, never the scheduled path).
        return {"takeaways": [a["title"] for a in items[:5]], "key_question_answers": {},
                "stories": [{"title": a["title"], "area": a["area"], "source": a["source"],
                             "url": a["url"], "llm_score": a.get("llm_score"),
                             "published": a.get("published"), "fetched": a.get("fetched"),
                             # list form, matching synthesis (see emailer._bullets)
                             "what_happened": [(a.get("summary") or a.get("content") or "")[:300]],
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
                style=cfg["settings"]["briefing"].get("synthesis_style", ""),
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

    # Reconcile: match every synthesized story to its article by the echoed [n] id
    # (url/title fallback), restore canonical urls, attach score/date meta, and drop
    # duplicates or stories with blank required sections. Replaces the old url/title
    # "safety net", which appended half-finished stub cards when the model mangled a
    # Google-News URL (sent-briefing bug, 2026-07-09). Runs only on the LLM path —
    # _basic_briefing (--no-llm dev flag) is built straight from the DB rows and would
    # fail the blank-section validation by design.
    if use_llm:
        missing = _reconcile_stories(briefing, top)
        if missing:
            # One targeted retry, synthesizing ONLY the missing items (a fresh, smaller
            # prompt — temp-0 on the identical prompt would just repeat the failure).
            log.warning("Synthesis left %d item(s) without a valid story — retrying those: %s",
                        len(missing), "; ".join(str(a["title"])[:70] for a in missing))
            try:
                extra = synthesize.build_briefing(
                    client, models["synthesis"],
                    cfg["settings"]["llm"]["max_tokens_synthesis"],
                    cfg["settings"]["org"], cfg["settings"]["key_questions"], missing,
                    style=cfg["settings"]["briefing"].get("synthesis_style", ""),
                )
                still_missing = _reconcile_stories(extra, missing)
                briefing["stories"].extend(extra["stories"])
            except Exception as exc:
                log.warning("Retry synthesis failed (%s)", exc)
                still_missing = missing
            if still_missing:
                # Drop rather than send a degraded card. Removing them from `top` also
                # keeps them OUT of mark_briefed, so they stay eligible for the next run.
                log.error("Dropping %d story(ies) that could not be synthesized (they remain "
                          "eligible next run): %s", len(still_missing),
                          "; ".join(str(a["title"])[:70] for a in still_missing))
                gone = {a["id"] for a in still_missing}
                top = [a for a in top if a["id"] not in gone]
        if not top:
            log.error("Reconciliation left no publishable stories — NOT sending; failing so "
                      "the watchdog alerts and the next run retries.")
            raise SystemExit(1)

    # Best-effort link-health check so the renderer can flag any dead links (fail-safe).
    try:
        _flag_broken_links(briefing["stories"])
    except Exception as exc:
        log.warning("link-health check skipped (%s)", exc)

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
    # "What UHealth should consider" section on/off (config: briefing.show_consider_section).
    show_consider = settings["briefing"].get("show_consider_section", True)
    html = emailer.render_html(briefing, date_h, settings["org"]["name"], failing, runners=runners,
                               show_consider=show_consider)

    # Email digest for the top N stories (HTML, with larger titles). Captured/Published
    # dates are matched from the DB rows (by url, then title). Plain text saved too.
    org_short = settings["org"].get("short_name", settings["org"]["name"])
    top_n = settings["briefing"].get("digest_top_n", 5)
    digest = emailer.render_digest(briefing["stories"], date_h, org_short, articles=top, top_n=top_n,
                                   runners=runners, show_consider=show_consider)
    digest_html = emailer.render_digest_html(briefing["stories"], date_h, org_short, articles=top,
                                             top_n=top_n, runners=runners,
                                             show_consider=show_consider)

    (out_dir / f"{run_date}.html").write_text(html, encoding="utf-8")
    (out_dir / f"{run_date}.json").write_text(json.dumps(briefing, indent=2), encoding="utf-8")
    (out_dir / f"{run_date}_digest.txt").write_text(digest, encoding="utf-8")
    (out_dir / f"{run_date}_digest.html").write_text(digest_html, encoding="utf-8")

    # Delivery. --recipients forces a single TEST send (new briefing format) to exactly the
    # given group, skipping the per-profile send — so test runs never hit production leadership.
    # Otherwise: if executive profiles are configured, send each person their own copy;
    # else fall back to the single shared digest to digest_recipients (legacy behavior).
    if args.recipients:
        test_to = _resolve_recipients(settings, args.recipients)
        log.info("TEST send: delivering only to %s", ", ".join(test_to) or "(none resolved)")
        sent = _send_html(settings, f'[TEST] {settings["briefing"]["subject_prefix"]} — {date_h}',
                          html, args.dry_run, run_date, label="Test briefing",
                          recipients_override=test_to)
    else:
        sent = _send_personalized(settings, briefing, date_h, failing, args.dry_run, run_date,
                                  out_dir, runners=runners)
        if sent is None:
            sent = _send_html(settings, f'{settings["briefing"]["subject_prefix"]} — {date_h}',
                              digest_html, args.dry_run, run_date, label="Digest")

    # Second, independent prioritization: semantic profiles (Ambulatory Leadership) are
    # re-scored against their own role_description over the SAME scored_pool — one
    # ingestion, one house-scoring pass, two prioritizations. Runs after the shared send
    # so overlapping stories inherit its deep-dive enrichment.
    _run_semantic_pass(con, cfg, client, use_llm, scored_pool, date_h, args.dry_run,
                       run_date, out_dir, args.recipients)

    # Only consume dedup state when the briefing actually went out — a dry run or a
    # failed/skipped send must not mark stories as already-briefed.
    if sent:
        # Full ISO timestamp (not just the date) so the rebrief window is measured
        # precisely from the first time each story was briefed.
        ids = [a["id"] for a in top]
        # Also stamp duplicates that dedup collapsed into a SENT story, plus re-reports
        # of already-briefed history (dup_map key None) — otherwise a dropped copy
        # stays eligible and resurfaces solo on a later day (Cleveland Clinic $25M,
        # briefed 7/23 via one source, repeated 7/24 via the unstamped second source).
        extra = list(dup_map.get(None, []))
        for i in ids:
            extra.extend(dup_map.get(i, []))
        store.mark_briefed(con, ids + extra, datetime.now(timezone.utc).isoformat())
        con.commit()


if __name__ == "__main__":
    main()
