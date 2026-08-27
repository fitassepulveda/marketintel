"""Per-profile semantic relevance scoring.

The house scorer (llm_relevance) asks: "how relevant is this to UHealth, judged
against this intelligence area's key question?" — one score, shared by everyone.

This module asks the SAME question through ONE PERSON'S lens: "how relevant is
this to someone whose job is <role_description>?" It is the same mechanism the
strategy-team briefing already uses, just pointed at an individual.

Why this rather than weighting the shared sub-scores: weights only re-average a
vector that was produced without any knowledge of the person, which measurably
barely moves the ranking. A score judged against the role description is a real
per-person judgment — and it lands on the SAME 0-10 scale as the house score, so
existing thresholds and tier bands keep their meaning.

COST: one extra scoring pass per profile that defines a role_description
(~5 batched requests/day at current volume). Profiles without one are untouched.

CONFIDENTIALITY: `role_description` and `relevance_guidance` are sent to the LLM
provider on every run. On an unpaid API tier that content may be used for model
training and read by human reviewers, so these fields must describe the role in
GENERAL, non-confidential terms — topic shape, not internal plans, figures,
timelines or named projects.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone

from src.llm_client import LLMClient, QuotaExhausted, strip_fences
from src.prioritize.llm_relevance import ScoringUnavailable

log = logging.getLogger("prioritize.profile")

# Bump when SYSTEM or the stored shape changes, so old scores are not reused.
PROFILE_SCORE_VERSION = 1

SYSTEM = """You score news items for ONE executive's personal intelligence briefing.

You are given the organization, then a description of THIS person's role — what they
own, decide, and are working on. Score each item 0-10 for relevance TO THAT PERSON,
not to the organization in general. An item that matters enormously to the organization
but has nothing to do with this person's remit scores LOW. An item that looks minor
organization-wide but lands directly in their remit scores HIGH.

Weight ACTIONABILITY for this person specifically: higher when it implies a decision,
response, or plan THEY would own; lower when it is passive, informational, or clearly
someone else's remit.

USE ONE DECIMAL PLACE. Score to a tenth (e.g. 7.4, 6.8, 8.3), never to a whole number.
The decimal is how items that would otherwise share an integer get ranked against each
other. Reserve above 8.0 for items genuinely warranting this person's attention today.

Respond ONLY with a JSON array, one object per item, in the same order:
[{"i": <index>, "score": <0.0-10.0, one decimal>, "why": "<one sentence, in terms of
this person's remit>"}]"""


def ensure_table(con):
    """Create the per-profile score table (idempotent)."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS profile_scores (
            article_id INTEGER NOT NULL,
            profile    TEXT NOT NULL,      -- profiles.yaml `name`
            score      REAL NOT NULL,      -- 0-10, one decimal
            rationale  TEXT,
            version    INTEGER NOT NULL,   -- PROFILE_SCORE_VERSION at write time
            scored_at  TEXT NOT NULL,
            PRIMARY KEY (article_id, profile)
        )""")
    con.commit()


def save(con, article_id: int, profile: str, score: float, rationale: str):
    con.execute(
        "INSERT INTO profile_scores (article_id, profile, score, rationale, version, scored_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(article_id, profile) DO UPDATE SET "
        "score=excluded.score, rationale=excluded.rationale, version=excluded.version, "
        "scored_at=excluded.scored_at",
        (article_id, profile, score, rationale, PROFILE_SCORE_VERSION,
         datetime.now(timezone.utc).isoformat()))


def load_all(con, profile: str) -> dict[int, tuple[float, str]]:
    """{article_id: (score, rationale)} for CURRENT-version scores only."""
    return {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT article_id, score, rationale FROM profile_scores "
        "WHERE profile=? AND version=?", (profile, PROFILE_SCORE_VERSION))}


def score_batch(client: LLMClient, model: str, org: dict, profile: dict,
                articles: list[dict], batch_size: int = 15) -> list[tuple[float, str]]:
    """Return [(score, rationale)] aligned with `articles`, judged through `profile`.

    Mirrors llm_relevance.score_batch — including its quota fail-fast and its
    "every batch failed means the LLM is down" guard — so cost and failure
    behaviour stay consistent with the house scorer.
    """
    role = (profile.get("role_description") or "").strip()
    if not role:
        raise ValueError(f"profile {profile.get('name')!r} has no role_description")

    system = SYSTEM
    extra = (profile.get("relevance_guidance") or "").strip()
    if extra:
        system = f"{SYSTEM}\n\n{extra}"

    results: list[tuple[float, str]] = [(0.0, "not scored")] * len(articles)
    n_batches = n_failed = 0
    for start in range(0, len(articles), batch_size):
        n_batches += 1
        batch = articles[start:start + batch_size]
        items_txt = "\n".join(
            f'[{i}] area={a["area"]} | source={a["source"]}\n'
            f'    title: {a["title"]}\n'
            f'    summary: {(a.get("summary") or "")[:500]}'
            for i, a in enumerate(batch)
        )
        prompt = (
            f"Organization: {org['name']} — {org['description']} Region: {org['region']}\n\n"
            f"This person's role: {profile.get('title', '')}\n{role}\n\n"
            f"Items:\n{items_txt}"
        )
        try:
            text = strip_fences(client.complete(model, system, prompt, max_tokens=4000))
            for obj in json.loads(text):
                idx = start + int(obj["i"])
                if start <= idx < start + len(batch):
                    results[idx] = (float(obj["score"]), str(obj.get("why", "")))
        except QuotaExhausted as exc:
            log.error("Quota exhausted scoring profile %s (%s); aborting remaining batches",
                      profile.get("name"), exc)
            err = ScoringUnavailable(
                f"quota exhausted after {n_batches - n_failed - 1}/{n_batches} batches "
                f"for profile {profile.get('name')}")
            err.partial = results
            raise err from exc
        except Exception as exc:
            n_failed += 1
            log.warning("Profile scoring batch failed for %s (%s); items keep 0",
                        profile.get("name"), exc)
    if n_batches and n_failed == n_batches:
        raise ScoringUnavailable(
            f"all {n_batches} profile-scoring batches failed for {profile.get('name')}")
    return results
