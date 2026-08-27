#!/usr/bin/env python3
"""Verify the candidate RSS feeds in config/sources_candidates.yaml.

RUN THIS FROM YOUR OWN TERMINAL — the Cowork sandbox has no outbound network.

    source .venv/bin/activate
    python scripts/verify_candidate_feeds.py

For each feed: reachable? parses? how many entries, how recent, and a sample title.
Nothing is written; promote the PASSes into config/sources.yaml by hand.
"""
import sys, time
from pathlib import Path
import yaml

try:
    import feedparser
except ImportError:
    sys.exit("feedparser not installed — activate the venv first.")

ROOT = Path(__file__).resolve().parent.parent
CAND = ROOT / "config" / "sources_candidates.yaml"
UA = "Mozilla/5.0 (compatible; marketintel/1.0)"

def main():
    data = yaml.safe_load(CAND.read_text())
    rows, passes = [], 0
    for area, entries in data.items():
        for s in entries:
            name, url = s["name"], s["url"]
            try:
                f = feedparser.parse(url, agent=UA)
                status = getattr(f, "status", None)
                n = len(f.entries)
                if f.bozo and n == 0:
                    rows.append(("FAIL", area, name, f"unparseable ({type(f.bozo_exception).__name__})", url)); continue
                if status and status >= 400:
                    rows.append(("FAIL", area, name, f"HTTP {status}", url)); continue
                if n == 0:
                    rows.append(("FAIL", area, name, "0 entries", url)); continue
                newest = ""
                for e in f.entries[:1]:
                    tp = e.get("published_parsed") or e.get("updated_parsed")
                    if tp:
                        age_d = (time.time() - time.mktime(tp)) / 86400
                        newest = f"newest {age_d:.0f}d old"
                title = f.entries[0].get("title", "")[:48]
                rows.append(("PASS", area, name, f"{n} entries, {newest} | {title}", url)); passes += 1
            except Exception as exc:
                rows.append(("FAIL", area, name, f"{type(exc).__name__}: {exc}"[:60], url))

    w = max(len(r[2]) for r in rows)
    for st, area, name, detail, url in rows:
        mark = "✓" if st == "PASS" else "✗"
        print(f"{mark} {st}  {name:<{w}}  {area:<26} {detail}")
        if st == "FAIL":
            print(f"        {url}")
    print(f"\n{passes}/{len(rows)} feeds usable.")
    print("Promote the PASS rows into config/sources.yaml under the same area key.")

if __name__ == "__main__":
    main()
