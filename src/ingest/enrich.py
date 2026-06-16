"""Best-effort publish-date extraction from an article page's metadata.

Shared by the Yutori scout path and RSS ingestion: when a feed or scout item has
no publish date, we fetch the page and read the date the site itself records
(`article:published_time` / `og:published_time` / JSON-LD `datePublished` /
`<time datetime=...>`). This lets the 72h recency filter run on a real publish
date instead of falling back to fetch time — which can't tell an old story that
was surfaced today from a genuinely new one.

Best-effort by design: any failure returns "" so a single bad page never crashes
the run (a source failure must never take down the pipeline).
"""
from __future__ import annotations

import logging
import re

import requests

log = logging.getLogger("ingest.enrich")

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Patterns for the publish date in a page's metadata (most newsrooms expose one).
_PUB_DATE_PATTERNS = [
    r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|og:published_time|'
    r'datePublished|publishdate|pubdate|date|DC\.date\.issued)["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']'
    r'(?:article:published_time|og:published_time)["\']',
    r'"datePublished"\s*:\s*"([^"]+)"',
    r'<time[^>]+datetime=["\']([^"\']+)["\']',
]


def extract_published_date(html: str) -> str:
    """Return the first publish date found in the page HTML, or "" if none."""
    for pat in _PUB_DATE_PATTERNS:
        m = re.search(pat, html or "", re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def fetch_published_date(url: str, timeout: int = 10) -> str:
    """Fetch an article page and read its publish date from metadata.

    Best-effort: returns "" on any failure (never raises)."""
    if not url:
        return ""
    try:
        resp = requests.get(url, headers={"User-Agent": _BROWSER_UA}, timeout=timeout)
        if resp.ok:
            return extract_published_date(resp.text)
    except Exception as exc:
        log.debug("date enrich failed for %s: %s", url, exc)
    return ""
