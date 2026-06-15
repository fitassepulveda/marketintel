"""RSS ingestion — free, covers all sources with type: rss."""
from __future__ import annotations
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape

import feedparser

log = logging.getLogger("ingest.rss")


def _clean(text: str) -> str:
    """Strip stray HTML tags/entities some feeds put in titles & summaries."""
    return unescape(re.sub(r"<[^>]+>", "", text or "")).strip()

# Some sites (HHS, Tribune papers) serve an HTML block page to bot-like
# user agents, which feedparser chokes on ("mismatched tag"). Use a browser UA.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def fetch_feed(source: dict, area: str, lookback_hours: int) -> tuple[list[dict], str | None]:
    """Pull one RSS feed. Returns (items, error)."""
    items, error = [], None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    try:
        feed = feedparser.parse(source["url"], agent=USER_AGENT)
        if feed.bozo and not feed.entries:
            error = f"feed parse error: {getattr(feed, 'bozo_exception', 'unknown')}"
        for e in feed.entries:
            published = None
            for key in ("published_parsed", "updated_parsed"):
                if getattr(e, key, None):
                    published = datetime.fromtimestamp(time.mktime(getattr(e, key)), tz=timezone.utc)
                    break
            # Keep undated items (some feeds omit dates); skip clearly old ones
            if published and published < cutoff:
                continue
            if not getattr(e, "link", None) or not getattr(e, "title", None):
                continue
            title = _clean(e.title)
            if not title:
                continue
            items.append({
                "url": e.link,
                "title": title,
                "summary": _clean(getattr(e, "summary", ""))[:2000],
                "source": source["name"],
                "area": area,
                "published": published.isoformat() if published else "",
            })
    except Exception as exc:  # network errors etc.
        error = str(exc)
    if error:
        log.warning("RSS %s: %s", source["name"], error)
    else:
        log.info("RSS %s: %d items", source["name"], len(items))
    return items, error


def fetch_all(sources_by_area: dict, lookback_hours: int):
    """Yield (source, area, items, error) for every RSS source."""
    for area, sources in sources_by_area.items():
        for source in sources:
            if source.get("type") != "rss":
                continue
            items, error = fetch_feed(source, area, lookback_hours)
            yield source, area, items, error
