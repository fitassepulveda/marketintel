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
    ensure_subscores(con, client, cfg, pool)

    weights = cfg["weights"]
    settings = cfg["settings"]
    date_h = datetime.now().strftime("%A, %B %d, %Y")
    org_short = settings["org"].get("short_name", settings["org"]["name"])
    models = settings["llm"]["models"][settings["llm"]["provider"]]
    out_dir = config.DATA_DIR / "briefings"
    out_dir.mkdir(exist_ok=True)
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for p in profs:
        ranked = P.rank_for_profile(p, weights, pool)
        if not ranked:
            print(f"{p['name']}: nothing above their threshold today.")
            continue
        briefing = synthesize.build_briefing(
            client, models["synthesis"], settings["llm"]["max_tokens_synthesis"],
            settings["org"], settings["key_questions"], ranked)
        html = emailer.render_digest_html(briefing["stories"], date_h, org_short,
                                          articles=ranked, top_n=len(ranked), runners=None)
        tag = p["name"].split()[0].lower()
        (out_dir / f"{run_date}_{tag}.html").write_text(html, encoding="utf-8")
        subject = f'{settings["briefing"]["subject_prefix"]} — {p.get("title","")} — {date_h}'
        if args.dry_run:
            print(f"Dry run: {p['name']} briefing saved ({len(ranked)} stories), not sent.")
        else:
            emailer.send(html, subject, {
                "host": config.env("SMTP_HOST"), "port": config.env("SMTP_PORT"),
                "user": config.env("SMTP_USER"), "password": config.env("SMTP_PASS"),
                "from": config.env("EMAIL_FROM"), "to": [p["email"]]}, subtype="html")
            print(f"Sent {p['name']} briefing ({len(ranked)} stories) to {p['email']}.")


if __name__ == "__main__":
    main()
