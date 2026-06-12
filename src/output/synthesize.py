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
  "takeaways": ["3-5 most important developments, one sentence each"],
  "key_question_answers": {"<area>": "1-3 sentence answer to that area's key question based on today's items, or 'No significant developments today.'"},
  "stories": [
    {"title": "...", "area": "...", "source": "...", "url": "...",
     "what_happened": "1-2 sentences",
     "why_it_matters": "1-2 sentences specific to the organization",
     "exposure": "institutional exposure / opportunity in one sentence"}
  ],
  "watch": ["developments to watch in coming days/weeks/months"],
  "actions": ["recommended actions or considerations"]
}
Merge duplicate/related items into a single story. Order stories by importance.
Be concrete, executive-ready, and concise."""


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
        f"Organization: {org['name']} — {org['description']} Region: {org['region']}\n\n"
        f"Key questions by area:\n{kq_txt}\n\nToday's top-ranked items:\n{items_txt}"
    )
    text = strip_fences(client.complete(model, SYSTEM, prompt, max_tokens=max_tokens))
    return json.loads(text)
