"""A/B test the VALUE OF YUTORI enrichment on one real article — no email, no DB writes.

Takes the HIGHEST-SCORING article from the MOST RECENT sent briefing and synthesizes it
twice with the same Gemini pipeline:

  A) RSS-only   — exactly what synthesis sees without Yutori (title + feed summary)
  B) Yutori     — after the deep-dive suite: Browsing agent reads the article page
                  (full text + key facts) AND a Research task adds broader context

Outputs (in data/briefings/):
  _yutori_test_<date>_A_rss.html       rendered story card, RSS-only
  _yutori_test_<date>_B_yutori.html    rendered story card, Yutori-enriched
  _yutori_test_<date>_compare.html     side-by-side + the raw enrichment material

Usage (on your Mac, venv active; needs GEMINI_API_KEY + YUTORI_API_KEY in .env):
  python3 scripts/test_yutori_value.py               # full A/B run  (~$0.50 Yutori cost)
  python3 scripts/test_yutori_value.py --pick-only   # just show which article would be used
  python3 scripts/test_yutori_value.py --url URL     # override: test a specific stored article
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, store                     # noqa: E402
from src.llm_client import LLMClient              # noqa: E402
from src.ingest import deep_dive                  # noqa: E402
from src.output import emailer, synthesize        # noqa: E402

ENRICH_KEYS = ("full_text", "extracted_facts", "research_context")


def pick_article(con, url: str | None) -> dict:
    """Highest composite from the most recent send. mark_briefed() stamps every story
    of a run with ONE identical timestamp, so ordering by briefed_on DESC then
    composite DESC yields exactly 'top story of the latest briefing'."""
    if url:
        row = con.execute("SELECT * FROM articles WHERE url=?", (url,)).fetchone()
        if not row:
            sys.exit(f"No stored article with url {url}")
        return dict(row)
    row = con.execute(
        "SELECT * FROM articles WHERE briefed_on IS NOT NULL "
        "ORDER BY briefed_on DESC, composite_score DESC LIMIT 1").fetchone()
    if not row:
        sys.exit("No briefed articles in the DB yet — run a briefing first.")
    return dict(row)


def synthesize_one(cfg, client, art: dict) -> dict:
    s = cfg["settings"]
    models = s["llm"]["models"][s["llm"]["provider"]]
    briefing = synthesize.build_briefing(
        client, models["synthesis"], s["llm"]["max_tokens_synthesis"],
        s["org"], s["key_questions"], [art],
        style=s["briefing"].get("synthesis_style", ""),
    )
    # Heal the single story like the real pipeline does (canonical url + meta).
    stories = briefing.get("stories") or []
    if not stories:
        sys.exit("Synthesis returned no story.")
    st = stories[0]
    st.update(url=art["url"], area=art["area"], source=art["source"],
              llm_score=art.get("llm_score"), published=art.get("published"),
              fetched=art.get("fetched"))
    briefing["stories"] = [st]
    return briefing


def force_yutori(cfg) -> None:
    """Force the deep-dive suite ON for this one article, regardless of settings.yaml
    (keeps base_url/timeout from config). Research threshold dropped to 0 so the
    research pass always fires — that's the 'suite' being tested."""
    y = cfg["settings"].setdefault("yutori", {})
    y["deep_dive"] = {**(y.get("deep_dive") or {}),
                      "enabled": True, "browse_all": True, "research_min_relevance": 0,
                      "max_browse": 1, "max_research": 1,
                      "poll_timeout_seconds": 420, "poll_interval_seconds": 10}


def compare_html(art, html_a, html_b, enriched, date_h) -> str:
    def cell(label, inner):
        return (f'<td style="width:50%;vertical-align:top;padding:10px;border:1px solid #ddd">'
                f'<h2 style="font-family:Arial;color:#006888;font-size:15px;margin:0 0 8px">'
                f'{escape(label)}</h2>{inner}</td>')
    facts = enriched.get("extracted_facts") or []
    material = (
        '<h2 style="font-family:Arial;color:#006888;font-size:15px">What Yutori added (raw material)</h2>'
        f'<p style="font-family:Arial;font-size:12px"><b>Full text extracted:</b> '
        f'{len(enriched.get("full_text") or "")} chars</p>'
        + (('<p style="font-family:Arial;font-size:12px"><b>Key facts:</b></p><ul style="font-family:Arial;font-size:12px">'
            + "".join(f"<li>{escape(str(f))}</li>" for f in facts) + "</ul>") if facts else "")
        + (f'<p style="font-family:Arial;font-size:12px"><b>Research context:</b> '
           f'{escape(enriched.get("research_context") or "(none returned)")}</p>')
        + (f'<details style="font-family:Arial;font-size:11px;color:#555"><summary>Full text</summary>'
           f'<p>{escape((enriched.get("full_text") or "")[:6000])}</p></details>'
           if enriched.get("full_text") else "")
    )
    return (
        f'<div style="max-width:1400px;margin:auto;font-family:Arial">'
        f'<h1 style="color:#006888;font-size:18px">Yutori value test — {escape(date_h)}</h1>'
        f'<p style="font-size:13px;color:#444"><b>Article:</b> {escape(art["title"])} '
        f'&nbsp;·&nbsp; {escape(art["source"])} &nbsp;·&nbsp; LLM relevance '
        f'{art.get("llm_score")}/10</p>'
        f'<table style="border-collapse:collapse;width:100%"><tr>'
        + cell("A — RSS + Gemini (no Yutori)", html_a)
        + cell("B — Yutori suite + Gemini", html_b)
        + f'</tr></table>{material}</div>'
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None, help="test a specific stored article by URL")
    ap.add_argument("--pick-only", action="store_true", help="show the chosen article and exit")
    args = ap.parse_args()

    cfg = config.load_all()
    con = store.connect()
    art = pick_article(con, args.url)
    print(f'Article: {art["title"]}\nSource:  {art["source"]}  |  area {art["area"]}\n'
          f'Scores:  llm {art.get("llm_score")}/10, composite {art.get("composite_score")}\n'
          f'Briefed: {art.get("briefed_on")}\nURL:     {art["url"]}')
    if args.pick_only:
        return

    client = LLMClient(cfg["settings"]["llm"]["provider"])
    date_h = datetime.now().strftime("%A, %B %d, %Y")
    out = config.DATA_DIR / "briefings"
    out.mkdir(exist_ok=True)
    tag = datetime.now().strftime("%Y-%m-%d")

    # --- A: RSS-only (strip any enrichment already stored on the row) -----------------
    art_a = {k: v for k, v in art.items() if k not in ENRICH_KEYS}
    print("\n[A] Synthesizing RSS-only version…")
    brief_a = synthesize_one(cfg, client, art_a)
    html_a = emailer.render_html(brief_a, date_h, cfg["settings"]["org"]["name"], [])
    (out / f"_yutori_test_{tag}_A_rss.html").write_text(html_a, encoding="utf-8")

    # --- B: Yutori suite, then the identical synthesis --------------------------------
    art_b = copy.deepcopy(art_a)
    force_yutori(cfg)
    print("[B] Running Yutori deep-dive (browse + research; polls up to ~7 min)…")
    stats = deep_dive.enrich_stories([art_b], cfg)
    print(f"[B] Yutori done: {stats}. Synthesizing enriched version…")
    if not stats.get("browsed") and not stats.get("researched"):
        print("WARNING: Yutori returned nothing (check YUTORI_API_KEY / paywall) — "
              "B will equal A in inputs.")
    brief_b = synthesize_one(cfg, client, art_b)
    html_b = emailer.render_html(brief_b, date_h, cfg["settings"]["org"]["name"], [])
    (out / f"_yutori_test_{tag}_B_yutori.html").write_text(html_b, encoding="utf-8")

    cmp_path = out / f"_yutori_test_{tag}_compare.html"
    cmp_path.write_text(compare_html(art, html_a, html_b, art_b, date_h), encoding="utf-8")
    (out / f"_yutori_test_{tag}.json").write_text(
        json.dumps({"article": {k: art.get(k) for k in
                                ("title", "url", "source", "llm_score", "composite_score")},
                    "yutori_stats": stats,
                    "enrichment": {k: art_b.get(k) for k in ENRICH_KEYS},
                    "A_rss": brief_a["stories"][0], "B_yutori": brief_b["stories"][0]},
                   indent=2), encoding="utf-8")
    print(f"\nDone. Open:\n  {cmp_path}\n(A/B story cards + raw JSON saved alongside.)")


if __name__ == "__main__":
    main()
