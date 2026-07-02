"""Per-story enrichment via Yutori (runs on the SELECTED briefing stories).

Two layers, config-gated and fail-safe:
  * BROWSING (Yutori Browsing API) on EVERY reported story — an agent opens the
    article URL and extracts the full content (facility type, size, services,
    dollar figures, location, dates). This is what gives the write-ups real
    specificity instead of working from a bare headline. Billed per step (~$0.015).
  * RESEARCH (Yutori Research API) ADDITIONALLY on high-relevance stories
    (llm_score >= research_min_relevance) — a broader multi-agent web pass for
    context/fact-checking. Billed ~$0.35 per task.

Both are asynchronous: we launch all tasks, then poll until a shared deadline and
attach whatever finished. Anything that errors, is paywalled, or times out simply
leaves that story as-is — this step can never delay or break the morning send.

Enable via config/settings.yaml -> yutori.deep_dive.
"""
from __future__ import annotations
import logging
import os
import time

import requests

log = logging.getLogger("ingest.deep_dive")

BROWSE_CREATE = "/browsing/tasks"
BROWSE_STATUS = "/browsing/tasks/{tid}"
RESEARCH_CREATE = "/research/tasks"
RESEARCH_STATUS = "/research/tasks/{tid}"

# What we ask the Browsing agent to pull out of the article page.
BROWSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "2-4 sentence factual summary of what the article reports"},
        "key_facts": {"type": "array", "items": {"type": "string"},
                      "description": "discrete facts: facility type, size (sq ft/beds), services, "
                                     "dollar figures, city/location, dates"},
        "full_text": {"type": "string", "description": "the main body text of the article"},
        "paywalled": {"type": "boolean", "description": "true if the page was a paywall/unavailable"},
    },
    "required": ["summary"],
}
BROWSE_TASK = (
    "Read this news article and extract its content. Capture what was announced and any "
    "facility type, size (square feet or beds), services offered, dollar amounts, city/"
    "location, and dates. If the page is a paywall, login wall, or otherwise unavailable, "
    "set paywalled=true and return whatever is visible."
)

RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "additional_context": {"type": "string",
                               "description": "2-4 sentences of background / related developments"},
        "key_facts": {"type": "array", "items": {"type": "string"}},
        "implication": {"type": "string", "description": "one sentence on what it means for the organization"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["additional_context"],
}


def _headers() -> dict:
    key = os.environ.get("YUTORI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("YUTORI_API_KEY not set")
    return {"X-API-Key": key, "Content-Type": "application/json"}


def _cfg(cfg: dict) -> dict:
    dd = (cfg["settings"].get("yutori", {}) or {}).get("deep_dive", {}) or {}
    y = cfg["settings"].get("yutori", {}) or {}
    return {
        "enabled": bool(dd.get("enabled", False)),
        "browse_all": bool(dd.get("browse_all", True)),
        "research_min_relevance": float(dd.get("research_min_relevance", 8)),
        "max_browse": int(dd.get("max_browse", 12)),
        "max_research": int(dd.get("max_research", 5)),
        "browsing_max_steps": int(dd.get("browsing_max_steps", 12)),
        "poll_timeout_seconds": int(dd.get("poll_timeout_seconds", 300)),
        "poll_interval_seconds": int(dd.get("poll_interval_seconds", 10)),
        "base_url": y.get("base_url", "https://api.yutori.com/v1").rstrip("/"),
        "request_timeout": int(y.get("timeout_seconds", 60)),
    }


def _post(c: dict, path: str, payload: dict) -> str | None:
    try:
        r = requests.post(c["base_url"] + path, headers=_headers(), json=payload, timeout=c["request_timeout"])
        r.raise_for_status()
        return r.json().get("task_id")
    except Exception as exc:
        log.warning("deep-dive launch failed (%s): %s", path, exc)
        return None


def _get(c: dict, path: str) -> dict | None:
    try:
        r = requests.get(c["base_url"] + path,
                         headers={"X-API-Key": os.environ.get("YUTORI_API_KEY", "").strip()},
                         timeout=c["request_timeout"])
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("deep-dive poll failed: %s", exc)
        return None


def _research_query(story: dict, org: dict) -> str:
    return (
        f"Research additional context for this news item relevant to {org['name']} "
        f"({org.get('region','')}). Story: \"{story.get('title','')}\". "
        f"Summary: {(story.get('summary') or story.get('content') or '')[:500]}\n\n"
        "Find related/prior developments, relevant figures and dates, and verify key claims. "
        "Prefer primary sources from the last 30 days."
    )


def enrich_stories(stories: list[dict], cfg: dict) -> dict:
    """Browse every reported story; research the high-relevance ones. Mutates stories
    in place (adds full_text / extracted_facts / research_context). Never raises.
    Returns {'browsed': n, 'researched': n}."""
    c = _cfg(cfg)
    if not c["enabled"] or not os.environ.get("YUTORI_API_KEY", "").strip():
        return {"browsed": 0, "researched": 0}
    org = cfg["settings"]["org"]
    user_tz = org.get("timezone", "America/New_York")

    browse_targets = (stories[: c["max_browse"]] if c["browse_all"] else [])
    research_targets = [s for s in stories
                        if float(s.get("llm_score") or 0) >= c["research_min_relevance"]][: c["max_research"]]

    pending: dict = {}  # task_id -> (story, kind)
    for s in browse_targets:
        if not s.get("url"):
            continue
        tid = _post(c, BROWSE_CREATE, {
            "task": BROWSE_TASK, "start_url": s["url"],
            "max_steps": c["browsing_max_steps"], "agent": "navigator-n1.5-latest",
            "output_schema": BROWSE_SCHEMA})
        if tid:
            pending[tid] = (s, "browse")
    for s in research_targets:
        tid = _post(c, RESEARCH_CREATE, {
            "query": _research_query(s, org), "output_schema": RESEARCH_SCHEMA,
            "user_timezone": user_tz, "skip_email": True})
        if tid:
            pending[tid] = (s, "research")
    if not pending:
        return {"browsed": 0, "researched": 0}
    log.info("deep-dive: launched %d browsing + %d research tasks",
             len(browse_targets), len(research_targets))

    browsed = researched = 0
    deadline = time.time() + c["poll_timeout_seconds"]
    while pending and time.time() < deadline:
        time.sleep(c["poll_interval_seconds"])
        for tid in list(pending):
            story, kind = pending[tid]
            path = (BROWSE_STATUS if kind == "browse" else RESEARCH_STATUS).format(tid=tid)
            payload = _get(c, path)
            if not payload:
                continue
            status = payload.get("status")
            if status not in ("succeeded", "failed"):
                continue
            pending.pop(tid)
            if status == "failed":
                log.info("deep-dive %s task failed (%s)", kind, payload.get("rejection_reason"))
                continue
            sr = payload.get("structured_result") or {}
            if isinstance(sr, list):
                sr = sr[0] if sr else {}
            if kind == "browse" and sr:
                if sr.get("full_text"):
                    story["full_text"] = str(sr["full_text"])[:6000]
                if sr.get("summary"):
                    story["summary"] = str(sr["summary"])          # richer than the RSS headline
                if sr.get("key_facts"):
                    story["extracted_facts"] = sr["key_facts"]
                browsed += 1
            elif kind == "research" and sr:
                bits = [sr.get("additional_context", "")]
                if sr.get("implication"):
                    bits.append("Implication: " + sr["implication"])
                if sr.get("sources"):
                    bits.append("Sources: " + "; ".join(sr["sources"][:4]))
                story["research_context"] = " ".join(b for b in bits if b)[:2000]
                researched += 1
    if pending:
        log.warning("deep-dive: %d task(s) unfinished at the %ds deadline — skipped",
                    len(pending), c["poll_timeout_seconds"])
    log.info("deep-dive: enriched %d browsed / %d researched", browsed, researched)
    return {"browsed": browsed, "researched": researched}
