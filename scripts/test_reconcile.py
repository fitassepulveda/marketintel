"""Regression test for the 2026-07-09 sent-briefing bug: a half-baked duplicate story
card at the top of the email (raw headline, empty 'Why it matters'), caused by url/title
matching failing when the model mangles a ~500-char Google-News URL and rewrites a title.

Exercises run_briefing._reconcile_stories + emailer.render_html directly — no network,
no LLM, no DB. Run:  python3 scripts/test_reconcile.py   (exit 0 = all good)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# This test only exercises pure functions; stub feed-ingestion deps if absent so it
# runs anywhere (CI, sandbox) without network access to PyPI.
try:
    import feedparser  # noqa: F401
except ImportError:
    import types
    sys.modules["feedparser"] = types.ModuleType("feedparser")

import run_briefing  # noqa: E402
from src.output import emailer  # noqa: E402

GN = "https://news.google.com/rss/articles/CBMiZ0FVX3lxTE9" + "x" * 400  # realistic blob

TOP = [
    {"id": 101, "title": "Health system opens stand-alone ER in Broward - The Business Journals",
     "url": GN, "area": "south_florida_competitive",
     "source": "South Florida Business Journal (Health)",
     "summary": "Health system opens stand-alone ER in Broward  The Business Journals",
     "content": "", "llm_score": 9.0, "composite_score": 90,
     "published": "2026-07-09T10:00:00+00:00", "fetched": "2026-07-09T11:00:00+00:00"},
    {"id": 102, "title": "CMS finalizes site-neutral payment rule",
     "url": "https://example.com/cms-rule", "area": "national_policy", "source": "STAT",
     "summary": "CMS rule...", "content": "", "llm_score": 8.0, "composite_score": 80,
     "published": "2026-07-09T09:00:00+00:00", "fetched": "2026-07-09T11:00:00+00:00"},
    {"id": 103, "title": "Insurer expands Medicare Advantage in FL",
     "url": "https://example.com/ma-expand", "area": "payer_insurance", "source": "Modern Healthcare",
     "summary": "MA expansion...", "content": "", "llm_score": 7.5, "composite_score": 75,
     "published": "2026-07-09T08:00:00+00:00", "fetched": "2026-07-09T11:00:00+00:00"},
]

def _story(id=None, title="", url="", why="filled", what="filled", watch="filled"):
    return {"id": id, "title": title, "topline": title, "url": url, "area": "x", "source": "x",
            "what_happened": what, "why_it_matters": why, "watch_next": watch,
            "exposure": "", "coverage_label": "coverage"}

fails = []
def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        fails.append(name)

# --- Case 1: the exact 07-09 failure — id echoed, but URL mangled AND title rewritten.
b = {"stories": [
    _story(0, "A competing health system has opened a new free-standing ED in Broward.",
           GN[:80] + "TRUNCATED"),                      # mangled URL, rewritten title
    _story(1, "CMS finalizes site-neutral payment rule", "https://example.com/cms-rule"),
    _story(2, "Insurer expands Medicare Advantage in FL", "https://example.com/ma-expand"),
]}
missing = run_briefing._reconcile_stories(b, list(TOP))
check("mangled url+title: matched by id, nothing missing", missing == [])
check("mangled url+title: no stub appended (3 stories)", len(b["stories"]) == 3)
s0 = b["stories"][0]
check("canonical DB url restored", s0["url"] == GN)
check("score/date meta attached", s0["llm_score"] == 9.0 and s0["published"])

# --- Case 2: no ids at all (model ignores instruction) -> url/title fallback still works,
# and the Broward story (unmatchable: bad url + rewritten title) is reported missing,
# NEVER appended as a stub.
b = {"stories": [
    _story(None, "A competing health system opened a free-standing ED.", "https://bad.example/x"),
    _story(None, "CMS finalizes site-neutral payment rule", "https://EXAMPLE.com/cms-rule/"),
    _story(None, "insurer expands medicare advantage in fl", "https://nope.example/y"),
]}
missing = run_briefing._reconcile_stories(b, list(TOP))
check("fallback: url match survives case/slash", any(s["url"] == "https://example.com/cms-rule"
                                                     for s in b["stories"]))
check("fallback: title match works", any(s["url"] == "https://example.com/ma-expand"
                                         for s in b["stories"]))
check("fallback: unmatchable story dropped, article reported missing",
      len(b["stories"]) == 2 and [a["id"] for a in missing] == [101])

# --- Case 3: duplicate story for one article + a blank why_it_matters story.
b = {"stories": [
    _story(0, "t", GN),
    _story(0, "t dup", GN),                             # duplicate of article 0
    _story(1, "t2", "https://example.com/cms-rule", why="  "),  # blank required section
    _story(2, "t3", "https://example.com/ma-expand"),
]}
missing = run_briefing._reconcile_stories(b, list(TOP))
check("duplicate collapsed", sum(1 for s in b["stories"] if s["url"] == GN) == 1)
check("blank why_it_matters rejected -> article 102 missing",
      [a["id"] for a in missing] == [102])

# --- Case 4: renderer never emits an empty labeled section, and a scored story
# always outranks nothing (regression: stub sorted to top, real story to bottom).
b = {"stories": [_story(0, "Broward ED story", GN)]}
run_briefing._reconcile_stories(b, list(TOP))
html = emailer.render_html(b, "Thursday, July 9, 2026", "UHealth", [])
check("no empty 'Why it matters:' in html",
      not re.search(r"Why it matters:</b>\s*</p>", html))
check("relevance badge rendered", "Relevance 9/10" in html)

empty = {"stories": [{"title": "x", "area": "x", "source": "x", "url": "https://e.com",
                      "what_happened": "", "why_it_matters": "", "watch_next": ""}]}
html = emailer.render_html(empty, "d", "org", [])
check("empty sections skipped entirely",
      "What happened:" not in html and "Why it matters:" not in html)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("ALL PASS")
