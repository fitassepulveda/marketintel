"""LLM relevance scoring: each item scored 0-10 against its area's key question."""
from __future__ import annotations
import json
import logging

from src.llm_client import LLMClient, strip_fences

log = logging.getLogger("prioritize.llm")

SYSTEM = """You score news items for a healthcare system's executive intelligence briefing.
Score each item 0-10 for strategic relevance to the organization described, judged against
the intelligence area's key question. Consider monetary impact, operational impact, and
time-sensitivity. For items in the south_florida_competitive area, weigh these factors:
capital investment size, physical capacity expansion, geographic proximity to the
organization's region, and impact on high-value service lines.
Respond ONLY with a JSON array, one object per item, in the same order:
[{"i": <index>, "score": <0-10>, "why": "<one sentence>"}]"""


def score_batch(client: LLMClient, model: str, org: dict, key_questions: dict,
                articles: list[dict], batch_size: int = 12) -> list[tuple[float, str]]:
    """Return [(score, rationale)] aligned with `articles`."""
    results: list[tuple[float, str]] = [(0.0, "not scored")] * len(articles)
    for start in range(0, len(articles), batch_size):
        batch = articles[start:start + batch_size]
        items_txt = "\n".join(
            f'[{i}] area={a["area"]} | key question: {key_questions.get(a["area"], "")}\n'
            f'    source={a["source"]} | title: {a["title"]}\n'
            f'    summary: {a["summary"][:500]}'
            for i, a in enumerate(batch)
        )
        prompt = (
            f"Organization: {org['name']} — {org['description']} Region: {org['region']}\n\n"
            f"Items:\n{items_txt}"
        )
        try:
            text = strip_fences(client.complete(model, SYSTEM, prompt, max_tokens=1500))
            for obj in json.loads(text):
                idx = start + int(obj["i"])
                if start <= idx < start + len(batch):
                    results[idx] = (float(obj["score"]), str(obj.get("why", "")))
        except Exception as exc:
            log.warning("LLM scoring batch failed (%s); items keep score 0", exc)
    return results
