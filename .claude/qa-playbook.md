# Meta 800 QA Playbook

## Objective

Evaluate whether a TAIGA dataset instance is suitable for Meta inclusion.

An instance must:

- Have exactly 8 rubric criteria
- Have realistic and localized bugs
- Not contain structural sabotage
- Not modify excessive files
- Have TAIGA average score (`score_mean`) between 0.4–0.8
- Have `num_oscillating` >= 3 (when present and numeric)
- Belong to a repo whose average score (all instances) is in [0.4, 0.8]
- Not duplicate bug locations from already-accepted instances of the same repo
- Not contain trivial or obvious mistakes
- Pass bug-to-file mapping and criterion quality checks

---

## Golden Rule (Bug Realism)

> **Would a senior dev reviewing this diff approve it as an improvement?**

The single most predictive factor: the code change should look like an **intentional improvement** (cleanup, simplification, modernization), not an obvious mistake. Bugs that look like "someone was refactoring" tend to evade detection; bugs that look wrong get caught immediately.

---

## Acceptance Criteria

ACCEPT if:

1. Rubric contains exactly 8 entries (criterion + weight=1 each).
2. Diff appears localized (≤ 20 files).
3. Changes resemble realistic engineering mistakes (per Golden Rule).
4. No massive deletions or rewrites.
5. Bugs are not obviously artificial.
6. Average score in [0.4, 0.8].
7. Each criterion explicitly references file path and function/class name.
8. At most one bug per file (no two criteria pointing to the same file).
9. Criterion wording uses behavioral framing ("X uses Y instead of Z, causing W")—not diff description.
10. Test modifications are minimal (typically ≤ 2 assertion removals per bug; total < ~10).

---

## Rubric Criterion Quality

Each criterion MUST:

- Include **repository-relative file path** (e.g. `src/module/file.py`).
- Include **function or class name** (e.g. `DeJson.de_json()`, `__build_direct_link()`).
- Use **behavioral framing**: "X uses Y instead of Z, causing W" — not "X changed from Z to Y".
- Avoid line numbers, exact diff language, and self-describing keywords (`verify=False`, `raise_on_error=False`).


---

## Reject If:

- Rubric malformed or key "rubric" missing.
- Fewer/more than 8 criteria or weights ≠ 1.
- Criterion lacks file path or function/class name.
- Two criteria reference the same file.
- Massive refactor; > 20 files changed.
- Nonsensical edits or blacklisted evasion patterns.
- Score outside [0.4, 0.8].
- Criterion references a file not present in diff.
- Excessive test modifications (> 10 assertion removals total).

---

## Output Requirements

Final verdict: **ACCEPTED** or **REJECTED**

Followed by concise reasoning (2–4 sentences) citing which checks passed or failed.

**CSV update:** The verdict must be persisted to `instances_output.csv`. Find the row matching `problem_id` and set: `status` = `"done"`, `qa_result` = `"accepted"` or `"rejected"` (lowercase), `qa_notes` = the reasoning text, `processed_at` = ISO 8601 timestamp. The orchestrator (qa-runner) or the instance-evaluator agent performs this update.

**Repo store (when accepted):** When the verdict is **accepted**, the instance-evaluator must also update `data/repo_store.json`: add this instance’s rubric to `processed_rubrics` for its repo (derive repo_id by stripping the trailing `-NN` from `problem_id`). No separate manual step.
