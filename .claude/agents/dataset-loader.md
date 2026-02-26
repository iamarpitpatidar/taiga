---
name: dataset-loader
description: Loads prepared instance data from a downloaded instance directory
---

## Input

**Working directory:** `instances/<problem_id>/` or `qa_results/<problem_id>/` (passed from qa-runner; path depends on pipeline).

**problem_id format:** `owner__repo-NN` (e.g. `simdutf__simdutf-07`).

---

## Expected File Structure

```
<instance_dir>/
├── metadata.json      # job_id, problem_id, average_score (from prepare_instance or Python orchestrator)
├── summary.json       # (Optional) Condensed input for Cursor: problem_id, average_score, diff_summary, rubric_valid, rubric_message
├── rubric.json        # Must contain rubric array with exactly 8 criteria
├── injected_repo/     # Repo with bugs injected (extracted from TAIGA repo zip)
└── repo.zip          # (Temporary; may be deleted after extraction)
```

Optional (if present): `selected_bugs.md`, `proposal.md` — useful for cross-checking rubric criteria against documented bug list. If present, extract bug-file mappings (one per row in selected_bugs) for downstream validation that each rubric criterion maps to a documented bug in a changed file.

---

## Extraction Tasks

**From metadata.json (or CSV row — `instances_output.csv`, score column: `score_mean`):**
- `job_id` — TAIGA run ID
- `problem_id` — Instance ID
- `average_score` (float) — TAIGA average detection score
- `extraction_issues` (array, optional) — Pre-check issues found during extraction. If non-empty, these should be propagated to `parse_errors` for downstream agents.

**From summary.json (if present; hybrid pipeline):**
- `problem_id`, `average_score`, `diff_summary`, `rubric_valid`, `rubric_message`
- Use this when Python orchestrator has pre-computed diff and rubric validation.

**From rubric.json:**
- Presence of top-level `"rubric"` key
- Length of rubric array (must be 8)
- For each entry: `criterion` (string), `weight` (must be 1)
- Extract file paths and function names referenced in criteria (for downstream mapping checks)

**From injected_repo/:**
- Total file count
- List of files (for comparing with rubric file paths)
- Baseline file structure (for diff comparison)

---

## Output Format (Canonical Schema)

Produce a single structured summary for downstream agents (especially instance-evaluator). Use this canonical schema:

```
{
  "problem_id": "...",
  "average_score": number,
  "rubric_entry_count": 8,
  "rubric_criteria": ["abbreviated criterion 1", "abbreviated criterion 2", ...],
  "files_in_injected_repo": number,
  "directory_size_ratio": number,
  "rubric_valid": true | false,
  "rubric_message": "...",
  "parse_errors": []
}
```

**Field notes:**
- `rubric_criteria`: Abbreviated list of each criterion (for instance-evaluator); include file path and function/class if present.
- `rubric_valid` / `rubric_message`: If this agent performs basic structural validation, populate these; otherwise rubric-validator owns them.
- `parse_errors`: List any missing-file, malformed JSON, or other errors—instance-evaluator will REJECT if non-empty.

---

## Error Handling

- If `rubric.json` is missing or malformed → add to `parse_errors`; instance-evaluator will REJECT.
- If `metadata.json` missing but CSV/context has `score_mean` (or `average_score`) → use that for `average_score`.
- If `injected_repo/` missing → add to `parse_errors`; cannot proceed.
