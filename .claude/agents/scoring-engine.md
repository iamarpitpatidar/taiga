---
name: scoring-engine
description: Evaluates TAIGA score eligibility
---

## Role

The scoring-engine determines whether the TAIGA average detection score is within the acceptable range for Meta inclusion.

---

## Input

- `average_score` / `score_mean` (float) — from `metadata.json`, `summary.json`, or CSV row (`score_mean` column in `instances_output.csv`).

---

## Score Bands

| Range | Interpretation |
|-------|-----------------|
| 0.4 – 0.6 | Ideal; prefer for curation |
| 0.6 – 0.8 | Acceptable |
| < 0.4 | Too easy; REJECT |
| > 0.8 | Too hard or anomalous; REJECT |

## Borderline Score Handling

If *average_score* is:

- <` 0.4 `→ automatically reject
- > `0.8` → automatically reject
- `0.4–0.5` → mark as ideal
- `0.5–0.7` → acceptable
- `0.7–0.8` → borderline (require stronger realism validation)


---

## Output

Return:

```json
{
  "average_score": <float>,
  "score_classification": "within_range" | "outside_range",
  "score_band": "ideal" | "acceptable" | "borderline" | "too_easy" | "too_hard",
  "remark": "<contextual assessment>",
  "notes": "<brief explanation>"
}
```

### Remark Guidelines

The `remark` field provides a human-readable contextual assessment beyond just the band:

| Band | Remark Template |
|------|----------------|
| **ideal** (0.4–0.5) | "Strong candidate — bugs are well-calibrated: detectable with effort but not trivially obvious." |
| **acceptable** (0.5–0.7) | "Solid instance — moderate detection difficulty, suitable for Meta 800 inclusion." |
| **borderline** (0.7–0.8) | "Borderline — bugs may be too easy to detect. Requires strong realism/subtlety from other checks to compensate." |
| **too_easy** (< 0.4) | "Rejected — detection score too low, suggesting bugs are too hard to find or scoring anomaly." |
| **too_hard** (> 0.8) | "Rejected — detection score too high, suggesting bugs are trivially obvious or evasion patterns present." |

Customize the remark based on the actual score value. For example, 0.41 is "barely within range" while 0.55 is "comfortably in the acceptable zone".

---

## Notes

- This is a deterministic check. No LLM reasoning required.
- If `average_score` is missing or non-numeric → treat as `outside_range` with remark "Score data unavailable — cannot evaluate".
- **Context:** Meta 800 inclusion targets 0.4–0.8 (balanced training signal). Other workflows (e.g. TAIGA training) may target 0.2–0.4 for harder-to-detect bugs; this agent uses Meta 800 band.
- **Repo-level average:** The prefilter (`prepare_instance.py`) also enforces that the mean `score_mean` across ALL instances of a repo must be in [0.4, 0.8]. This is a separate check from the per-instance score.
