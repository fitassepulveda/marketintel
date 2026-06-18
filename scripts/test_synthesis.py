"""Test how a hand-written article SCORES and FORMATS through the real pipeline — with NO
ingestion, database, or email send.

1. Edit scripts/test_article.json with your article(s) — put the body in "summary".
2. Run:  python3 scripts/test_synthesis.py

For each article it runs the REAL LLM relevance scorer (with the live guidance) AND the
deterministic forced-floor rules (e.g. FIU + Baptist in one sentence -> 9), prints the
score and whether a floor fired, then runs the REAL Gemini synthesis and email renderer,
writing a preview to data/briefings/_test_<n>_<slug>.html. Nothing is sent; the DB is untouched.

Needs network + GEMINI_API_KEY in .env (run on your Mac, venv active), like a real run.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402
from src.llm_client import LLMClient  # noqa: E402
from src.prioritize import llm_relevance, scoring  # noqa: E402
from src.output import synthesize, emailer  # noqa: E402

INPUT = Path(__file__).resolve().parent / "test_article.json"


def main() -> None:
    cfg = config.load_all()
    s = cfg["settings"]
    articles = json.loads(INPUT.read_text(encoding="utf-8"))["articles"]
    if not articles:
        print("No articles in test_article.json."); return

    # Fill the fields the pipeline expects but a hand-written article may omit.
    now = datetime.now(timezone.utc).isoformat()
    for a in articles:
        a.setdefault("summary", "")
        a.setdefault("content", "")
        a.setdefault("source", "")
        a.setdefault("url", "")
        a.setdefault("area", "south_florida_competitive")
        a.setdefault("llm_score", 0)
        a.setdefault("composite_score", (a.get("llm_score") or 0) * 10)
        a.setdefault("llm_rationale", "")
        a.setdefault("fetched", now)

    client = LLMClient(s["llm"]["provider"])
    models = s["llm"]["models"][s["llm"]["provider"]]
    weights = cfg["weights"]
    floor_rules = s["briefing"].get("forced_floor_rules", [])
    date_h = datetime.now().strftime("%A, %B %d, %Y")
    org_short = s["org"].get("short_name", s["org"]["name"])
    out = config.DATA_DIR / "briefings"
    out.mkdir(exist_ok=True)

    def _slug(t: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:40] or "article"

    # 1) SCORE — same LLM relevance scorer + the deterministic forced-floor rules as a real run.
    print(f"Scoring {len(articles)} article(s) with {models['scoring']} ...")
    scores = llm_relevance.score_batch(
        client, models["scoring"], s["org"], s["key_questions"], articles,
        guidance=s["briefing"].get("relevance_guidance", ""),
    )
    for a, (llm_score, why) in zip(articles, scores):
        text = f'{a.get("title", "")}. {a.get("summary") or ""} {a.get("content") or ""}'
        floor, reason = scoring.forced_floor(text, floor_rules)
        a["_floored"] = floor is not None and floor > llm_score
        if a["_floored"]:
            why = f"[Auto-floor {floor:g}] {reason} (LLM had: {why})"
            llm_score = floor
        a["llm_score"] = llm_score
        a["llm_rationale"] = why
        a["composite_score"] = scoring.composite(weights, a, llm_score)

    # 2) SYNTHESIZE + RENDER — each article on its own for an isolated preview.
    written = []
    for i, a in enumerate(articles, 1):
        flag = "   <-- FORCED-FLOOR RULE FIRED" if a.get("_floored") else ""
        print(f"\n[{i}/{len(articles)}] {a['title'][:65]}")
        print(f"    SCORE: LLM relevance {a['llm_score']:g}/10  (composite {a['composite_score']:g}){flag}")
        print(f"    why:   {a['llm_rationale']}")
        briefing = synthesize.build_briefing(
            client, models["synthesis"], s["llm"]["max_tokens_synthesis"],
            s["org"], s["key_questions"], [a],
        )
        print("    --- Gemini synthesis ---")
        print(json.dumps(briefing.get("stories", briefing), indent=2, ensure_ascii=False))
        html = emailer.render_digest_html(briefing["stories"], date_h, org_short,
                                          articles=[a], top_n=12)
        fname = f"_test_{i}_{_slug(a['title'])}.html"
        (out / fname).write_text(html, encoding="utf-8")
        written.append(out / fname)

    print("\n=== Open these to preview each formatted article ===")
    for p in written:
        print(f"  {p}")


if __name__ == "__main__":
    main()
