"""Composite scoring: source weight x category weight x LLM relevance (deck slide 2/4)."""


def source_weight(weights: dict, source_name: str) -> float:
    return float(weights.get("source_weights", {}).get(source_name, weights.get("default_source_weight", 6)))


def category_weight(weights: dict, area: str) -> float:
    return float(weights["category_weights"].get(area, 5))


def keyword_hits(weights: dict, text: str) -> int:
    text_l = text.lower()
    return sum(1 for kw in weights.get("boost_keywords", []) if kw.lower() in text_l)


def pre_rank(weights: dict, article: dict) -> float:
    """Cheap pre-rank (no LLM) used to choose which items get LLM-scored.
    0-1 scale: source + category + keyword boost."""
    s = source_weight(weights, article["source"]) / 10
    c = category_weight(weights, article["area"]) / 10
    k = min(keyword_hits(weights, f"{article['title']} {article['summary']}"), 3) / 3
    return 0.35 * s + 0.45 * c + 0.20 * k


def composite(weights: dict, article: dict, llm_score: float) -> float:
    """Final 0-100 composite score."""
    w = weights["composite"]
    s = source_weight(weights, article["source"]) / 10
    c = category_weight(weights, article["area"]) / 10
    l = max(0.0, min(llm_score, 10.0)) / 10
    return round(100 * (w["source_weight"] * s + w["category_weight"] * c + w["llm_relevance"] * l), 1)
