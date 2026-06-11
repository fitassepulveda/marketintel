#!/usr/bin/env python3
"""Verify every RSS source in config/sources.yaml (plan task 1.1).

Run:  python scripts/verify_sources.py
Prints OK/FAIL per feed with entry counts, so bad URLs can be fixed in the config.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedparser  # noqa: E402

from src import config  # noqa: E402


def main():
    cfg = config.load_all()
    ok, fail = 0, 0
    for area, sources in cfg["sources"].items():
        for s in sources:
            if s.get("type") != "rss":
                continue
            feed = feedparser.parse(s["url"])
            entries = len(feed.entries)
            if entries > 0:
                ok += 1
                flag = " (verify flag can be removed)" if s.get("verify") else ""
                print(f"  OK   {s['name']:<35} {entries:>3} entries{flag}")
            else:
                fail += 1
                exc = getattr(feed, "bozo_exception", "no entries returned")
                print(f"  FAIL {s['name']:<35} [{area}] {s['url']}\n       -> {exc}")
    print(f"\n{ok} feeds OK, {fail} need fixing.")
    if fail:
        print("Fix the FAIL urls in config/sources.yaml, or switch the source to type: yutori.")


if __name__ == "__main__":
    main()
