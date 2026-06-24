"""Per-story deep-dive enrichment via the Yutori Research API.

For each story SELECTED for the briefing, launch a one-time Yutori research task to
gather additional context, then attach the structured findings to the story so the
synthesis step writes a richer narrative. This is the deck's "secondary data pull
to capture additional context on high-priority findings."

Design notes:
  * The Research API is ASYNC: POST /v1/research/tasks returns a task_id; results
    arrive later via GET /v1/research/tasks/{id}. To bound wall-clock time we launch
    ALL tasks first, then poll them together until a shared deadline.
  * COST/LATENCY bounded: only the top `max_stories` are enriched (each task is
    ~$0.35), and polling stops at `poll_timeout_seconds`.
  * FAIL-SAFE: any error, timeout, or rejection leaves the story unchanged. Deep-dive
    must never crash or delay the morning briefing — it's purely additive.

Enabled via config/settings.yaml -> yutori.deep_dive.enabled (default OFF). See the
snippet in the project docs to turn it on.
"""
from __future__ import annotations
import logging
import os
import time

import requests

log = logging.getLogger("ingest.deep_dive")

CREATE_PATH = "/research/tasks"
STATUS_PATH = "/research/tasks/{task_id}"

# What we ask Yutori to return for each story (structured, schema-validated).
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "additional_context": {"type": "string",
                               "description": "2-4 sentences of background and related developments not in the original story"},
        "key_facts": {"type": "array", "items": {"type": "string"},
                      "description": "Discrete factual bullet points (figures, dates, names)"},
        "implication": {"type": "string",
                        "description": "One sentence on what this means for the organization"},
        "sources": {"type": "array", "items": {"type": "string"},
                    "description": "URLs of corroborating sources"},
    },
    "required": ["additional_context"],
}


def _headers() -> dict:
    key = os.environ.get("YUTORI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("YUTORI_API_KEY not set")
    return {"X-API-Key": key, "Content-Type": "application/json"}


def _cfg(cfg: dict) -> dict:
    """Deep-dive settings with safe defaults (so settings.yaml need not define them)."""
    dd = (cfg["settings"].get("yutori", {}) or {}).get("deep_dive", {}) or {}
    return {
        "enabled": bool(dd.get("enabled", False)),
        "max_stories": int(dd.get("max_stories", 3)),
        "poll_timeout_seconds": int(dd.get("poll_timeout_seconds", 240)),
        "poll_interval_seconds": int(dd.get("poll_interval_seconds", 10)),
        "base_url": cfg["settings"].get("yutori", {}).get("base_url", "https://api.yutori.com/v1"),
        "request_timeout": int(cfg["settings"].get("yutori", {}).get("timeout_seconds", 60)),
    }


def _build_query(story: dict, org: dict) -> str:
    return (
        f"Research additional context for this news item relevant to {org['name']} "
        f"({org.get('region','')}). Story: \"{story.get('title','')}\". "
        f"Summary: {(story.get('summary') or story.get('content') or '')[:600]}\n\n"
        "Find related developments, relevant figures and dates, and what it implies for "
        f"{org['name']}'s strategy. Prefer primary sources and reporting from the last 30 days."
    )


def _launch(c: dict, query: str, user_tz: str) -> str | None:
    try:
        resp = requests.post(
            c["base_url"].rstrip("/") + CREATE_PATH, headers=_headers(),
            json={"query": query, "output_schema": OUTPUT_SCHEMA,
                  "user_timezone": user_tz, "skip_email": True},
            timeout=c["request_timeout"])
        resp.raise_for_status()
        return resp.json().get("task_id")
    except Exception as exc:
        log.warning("deep-dive launch failed: %s", exc)
        return None


def _fetch(c: dict, task_id: str) -> dict | None:
    """Return the status payload, or None on error."""
    try:
        resp = requests.get(c["base_url"].rstrip("/") + STATUS_PATH.format(task_id=task_id),
                            headers={"X-API-Key": os.environ.get("YUTORI_API_KEY", "").strip()},
                            timeout=c["request_timeout"])
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("deep-dive poll failed (%s): %s", task_id, exc)
        return None


def enrich_stories(stories: list[dict], cfg: dict) -> int:
    """Attach a `deep_dive` dict to up to max_stories stories, in place.

    Returns the number of stories enriched. Never raises."""
    c = _cfg(cfg)
    if not c["enabled"]:
        return 0
    if not os.environ.get("YUTORI_API_KEY", "").strip():
        log.info("deep-dive enabled but YUTORI_API_KEY not set — skipping")
        return 0

    targets = stories[: c["max_stories"]]
    org = cfg["settings"]["org"]
    user_tz = org.get("timezone", "America/New_York")

    # 1) launch all tasks
    pending = {}  # task_id -> story
    for s in targets:
        tid = _launch(c, _build_query(s, org), user_tz)
        if tid:
            pending[tid] = s
    if not pending:
        return 0
    log.info("deep-dive: launched %d research tasks", len(pending))

    # 2) poll until all done or deadline
    deadline = time.time() + c["poll_timeout_seconds"]
    enriched = 0
    while pending and time.time() < deadline:
        time.sleep(c["poll_interval_seconds"])
        for tid in list(pending):
            payload = _fetch(c, tid)
            if not payload:
                continue
            status = payload.get("status")
            if status in ("succeeded", "failed"):
                story = pending.pop(tid)
                if status == "succeeded":
                    sr = payload.get("structured_result") or {}
                    if isinstance(sr, list):
                        sr = sr[0] if sr else {}
                    if sr:
                        story["deep_dive"] = sr
                        # Fold the context into content so synthesis naturally uses it.
                        extra = sr.get("additional_context", "")
                        if extra:
                            story["content"] = (story.get("content") or "") + "\n\nDEEP DIVE: " + extra
                        enriched += 1
                else:
                    log.info("deep-dive task %s failed (%s)", tid, payload.get("rejection_reason"))
    if pending:
        log.warning("deep-dive: %d task(s) did not finish before the %ds deadline — skipped",
                    len(pending), c["poll_timeout_seconds"])
    log.info("deep-dive: enriched %d/%d stories", enriched, len(targets))
    return enriched
