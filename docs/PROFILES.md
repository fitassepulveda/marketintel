# Per-Executive Profiles — reference

How a person gets their own version of the briefing. Written 2026-08-27; covers the
switch from weighted sub-scores to semantic role-based scoring.

**Point another session at this file.** It is the whole picture: how scoring works, how to
add a profile, what has already been tried and rejected, and what is still unverified.

---

## 1. The three scoring modes

A profile in `config/profiles.yaml` runs in exactly one mode, decided by which fields it has.

| Mode | Trigger field | What happens | Extra LLM cost |
|---|---|---|---|
| **name-only** | neither below | Identical to the shared briefing; only the greeting differs | none |
| **weights** (legacy) | `subscore_weights` or `ahp_pairwise` | Weighted average of the shared 9-dimension sub-score vector | one shared sub-scoring pass for all such profiles |
| **semantic** (preferred) | `role_description` | Each article scored 0-10 for relevance **to that person's role** | ~5 batched requests/day per profile |

`src/profiles.py::personal_composite()` prefers `article["profile_score"]` (semantic) and falls
back to the weighted average when it is absent. `uses_semantic_scoring(profile)` is the check.

---

## 2. Why semantic, not weights

**Weights are close to a no-op.** Sub-scores are produced by a prompt that receives only
`org.name / description / region` — no person, no role. One vector per article, shared by
everyone. A profile's weights just re-average numbers that were never person-aware, and
`subscore_weights` normalise to sum to 1, so a 10-vs-6 spread compresses to 13.0% vs 7.8%.

Measured on 54 articles (2026-08-21/24/25): **a tuned profile's weights produced an IDENTICAL
top ten to uniform weights**, max score gap 6.4 points. Overlap with the shared briefing stayed
at 4-6 of 6. With semantic scoring, overlap dropped to 1-3 of 6 — real differentiation.

**Semantic also fixes the scale problem.** A weighted mean across 9 dimensions compresses
toward the middle (house mean 55.4 / max 100 vs personal mean 32.4 / max 85), so the shared
`threshold: 55` and the two-tier bands starved a weighted profile. Rescaling by a constant does
NOT fix this: it preserves rank order exactly (so it is just a threshold change in disguise),
it clips at the 100 ceiling, and it cannot reconcile the two scales anyway — the house/personal
ratio ranged 0.61-4.84 and the Spearman rank correlation was only 0.51. A semantic score is
relevance x10, the SAME scale as the house composite, so 55 and the 80/60/70 tier bands keep
their meaning with no changes.

Also considered and not needed once semantic landed: a top-k-mean aggregation (average only the
person's strongest 3-4 dimensions) and percentile-within-day normalisation. The latter is
actively wrong here because it forces a fixed number of stories through daily, breaking the
"no padding" rule of the two-tier output.

---

## 3. Adding a new profile

```yaml
  - name: "Firstname"                 # identifier; also the key in profile_scores
    title: "Their Role"               # goes into the scoring prompt
    display_name: "Firstname"         # greeting only
    email: "someone@example.edu"      # REQUIRED before active: true
    active: false                     # keep false until a dry run looks right
    threshold: 55                     # semantic profiles can use the house default
    max_stories: 12

    role_description: >
      What they own, decide, and are working on — and explicitly what is NOT theirs.
      GENERAL TERMS ONLY (see §5). The "not their remit" sentence does a lot of work.

    relevance_guidance: |
      A calibration ladder. Concrete story shapes with target scores, e.g.:
      - <the thing they most need to see> -> 9-10.
      - <adjacent, still theirs> -> 7-8.
      - <another leader's remit> -> 2-3.

    keyword_interests: [term, phrase]  # +3 each, capped +6.  Matched LOCALLY, never sent to the LLM
    keyword_avoid: [term, phrase]      # -5 each, capped -15. Matched LOCALLY, never sent to the LLM
```

Then:

```bash
python scripts/personalize.py --list-profiles     # confirm it loads
python scripts/personalize.py --dry-run           # build locally, send nothing
```

Only flip `active: true` once the dry run looks right. Inactive profiles are skipped entirely —
they cannot be emailed and do not appear in `--list-profiles`.

---

## 4. Field reference and gotchas

- **`keyword_interests` / `keyword_avoid` are matched on WHOLE WORDS.** `src/profiles.py::_matches()`
  uses `(?<!\w)term(?!\w)`. This was substring matching until 2026-08-27, which made `MOB` hit
  "mobility", `ASC` hit "Ascension", and — the one that matters — **`pharma` hit `pharmacy`**, so a
  pharma penalty would cancel a pharmacy boost on every pharmacy story.
- **Avoid bare generic acronyms** in either list. Tested: adding bare `AI` to an avoid list dropped
  8 stories including a health-system ownership deal that merely *mentioned* AI. Prefer specific
  terms (`generative AI`, `machine learning`).
- **There are NINE sub-score dimensions, not six** (`financial_impact`, `strategic_impact`,
  `competitive_relevance`, `operational_impact`, `time_sensitivity`, `proximity`, `actionability`,
  `direct_relevance`, `magnitude`). `dimension_weights()` does `.get(d, 0)`, so a weights profile
  copied from an old example **silently zeroes any dimension it omits** — including
  `direct_relevance`, the one separating org-specific news from general industry noise. Only
  relevant for legacy weights profiles.
- **`threshold`** — 55 is correct for semantic profiles (same scale as the house). A weights
  profile needs its own much lower threshold (~30s) or it passes 1-3 stories/day.

---

## 5. CONFIDENTIALITY — read before writing a role description

`role_description` and `relevance_guidance` **are sent to the LLM provider on every run**.
`keyword_interests` / `keyword_avoid` are not — they are matched locally.

The pipeline runs on the **Gemini free tier**, whose terms state that Google uses submitted
content "to provide, improve, and develop Google products and services and machine learning
technologies", that "human reviewers may read, annotate, and process your API input and output",
and explicitly: *"Do not submit sensitive, confidential, or personal information to the Unpaid
Services."* The paid tier does not use prompts for product improvement.

So while on the free tier: **write every prompt as if it will be published.**
- Describe the SHAPE of the role: domains owned, decision types, what is out of scope.
- Never include internal project names, capital figures, capacity numbers, contract dates,
  negotiation postures, internal metrics/targets, or internal planning thresholds.
- Enabling billing is what buys the right to be more specific. Cost is negligible
  (see §7), so this is the cheapest available accuracy upgrade.

Separately, and regardless of tier: **internal context never appears in reader-facing briefing
text.** It informs selection and scoring only. Rationale shown to a reader is general
institutional reasoning, never person-specific and never citing internal plans.

---

## 6. Storage and versioning

| Table / column | Holds | Version key |
|---|---|---|
| `articles.subscores` + `articles.subscores_version` | shared 9-dim vector | `subscores.SUBSCORE_VERSION` |
| `profile_subscores` (article_id, profile) | per-profile dimension vector | `SUBSCORE_VERSION` |
| `profile_scores` (article_id, profile) | per-profile 0-10 score + rationale | `profile_relevance.PROFILE_SCORE_VERSION` |

Scores are cached and reused, so the daily marginal cost is only genuinely new articles.
**Bump the relevant version constant whenever a prompt or dimension list changes** — otherwise
stale entries are silently reused. Schema creation is idempotent (`ensure_column`,
`ensure_table`) and runs at startup, so a fresh clone self-heals.

`profile_scores.rationale` is captured but not yet rendered. Surfacing it — "why this matters to
you" — is an obvious next improvement and is safe under §5 because it is generated from the
general role description.

---

## 7. Cost

Volume is small enough that cost should not drive design decisions. Measured: ~73 new articles/day,
batched 15 per request = **5 requests/day** for one scoring pass, against a free-tier limit of 250/day.
At paid 2.5-Flash rates ($0.30/M in, $2.50/M out) that is roughly **$0.26/month per pass**. Even
six semantic profiles stay well inside the free-tier request cap and cost a few dollars a month
paid. The real constraints are the 10-req/min rate limit and the 4-failures/day halt guardrail
(`scripts/guard_skip_if_ran.py`), not money.

---

## 8. THE BIGGER LIMIT — source coverage

Scoring improvements cannot surface stories that were never ingested. Measured over 30 days
(1,836 articles): local TV health segments are **50% of the pool**, Fierce Biotech another 7%.
Keyword counts across everything: `real estate` 0, `square feet` 0, `groundbreaking` 1,
`construction` 6, `medical office` 4, `pharmacy` 9.

For an ambulatory/operations reader that is about a dozen relevant items per month. Candidate
RSS feeds are staged in `config/sources_candidates.yaml` (not loaded by the pipeline) with
`scripts/verify_candidate_feeds.py` to test them from a networked terminal. Highest-value
addition is Becker's ASC Review. Zoning/permit agendas and Florida AHCA licensure filings — where
facility development appears before it is news — have no RSS and would need a scraper or scout.

Second limiter: summaries median **102 characters**, 69% under 120, only 7% reach the scorer's
500-char truncation. The scorer is largely reading headlines; richer ingestion probably beats
further prompt tuning.

---

## 9. State as of 2026-08-27

Existing profiles: Kique, Jake, Pranav, CJ, AXS (all active, name-only), Fernando (inactive),
**Rafic** (inactive, semantic — Chief Ambulatory Officer, the first non-name-only profile).

Rafic's tuning: pharmacy/dispensing boosted; pharma and drug development penalised; tech/IT
penalised. Bare `AI` deliberately excluded from the avoid list.

**Unverified — do not treat as working:**
1. No semantic scoring has ever run against a live model. Everything was exercised with a stub
   (`scripts/test_profile_scoring.py`) because the Cowork sandbox has no outbound network.
   A real `python scripts/personalize.py --dry-run` from a networked terminal is the next step.
2. The pharmacy boost has never fired on a real pharmacy story — none appeared in the sample days.
3. No accuracy ground truth exists. The feedback xlsx (score + rationale + Notes column) is the
   natural instrument: dry-run for a week, mark the output, compare.
4. Rafic has no email address; he cannot be activated until one is set.
