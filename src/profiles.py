"""Per-executive personalization.

Re-ranks an already-scored article pool through one executive's lens, using the
stored six-dimension sub-scores (src/prioritize/subscores.py). No extra LLM
relevance calls per person — adding an executive is essentially free.

Because the house composite is currently 100% LLM relevance (area/source weights
off, see docs/SCORING.md), a personal score here is simply that executive's
WEIGHTED VIEW of the article's sub-scores, on the same 0–100 scale:

    personal_relevance = Σ weight_d · subscore_d            # 0–10
    personal_composite = 10 · personal_relevance            # 0–100
                         + optional small area / keyword nudges

so it stays directly comparable to the global threshold.
"""
from __future__ import annotations

import re

import numpy as np
import yaml

from src import ahp, config
from src.prioritize import subscores

DIMS = subscores.DIMENSIONS


def load_profiles() -> tuple[list[dict], dict]:
    with open(config.CONFIG_DIR / "profiles.yaml") as f:
        raw = yaml.safe_load(f)
    return raw.get("profiles", []), raw.get("defaults", {})


def active_profiles() -> list[dict]:
    profiles, defaults = load_profiles()
    out = []
    for p in profiles:
        if not p.get("active"):
            continue
        merged = {**defaults, **p}
        merged["_weights"] = dimension_weights(merged)
        out.append(merged)
    return out


def dimension_weights(profile: dict) -> dict:
    """{dimension: weight} summing to 1. Uses ahp_pairwise (eigenvector-derived)
    if present, else subscore_weights, else uniform."""
    if "ahp_pairwise" in profile:
        matrix = ahp.matrix_from_pairwise(DIMS, profile["ahp_pairwise"])
        w = ahp.ahp_weights(matrix)["weights"]
        return dict(zip(DIMS, w))
    if "subscore_weights" in profile:
        raw = np.array([float(profile["subscore_weights"].get(d, 0)) for d in DIMS])
        if raw.sum() == 0:
            raw = np.ones(len(DIMS))
        return dict(zip(DIMS, raw / raw.sum()))
    u = 1.0 / len(DIMS)
    return dict.fromkeys(DIMS, u)


def personal_relevance(profile: dict, article: dict) -> float:
    """0–10 weighted average of an article's sub-scores for this profile."""
    ss = article.get("subscores") or {}
    w = profile["_weights"]
    return sum(w[d] * float(ss.get(d, 0)) for d in DIMS)


def _area_nudge(profile: dict, area: str) -> float:
    """Optional small additive nudge (points) if the profile sets area_weights.
    Neutral at 7/10; range roughly -6..+3. Off entirely when area_weights absent."""
    overrides = profile.get("area_weights") or {}
    if area not in overrides:
        return 0.0
    return float(overrides[area]) - 7.0


# Nudge sizes (points on the 0-100 personal scale).
KEYWORD_BONUS = 3.0          # per matching interest keyword
KEYWORD_BONUS_CAP = 6.0      # ...capped, so a keyword-stuffed headline can't run away
KEYWORD_PENALTY = 5.0        # per matching avoid keyword
KEYWORD_PENALTY_CAP = 15.0   # a de-prioritised topic should be able to sink a story


def _matches(terms: list[str], text: str) -> list[str]:
    """Terms present in `text` as WHOLE WORDS (or whole phrases).

    Word boundaries matter: plain substring matching made "MOB" hit "mobility",
    "ASC" hit "Ascension", and — critically — "pharma" hit "pharmacy", which
    would make a pharma penalty cancel a pharmacy boost.
    """
    hits = []
    for t in terms:
        t = t.strip().lower()
        if not t:
            continue
        if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text):
            hits.append(t)
    return hits


def _keyword_nudge(profile: dict, article: dict) -> float:
    """Net keyword adjustment: interests add, avoid-terms subtract.

    `keyword_interests` -> +KEYWORD_BONUS each (capped)
    `keyword_avoid`     -> -KEYWORD_PENALTY each (capped)
    Both are matched on whole words against title + summary.
    """
    interests = profile.get("keyword_interests") or []
    avoid = profile.get("keyword_avoid") or []
    if not interests and not avoid:
        return 0.0
    text = f'{article.get("title","")} {article.get("summary","")}'.lower()
    bonus = min(len(_matches(interests, text)) * KEYWORD_BONUS, KEYWORD_BONUS_CAP)
    penalty = min(len(_matches(avoid, text)) * KEYWORD_PENALTY, KEYWORD_PENALTY_CAP)
    return bonus - penalty


def keyword_hits(profile: dict, article: dict) -> tuple[list[str], list[str]]:
    """(matched interests, matched avoid-terms) — for debugging and dry-run output."""
    text = f'{article.get("title","")} {article.get("summary","")}'.lower()
    return (_matches(profile.get("keyword_interests") or [], text),
            _matches(profile.get("keyword_avoid") or [], text))


def personal_composite(profile: dict, article: dict) -> float:
    """This profile's 0–100 score for an article.

    TWO SOURCES OF RELEVANCE, in priority order:

    1. `article["profile_score"]` — a 0-10 relevance judged by the LLM against this
       person's `role_description` (src/prioritize/profile_relevance.py). This is the
       real per-person signal, and it is on the SAME scale as the house composite,
       so shared thresholds and tier bands keep their meaning.
    2. Otherwise, the weighted average of the shared org-level sub-scores. Kept as a
       fallback for profiles with no role_description — but note it re-weights a
       vector produced without any knowledge of the person, so it moves the ranking
       far less than the weights suggest.

    Keyword and area nudges apply to both.
    """
    ps = article.get("profile_score")
    rel = float(ps) if ps is not None else personal_relevance(profile, article)
    score = 10.0 * rel + _area_nudge(profile, article["area"]) + _keyword_nudge(profile, article)
    return round(max(0.0, min(score, 100.0)), 1)


def uses_semantic_scoring(profile: dict) -> bool:
    """True when this profile is scored against its own role description."""
    return bool((profile.get("role_description") or "").strip())


def rank_for_profile(profile: dict, weights_cfg: dict, articles: list[dict]) -> list[dict]:
    """Articles above this profile's threshold, ranked, capped to max_stories."""
    threshold = float(profile.get("threshold", weights_cfg.get("score_threshold", 55)))
    out = []
    for a in articles:
        score = personal_composite(profile, a)
        if score >= threshold:
            out.append({**a, "personal_relevance": round(personal_relevance(profile, a), 1),
                        "personal_composite": score})
    out.sort(key=lambda x: x["personal_composite"], reverse=True)
    return out[: int(profile.get("max_stories", 8))]
