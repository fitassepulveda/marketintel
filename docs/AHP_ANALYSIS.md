# AHP, Eigen-Analysis & Per-Executive Personalization

An analysis-and-calibration layer on top of the live scoring (`docs/SCORING.md`).
It does not change how the daily briefing is scored; it helps you *understand* the
scoring and *tailor* briefings to individual executives. Everything here rests on
the six structured sub-scores in `src/prioritize/subscores.py`:

`financial_impact · strategic_impact · competitive_relevance · operational_impact ·
time_sensitivity · proximity` (each 0–10, stored in SQL).

---

## Part 1 — Why eigenvalues, and where

### 1a. Prescriptive: AHP (deriving weights we *should* use)

Hand-picking weights is hard to defend. The **Analytic Hierarchy Process** (Saaty)
turns simple pairwise judgments ("how much more important is financial_impact than
time_sensitivity?", Saaty 1–9) into a weight set: the **principal eigenvector** of
the judgment matrix is the weights, and the **principal eigenvalue (λ_max)** gives a
**Consistency Ratio (CR)** that flags self-contradictory judgments. CR < 0.10 is
acceptable.

Current judgments (`config/ahp.yaml`) yield:

| Dimension | AHP weight |
|---|---|
| financial_impact | 23.4% |
| strategic_impact | 21.1% |
| competitive_relevance | 20.0% |
| operational_impact | 14.1% |
| time_sensitivity | 12.6% |
| proximity | 8.7% |

λ_max = 6.147, **CR = 0.024 → consistent.** Reproduce with
`python scripts/ahp.py --judgments`; export to a graphable workbook with
`python scripts/ahp_to_excel.py`.

### 1b. Descriptive: eigen-analysis of real data (what *is* driving rankings)

Once articles carry sub-scores, `python scripts/ahp.py --data` eigen-decomposes the
**correlation matrix** of the six dimensions across all scored articles (PCA): the
**variance explained** per component and the **PC1 loadings** show which dimensions
actually move the signal. Prescriptive (1a) says what *should* matter; descriptive
(1b) shows what *does* — the gap is the insight.

> Note: the live composite is currently **100% LLM relevance** (see `docs/SCORING.md`).
> AHP here is not re-weighting that composite; it (a) gives a defensible weighting
> for the sub-score *framework*, and (b) powers per-executive personalization below.

---

## Part 2 — Per-executive personalization

### Score once, re-rank cheaply

The pipeline scores each article once. Each executive profile then re-ranks that
same pool through their own weight vector — pure arithmetic, no extra relevance
calls. Adding an executive costs one synthesis call for their briefing, nothing more.

### A profile (`config/profiles.yaml`)

`name`, `title`, `email` (delivery address), `active: true/false`, and **what they
care about** — either `subscore_weights` (direct 0–10 per dimension) or
`ahp_pairwise` (pairwise judgments → eigenvector weights, same method as Part 1a).
Optional: `area_weights` (small nudge), `keyword_interests`, `threshold`.

### How a personal score is computed

Because the house composite is 100% LLM relevance, a personal score is that
executive's **weighted view of the article's sub-scores**, on the same 0–100 scale:

```
personal_relevance = Σ (dimension_weight × article_subscore)     # 0–10
personal_composite = 10 × personal_relevance                     # 0–100
                     + small area nudge (if area_weights set, neutral at 7)
                     + small keyword nudge (+3 if an interest keyword appears)
```

Each executive's briefing is the top of *their* ranking, above *their* threshold.

### Validated behavior

Same two stories, two lenses (test run): a CFO elevates a CMS reimbursement story
(personal relevance 7.1 vs a strategist's 5.9); a Chief Strategy Officer elevates a
competitor's $500M expansion (8.7 vs the CFO's 8.0) — from one shared, once-scored
pool.

---

## Part 3 — Managing executives

| Action | How |
|---|---|
| Add an executive | Copy a block in `config/profiles.yaml`, set `active: true`, fill name/email/weights |
| Remove / pause | Delete the block, or `active: false` |
| Link to an email | Set `email:` |
| See who's configured | `python scripts/personalize.py --list-profiles` |
| Choose their lens | `subscore_weights` (direct) or `ahp_pairwise` (derived) |

---

## Part 4 — How to run

```bash
pip install -r requirements.txt                  # numpy + openpyxl now included

python run_briefing.py                           # the shared briefing (unchanged)
python scripts/personalize.py --dry-run          # per-exec briefings saved locally, not sent
python scripts/personalize.py                    # send per-exec briefings

python scripts/ahp.py                            # AHP (judgments) + eigen-analysis (data)
python scripts/ahp_to_excel.py                   # graphable workbook -> data/ahp_results.xlsx
```

`personalize.py` runs *after* `run_briefing.py` and reuses the articles it already
scored, so it never duplicates or interferes with the main scoring/dedup/selection.

---

## Recommended sequence

1. Run the normal briefing daily for several days so the DB accumulates scored,
   sub-scored articles.
2. `python scripts/ahp.py --data` — compare what *does* drive rankings to the
   prescriptive weights from `--judgments`; adjust `config/ahp.yaml` (or the rubric)
   if they disagree.
3. Activate executive profiles one at a time, confirm each looks right in
   `--dry-run`, then send.
