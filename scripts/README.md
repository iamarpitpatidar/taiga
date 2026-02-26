# QA Pipeline Scripts

Fast deterministic validation scripts that replace LLM agents for better performance.

## Scripts

### 1. `validate_structure.py` (replaces dataset-loader agent)

Validates instance directory structure and extracts metadata.

**Usage:**
```bash
# JSON output (for pipeline integration)
python3 scripts/validate_structure.py instances/<problem_id>/<job_id>

# Human-readable summary
python3 scripts/validate_structure.py instances/<problem_id>/<job_id> --output summary
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
$ python3 scripts/validate_structure.py instances/fluent__fluentd-04/483570b9-b936-47bc-8e26-107a70bd808f --output summary

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
python3 scripts/check_score.py --score 0.45

# Read from metadata.json
python3 scripts/check_score.py --metadata instances/<problem_id>/<job_id>/metadata.json

# Human-readable summary
python3 scripts/check_score.py --score 0.45 --output summary
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
$ python3 scripts/check_score.py --score 0.45 --output summary

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
    ["python3", "scripts/validate_structure.py", instance_dir, "--output", "json"],
    capture_output=True, text=True
)
data = json.loads(result.stdout)
```

**Performance improvement:**
- Agent call: ~3-5 seconds + API cost
- Script call: ~50-100ms, free

---

## Other Agent Scripts (TODO)

Consider converting:
- ⚠️ **diff-analyzer** → 90% scriptable (file counting, line diffs, classification)
- ⚠️ **rubric-validator** → 70% scriptable (structural checks, file existence, cross-instance duplicates)

Keep as agents:
- ✅ **instance-evaluator** (needs LLM reasoning for final decision)
