# Prioritization — Adjustments & Features

Every prioritization change made to the Market Intelligence briefing, with what it does and
where it lives. Most scoring behavior is in `config/settings.yaml → briefing.relevance_guidance`
(no code change needed to tune); selection mechanics are in `run_briefing.py` + `config/`.

_Last updated: 2026-06-17._

---

## 1. Scoring frame & audience

1. **Exec-facing strategic lens.** Score each story by its **impact on UHealth's strategy and
   long-term direction**, fusing external competitive intelligence with operational/financial/
   strategic impact. A story can score high purely on strategic impact with no competitor in it.
   _(relevance_guidance)_
2. **Grounded UHealth profile.** The org description now reflects UHealth's real profile from
   umiamihealth.org — academic medical center; ~1.7M visits, 700 beds, 40 locations, 14,628 staff;
   active expansion (SoLé Mia, Doral, Pinecrest, Griffin building, Tower, Bascom Palmer Abu Dhabi);
   ~$178.8M NIH; AI priority; UnitedHealthcare negotiation; Jackson/HSS/VA/Siemens/Labcorp.
   _(org.description + relevance_guidance)_
3. **Pure LLM relevance, full 0–10 range, strict.** Composite = 100% LLM relevance (area/source
   weights off). Most general policy/industry-trend items belong at 3–5, not 7–9. _(weights.yaml)_

## 2. The scoring gates

4. **Gate 1 — direct UHealth relevance.** UHealth is a provider/academic system, not a pharma,
   PBM, or insurer. Third-party lawsuits/disputes score 2–3 unless they hit UHealth directly.
5. **Gate 2 — actionability.** "State of the market" / "what it means for patients" / awareness /
   routine-regulatory-color pieces cap at 3–5. Opinion pieces = 1.
6. **Gate 3 — judge against UHealth's existing position.** Relevance = a real gap/threat/
   opportunity given what UHealth already is — not a keyword match. Topics where UHealth is
   already strong (e.g. talent pipeline via the Miller School) are not priorities just for matching.

## 3. What scores high (strategic vectors, 8–10)

7. **Growth / competitive footprint** — competitor capital/capacity/M&A/partnership/service-line
   moves in South Florida, weighted to UHealth's expansion zones (North Miami/Aventura, Doral,
   Pinecrest, west Miami-Dade).
8. **Federal/state funding & policy** — NIH/research-grant funding, Medicare/Medicaid
   reimbursement, 340B, graduate medical education, academic-medical-center policy.
9. **Payer / reimbursement leverage** — a major payer's network/reimbursement/denial moves
   affecting UHealth's own contracts (cf. the UnitedHealthcare negotiation). Not pharma/PBM.
10. **Flagship service lines (as a lens for EXTERNAL developments)** — ophthalmology (Bascom
    Palmer), oncology (Sylvester), neuro/neurosurgery, urology (Desai Sethi), transplant, cardiac,
    rehab (Miami Project).
11. **Workforce / talent** — nurse/physician labor markets, unionization, competitor recruitment
    (scored on a real gap/threat, not generic trend pieces).
12. **AI / health-tech (gated — see #16).**
13. **Partnership ecosystem** — moves by/affecting Jackson Health, HSS, the VA, Siemens, Labcorp.

## 4. Exclusions & de-prioritizations

14. **Pharmacy / drug-pricing / PBM is NOT a priority** (reversed mid-project). Scored only on
    direct UHealth impact; a PBM lawsuit or drug-pricing rule is general (3–5) or, if it's another
    company's fight, low (2–3). Don't boost an item just because it involves drugs/pharmacy/PBMs.
15. **Exclude UHealth's own news.** The audience already knows their own announcements. Anything
    about UHealth's own institutes/people (Sylvester, Bascom Palmer, Desai Sethi, Miami Transplant,
    Miller School, Miami Project, UHealth Tower) scores 1–2. Sole exception: an external reputation
    risk leadership must respond to.
16. **AI relevance gate.** AI scores high only if UHealth could actually adopt it OR a peer health
    system is concretely deploying it. Biotech/pharma AI, vendors UHealth doesn't use, and generic
    "AI is transforming healthcare" commentary score 2–4. The test is "could UHealth act on this?",
    not "does it mention AI?"
17. **Human-interest floor.** Patient-facing human-interest / cancer-survivorship / wellness
    pieces and routine local public-health warnings (e.g. a state opioid advisory) score 1–3.
18. **Geography is a competitive-footprint lens, not a universal gate.** National peer-system
    operational/AI practices count even when out-of-region.
19. **Cybersecurity & research = "keep an eye on" awareness.** Healthcare data breaches (UHealth
    holds lots of patient data) and research funding/policy color sit in the middle tier (5–6) —
    good for exec awareness on a slow day, never high on their own.

## 5. Deterministic rules (code-enforced, regardless of content)

20. **FIU + Baptist same-sentence → auto-floor 9.** Any article with both "FIU" (or "Florida
    International University") and "Baptist" in a single sentence is floored at 9 — because Baptist
    is moving into academic medicine via FIU's medical school, a direct threat to UHealth's
    academic-medical positioning. A floor, not a cap (never lowers a higher score). Config-driven
    and extensible. _(settings.yaml → briefing.forced_floor_rules; scoring.forced_floor)_

## 6. Output selection & email

21. **Selection: include all ≥ 90, min 5, max 12.** Replaced the fixed top-5. Strong days surface
    more (up to 12); quiet days still show a baseline of 5. _(settings.yaml: select_threshold 90 /
    min_stories 5 / max_stories 12; run_briefing.prioritize)_
22. **Re-brief window = 24h** (kept as-is by decision) so stories don't repeat day-to-day.
23. **LLM relevance score badge** shown next to each headline in the email. _(emailer)_
24. **"Also considered" runners-up list** — the next 5 stories below the cut, as title + link +
    score, for comparison. _(emailer + run_briefing)_

## 7. Sources & data hygiene

25. **Removed NOAA weather bulletins** from the source list (noise, undateable). _(sources.yaml)_
26. **Purged stale undated Fierce rows** that were lingering from a pre-switch native-feed pull.
    _(scripts/purge_stale_undated.py)_

## 8. Calibration (worked examples baked into the prompt)

27. **Worked examples from your own judgments** anchor the scale — drawn from your original review
    spreadsheet, the daily-review notes (2026-06-17), plus strategic/flagship illustrative cases
    (e.g. Yale peer-AI → 8, biotech-AI → 3, Abridge vendor → 3, talent-pipeline → 5, cancer-
    survivorship → 2, opioid warning → 3, IRhythm breach → 6). _(relevance_guidance)_

## 9. Tooling to review & tune prioritization

28. **Daily review spreadsheet** of scored articles with score, rationale, feedback columns, and
    tiered color coding.
29. **"Today's new articles" workbook** — today's extractions + significance, excluding anything
    already shown in a prior briefing, with a notes/feedback column and color key.
30. **`scripts/test_synthesis.py`** — run a hand-written article through the real scorer + the
    forced-floor + Gemini synthesis + email rendering, to test scoring and formatting in isolation.
31. **`scripts/score_report.py`** + `--dry-run` — inspect how everything ranks without sending.
