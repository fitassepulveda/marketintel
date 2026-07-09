"""Briefing synthesis: turn top-ranked items into the executive summary format."""
from __future__ import annotations
import json
import logging

from src.llm_client import LLMClient, strip_fences

log = logging.getLogger("output.synthesize")

SYSTEM = """You write a daily market intelligence briefing for healthcare system executives.
Use ONLY the provided items — do not invent facts. Follow this structure exactly and respond
with JSON:

TONE (applies throughout, especially why_it_matters): measured and analytical, never alarmist.
You are reading external news reports, NOT the organization's internal strategy, financials, or
operating plans — you do not know how a given development actually lands internally, so do not
write as if you do. Avoid dramatic/urgent language ("threatens," "jeopardizes," "erodes,"
"existential," "urgent," "must act now," "at risk") and avoid stating a specific internal impact
as settled fact. Prefer hedged, exploratory framing ("could," "may," "worth monitoring," "may be
worth weighing") over declarative alarm. State what happened and its plausible strategic
relevance without overstating certainty, scale, or severity — let leadership judge the actual
weight given context this analysis doesn't have.
{
  "takeaways": ["3-5 punchy, MBB-consultant-style bullets that FUSE the key development with its 'so what' / the recommended action — each ties an insight to what leadership should consider doing, so takeaways and actions read as one thought. Sharp, concrete, one sentence each. Use **double asterisks** to bold the 2-4 highest-impact words or phrases in each bullet (strategic bolding)."],
  "key_question_answers": {"<area>": "1-3 sentence answer to that area's key question based on today's items, or 'No significant developments today.'"},
  "stories": [
    {"id": 0,
     "title": "...",
     "topline": "REQUIRED — a ONE-SENTENCE executive topline that replaces the article headline in the report: state what happened phrased so an executive immediately grasps the IMPACT (not the publisher's headline wording). One line, ~12-22 words, concrete and specific.",
     "area": "...", "source": "...", "url": "...",
     "what_happened": "1-2 sentences. PRESERVE the concrete specifics the item provides — square footage, bed counts, dollar amounts, locations, dates, named parties. Never drop a hard figure for smoother prose; executives read for the numbers. HARD RULE: if the item's key facts include high-importance figures — capital investment / dollar amounts, square footage, bed capacity — they MUST appear in what_happened, and this rule OVERRIDES the sentence cap: add a third sentence if that's what it takes to fit them.",
     "why_it_matters": "REQUIRED, never blank: 1-2 sentences on the strategic significance to the organization (refer to it by its short name) — why leadership should care. FOLD IN the specific institutional risk OR opportunity this creates (these concepts overlap, so combine them here rather than separating them out). Keep it measured and hedged, not alarmist (see TONE above) — this is a plausible external read, not a verdict on internal impact.",
     "exposure": "OPTIONAL — leave as an empty string \"\". The institutional risk/opportunity is now folded into why_it_matters; do not duplicate it here.",
     "watch_next": "REQUIRED — WHAT UHEALTH SHOULD CONSIDER (rendered under that label, so do NOT restate the label in your text). Frame it as an OPPORTUNITY or CONSIDERATION, not a precise directive — exploratory and suggestive rather than declarative (e.g. 'Evaluate opportunities to enhance UHealth's community-benefit impact and awareness across social-determinants-of-health areas.'). It may be a thing to watch (with a rough time horizon) or 'no action needed — monitor only' when that fits. One sentence, specific to UHealth but not over-precise.",
     "context": "OPTIONAL — include ONLY when the item provides 'additional research context': 1-2 sentences of its most decision-useful background (prior/related developments, scale comparisons, market position), with specifics. Empty string otherwise. Do not repeat what_happened.",
     "coverage_label": "a short descriptive label for the source link, e.g. 'STAT reporting on pharma job shifts'"}
  ],
  "watch": ["developments to watch in coming days/weeks/months"],
  "actions": ["recommended actions — but PREFER to fold each action directly into the matching takeaway above so the two read as one; use this list only for any action not already captured there. May be empty."]
}
When an item includes 'additional research context' or 'key facts', you MUST put them to
work: fold their most relevant specifics into what_happened, why_it_matters, watch_next,
and the context field. That material was commissioned specifically to sharpen this
briefing — a story that ignores it and paraphrases the headline is a failure.
Produce ONE story object for EACH item provided, preserving that item's exact url.
CRITICAL: each story's "id" MUST be the integer [n] index of the source item it was built
from, copied exactly — this is how stories are matched back to their articles (long URLs
are easy to mistype, so the id is the ground truth).
Do NOT drop, omit, or merge items — duplicates have already been removed upstream.
Order stories by importance. Be concrete, executive-ready, and concise."""


def build_briefing(client: LLMClient, model: str, max_tokens: int, org: dict,
                   key_questions: dict, articles: list[dict], style: str = "") -> dict:
    def _item(i, a):
        # full_text / extracted_facts / research_context come from the Yutori enrichment
        # step (deep_dive) when on. extracted_facts carries the discrete figures (sq ft,
        # beds, dollar amounts, dates) — pass it explicitly: full_text is truncated and
        # summaries vary, so this list is the reliable channel for hard numbers.
        ft = f'\nfull article text (extracted): {a["full_text"][:3500]}' if a.get("full_text") else ""
        kf = (f'\nkey facts (extracted from the article — treat as reliable): '
              + "; ".join(str(f) for f in a["extracted_facts"][:10])) if a.get("extracted_facts") else ""
        rc = f'\nadditional research context: {a["research_context"]}' if a.get("research_context") else ""
        return (f'[{i}] area={a["area"]} | score={a["composite_score"]} | source={a["source"]}\n'
                f'title: {a["title"]}\nurl: {a["url"]}\n'
                f'summary: {(a["summary"] or a["content"] or "")[:800]}{kf}{ft}{rc}\n'
                f'relevance rationale: {a.get("llm_rationale", "")}')
    items_txt = "\n\n".join(_item(i, a) for i, a in enumerate(articles))
    kq_txt = "\n".join(f"- {area}: {q}" for area, q in key_questions.items())
    prompt = (
        f"Organization: {org['name']} (short name: {org.get('short_name', org['name'])}) — "
        f"{org['description']} Region: {org['region']}\n\n"
        f"Key questions by area:\n{kq_txt}\n\nToday's top-ranked items:\n{items_txt}"
    )
    # Style guide (from config) shapes HOW it's written — tone, certainty, phrasing —
    # without changing the JSON structure. Tunable with no code change.
    system = SYSTEM if not style.strip() else f"{SYSTEM}\n\nWRITING STYLE (follow strictly):\n{style.strip()}"
    text = strip_fences(client.complete(model, system, prompt, max_tokens=max_tokens))
    return json.loads(text)
