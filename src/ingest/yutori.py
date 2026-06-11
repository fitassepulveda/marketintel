"""Yutori ingestion — scraping + NLP enrichment for non-RSS sources.

STATUS: ADAPTER STUB (plan task 1.3).
The request/response shapes below are placeholders. When the Yutori API key is
procured, replace `_call_yutori` with the real endpoint(s) per Yutori's docs.
The rest of the pipeline only depends on the normalized dicts this module
returns, so nothing else needs to change.
"""
import logging
import os

import requests

log = logging.getLogger("ingest.yutori")


def _call_yutori(base_url: str, source: dict, max_pages: int) -> list[dict]:
    """Placeholder for the real Yutori scrape+enrich call.

    Expected to return a list of raw article dicts:
      { url, title, summary, content, published, entities, topics, sentiment }
    """
    api_key = os.environ.get("YUTORI_API_KEY", "")
    if not api_key:
        raise RuntimeError("YUTORI_API_KEY not set")

    # TODO(plan 1.3): replace with the actual Yutori API contract.
    resp = requests.post(
        f"{base_url}/scrape",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"url": source["url"], "max_pages": max_pages, "enrich": True},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json().get("articles", [])


def fetch_source(source: dict, area: str, yutori_cfg: dict) -> tuple[list[dict], str | None]:
    """Scrape one non-RSS source. Returns (normalized items, error)."""
    items, error = [], None
    try:
        raw = _call_yutori(yutori_cfg["base_url"], source, yutori_cfg.get("max_pages_per_source", 10))
        for a in raw:
            if not a.get("url") or not a.get("title"):
                continue
            items.append({
                "url": a["url"],
                "title": a["title"],
                "summary": (a.get("summary") or "")[:2000],
                "content": (a.get("content") or "")[:8000],
                "source": source["name"],
                "area": area,
                "published": a.get("published", ""),
                "enrichment": {
                    "entities": a.get("entities"),
                    "topics": a.get("topics"),
                    "sentiment": a.get("sentiment"),
                },
            })
    except Exception as exc:
        error = str(exc)
        log.warning("Yutori %s: %s", source["name"], error)
    return items, error


def fetch_all(sources_by_area: dict, yutori_cfg: dict, enabled: bool = True):
    """Yield (source, area, items, error) for every Yutori source."""
    for area, sources in sources_by_area.items():
        for source in sources:
            if source.get("type") != "yutori":
                continue
            if not enabled:
                yield source, area, [], "yutori disabled (--no-yutori or missing key)"
                continue
            items, error = fetch_source(source, area, yutori_cfg)
            yield source, area, items, error
