#!/usr/bin/env python3
"""Per-executive personalized briefings.

Runs AFTER the normal pipeline and reuses the articles it already scored — so it
does not touch run_briefing.py or its scoring/dedup/selection logic. It:

  1. pulls the recently-scored article pool from SQLite,
  2. adds the six structured sub-scores to any pool items that lack them,
  3. re-ranks the pool through each active profile's lens (config/profiles.yaml),
  4. synthesizes and emails each executive their own briefing.

  python run_briefing.py                          # the shared briefing (unchanged)
  python scripts/personalize.py --dry-run         # per-exec briefings, saved locally, not sent
  python scripts/personalize.py --list-profiles   # show configured executives
  python scripts/personalize.py                   # send per-exec briefings
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, store, profiles as P            # noqa: E402
from src.llm_client import LLMClient                     # noqa: E402
from src.output import emailer, synthesize               # noqa: E402
from src.prioritize import subscores                     # noqa: E402


def recent_scored_pool(con, lookback_hours: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    rows = con.execute(
        "SELECT * FROM articles WHERE composite_score IS NOT NULL AND fetched >= ? "
        "ORDER BY composite_score DESC", (since,)
    ).fetchall()
    return [dict(r) for r in rows]


def ensure_subscores(con, client, cfg, pool: list[dict]) -> None:
    """Sub-score any pool items that don't have sub-scores yet (reused across runs)."""
    import json
    todo = [a for a in pool if not a.get("subscores")]
    if not todo:
        return
    models = cfg["settings"]["llm"]["models"][cfg["settings"]["llm"]["provider"]]
    results = subscores.score_batch(client, models["scoring"], cfg["settings"]["org"], todo)
    for art, ss in zip(todo, results):
        subscores.save(con, art["id"], ss)
        art["subscores"] = ss
    con.commit()
    # parse subscores for items that already had them stored as JSON text
    for a in pool:
        if isinstance(a.get("subscores"), str):
            try:
                a["subscores"] = json.loads(a["subscores"])
            except Exception:
                a["subscores"] = {}


def shared_selection(pool: list[dict], settings: dict) -> list[dict]:
    """Mirror run_briefing's selection: top stories by the house composite score, using
    the same select_threshold / min / max. Used for name-only profiles so their report
    matches the shared briefing (only the greeting differs)."""
    s = sorted(pool, key=lambda a: a.get("composite_score") or 0, reverse=True)
    b = settings["briefing"]
    sel_t = b.get("select_threshold", 90)
    mn = b.get("min_stories", 5)
    mx = b.get("max_stories", 12)
    strong = [a for a in s if (a.get("composite_score") or 0) >= sel_t]
    return strong[:mx] if len(strong) >= mn else s[:mn]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list-profiles", action="store_true")
    args = ap.parse_args()

    cfg = config.load_all()
    profs = P.active_profiles()

    if args.list_profiles:
        if not profs:
            print("No active profiles. Edit config/profiles.yaml (set active: true).")
        for p in profs:
            top = sorted(p["_weights"].items(), key=lambda x: -x[1])[:3]
            dims = ", ".join(f"{k}={v:.0%}" for k, v in top)
            print(f"  {p['name']:<32} {p['email']:<24} top dims: {dims}")
        return

    if not profs:
        print("No active profiles to brief. Edit config/profiles.yaml.")
        return

    con = store.connect()
    subscores.ensure_column(con)
    pool = recent_scored_pool(con, cfg["settings"]["briefing"]["lookback_hours"])
    if not pool:
        print("No recently-scored articles. Run run_briefing.py first.")
        return

    client = LLMClient(cfg["settings"]["llm"]["provider"])
    weights = cfg["weights"]
    settings = cfg["settings"]
    org_name = settings["org"]["name"]
    date_h = datetime.now().strftime("%A, %B %d, %Y")
    models = settings["llm"]["models"][settings["llm"]["provider"]]
    out_dir = config.DATA_DIR / "briefings"
    out_dir.mkdir(exist_ok=True)
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    failing = store.failing_sources(con)

    def _has_custom(p):
        return ("subscore_weights" in p) or ("ahp_pairwise" in p)

    # Sub-scoring is only needed for profiles that personalize CONTENT; skip it entirely
    # (and its API cost) when every active profile is name-only.
    if any(_has_custom(p) for p in profs):
        ensure_subscores(con, client, cfg, pool)

    # Name-only profiles all share ONE synthesized report (identical to the shared
    # briefing); build it once, then render per person so only the greeting differs.
    shared_briefing, shared_count = None, 0
    if any(not _has_custom(p) for p in profs):
        shared = shared_selection(pool, settings)
        if shared:
            shared_briefing = synthesize.build_briefing(
                client, models["synthesis"], settings["llm"]["max_tokens_synthesis"],
                settings["org"], settings["key_questions"], shared,
                style=settings["briefing"].get("synthesis_style", ""))
            shared_count = len(shared)

    for p in profs:
        greeting_name = p.get("display_name") or p.get("name", "")
        if _has_custom(p):
            ranked = P.rank_for_profile(p, weights, pool)
            if not ranked:
                print(f"{p['name']}: nothing above their threshold today.")
                continue
            briefing = synthesize.build_briefing(
                client, models["synthesis"], settings["llm"]["max_tokens_synthesis"],
                settings["org"], settings["key_questions"], ranked,
                style=settings["briefing"].get("synthesis_style", ""))
            count = len(ranked)
        else:
            if not shared_briefing:
                print(f"{p['name']}: no stories to brief today.")
                continue
            briefing, count = shared_briefing, shared_count

        # Use the executive-summary format (takeaways + key-question answers + stories)
        # with the greeting folded into the header so it reads as one unified report.
        html = emailer.render_html(briefing, date_h, org_name, failing, greeting=greeting_name)
        tag = p["name"].split()[0].lower()
        (out_dir / f"{run_date}_{tag}.html").write_text(html, encoding="utf-8")
        subject = f'{settings["briefing"]["subject_prefix"]} — {p.get("title","")} — {date_h}'
        if args.dry_run:
            print(f"Dry run: {p['name']} briefing saved ({count} stories), not sent.")
        else:
            emailer.send(html, subject, {
                "host": config.env("SMTP_HOST"), "port": config.env("SMTP_PORT"),
                "user": config.env("SMTP_USER"), "password": config.env("SMTP_PASS"),
                "from": config.env("EMAIL_FROM"), "to": [p["email"]]}, subtype="html")
            print(f"Sent {p['name']} briefing ({count} stories) to {p['email']}.")


if __name__ == "__main__":
    main()
