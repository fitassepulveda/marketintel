"""Composite scoring: source weight x category weight x LLM relevance (deck slide 2/4)."""
from __future__ import annotations
import difflib
import math
import re


def _term_in_sentence(term: str, sentence: str) -> bool:
    """Whole-word, case-insensitive match (so 'FIU' / 'Baptist' don't match inside words)."""
    return re.search(r"\b" + re.escape(term) + r"\b", sentence, re.IGNORECASE) is not None


def forced_floor(text: str, rules: list) -> tuple[float | None, str]:
    """Deterministic score floor applied AFTER LLM scoring (config: briefing.forced_floor_rules).

    Each rule has `same_sentence_all`: a list of term-groups. A rule fires if a SINGLE
    sentence of `text` contains at least one term from EVERY group (groups are OR within,
    AND across). Returns (highest firing `score`, its `reason`), or (None, "").
    e.g. groups [["FIU","Florida International University"], ["Baptist"]] fire on a sentence
    naming FIU (or its full name) AND Baptist."""
    if not rules or not text:
        return None, ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    best_score, best_reason = None, ""
    for rule in rules:
        groups = rule.get("same_sentence_all") or []
        score = rule.get("score")
        if not groups or score is None:
            continue
        for s in sentences:
            if all(any(_term_in_sentence(t, s) for t in group) for group in groups):
                if best_score is None or score > best_score:
                    best_score, best_reason = float(score), rule.get("reason", "")
                break
    return best_score, best_reason


def _cosine(u: list, v: list) -> float:
    dot = sum(x * y for x, y in zip(u, v))
    nu = math.sqrt(sum(x * x for x in u))
    nv = math.sqrt(sum(y * y for y in v))
    return dot / (nu * nv) if nu and nv else 0.0


def semantic_dedupe_track(articles: list, vectors: list, threshold: float,
                          seed: list = (), seed_vectors: list = ()) -> tuple[list, list]:
    """Collapse same-event stories by embedding similarity (meaning, not words).

    `articles` ordered best-first, aligned with `vectors`. Greedy: keep the first
    member of each cluster, drop later items whose vector is within `threshold`
    cosine similarity of one already kept.

    `seed`/`seed_vectors` are already-briefed HISTORY stories: they are treated as
    pre-kept (checked first, never returned), so any candidate matching one is a
    re-report of an event we've already shown and gets dropped.

    Returns (kept, absorbed): `kept` is today's surviving articles; `absorbed` is
    a list of (dropped_article, absorbing_article) pairs — the absorber is either
    a seed/history item or an earlier kept candidate. Callers use it to stamp
    dropped duplicates as briefed alongside their surviving copy, so a dropped
    copy can never resurface solo on a later day (Cleveland Clinic $25M, 7/24)."""
    kept_all, kept_vecs = list(seed), list(seed_vectors)
    kept, absorbed = [], []
    for a, v in zip(articles, vectors):
        hit = None
        for k, kv in zip(kept_all, kept_vecs):
            if _cosine(v, kv) >= threshold:
                hit = k
                break
        if hit is not None:
            absorbed.append((a, hit))
            continue
        kept_all.append(a)
        kept_vecs.append(v)
        kept.append(a)
    return kept, absorbed


def semantic_dedupe(articles: list, vectors: list, threshold: float) -> list:
    """Back-compat wrapper around semantic_dedupe_track (no history, no tracking)."""
    kept, _ = semantic_dedupe_track(articles, vectors, threshold)
    return kept


def _strip_source_suffix(title: str) -> str:
    """Drop a trailing outlet attribution: "... if Amendment 3 passes - WLRN" -> "... passes".

    Used ONLY for measure extraction. Station names carry numbers of their own — "WPLG
    Local 10", "NBC 6" — and without this any two stories from one station would share a
    bogus "measure". Stripping the suffix everywhere was tried and reverted: it made the
    other rules merge a Baptist hospital opening with an unrelated Baptist gift, and broke
    a real merge that depended on a shared word. Only a short trailing chunk is removed.
    """
    parts = re.split(r"\s+[-–—]\s+", title or "")
    if len(parts) > 1 and 0 < len(parts[-1].split()) <= 5 and len(parts[0].split()) >= 4:
        return " ".join(parts[:-1])
    return title or ""


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


# NAMED MEASURES (added 2026-09-01). A statute, rule or ballot item is identified by its
# NUMBER, and the number is the one thing two outlets keep when they reframe everything
# else: "Florida's public hospitals could lose millions if Amendment 3 passes" and "Public
# hospitals face cutbacks if Florida property taxes are reduced under Amendment 3" are the
# same report, but share only 3 distinctive words (overlap 0.30 against a 0.60 gate) — and
# _sig_tokens threw away the "3", because it drops anything shorter than 4 characters. Both
# copies shipped in the same briefing, one in the top stories and one in "Also worth noting".
#
# A bare numeral is far too ambiguous to match on ("3 ways", "top 10", "26 systems"), so the
# number is kept WITH THE WORD BEFORE IT — "amendment 3", "hr 1", "cy 2027" — and a shared
# measure only counts as a match alongside another shared distinctive word. That is the same
# shape as the shared-dollar-amount rule: a strong identifier plus one corroborating token.
# Words that make a following number a QUANTITY, a DATE or a LIST POSITION rather than the
# NAME of a measure: "top 10", "in 2026", "involving 76,000". A measure is named by a noun
# ("Amendment 3", "HR 1", "CY 2027"), so prepositions and counting words never lead one.
_MEASURE_LEAD_STOPWORDS = {
    "top", "best", "first", "next", "last", "these", "those", "another",
    "in", "on", "at", "of", "to", "for", "by", "from", "with", "and", "the",
    "up", "down", "over", "under", "about", "nearly", "than", "plus", "involving",
    "including", "is", "are", "was", "were", "has", "have", "had", "adds", "hits",
}


def _measure_tokens(text: str) -> set:
    """Word+number pairs naming a measure, e.g. {'amendment 3', 'hr 1', 'cy 2027'}.

    Applied to TITLES ONLY. Summaries are full of incidental numbers ("in 2026",
    "involving 76,000 workers") that are quantities, not names.

    Matched on the normalized title (punctuation stripped), so "H.R. 1", "HR 1" and
    "Amendment 3," all normalize the same way. Leading words that make a number a list
    position rather than a name ("top 10") are excluded.
    """
    out = set()
    for lead, num in re.findall(r"\b([a-z]{2,})\s+(\d{1,4})\b",
                                _norm_title(_strip_source_suffix(text))):
        if lead in _MEASURE_LEAD_STOPWORDS:
            continue
        out.add(f"{lead} {num}")
    return out


def _same_event(a: dict, b: dict, title_threshold: float, token_overlap: float) -> bool:
    ta, tb = a.get("title", ""), b.get("title", "")
    # VETO FIRST: the same kind of measure carrying DIFFERENT numbers means these are
    # different measures, however alike the wording ("Amendment 3" vs "Amendment 5",
    # "CY 2027 rule" vs "CY 2026 rule", "phase 2" vs "phase 3"). Without this the
    # distinctive-word test in (3) below merges them, since everything except the number
    # is identical — the number is exactly what the old tokenizer was throwing away.
    ma_all, mb_all = _measure_tokens(ta), _measure_tokens(tb)
    for lead in {m.split()[0] for m in ma_all} & {m.split()[0] for m in mb_all}:
        nums_a = {m.split()[1] for m in ma_all if m.startswith(lead + " ")}
        nums_b = {m.split()[1] for m in mb_all if m.startswith(lead + " ")}
        if not (nums_a & nums_b):
            return False
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
    # 2b) Same NAMED MEASURE (statute / rule / ballot item) AND a shared distinctive word.
    #     Catches two outlets covering one policy story through different frames. The
    #     corroborating word must not be the measure's own lead word, or "phase 2" would
    #     corroborate itself and merge two unrelated trials.
    shared_measures = ma_all & mb_all
    if shared_measures:
        # The corroborating word must not be the measure's own lead word (or "phase 2"
        # corroborates itself) and must not be a bare number (or a shared year corroborates
        # any two listicles). Station numbers never reach here — _measure_tokens strips the
        # outlet suffix, so "WPLG Local 10" is not a measure.
        leads = {m.split()[0] for m in shared_measures}
        corroborating = {w for w in (_sig_tokens(ta) & _sig_tokens(tb)) - leads
                         if not w.isdigit()}
        if corroborating:
            return True
    sa, sb = _sig_tokens(ta), _sig_tokens(tb)
    if len(sa) >= 3 and len(sb) >= 3:
        if len(sa & sb) / min(len(sa), len(sb)) >= token_overlap:
            return True
    return False


def dedupe_by_title_track(articles: list, title_threshold: float = 0.90,
                          token_overlap: float = 0.6, seed: list = ()) -> tuple[list, list]:
    """Keyword-fallback twin of semantic_dedupe_track (same seed/tracking contract).

    Input ordered best-first; the first member of each cluster is kept and later
    duplicates dropped. A duplicate is: a near-identical headline, OR a shared
    dollar amount plus a shared word, OR headlines sharing most distinctive words.
    `seed` items (already-briefed history) are pre-kept and never returned.
    Returns (kept, absorbed) — see semantic_dedupe_track."""
    kept_all = list(seed)
    kept, absorbed = [], []
    for a in articles:
        hit = None
        for k in kept_all:
            if _same_event(a, k, title_threshold, token_overlap):
                hit = k
                break
        if hit is not None:
            absorbed.append((a, hit))
            continue
        kept_all.append(a)
        kept.append(a)
    return kept, absorbed


def dedupe_by_title(articles: list, title_threshold: float = 0.90,
                    token_overlap: float = 0.6) -> list:
    """Back-compat wrapper around dedupe_by_title_track (no history, no tracking)."""
    kept, _ = dedupe_by_title_track(articles, title_threshold, token_overlap)
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
