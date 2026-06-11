"""RSS ingestion — free, covers all sources with type: rss."""
import logging
import time
from datetime import datetime, timedelta, timezone

import feedparser

log = logging.getLogger("ingest.rss")


def fetch_feed(source: dict, area: str, lookback_hours: int) -> tuple[list[dict], str | None]:
    """Pull one RSS feed. Returns (items, error)."""
    items, error = [], None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    try:
        feed = feedparser.parse(source["url"])
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
            items.append({
                "url": e.link,
                "title": e.title,
                "summary": getattr(e, "summary", "")[:2000],
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
