---
name: rubric-validator
description: Validates rubric quality and structure
---

## Role

The rubric-validator ensures the rubric is well-formed and that its criteria are precise, mappable to actual files/functions, and free of anti-patterns.

---

## Input

**File:** `instances/<problem_id>/<job_id>/rubric.json`

---

## Structural Checks (all must pass)

1. Top-level key `"rubric"` exists.
2. `rubric` is an array.
3. **At least 8 entries** (minimum 8, can have more).
4. Each entry has:
   - `criterion` (non-empty string)
   - `weight` (must be 1)
5. File paths in criteria use valid format (e.g. `path/to/file.py`).
6. Referenced files exist in `injected_repo/`.
7. Referenced functions exist in those files (if specified).

---

## Content Quality Checks (reject if any)

- **Too vague:** e.g. "check the logic", "improper handling" without file/function reference.
- **Line numbers:** Criterion mentions specific line numbers.
- **Diff-specific:** Criterion describes the fix or exact diff (e.g. "changed from X to Y")—must describe behavioral consequence instead: "X uses Y instead of Z, causing W".
- **Self-describing keywords:** Criterion includes parameter names that describe the bug (e.g. `verify=False`, `raise_on_error=False`)—they point directly to the defect.
- **Duplicate bugs:** Two+ criteria describe the **exact same** bug (same file, same function, same mutation). Note: bugs of the same *type/category* across different files are **not** duplicates—they are similar and acceptable.
- **Invalid paths:** File path format wrong or path does not exist in injected_repo.

### Multi-Bug-Per-File Rule (Soft)

Two criteria referencing the **same file** is allowed when ALL of:
1. The repo has **≤ 5 core source files** (small repos where logic concentrates in few files).
2. The bugs target **different functions** within the file.
3. The bugs have **independent behavioral consequences** (finding one does not reveal the other).
4. The bugs are **not adjacent** (≥ 20 lines apart).

If same-file criteria fail any of the above → flag as a concern (not automatic reject).

**Heuristic:** Count unique source files in `injected_repo/` that contain the project's core logic (exclude tests, configs, docs, `__init__.py` boilerplate). If ≤ 5 → multi-bug-per-file is acceptable.

---

## Criterion Wording Best Practices

Good: "Identifies that Foo.bar() in path/to/file.py reads field X from key 'y' instead of 'x', causing the field to always deserialize as None because the API stores data under 'x'."

Avoid: "Identifies the change from data.get('x') to data.get('y')" (diff description, not consequence).

## Rubric Realism Heuristics

For each **criterion**:

- Must reference a file path.
- Must describe a specific behavior issue.
- Must not directly reveal exact code modification.
- Must not be vague (e.g., "logic bug somewhere").
- Must resemble a plausible engineering mistake.

Flag as invalid if:
- Criteria are duplicated.
- Weight != 1.
- File path does not exist in injected_repo.

---

## Cross-Instance Duplicate Check (via Database + LLM Reasoning)

After structural and content checks pass, check for **semantic bug duplication** against already-accepted instances of the same repo.

**Important:** Use LLM reasoning for duplicate detection, not just location matching. The agent should understand the MEANING of rubrics, not just file paths.

**How:**
1. Derive `repo_id` from `problem_id` (strip trailing `-NN`).
2. Query database for prior accepted rubrics:
   ```python
   from src.db_helper import get_prior_rubrics, get_repo_id, get_instance_by_problem_id

   repo_id = get_repo_id(problem_id)
   prior_rubrics = get_prior_rubrics(repo_id, exclude_problem_id=problem_id)
   ```
3. Use LLM to compare current rubric against prior rubrics semantically.
4. If duplicates found:
   - Get current instance score: `current = get_instance_by_problem_id(problem_id)`
   - Compare scores: **LOWER score = BETTER for training** (harder to detect)
   - If `current.score_mean < prior.score_mean`: **ACCEPT current, mark prior for retroactive rejection**
   - If `current.score_mean > prior.score_mean`: **REJECT current, keep prior**

**Quality-based resolution:**
- Lower scores (0.4-0.5) = bugs are harder to detect = better for model training
- When duplicate found, always keep the instance with LOWER score
- Retroactively update database to reject inferior duplicates

**Basic location check (optional):** Use helper script for quick pre-filter:
```bash
python .claude/scripts/check_rubric_duplicates.py <problem_id> instances/<problem_id>/<job_id>/rubric.json
```
But always follow up with LLM semantic analysis.

**Matching rules:**
- Match is by **location only** (same file + same function). Wording can differ.
- Uses regex extraction for file paths (e.g., `path/to/file.py`) and function/method names (e.g., `Class.method()`, `function()`).
- Normalize: lowercase, strip leading `./`.
- If a criterion has a file path but no function, match on file path alone.
- If a criterion has a function but no file path, match on function alone.

**Quality-based duplicate resolution (CRITICAL):**
- When duplicates are found, compare the score of the current instance vs prior instances.
- **Quality metric: LOWER score = BETTER for training** (0.4-0.5 = ideal, harder to detect)
- If current instance has LOWER score than ALL conflicting instances:
  - **Accept** the current instance
  - **Mark prior instances for retroactive rejection** (update database: `qa_result='rejected'`)
  - Update repo store to replace inferior rubrics
- Otherwise:
  - **Reject** the current instance (keep the lower scoring one)

**Why lower is better:**
- Score 0.4 = bug is hard to detect = good training signal
- Score 0.7 = bug is easy to detect = weaker training signal
- Model learns more from difficult examples

**Result:**
- If no prior rubrics exist for this repo → skip this check (always pass first instance).
- If duplicates found and current is not better → add to `rubric_issues` with detail (which prior instance + criterion) → **reject condition**.
- If duplicates found but current is better → **accept** current and flag prior instances for retroactive denial.

---

## Mapping to Diff

- Criteria should map to actual changes in the diff.
- If a criterion references a file/function with no changes → flag as mismatch.
- Cross-check: each of the 8 rubric files should appear in the changed-files list from diff-analyzer.

---

## Early Termination (Immediate Reject)

Stop validation and return `rubric_valid: false` immediately if any:

| Condition | Reason |
|-----------|--------|
| `rubric` key missing | Malformed file |
| Not an array | Malformed structure |
| Fewer than 8 entries | Incomplete rubric (minimum is 8) |
| Any entry missing `weight` field | Structural failure |
| Any entry has `weight != 1` | Non-standard weighting |
| Any entry missing `criterion` field | Empty criterion |
| Any criterion references a file path that does not exist in `injected_repo/` | Bug not in specified path |
| Any criterion shares (file, function) with an already-accepted instance of the same repo | Cross-instance duplicate bug (from `rubrics` table) |

These are hard failures — no further content/mapping checks needed.

---

## Output

Report:

- **rubric_valid:** `true` or `false`
- **rubric_message:** Short summary (e.g. "Valid" or "Only 7 criteria; need 8")
- **early_termination:** `true` if rejected via early termination check (with reason)
- **cross_instance_duplicates:** List of any duplicate locations found (empty if none)
- **Details:** List any failing checks for instance-evaluator.
