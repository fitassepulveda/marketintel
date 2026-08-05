#!/usr/bin/env python3
"""Test the Background-line feature against a PAST day's briefing.

`run_briefing.py` has no date flag and its stories are stamped `briefed_on`, so a normal
run can't reproduce an earlier day. This pulls the articles a given day actually briefed
straight out of intel.db and runs ONLY the background step (src/prioritize/related_context)
against them, so you can see what note each story would have carried.

Nothing is sent, nothing is written to the DB, no re-scoring happens.

Usage:
    .venv/bin/python scripts/test_background_notes.py 2026-08-04
    .venv/bin/python scripts/test_background_notes.py 2026-08-04 --html out.html
"""
from __future__ import annotations
import argparse
import logging
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from src import config, store  # noqa: E402
from src.llm_client import LLMClient  # noqa: E402
from src.prioritize import related_context as rc  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("date", help="briefing date to replay, YYYY-MM-DD (e.g. 2026-08-04)")
    ap.add_argument("--html", default=None, help="also write a simple HTML preview here")
    ap.add_argument("--limit", type=int, default=0, help="only the top N stories (0 = all)")
    args = ap.parse_args()

    load_dotenv()
    cfg = config.load_all()
    settings = cfg["settings"]
    ac_cfg = settings.get("additional_context", {}) or {}

    con = store.connect()
    con.row_factory = __import__("sqlite3").Row
    rows = [dict(r) for r in con.execute(
        "SELECT id, title, summary, url, published, composite_score, llm_score "
        "FROM articles WHERE briefed_on LIKE ? ORDER BY composite_score DESC",
        (f"{args.date}%",)
    ).fetchall()]

    if not rows:
        print(f"No stories found with briefed_on = {args.date}.")
        print("Days available:")
        for d, n in con.execute(
            "SELECT substr(briefed_on,1,10), count(*) FROM articles "
            "WHERE briefed_on IS NOT NULL GROUP BY 1 ORDER BY 1 DESC LIMIT 15"):
            print(f"  {d}  ({n} stories)")
        return 1

    if args.limit:
        rows = rows[:args.limit]

    provider = settings["llm"]["provider"]
    client = LLMClient(provider)
    model = settings["llm"]["models"][provider]["scoring"]
    org = settings["org"]
    lookback = int(ac_cfg.get("lookback_days", 120))
    max_related = int(ac_cfg.get("max_related", 6))
    exclude = {rc._norm_url(r.get("url", "")) for r in rows}

    print(f"\nReplaying background notes for {args.date} — {len(rows)} stories\n")
    print("=" * 78)

    results = []
    with_note = 0
    for i, story in enumerate(rows, 1):
        score = story.get("llm_score")
        print(f"\n[{i}] {story['title'][:90]}")
        print(f"    relevance {score}/10   published {str(story.get('published'))[:10]}")
        try:
            cands = rc.find_related_prior(con, story, exclude, lookback, max_related)
            res = rc.assess(client, model, org, story, cands)
        except Exception as exc:                      # noqa: BLE001 - report, don't crash
            print(f"    ERROR: {type(exc).__name__}: {exc}")
            results.append((story, None))
            continue
        if res is None:
            print("    (no background note — treated as genuinely new)")
            results.append((story, None))
            continue
        with_note += 1
        print(f"    BACKGROUND: {res['summary']}")
        for w in res.get("web", [])[:3]:
            print(f"      earlier reporting: {w.get('title','')[:66]}")
        for p in res.get("related", [])[:3]:
            print(f"      prior in DB ({p.get('date','')}): {p.get('title','')[:60]}")
        results.append((story, res))

    print("\n" + "=" * 78)
    print(f"{with_note} of {len(rows)} stories picked up a background note.\n")

    if args.html:
        parts = [f"<h2>Background-note replay — {escape(args.date)}</h2>"]
        for story, res in results:
            parts.append(f'<h3 style="margin-bottom:2px">{escape(story["title"])}</h3>')
            parts.append(f'<p style="color:#666;font-size:12px;margin:0 0 6px">'
                         f'relevance {story.get("llm_score")}/10</p>')
            if res:
                links = " · ".join(
                    f'<a href="{escape(w.get("url",""))}">{escape((w.get("title") or "source")[:60])}</a>'
                    for w in res.get("web", [])[:4])
                parts.append(
                    f'<p style="background:#F7F9FC;border-left:3px solid #6b7a90;padding:6px 10px">'
                    f'<b>Background:</b> {escape(res["summary"])}'
                    + (f'<br><span style="font-size:12px;color:#666">Earlier reporting: {links}</span>'
                       if links else "")
                    + '</p>')
            else:
                parts.append('<p style="color:#999"><i>no background note</i></p>')
        Path(args.html).write_text("<div style='font-family:Arial'>"
                                   + "".join(parts) + "</div>", encoding="utf-8")
        print(f"HTML preview written to {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
