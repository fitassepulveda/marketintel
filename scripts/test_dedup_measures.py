#!/usr/bin/env python3
"""Regression test: named-measure dedup (added 2026-09-01).

The case that motivated it — 2026-08-28, both tiers of the same briefing:
  top stories : "Florida's public hospitals could lose millions in funding if Amendment 3
                 passes, new report says"        (WLRN,  via GNews - Jackson Health)
  also worth  : "Public hospitals face cutbacks if Florida property taxes are reduced
                 under Amendment 3"              (WPEC,  via GNews - Broward Health)
One report, two frames. Headline similarity 0.45 (gate 0.90), distinctive-token overlap
0.30 (gate 0.60) — and the one token that identifies the event, the "3" of Amendment 3,
was discarded by the len>=4 filter in _sig_tokens.

Runs offline; no DB and no API needed.
    python scripts/test_dedup_measures.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.prioritize.scoring import _same_event, _measure_tokens, dedupe_by_title_track

T, O = 0.90, 0.6
def art(title, summary=""):
    return {"title": title, "summary": summary or title}

MERGE = [
    ("the motivating case",
     art("Florida’s public hospitals could lose millions in funding if Amendment 3 passes, "
         "new report says - WLRN"),
     art("Public hospitals face cutbacks if Florida property taxes are reduced under "
         "Amendment 3 - WPEC")),
    ("same rule, different framing",
     art("Hospital, physician groups urge CMS to pull back CY 2027 outpatient proposed rule"),
     art("AHA asks CMS to withdraw CY 2027 outpatient payment provisions")),
    ("statute by number",
     art("Rural hospitals brace for HR 1 Medicaid cuts"),
     art("HR 1 reimbursement changes hit rural hospital margins")),
]

KEEP = [
    ("different measures, same topic",
     art("Florida hospitals weigh Amendment 3 property tax impact"),
     art("Georgia hospitals weigh Amendment 5 property tax impact")),
    ("list positions are not measures",
     art("10 states with the longest, shortest workweeks"),
     art("Top 10 health systems by operating margin")),
    ("shared trial phase, unrelated stories",
     art("AstraZeneca's heart failure drug shows promise in phase 2 trial"),
     art("Electra aims to light up Nasdaq with IPO for phase 2 push")),
    ("percentage ranges are not measures",
     art("From -4.8% to 26.7%: 26 large systems ranked by Q2 margins"),
     art("Orlando Health margin hits 3% in Q3, opens 2 hospitals in July")),
]

fails = 0
print("SHOULD MERGE")
for name, a, b in MERGE:
    ok = _same_event(a, b, T, O)
    fails += not ok
    print(f'  [{"PASS" if ok else "FAIL"}] {name}  shared measure={sorted(_measure_tokens(a["title"]) & _measure_tokens(b["title"]))}')
print("SHOULD STAY SEPARATE")
for name, a, b in KEEP:
    ok = not _same_event(a, b, T, O)
    fails += not ok
    print(f'  [{"PASS" if ok else "FAIL"}] {name}')

# End to end: the higher-scored copy survives, the other is absorbed.
a = {**MERGE[0][1], "id": 4254, "composite_score": 82.0}
b = {**MERGE[0][2], "id": 4256, "composite_score": 78.0}
kept, absorbed = dedupe_by_title_track([a, b], T, O)
ok = [k["id"] for k in kept] == [4254] and [(d["id"], s["id"]) for d, s in absorbed] == [(4256, 4254)]
fails += not ok
print(f'\nEND TO END\n  [{"PASS" if ok else "FAIL"}] 2 in -> {len(kept)} kept, {len(absorbed)} absorbed '
      f'(survivor keeps the higher score)')

print("\nALL PASS" if not fails else f"\n{fails} FAILURE(S)")
sys.exit(1 if fails else 0)
