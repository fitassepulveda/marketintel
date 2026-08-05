"""Background note: 'is this actually new, or has it been running for a while?'

The briefing scores every story on its own and its DB starts mid-June 2026, so a fresh
article about a long-running issue (a merger effort, a regulatory fight, litigation) reads
as a brand-new development. This module is the correction.

For each story selected for the briefing, this:
  1. SEARCHES THE LIVE WEB via Gemini (Google Search grounding) for the EARLIER reporting
     on the same underlying issue, returning the real source links it used,
  2. ALSO gives the model related PRIOR articles already in our database (cheap keyword
     retrieval) so it can flag ongoing/earlier coverage, and
  3. attaches an `additional_context` block (one-line background + prior-coverage links +
     web links) to the matching briefing story.

The email renders a "Background" line ONLY when the story genuinely has a history, so a
story with no prior coverage looks exactly as it does today.

Config-gated (settings.additional_context.enabled, default OFF) and fully fail-safe:
any error leaves the briefing unchanged.
"""
from __future__ import annotations
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from src.llm_client import strip_fences

log = logging.getLogger("prioritize.related_context")

_STOP = {
    "health", "hospital", "hospitals", "system", "systems", "million", "billion",
    "care", "center", "centers", "new", "with", "from", "the", "for", "and", "its",
    "into", "amid", "report", "announces", "announced", "plans", "after", "over",
    "this", "that", "will", "says", "their", "more", "than", "what", "how",
}


def _tokens(text: str) -> set:
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split()
            if len(w) >= 4 and w not in _STOP}


def _norm_url(u: str) -> str:
    return re.sub(r"[?#].*$", "", (u or "").strip().lower().rstrip("/"))


def find_related_prior(con, story: dict, exclude_urls: set,
                       lookback_days: int, max_candidates: int) -> list[dict]:
    """Articles in the DB (within lookback) that share distinctive words with the story
    and are not part of today's selected set. Ranked by shared-word count."""
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    story_tokens = _tokens(f'{story.get("title","")} {story.get("summary","")}')
    if len(story_tokens) < 2:
        return []
    self_url = _norm_url(story.get("url", ""))
    scored = []
    for r in con.execute(
        "SELECT title, summary, url, published, fetched, briefed_on FROM articles "
        "WHERE fetched >= ?", (since,)
    ).fetchall():
        d = dict(r)
        nu = _norm_url(d.get("url", ""))
        if nu == self_url or nu in exclude_urls:
            continue
        overlap = story_tokens & _tokens(f'{d.get("title","")} {d.get("summary","")}')
        if len(overlap) >= 2:
            d["_overlap"] = len(overlap)
            scored.append(d)
    scored.sort(key=lambda x: x["_overlap"], reverse=True)
    return scored[:max_candidates]


SYSTEM = """You add a one-line BACKGROUND note to a healthcare executive briefing.

Your job is to answer ONE question: is this CURRENT story a genuinely new development, or is
it the latest installment in a storyline that has been running for a while? The briefing
scores each story on its own and has no memory before mid-2026, so a fresh article about an
old issue can look like breaking news. You are the correction for that.

You have two inputs: (1) SEARCH THE LIVE WEB for the EARLIER reporting on this same
underlying issue — deliberately look for the oldest relevant coverage, not just the most
recent, and note when the story began and what has already happened (prior attempts,
votes, filings, rulings, deals that closed or collapsed); and (2) a numbered list of PRIOR
articles already in our database.

Write the background as ONE sentence, maximum two if a date and an outcome will not fit in
one. Lead with when the storyline started or when the last significant step occurred, and
include specific dates. Do not restate the current story, do not speculate, and do not
explain implications — background only.

Set has_context FALSE when the story is genuinely new, when the only earlier coverage is
the announcement of this same event, or when you cannot find real prior reporting. A story
with no history should get no note. Be strict and factual: ignore loosely-related or
generic matches, base every claim on what you actually found (web) or were given (prior
articles), and never invent sources.

End your answer with a single line of JSON (and nothing after it):
{"has_context": true|false, "summary": "one-sentence background note with dates, or empty",
 "related_indices": [indices of genuinely related PRIOR articles]}"""


def _parse_trailing_json(text: str) -> dict:
    """Grounded answers are prose + a trailing JSON line; pull the last {...} out."""
    t = strip_fences(text)
    start = t.rfind("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except Exception:
            pass
    return {}


def assess(client, model: str, org: dict, story: dict, candidates: list[dict]) -> dict | None:
    """Web-search + prior-DB context for one story. Returns {summary, related, web} or None."""
    listing = "\n".join(
        f'[{i}] ({(c.get("published") or c.get("fetched") or "")[:10]}) {c.get("title","")} — '
        f'{(c.get("summary") or "")[:200]}'
        for i, c in enumerate(candidates)
    ) or "(none on file)"
    prompt = (
        f"Organization: {org['name']} ({org.get('region','')}).\n\n"
        f'CURRENT story: {story.get("title","")}\n'
        f'{(story.get("summary") or story.get("content") or "")[:500]}\n\n'
        f"PRIOR articles already in our database:\n{listing}"
    )
    try:
        text, web_sources = client.web_research(model, SYSTEM, prompt, max_tokens=900)
    except Exception as exc:
        log.warning("additional-context web research failed: %s", exc)
        return None

    data = _parse_trailing_json(text)
    summary = str(data.get("summary", "")).strip()
    if not summary:
        # Couldn't parse JSON — use the prose answer (minus any trailing JSON) as the note.
        prose = strip_fences(text)
        cut = prose.rfind("{")
        summary = (prose[:cut] if cut > 40 else prose).strip()
    has_context = bool(data.get("has_context", bool(summary)))

    idxs = [i for i in data.get("related_indices", []) if isinstance(i, int) and 0 <= i < len(candidates)]
    related = [{"title": candidates[i].get("title", ""),
                "url": candidates[i].get("url", ""),
                "date": (candidates[i].get("published") or candidates[i].get("fetched") or "")[:10]}
               for i in idxs]
    web = [{"title": s.get("title", ""), "url": s.get("uri", "")} for s in (web_sources or [])][:5]

    if not has_context or (not summary and not related and not web):
        return None
    return {"summary": summary, "related": related, "web": web}


def add_context(con, client, cfg, top_stories: list[dict], briefing: dict) -> int:
    """Attach `additional_context` to briefing stories that have meaningful prior
    coverage. Returns the count enriched. Never raises."""
    ac_cfg = cfg["settings"].get("additional_context", {}) or {}
    if not ac_cfg.get("enabled", False):
        return 0
    if client is None:
        return 0
    model = cfg["settings"]["llm"]["models"][cfg["settings"]["llm"]["provider"]]["scoring"]
    org = cfg["settings"]["org"]
    lookback = int(ac_cfg.get("lookback_days", 120))
    max_candidates = int(ac_cfg.get("max_related", 6))
    max_stories = int(ac_cfg.get("max_stories", 12))
    exclude = {_norm_url(s.get("url", "")) for s in top_stories}
    top_stories = top_stories[:max_stories]

    # index briefing stories by normalized url + title for attachment
    b_by_url, b_by_title = {}, {}
    for bs in briefing.get("stories", []):
        if bs.get("url"):
            b_by_url[_norm_url(bs["url"])] = bs
        if bs.get("title"):
            b_by_title[str(bs["title"]).strip().lower()] = bs

    enriched = 0
    for story in top_stories:
        try:
            cands = find_related_prior(con, story, exclude, lookback, max_candidates)
            result = assess(client, model, org, story, cands)
            if not result:
                continue
            target = (b_by_url.get(_norm_url(story.get("url", "")))
                      or b_by_title.get(str(story.get("title", "")).strip().lower()))
            if target is not None:
                target["additional_context"] = result
                enriched += 1
        except Exception as exc:
            log.warning("additional-context skipped for one story: %s", exc)
    log.info("additional-context: enriched %d/%d stories", enriched, len(top_stories))
    return enriched
