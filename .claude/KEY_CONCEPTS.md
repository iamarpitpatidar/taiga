# Key Concepts for QA Pipeline

## Problem ID vs Job ID

**Critical distinction:**

- **`problem_id`**: Instance identifier (e.g., `simdutf__simdutf-07`)
  - Format: `owner__repo-NN`
  - Multiple jobs can share the same problem_id
  - Used for rubric duplicate checking (across all jobs of the same problem)

- **`job_id`**: TAIGA run identifier (UUID, e.g., `483570b9-b936-47bc-8e26-107a70bd808f`)
  - **PRIMARY KEY** for qa-runner operations
  - Unique per TAIGA run
  - Used for database updates, file operations, locking

## Directory Structure

```
instances/
├── <problem_id>/                     # e.g., simdutf__simdutf-07
│   ├── <job_id_1>/                   # e.g., 483570b9-b936-47bc-8e26-107a70bd808f
│   │   ├── injected_repo/
│   │   ├── rubric.json
│   │   ├── metadata.json
│   │   ├── result.json               # Written by qa-runner
│   │   └── QA_LOCK                   # Created during processing
│   ├── <job_id_2>/                   # Another run for same problem
│   │   └── ...
```

## When to Use Which ID

### Use `job_id`:
- ✅ qa-runner command: `/qa-runner <job_id>`
- ✅ Database queries: `WHERE job_id = ?`
- ✅ Database updates: `UPDATE instances SET status = 'done' WHERE job_id = ?`
- ✅ File paths: `instances/<problem_id>/<job_id>/`
- ✅ Locking: `instances/<problem_id>/<job_id>/QA_LOCK`

### Use `problem_id`:
- ✅ Rubric duplicate checking (across all jobs of same problem)
- ✅ Repo store operations: `add_accepted_rubric(repo_id, problem_id, rubric)`
- ✅ Deriving repo_id: `get_repo_id(problem_id)` → strips `-NN` suffix

## Database Operations

```python
# Query by job_id (primary key)
instance = get_instance_by_job_id(job_id)

# Update by job_id
update_qa_status(job_id, qa_result="accepted", ...)

# Store rubric by problem_id (for duplicate checking)
add_accepted_rubric(repo_id, problem_id, rubric)

# Check duplicates by problem_id
prior_rubrics = get_prior_rubrics(repo_id, exclude_problem_id=problem_id)
```

## Why This Matters

1. **Multiple attempts**: Same problem can have multiple TAIGA runs (jobs)
2. **Duplicate detection**: Need to check across all jobs of a problem
3. **Quality comparison**: When duplicates found, keep the better scoring job
4. **Data integrity**: job_id ensures unique row identification

## Example

```
Problem: simdutf__simdutf-07
Jobs:
  - 483570b9-b936-47bc-8e26-107a70bd808f (score: 0.45) ← better
  - a1b2c3d4-e5f6-g7h8-i9j0-k1l2m3n4o5p6 (score: 0.75)

QA runs independently on each job_id.
Rubric duplicates are checked at problem_id level.
```
