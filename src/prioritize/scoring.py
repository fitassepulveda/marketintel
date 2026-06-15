"""Composite scoring: source weight x category weight x LLM relevance (deck slide 2/4)."""
from __future__ import annotations
import difflib
import math
import re


def _cosine(u: list, v: list) -> float:
    dot = sum(x * y for x, y in zip(u, v))
    nu = math.sqrt(sum(x * x for x in u))
    nv = math.sqrt(sum(y * y for y in v))
    return dot / (nu * nv) if nu and nv else 0.0


def semantic_dedupe(articles: list, vectors: list, threshold: float) -> list:
    """Collapse same-event stories by embedding similarity (meaning, not words).

    `articles` ordered best-first, aligned with `vectors`. Greedy: keep the first
    member of each cluster, drop later items whose vector is within `threshold`
    cosine similarity of one already kept. Generalizes across any wording/topic.
    """
    kept, kept_vecs = [], []
    for a, v in zip(articles, vectors):
        if any(_cosine(v, kv) >= threshold for kv in kept_vecs):
            continue
        kept.append(a)
        kept_vecs.append(v)
    return kept


def _norm_title(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for fuzzy comparison."""
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (title or "").lower()).split())


# Common words that shouldn't count as a "distinctive" shared token for dedup.
_DEDUP_STOPWORDS = {
    "health", "hospital", "hospitals", "system", "systems", "million", "billion",
    "dollar", "dollars", "announces", "announce", "announced", "plans", "plan",
    "care", "center", "centers", "opens", "open", "expands", "expansion",
    "new", "with", "from", "the", "for", "and", "its", "into", "amid", "report",
}
_SCALE = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "m": 1e6, "k": 1e3}


def _money_amounts(text: str) -> set:
    """Normalized dollar figures found in text, e.g. '$400M' and '$400 million' -> 400000000."""
    vals = set()

    def add(num, scale):
        try:
            n = float(num.replace(",", ""))
        except ValueError:
            return
        vals.add(int(round(n * _SCALE.get((scale or "").lower(), 1))))

    for num, scale in re.findall(r"\$\s*(\d[\d.,]*)\s*(billion|bn|b|million|m|k)?", text or "", re.I):
        add(num, scale)
    for num, scale in re.findall(r"(\d[\d.,]*)\s*(billion|million)\b", text or "", re.I):
        add(num, scale)
    return vals


def _sig_tokens(title: str) -> set:
    return {w for w in _norm_title(title).split() if len(w) >= 4 and w not in _DEDUP_STOPWORDS}


def _same_event(a: dict, b: dict, title_threshold: float, token_overlap: float) -> bool:
    ta, tb = a.get("title", ""), b.get("title", "")
    # 1) Near-identical headline.
    if difflib.SequenceMatcher(None, _norm_title(ta), _norm_title(tb)).ratio() >= title_threshold:
        return True
    # 2) Same dollar figure AND a shared distinctive word (e.g. same competitor + amount).
    txt_a = f"{ta} {a.get('summary', '')}"
    txt_b = f"{tb} {b.get('summary', '')}"
    if (_money_amounts(txt_a) & _money_amounts(txt_b)) and (_sig_tokens(ta) & _sig_tokens(tb)):
        return True
    # 3) Headlines share most of their distinctive words — catches the same story
    #    syndicated across feeds with reworded titles (e.g. "Baptist & Amazon One
    #    Medical announce partnership" vs "Amazon One Medical partners with Baptist").
    #    Require >=3 distinctive tokens each so generic short titles can't trivially match.
    sa, sb = _sig_tokens(ta), _sig_tokens(tb)
    if len(sa) >= 3 and len(sb) >= 3:
        if len(sa & sb) / min(len(sa), len(sb)) >= token_overlap:
            return True
    return False


def dedupe_by_title(articles: list, title_threshold: float = 0.90,
                    token_overlap: float = 0.6) -> list:
    """Collapse duplicate stories (same event from different sources/urls).

    Input should be ordered best-first; the first member of each cluster is kept and
    later duplicates dropped. A duplicate is: a near-identical headline, OR a shared
    dollar amount plus a shared word, OR headlines sharing most distinctive words.
    """
    kept = []
    for a in articles:
        if any(_same_event(a, k, title_threshold, token_overlap) for k in kept):
            continue
        kept.append(a)
    return kept


def source_weight(weights: dict, source_name: str) -> float:
    return float(weights.get("source_weights", {}).get(source_name, weights.get("default_source_weight", 6)))


def category_weight(weights: dict, area: str) -> float:
    return float(weights["category_weights"].get(area, 5))


def composite(weights: dict, article: dict, llm_score: float) -> float:
    """Final 0-100 composite score."""
    w = weights["composite"]
    s = source_weight(weights, article["source"]) / 10
    c = category_weight(weights, article["area"]) / 10
    l = max(0.0, min(llm_score, 10.0)) / 10
    return round(100 * (w["source_weight"] * s + w["category_weight"] * c + w["llm_relevance"] * l), 1)
