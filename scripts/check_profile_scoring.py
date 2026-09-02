#!/usr/bin/env python3
"""PRE-FLIGHT: prove the profile (role-scored) path works against the LIVE model.

This is the one link in the new two-pass briefing that had never made a real API call:
src/prioritize/profile_relevance.score_batch(). Everything else it relies on — the model,
the client, synthesis, rendering, SMTP — is already exercised by the daily briefing.

Read-only and cheap: ONE batched scoring call over a handful of articles already in the
database (a fraction of a cent). Writes nothing, caches nothing, sends nothing.

    python scripts/check_profile_scoring.py
    python scripts/check_profile_scoring.py --profile Ambulatory-Rafic --limit 8

Exit code 0 = the path works. Non-zero = it does not, and the message says why.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, store, profiles as P            # noqa: E402
from src.llm_client import LLMClient                     # noqa: E402
from src.prioritize import profile_relevance as PR       # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="Ambulatory-Rafic")
    ap.add_argument("--limit", type=int, default=6)
    args = ap.parse_args()

    profs, defaults = P.load_profiles()
    match = [p for p in profs if p["name"] == args.profile]
    if not match:
        print(f"FAIL: no profile named {args.profile!r}. Found: {[p['name'] for p in profs]}")
        return 1
    profile = {**defaults, **match[0]}
    if not P.uses_semantic_scoring(profile):
        print(f"FAIL: {args.profile!r} has no role_description — nothing to check.")
        return 1
    print(f"Profile      : {profile['name']}  ({profile.get('title','')})")
    print(f"Greeting     : Good morning {profile.get('display_name')},")
    print(f"Email        : {profile.get('email')}")
    print(f"Threshold    : {profile.get('threshold')}   max_stories: {profile.get('max_stories')}")

    cfg = config.load_all()
    provider = cfg["settings"]["llm"]["provider"]
    model = cfg["settings"]["llm"]["models"][provider]["scoring"]
    print(f"Provider     : {provider}   scoring model: {model}")

    con = store.connect()
    rows = con.execute(
        "SELECT * FROM articles WHERE composite_score IS NOT NULL "
        "ORDER BY fetched DESC LIMIT ?", (args.limit,)).fetchall()
    articles = [dict(r) for r in rows]
    if not articles:
        print("FAIL: no scored articles in the database to test with.")
        return 1
    print(f"Test items   : {len(articles)} most recently scored articles\n")

    try:
        results = PR.score_batch(client := LLMClient(provider), model,
                                 cfg["settings"]["org"], profile, articles)
    except Exception as exc:
        print(f"FAIL: the live scoring call raised {type(exc).__name__}: {exc}")
        print("      -> the role-scored briefing would be SKIPPED (the shared briefing is "
              "unaffected).")
        return 1

    real = [(a, s, w) for a, (s, w) in zip(articles, results) if w != "not scored"]
    print(f"{'score':>6}  {'house':>6}  title / rationale")
    print(f"{'-'*6}  {'-'*6}  {'-'*60}")
    for a, (score, why) in zip(articles, results):
        mark = "  " if why != "not scored" else " !"
        print(f"{score:>6.1f}{mark}{(a.get('composite_score') or 0)/10:>6.1f}  "
              f"{str(a['title'])[:70]}")
        print(f"{'':>14}  {str(why)[:100]}")

    print()
    problems = []
    if not real:
        problems.append("every item came back 'not scored' — the model call or its JSON failed")
    if any(not (0.0 <= s <= 10.0) for _, s, _ in real):
        problems.append("a score fell outside 0-10")
    if real and all(float(s).is_integer() for _, s, _ in real):
        problems.append("all scores are whole numbers — the one-decimal instruction was "
                        "ignored, so ranking will have lots of ties (not fatal)")
    if any(not str(w).strip() for _, _, w in real):
        problems.append("a rationale came back empty")

    if not real:
        print("RESULT: FAIL —", problems[0])
        return 1
    print(f"RESULT: the live scoring path WORKS — {len(real)}/{len(articles)} items scored.")
    for p in problems:
        print(f"  note: {p}")
    print("\nNothing was written to the database and no email was sent.")
    print("Sanity-check the numbers above: items in this role's remit (outpatient sites, "
          "real estate,\npharmacy, quality/rankings, oncology capital) should out-score "
          "pharma, IT and general news.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
