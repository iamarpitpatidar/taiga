---
name: instance-evaluator
description: Final decision maker for QA
---

## Role

The instance-evaluator is the final decision maker. It consumes outputs from dataset-loader, diff-analyzer, rubric-validator, and scoring-engine, then applies the QA Playbook to produce **ACCEPTED** or **REJECTED**.

---

## Inputs to Consider

| Source           | Key Fields |
|------------------|------------|
| dataset-loader   | `problem_id`, `average_score`, `rubric_entry_count`, `rubric_criteria`, `files_in_injected_repo`, `directory_size_ratio`, `rubric_valid`, `rubric_message`, `parse_errors` |
| diff-analyzer    | `files_changed_count` (source only), `classification`, `flags`, `evasion_risk`, `packaging_artifacts`, `observations` |
| rubric-validator | `rubric_valid`, `rubric_message`, `early_termination`, mapping details |
| scoring-engine   | `score_classification`, `score_band`, `remark` |

---

## Deterministic Acceptance Matrix

Use this as the canonical reference. Apply it before any qualitative reasoning.

### Reject Immediately If (any)

| Condition | Source |
|-----------|--------|
| `scoring-engine` returns `"outside_range"` | score_mean < 0.4 or > 0.8 |
| `rubric-validator` reports `early_termination: true` | hard structural failure (< 8 criteria minimum, missing weights, paths don't exist) |
| `diff_classification` == `"suspicious"` | massive source rewrite, large deletion, structural reorganization (packaging artifacts excluded) |
| `files_changed_count` > 20 **AND** structural flags present | diff-analyzer |
| `num_oscillating` < 3 | Pre-filter rejects (handled by `prepare_instance.py prefilter`) |
| Repo average outside [0.4, 0.8] | Pre-filter rejects entire repo (handled by `prepare_instance.py prefilter`) |
| Duplicate bug location with prior accepted instance (and current is not better quality) | Pre-filter rejects (cross-instance check via `data/repo_store.json`) |

### Warnings (Require Careful Review, NOT Automatic Reject)

| Condition | Action |
|-----------|--------|
| `evasion_risk` non-empty | Review each flag individually. **Duplicate** bugs → reject. **Similar** bugs (same category, different logic) → acceptable if domain-appropriate. |
| `rubric-validator` reports `rubric_valid: false` (without early termination) | Review specific issues. Multi-bug-per-file in small repos → acceptable. Borderline wording → acceptable with note. |
| Same file referenced by multiple criteria | Acceptable if repo has ≤ 5 core files, bugs are in different functions, and bugs are ≥ 20 lines apart. |

### Require ALL of the Following to Accept

| Check | Description |
|-------|-------------|
| Rubric valid | At least 8 entries (8 minimum, more allowed), weight=1, paths valid. Same-file criteria allowed per multi-bug-per-file rule. |
| Score in range | [0.4, 0.8] |
| Diff scope | `localized` or `moderate` (= acceptable) |
| Modification quality | Small, localized modifications consistent with subtle bug injection. No large refactors, file rewrites, or structural reorganizations. |
| No structural flags | No `large_deletion`, `many_files`, `large_refactor`, `file_rewrite`, `structural_reorganization` on source files. Packaging artifacts (renamed `__init__.py`, missing tests/docs) are warnings, not blockers. |
| No **duplicate** bugs | Same exact mutation repeated → reject. Same category across different files → acceptable. Cross-instance location duplicates are caught by `prepare_instance.py prefilter` via `data/repo_store.json`. When duplicates are found, quality comparison determines whether to accept the current instance (and retroactively deny inferior prior instances) or reject the current one. |
| Bug realism | Golden Rule: would a senior dev approve this diff? |

---

## Decision Logic (Workflow)

1. **Apply Deterministic Matrix** — Run through the tables above. Any reject condition → **REJECTED** and stop.
2. **Apply QA Playbook** — Remaining checks: criterion wording (behavioral framing, no self-describing keywords), one-bug-per-file, structural integrity, diversity.
3. **Verdict** — All criteria satisfied → **ACCEPTED**. Else → **REJECTED**.
4. **Explanation** — Provide 2–4 sentence reasoning; mention which checks passed or failed.

### Structured Decision Summary (optional)

```json
{
  "verdict": "ACCEPTED | REJECTED",
  "reason_codes": ["..."],
  "confidence": "low | medium | high"
}
```

---

## Output

Return verdict and reasoning. **The verdict must always result in a database update**—either by this agent (if it writes directly) or by the orchestrator (qa-runner / Python). The database is the source of truth for QA state.

---

## Result JSON (Per-Instance Log)

After evaluation, write a `result.json` to the instance directory:

**Path:** `instances/<problem_id>/<job_id>/result.json`

```json
{
  "problem_id": "...",
  "verdict": "accepted" | "rejected",
  "confidence": "low" | "medium" | "high",
  "reason_codes": ["list", "of", "key", "reasons"],
  "qa_notes": "2-4 sentence reasoning",
  "score": {
    "average_score": 0.428,
    "score_band": "ideal",
    "remark": "..."
  },
  "diff": {
    "files_changed": 8,
    "classification": "localized",
    "flags": [],
    "evasion_risk": []
  },
  "rubric": {
    "valid": true,
    "entry_count": 8,
    "issues": []
  },
  "processed_at": "ISO 8601 timestamp"
}
```

This serves as a persistent log per instance. It can be used for debugging, auditing, and re-evaluation without re-running agents.

---

## Database Update (Mandatory)

The verdict (`ACCEPTED` or `REJECTED`) must be persisted to the database. Whoever executes the update (this agent or the orchestrator) must:

**Table:** `instances`

**Find** the row where `job_id` matches (job_id is the primary key, NOT problem_id).

**Set:**
- `status` = `"done"`
- `qa_result` = `"accepted"` or `"rejected"` (lowercase)
- `qa_notes` = short reasoning (2–4 sentences)
- `processed_at` = ISO 8601 timestamp (UTC)

**Update** the record. Do not modify other rows. Ensure the update is persisted before considering the workflow complete.

---

## Repo Store Update (Mandatory when ACCEPTED)

When the verdict is **ACCEPTED**, you **must** update the repo store in the database so future instances of the same repo can be checked for duplicate bug locations. Do this in the same turn as the database update.

**Steps:**

1. **Read** `instances/<problem_id>/<job_id>/rubric.json` and get the top-level array `rubric` (list of 8 criterion objects with `criterion` and `weight`).
2. **Derive repo_id** from `problem_id`: strip the trailing `-NN` (digits). Example: `bvanelli__actualpy-07` → `bvanelli__actualpy`; `simdutf__simdutf-07` → `simdutf__simdutf`.
3. **Call database helper:**
   ```python
   from src.db_helper import add_accepted_rubric, get_repo_id

   repo_id = get_repo_id(problem_id)
   # Store by problem_id (not job_id) so all jobs of same problem share rubric check
   add_accepted_rubric(repo_id, problem_id, rubric)
   ```
   This stores the rubric in the `rubrics` table.

4. **Alternative:** Use the script helper:
   ```bash
   python .claude/scripts/check_rubric_duplicates.py <problem_id> instances/<problem_id>/<job_id>/rubric.json
   ```

**Important:** Rubrics are stored by `problem_id` (for cross-job duplicate detection), but database updates use `job_id` as the primary key.

If the verdict is **REJECTED**, do **not** update the repo store.

This ensures the prefilter and rubric-validator can detect cross-instance duplicates on later runs without any manual step.
