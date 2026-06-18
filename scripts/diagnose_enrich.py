"""Diagnose why date-enrichment recovers nothing.

Pulls a few currently-undated article URLs from data/intel.db, fetches each the
same way src/ingest/enrich.py does, and reports exactly what comes back: HTTP
status, redirect target, content type/length, whether any date pattern matched,
and whether date-ish markers even exist in the HTML. That distinguishes the three
failure modes: bot-block (403/challenge), JS-rendered page (200 but no date meta),
or a real article whose format our regex misses.

Run on a machine with real network (your Mac), venv active:
    python3 scripts/diagnose_enrich.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from src.ingest.enrich import _BROWSER_UA, _PUB_DATE_PATTERNS, extract_published_date  # noqa: E402

DB = "data/intel.db"
N = 8


def undated_urls(limit: int) -> list[tuple[str, str]]:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "select source, url from articles "
        "where (published is null or trim(published)='') and url<>'' limit ?", (limit,)
    ).fetchall()
    con.close()
    return rows


def main() -> None:
    rows = undated_urls(N)
    if not rows:
        print("No undated articles in the DB right now.")
        return
    for source, url in rows:
        print("\n" + "=" * 80)
        print(f"[{source}]\n{url}")
        try:
            r = requests.get(url, headers={"User-Agent": _BROWSER_UA}, timeout=10,
                             allow_redirects=True)
        except Exception as exc:
            print(f"  FETCH RAISED: {exc!r}")
            continue
        html = r.text or ""
        print(f"  status={r.status_code}  ok={r.ok}  final_url={r.url[:90]}")
        print(f"  content_type={r.headers.get('content-type','?')}  bytes={len(html)}")
        got = extract_published_date(html)
        print(f"  -> extracted date: {got!r}")
        # which pattern (if any) and whether date markers exist at all
        for i, pat in enumerate(_PUB_DATE_PATTERNS):
            import re
            m = re.search(pat, html, re.IGNORECASE)
            print(f"     pattern[{i}] {'MATCH '+m.group(1)[:40] if m else 'no match'}")
        for marker in ("published_time", "datePublished", "<time"):
            print(f"     contains {marker!r}: {marker in html}")
        low = html.lower()
        if any(s in low for s in ("captcha", "are you a robot", "cf-challenge",
                                  "enable javascript", "access denied")):
            print("     !! looks like a bot-block / challenge / JS-gate page")


if __name__ == "__main__":
    sys.exit(main())
