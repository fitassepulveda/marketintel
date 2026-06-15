#!/usr/bin/env python3
"""Score report — calibration tool (plan task 2.6).

Shows every scored article from the latest run, ranked by composite score,
with the component breakdown so you can judge whether the weighting is right.

Run:  python scripts/score_report.py            # top 30
      python scripts/score_report.py --all      # everything scored
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, store  # noqa: E402
from src.prioritize import scoring  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    cfg = config.load_all()
    w = cfg["weights"]
    con = store.connect()
    rows = con.execute(
        "SELECT * FROM articles WHERE composite_score IS NOT NULL "
        "ORDER BY composite_score DESC" + ("" if args.all else " LIMIT 30")
    ).fetchall()
    if not rows:
        print("No scored articles yet — run run_briefing.py first.")
        return

    thr = w["score_threshold"]
    mix = w["composite"]
    print(f"\nComposite mix: {int(mix['llm_relevance']*100)}% LLM relevance + "
          f"{int(mix['category_weight']*100)}% area weight + {int(mix['source_weight']*100)}% source weight"
          f"  |  threshold: {thr}\n")
    print(f"{'SCORE':>5}  {'IN?':<4} {'LLM':>4}  {'AREA':>4}  {'SRC':>4}  {'AREA':<28} {'SOURCE':<32} TITLE")
    print("-" * 140)
    for r in rows:
        d = dict(r)
        sw = scoring.source_weight(w, d["source"])
        cw = scoring.category_weight(w, d["area"])
        kept = "YES" if (d["composite_score"] or 0) >= thr else "no"
        briefed = " *sent*" if d["briefed_on"] else ""
        print(f"{d['composite_score']:>5.1f}  {kept:<4} {d['llm_score']:>4.1f}  {cw:>4.0f}  {sw:>4.0f}  "
              f"{d['area']:<28} {d['source'][:31]:<32} {d['title'][:60]}{briefed}")
        if d["llm_rationale"] and d["llm_rationale"] != "not scored":
            print(f"{'':>13}-> {d['llm_rationale'][:120]}")
    print(f"\n{len(rows)} articles shown. 'IN?' = above threshold ({thr}). "
          f"'*sent*' = included in a briefing.\n"
          f"Tune in config/weights.yaml: category_weights, composite mix, "
          f"score_threshold, dedup_cosine_similarity.")


if __name__ == "__main__":
    main()
