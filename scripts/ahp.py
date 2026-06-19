#!/usr/bin/env python3
"""AHP & eigen-analysis report.

  python scripts/ahp.py --judgments   # derive weights from config/ahp.yaml (prescriptive)
  python scripts/ahp.py --data        # eigen-analysis of real article sub-scores (descriptive)
  python scripts/ahp.py               # both

The --judgments mode works immediately. The --data mode needs articles that have
sub-scores (run run_personalized.py at least once).
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import ahp, config, store  # noqa: E402
from src.prioritize import subscores  # noqa: E402

DIMS = subscores.DIMENSIONS


def report_judgments():
    cfg_path = config.CONFIG_DIR / "ahp.yaml"
    pairwise = yaml.safe_load(open(cfg_path)).get("pairwise", {})
    matrix = ahp.matrix_from_pairwise(DIMS, pairwise)
    res = ahp.ahp_weights(matrix)

    print("\n=== AHP (PRESCRIPTIVE) — weights derived from pairwise judgments ===\n")
    ranked = sorted(zip(DIMS, res["weights"]), key=lambda x: -x[1])
    for dim, w in ranked:
        bar = "#" * int(round(w * 50))
        print(f"  {dim:<22} {w*100:5.1f}%  {bar}")
    print(f"\n  lambda_max = {res['lambda_max']:.3f}   "
          f"Consistency Ratio = {res['consistency_ratio']:.3f} "
          f"({'OK — judgments consistent' if res['consistent'] else 'TOO HIGH — revise judgments (want < 0.10)'})")
    print("\n  These weights are a defensible, audit-ready alternative to hand-picking.")
    return res["weights"]


def report_data():
    con = store.connect()
    subscores.ensure_column(con)
    rows = subscores.load_scored(con)
    print(f"\n=== EIGEN-ANALYSIS (DESCRIPTIVE) — {len(rows)} scored articles ===\n")
    if len(rows) < 5:
        print("  Not enough scored articles yet (need >= 5). Run run_personalized.py first.")
        return
    data = np.array([[r["subscores"].get(d, 0.0) for d in DIMS] for r in rows])

    res = ahp.eigen_analysis(data)
    print("  Variance explained by each principal component:")
    for i, ve in enumerate(res["variance_explained"][:3]):
        print(f"    PC{i+1}: {ve*100:5.1f}%")
    print("\n  PC1 loadings — dimensions actually driving what the system surfaces:")
    ranked = sorted(zip(DIMS, res["loadings"]), key=lambda x: -x[1])
    for dim, l in ranked:
        bar = "#" * int(round(l * 50))
        print(f"    {dim:<22} {l*100:5.1f}%  {bar}")
    print("\n  Mean sub-score per dimension (raw 0-10 average across articles):")
    for dim, m in sorted(zip(DIMS, data.mean(axis=0)), key=lambda x: -x[1]):
        print(f"    {dim:<22} {m:4.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judgments", action="store_true")
    ap.add_argument("--data", action="store_true")
    args = ap.parse_args()
    both = not (args.judgments or args.data)
    if args.judgments or both:
        report_judgments()
    if args.data or both:
        report_data()


if __name__ == "__main__":
    main()
