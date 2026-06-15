"""Yutori ingestion — competitor / non-RSS monitoring via the Scouting API.

How it works (plan task 1.3):
  * A Scout is a persistent monitor created ONCE per source (see
    scripts/setup_scouts.py). Yutori runs it on its own schedule and accumulates
    structured findings.
  * Each briefing run polls `GET /v1/scouting/tasks/{id}/updates` for findings
    newer than the last one we ingested (tracked by `last_update_ts` in the
    `scouts` table), so we never re-pay for or re-process old updates.
  * Scouts return structured output shaped as {headline, summary, source_url}
    (requested via `output_schema` at creation). We normalise those into the
    same dicts the rest of the pipeline already consumes — nothing downstream
    changes.

Sources without a scout row are simply skipped (run setup_scouts.py to add one),
which is how we limit scouting to a chosen subset of sources.
"""
from __future__ import annotations
import logging
import os
import re
from datetime import datetime, timezone

import requests

from .. import store

log = logging.getLogger("ingest.yutori")

UPDATES_PATH = "/scouting/tasks/{scout_id}/updates"
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


def _extract_published_date(html: str) -> str:
    for pat in _PUB_DATE_PATTERNS:
        m = re.search(pat, html or "", re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def _fetch_published_date(url: str, timeout: int) -> str:
    """Best-effort: fetch an article page and read its publish date from metadata."""
    try:
        resp = requests.get(url, headers={"User-Agent": _BROWSER_UA}, timeout=timeout)
        if resp.ok:
            return _extract_published_date(resp.text)
    except Exception as exc:
        log.debug("date enrich failed for %s: %s", url, exc)
    return ""


def _headers() -> dict:
    api_key = os.environ.get("YUTORI_API_KEY", "")
    if not api_key:
        raise RuntimeError("YUTORI_API_KEY not set")
    return {"X-API-Key": api_key}


def _ts_seconds(raw) -> int:
    """Normalize a Yutori timestamp to whole seconds (it may arrive in ms)."""
    try:
        ts = int(raw)
    except (TypeError, ValueError):
        return 0
    if ts > 10_000_000_000:  # larger than year 2286 in seconds => it's milliseconds
        ts //= 1000
    return ts


def _get_updates(base_url: str, scout_id: str, since_ts: int,
                 page_size: int, max_pages: int, timeout: int) -> list[dict]:
    """Fetch scout updates with timestamp > since_ts (paginated, newest first)."""
    url = base_url.rstrip("/") + UPDATES_PATH.format(scout_id=scout_id)
    headers = _headers()
    fresh: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params = {"page_size": page_size}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
        updates = body.get("updates", [])
        if not updates:
            break
        stop = False
        for upd in updates:
            if _ts_seconds(upd.get("timestamp", 0)) > since_ts:
                fresh.append(upd)
            else:
                stop = True  # reached already-ingested territory
        cursor = body.get("next_cursor")
        if stop or not cursor:
            break
    return fresh


def _normalize_update(upd: dict, source: dict, area: str) -> list[dict]:
    """Turn one scout update into zero+ pipeline article dicts.

    Prefers the structured_result array ({headline, summary, source_url}); falls
    back to the free-text `content` + first citation when structure isn't ready.
    """
    items: list[dict] = []
    # NOTE: a scout update's `timestamp` is when the scout RAN, not when the article
    # was published — never use it as the article date. We read the real article date
    # from the structured `published_date` field (requested in the scout schema); if
    # the scout couldn't find one, we leave it blank rather than show a wrong date.
    citations = [c.get("url") for c in (upd.get("citations") or []) if c.get("url")]

    structured = upd.get("structured_result")
    rows = structured if isinstance(structured, list) else ([structured] if isinstance(structured, dict) else [])

    if rows:
        for row in rows:
            if not isinstance(row, dict):
                continue
            link = row.get("source_url") or (citations[0] if citations else "")
            title = (row.get("headline") or "").strip()
            if not link or not title:
                continue
            items.append({
                "url": link,
                "title": title,
                "summary": (row.get("summary") or "")[:2000],
                "content": (upd.get("content") or "")[:8000],
                "source": source["name"],
                "area": area,
                "published": (row.get("published_date") or "").strip(),
                "enrichment": {
                    "scout_update_id": upd.get("id"),
                    "citations": citations,
                },
            })
    elif citations and (upd.get("content") or "").strip():
        # No structured output yet — keep the update as a single linkable item.
        items.append({
            "url": citations[0],
            "title": (upd.get("content") or "")[:160].strip(),
            "summary": (upd.get("content") or "")[:2000],
            "content": (upd.get("content") or "")[:8000],
            "source": source["name"],
            "area": area,
            "published": "",
            "enrichment": {"scout_update_id": upd.get("id"), "citations": citations},
        })
    return items


def stop_scout(con, yutori_cfg: dict, source_name: str) -> bool:
    """Archive a scout so it stops running (POST /done). Marks it inactive locally."""
    scout = store.get_scout(con, source_name)
    if scout is None:
        return False
    url = yutori_cfg["base_url"].rstrip("/") + f"/scouting/tasks/{scout['scout_id']}/done"
    resp = requests.post(url, headers=_headers(), timeout=yutori_cfg.get("timeout_seconds", 60))
    resp.raise_for_status()
    store.set_scout_active(con, source_name, False)
    log.info("Scout for '%s' archived (stopped running).", source_name)
    return True


def restart_scout(con, yutori_cfg: dict, source_name: str) -> bool:
    """Restart a previously archived scout (POST /restart). Marks it active locally."""
    scout = store.get_scout(con, source_name)
    if scout is None:
        return False
    url = yutori_cfg["base_url"].rstrip("/") + f"/scouting/tasks/{scout['scout_id']}/restart"
    resp = requests.post(url, headers=_headers(), timeout=yutori_cfg.get("timeout_seconds", 60))
    resp.raise_for_status()
    store.set_scout_active(con, source_name, True)
    log.info("Scout for '%s' restarted.", source_name)
    return True


def fetch_source(con, source: dict, area: str, yutori_cfg: dict) -> tuple[list[dict], str | None]:
    """Pull new findings for one scouted source. Returns (normalized items, error)."""
    scout = store.get_scout(con, source["name"])
    if scout is None:
        return [], "no scout configured (run scripts/setup_scouts.py)"
    if not scout["active"]:
        return [], "scout archived/stopped (restart with: setup_scouts.py --restart)"

    try:
        updates = _get_updates(
            yutori_cfg["base_url"], scout["scout_id"], scout["last_update_ts"],
            yutori_cfg.get("page_size", 20),
            yutori_cfg.get("max_update_pages", 5),
            yutori_cfg.get("timeout_seconds", 60),
        )
    except Exception as exc:
        log.warning("Yutori %s: %s", source["name"], exc)
        return [], str(exc)

    items: list[dict] = []
    newest_ts = scout["last_update_ts"]
    for upd in updates:
        items.extend(_normalize_update(upd, source, area))
        newest_ts = max(newest_ts, _ts_seconds(upd.get("timestamp", 0)))

    # Yutori often can't return a publish date. For any item still missing one, read
    # it from the article page's metadata (article:published_time / JSON-LD).
    if yutori_cfg.get("enrich_publish_dates", True):
        for it in items:
            if not it.get("published") and it.get("url"):
                it["published"] = _fetch_published_date(it["url"], yutori_cfg.get("timeout_seconds", 60))

    if newest_ts > scout["last_update_ts"]:
        store.set_scout_cursor(con, source["name"], newest_ts)
    return items, None


def fetch_all(con, sources_by_area: dict, yutori_cfg: dict, enabled: bool = True):
    """Yield (source, area, items, error) for every Yutori-type source."""
    for area, sources in sources_by_area.items():
        for source in sources:
            if source.get("type") != "yutori":
                continue
            if not enabled:
                yield source, area, [], "yutori disabled (--no-yutori or missing key)"
                continue
            items, error = fetch_source(con, source, area, yutori_cfg)
            yield source, area, items, error
