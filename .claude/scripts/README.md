# QA Pipeline Scripts

Fast deterministic validation scripts that replace LLM agents for better performance.

## Scripts

### 1. `validate_structure.py` (replaces dataset-loader agent)

Validates instance directory structure and extracts metadata.

**Usage:**
```bash
# JSON output (for pipeline integration)
python3 .claude/scripts/validate_structure.py instances/<problem_id>/<job_id>

# Human-readable summary
python3 .claude/scripts/validate_structure.py instances/<problem_id>/<job_id> --output summary
```

**Output:** Same JSON format as dataset-loader agent
- `problem_id`, `average_score`, `rubric_entry_count`
- `rubric_criteria` (abbreviated list)
- `files_in_injected_repo`, `rubric_valid`, `rubric_message`
- `parse_errors` (empty if valid)

**Exit codes:**
- `0` = validation passed (no errors)
- `1` = validation failed (has errors)

**Example:**
```bash
$ python3 .claude/scripts/validate_structure.py instances/fluent__fluentd-04/483570b9-b936-47bc-8e26-107a70bd808f --output summary

Problem ID: fluent__fluentd-04
Average Score: 0.4
Rubric Entries: 8/8
Rubric Valid: True
Files in Repo: 567

✓ All structural checks passed
```

---

### 2. `check_score.py` (replaces scoring-engine agent)

Classifies TAIGA detection scores for Meta 800 inclusion range [0.4, 0.8].

**Usage:**
```bash
# Direct score value
python3 .claude/scripts/check_score.py --score 0.45

# Read from metadata.json
python3 .claude/scripts/check_score.py --metadata instances/<problem_id>/<job_id>/metadata.json

# Human-readable summary
python3 .claude/scripts/check_score.py --score 0.45 --output summary
```

**Output:** Same JSON format as scoring-engine agent
- `average_score`, `score_classification` (within_range | outside_range)
- `score_band` (ideal | acceptable | borderline | too_easy | too_hard)
- `remark`, `notes`

**Exit codes:**
- `0` = score in range [0.4, 0.8]
- `1` = score outside range

**Example:**
```bash
$ python3 .claude/scripts/check_score.py --score 0.45 --output summary

Score: 0.45
Classification: within_range
Band: ideal

Strong candidate — bugs are well-calibrated: detectable with effort but not trivially obvious.

Notes: Score 0.450 is in ideal range [0.4, 0.5]
```

---

## Score Bands

| Range       | Band       | Classification | Meta 800 |
|-------------|------------|----------------|----------|
| < 0.4       | too_easy   | outside_range  | ❌ Reject |
| 0.4 - 0.5   | ideal      | within_range   | ✅ Prefer |
| 0.5 - 0.7   | acceptable | within_range   | ✅ Accept |
| 0.7 - 0.8   | borderline | within_range   | ⚠️ Careful |
| > 0.8       | too_hard   | outside_range  | ❌ Reject |

---

## Integration with QA Pipeline

Replace agent calls with script calls in Phase 1:

**Before (agent):**
```python
# Spawn dataset-loader agent
result = agent.run("dataset-loader", instance_dir)
```

**After (script):**
```python
# Run fast script
result = subprocess.run(
    ["python3", ".claude/scripts/validate_structure.py", instance_dir, "--output", "json"],
    capture_output=True, text=True
)
data = json.loads(result.stdout)
```

**Performance improvement:**
- Agent call: ~3-5 seconds + API cost
- Script call: ~50-100ms, free

---

### 3. `check_rubric_duplicates.py` (database-backed duplicate checking)

Checks for duplicate rubric locations across instances of the same repository.

**Usage:**
```bash
# Check for duplicates
python3 .claude/scripts/check_rubric_duplicates.py <problem_id> <path/to/rubric.json>

# JSON output (for pipeline integration)
python3 .claude/scripts/check_rubric_duplicates.py <problem_id> <path/to/rubric.json> --output json
```

**What it checks:**
- Cross-instance duplicate bug locations (same file + function)
- Repo average score (must be in [0.4, 0.8])
- Uses database (`rubrics` table) for persistent storage

**Output:**
- `has_duplicates`: bool
- `duplicate_details`: list of conflict descriptions
- `repo_id`, `repo_average`, `instance_score`

**Exit codes:**
- `0` = no duplicates found
- `1` = duplicates found or error

**Example:**
```bash
$ python3 .claude/scripts/check_rubric_duplicates.py simdutf__simdutf-07 instances/simdutf__simdutf-07/rubric.json

Problem ID: simdutf__simdutf-07
Repo ID: simdutf__simdutf
Repo Average: 0.52
Repo Average OK: True
Instance Score: 0.48

✅ All rubric locations are unique for this repo
```

**Integration:**
```python
from scripts.check_rubric_duplicates import check_duplicate_locations, store_accepted_rubric

# Check for duplicates
has_dup, details = check_duplicate_locations(repo_id, rubric, problem_id, score)

# Store accepted rubric
store_accepted_rubric(problem_id, rubric)
```

---

## Database Schema

The repo store is now database-backed:

**`repositories` table:**
- `repo_id` (PK) - e.g., "owner__repo"
- `average_score` - mean score across all instances
- `instance_count` - number of instances for this repo

**`rubrics` table:** (currently named `accepted_rubrics` in schema)
- `repo_id` - foreign key to repositories
- `problem_id` (PK) - e.g., "owner__repo-07"
- `rubric_json` - JSON array of accepted rubric criteria

> **Note:** Table is currently named `accepted_rubrics` in the database. Consider renaming to `rubrics` for clarity.

**Managed by:** `src/db_helper.py`

---

## Other Agent Scripts (TODO)

Consider converting:
- ⚠️ **diff-analyzer** → 90% scriptable (file counting, line diffs, classification)
- ⚠️ **rubric-validator** → 70% scriptable (structural checks, file existence, cross-instance duplicates)

Keep as agents:
- ✅ **instance-evaluator** (needs LLM reasoning for final decision)
