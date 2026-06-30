"""Briefing synthesis: turn top-ranked items into the executive summary format."""
from __future__ import annotations
import json
import logging

from src.llm_client import LLMClient, strip_fences

log = logging.getLogger("output.synthesize")

SYSTEM = """You write a daily market intelligence briefing for healthcare system executives.
Use ONLY the provided items — do not invent facts. Follow this structure exactly and respond
with JSON:
{
  "takeaways": ["3-5 punchy, MBB-consultant-style bullets that FUSE the key development with its 'so what' / the recommended action — each ties an insight to what leadership should consider doing, so takeaways and actions read as one thought. Sharp, concrete, one sentence each. Use **double asterisks** to bold the 2-4 highest-impact words or phrases in each bullet (strategic bolding)."],
  "key_question_answers": {"<area>": "1-3 sentence answer to that area's key question based on today's items, or 'No significant developments today.'"},
  "stories": [
    {"title": "...",
     "topline": "REQUIRED — a ONE-SENTENCE executive topline that replaces the article headline in the report: state what happened phrased so an executive immediately grasps the IMPACT (not the publisher's headline wording). One line, ~12-22 words, concrete and specific.",
     "area": "...", "source": "...", "url": "...",
     "what_happened": "1-2 sentences",
     "why_it_matters": "REQUIRED, never blank: 1-2 sentences on the strategic significance to the organization (refer to it by its short name) — why leadership should care. FOLD IN the specific institutional risk OR opportunity this creates (these concepts overlap, so combine them here rather than separating them out).",
     "exposure": "OPTIONAL — leave as an empty string \"\". The institutional risk/opportunity is now folded into why_it_matters; do not duplicate it here.",
     "watch_next": "REQUIRED — WHAT UHEALTH SHOULD CONSIDER (rendered under that label, so do NOT restate the label in your text). Frame it as an OPPORTUNITY or CONSIDERATION, not a precise directive — exploratory and suggestive rather than declarative (e.g. 'Evaluate opportunities to enhance UHealth's community-benefit impact and awareness across social-determinants-of-health areas.'). It may be a thing to watch (with a rough time horizon) or 'no action needed — monitor only' when that fits. One sentence, specific to UHealth but not over-precise.",
     "coverage_label": "a short descriptive label for the source link, e.g. 'STAT reporting on pharma job shifts'"}
  ],
  "watch": ["developments to watch in coming days/weeks/months"],
  "actions": ["recommended actions — but PREFER to fold each action directly into the matching takeaway above so the two read as one; use this list only for any action not already captured there. May be empty."]
}
Produce ONE story object for EACH item provided, preserving that item's exact url.
Do NOT drop, omit, or merge items — duplicates have already been removed upstream.
Order stories by importance. Be concrete, executive-ready, and concise."""


def build_briefing(client: LLMClient, model: str, max_tokens: int, org: dict,
                   key_questions: dict, articles: list[dict]) -> dict:
    items_txt = "\n\n".join(
        f'[{i}] area={a["area"]} | score={a["composite_score"]} | source={a["source"]}\n'
        f'title: {a["title"]}\nurl: {a["url"]}\n'
        f'summary: {(a["summary"] or a["content"] or "")[:800]}\n'
        f'relevance rationale: {a.get("llm_rationale", "")}'
        for i, a in enumerate(articles)
    )
    kq_txt = "\n".join(f"- {area}: {q}" for area, q in key_questions.items())
    prompt = (
        f"Organization: {org['name']} (short name: {org.get('short_name', org['name'])}) — "
        f"{org['description']} Region: {org['region']}\n\n"
        f"Key questions by area:\n{kq_txt}\n\nToday's top-ranked items:\n{items_txt}"
    )
    text = strip_fences(client.complete(model, SYSTEM, prompt, max_tokens=max_tokens))
    return json.loads(text)
