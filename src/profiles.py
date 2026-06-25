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


def _keyword_nudge(profile: dict, article: dict) -> float:
    """+3 if any of the profile's interest keywords appears in title/summary."""
    interests = [k.lower() for k in (profile.get("keyword_interests") or [])]
    if not interests:
        return 0.0
    text = f'{article.get("title","")} {article.get("summary","")}'.lower()
    return 3.0 if any(k in text for k in interests) else 0.0


def personal_composite(profile: dict, article: dict) -> float:
    rel = personal_relevance(profile, article)          # 0–10
    score = 10.0 * rel + _area_nudge(profile, article["area"]) + _keyword_nudge(profile, article)
    return round(max(0.0, min(score, 100.0)), 1)


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
