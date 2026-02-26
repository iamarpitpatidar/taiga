# QA Runner

Orchestrates the full QA validation pipeline for Meta 800 dataset selection. Uses the database (`instances` table) as the single source of truth.

This command is the single source of truth for evaluating a prepared TAIGA instance.

**⚠️ CRITICAL: Before starting, read `.claude/lessons_learned.md` to avoid common mistakes that have caused failures in the past.**

---

## Command Format

```bash
/qa-runner <job_id>
```

### Example
```bash
/qa-runner 483570b9-b936-47bc-8e26-107a70bd808f
```

**Note:** The qa-runner works on `job_id` (the UUID for a specific TAIGA run), NOT `problem_id`. The instance directory structure is `instances/<problem_id>/<job_id>/`.

---

# Working Directory

**Project Root**
`MetaCursorAgent/`

**Instance Directory**
`instances/<problem_id>/<job_id>/`

**Injected Repository**
`instances/<problem_id>/<job_id>/injected_repo/`

**Database**
`instances` table in the project database

**Repo Store (database):**
- `repositories` table — repo-level averages (built automatically from instances)
- `rubrics` table — processed rubrics for duplicate checking
- Managed by `src/db_helper.py` and `.claude/scripts/check_rubric_duplicates.py`

---

# Full Pipeline Flow (End-to-End)

Use this order for a full run. `repo_store.py` is **never run as a script**; it is a **module** imported and used only by `prepare_instance.py` as below.

| Step | Command / Action                                                                                                                                                                                                                                                               | Who invokes | What uses repo_store |
|------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------|----------------------|
| **1. Download** | `python boot.py download [--limit N]`                                                                                                                                                                                                                                          | User or CI | **Yes** — at start, builds repo averages in database (`repositories` table). Skips rows where per-instance score not in [0.4, 0.8], **num_oscillating** present and < 3, or **repo average** (all instances of that repo) not in [0.4, 0.8]. Downloads rubric + injected repo, clones original; writes `download_status`. |
| **2. Prefilter** | `python boot.py prefilter [--limit N]`                                                                                                                                                                                                                             | User or CI | **Yes** — (re)builds repo store from database; for each downloaded instance with empty `status`, checks num_oscillating ≥ 3, repo average in [0.4, 0.8], structural checks (metadata, rubric, dirs), and **cross-instance duplicate** (same file+function as an already-accepted instance of same repo). Rejects with `status=done`, `qa_result=rejected`, writes `result.json`. |
| **3. QA pipeline** | Run validate_structure.py → diff-analyzer → check_score.py → rubric-validator → instance-evaluator (see Phases below)                                                                                                                                                                 | User / qa-runner / orchestrator | **No** — scripts/agents read instance dirs and database only. |
| **4. Update database + repo store** | Set `status=done`, `qa_result=accepted` or `rejected`, `qa_notes`, `processed_at` in database. **If accepted**, must also call `db_helper.add_accepted_rubric()` to store the rubric in `rubrics` table for future duplicate checking. | Instance-evaluator (or orchestrator) | **Yes when accepted** — rubric is stored in database so prefilter/rubric-validator can detect duplicates later. No manual step. |

**Summary:** Repo store is updated **automatically** in the database: (1) repo averages in steps 1 and 2 (download and prefilter) via `db_helper.build_repo_store()`; (2) **processed rubrics** when the instance-evaluator marks an instance **accepted** via `db_helper.add_accepted_rubric()`.

---

# Pre-Run Checklist (Mandatory)

Before beginning evaluation, verify:

1. `instances/<problem_id>/<job_id>/` exists.
2. The following files exist:
   - `injected_repo/`
   - `rubric.json`
   - `metadata.json`
3. Database contains exactly one row matching `job_id`.
4. Row `status` is NOT `"done"`.
5. No `QA_LOCK` file exists inside instance directory.
6. `metadata.json` contains:
   - `problem_id` (e.g., `simdutf__simdutf-07`)
   - `job_id` (UUID, e.g., `483570b9-b936-47bc-8e26-107a70bd808f`)
   - `average_score` (or `score_mean` from database)

If any check fails → **abort immediately**.

**Important:** The `job_id` is the primary key for processing. Multiple jobs can exist for the same `problem_id`.

---

# Database Schema (`instances` table)

**Source of truth:** `instances` table in the project database. All columns below must be present.

**Primary Key:** `job_id` (UUID) — qa-runner operates on job_id, NOT problem_id

| Column                | Description |
|-----------------------|-------------|
| job_id                | **PRIMARY KEY** — TAIGA job identifier (UUID, e.g. `483570b9-b936-47bc-8e26-107a70bd808f`) |
| problem_id            | Instance ID: `owner__repo-NN` (e.g. `simdutf__simdutf-07`). Multiple jobs can share the same problem_id. |
| num_attempts          | Integer — number of TAIGA attempts |
| score_mean            | Float — average detection score; must be in **[0.4, 0.8]** for inclusion |
| score_max             | Float — max score across attempts |
| score_min             | Float — min score across attempts |
| raw_total_score_max   | Float — raw total score max |
| raw_total_score_mean  | Float — raw total score mean |
| created_at_first      | ISO 8601 — first run timestamp |
| completed_at_last     | ISO 8601 — last run timestamp |
| num_oscillating       | Integer — reject if **present, numeric, and < 3** (column may be empty; then do not reject on this rule) |
| download_status       | `""` \| `downloaded` \| `skipped_*` \| `error: ...` — set by `prepare_instance.py download` |
| status                | `""` \| `in_progress` \| `done` — QA pipeline state |
| qa_result             | `accepted` \| `rejected` — set after QA (or prefilter reject) |
| qa_notes              | 2–4 sentence explanation; required when `status == done` |
| processed_at          | ISO 8601 timestamp (UTC) when row was finalized |

---

# Concurrency Safety

## Locking Protocol

Before processing:

1. Create `instances/<problem_id>/<job_id>/QA_LOCK`.
2. **Set `status = in_progress`** using:
   ```bash
   python .claude/scripts/update_qa_status.py <job_id> --status in_progress
   ```
3. **Verify write succeeded** (script returns JSON with `"success": true`).

If verification fails → abort and remove lock.

After processing:

- **Update database atomically** using:
  ```bash
  python .claude/scripts/update_qa_status.py <job_id> \
    --status done \
    --result accepted \
    --notes "Instance accepted. Score 0.46 in ideal range..."
  ```
- **Verify update succeeded** (check `"success": true` in JSON output)
- If success, proceed to cleanup
- If failed, ABORT and leave QA_LOCK for debugging
- Remove `QA_LOCK`.

## ⚠️ CRITICAL: Use Scripts for ALL Database Operations

**NEVER write inline Python commands** to query or update the database. This causes:
- Lost updates due to multiple connections
- Broken transaction management
- Inconsistent database state

**ALWAYS use the update script:**
```bash
# ✅ Set to in_progress:
python .claude/scripts/update_qa_status.py <job_id> --status in_progress

# ✅ Mark as done (accepted):
python .claude/scripts/update_qa_status.py <job_id> \
  --status done --result accepted --notes "..."

# ✅ Mark as failed (rejected):
python .claude/scripts/update_qa_status.py <job_id> \
  --status failed --result rejected --notes "Early termination: suspicious diff"
```

**Example of WRONG approach:**
```bash
# ❌ NEVER DO THIS:
python -c "import sqlite3; conn = sqlite3.connect('data/database.sqlite'); ..."
```

---

# Atomic Database Update Rules

**CRITICAL:** Use `.claude/scripts/update_qa_status.py` for ALL status updates.

## Status Enum
- `in_progress` - QA pipeline is running (set after creating QA_LOCK)
- `failed` - Early termination due to hard failure (set with qa_result=rejected)
- `done` - QA complete (set with qa_result=accepted or rejected)

## Result Enum
- `accepted` - Instance passed QA (only for status=done)
- `rejected` - Instance failed QA (for status=done or status=failed)
- `""` (empty) - No result yet (for status=in_progress)

## Script Guarantees

The script handles:
1. Atomic updates by `job_id` (PRIMARY KEY)
2. Transaction management (commit + verification)
3. Status transition validation:
   - `in_progress`: can only be set from empty status
   - `done`/`failed`: can only be set from `in_progress`
4. Required fields validation:
   - `in_progress`: no result/notes required
   - `done`/`failed`: result and notes required
5. Error handling with rollback

## Usage Examples

```bash
# 1. Start processing (after creating QA_LOCK):
python .claude/scripts/update_qa_status.py <job_id> --status in_progress

# 2a. Complete with acceptance:
python .claude/scripts/update_qa_status.py <job_id> \
  --status done --result accepted --notes "Instance accepted. Score 0.46..."

# 2b. Complete with rejection:
python .claude/scripts/update_qa_status.py <job_id> \
  --status done --result rejected --notes "Rubric has only 6 criteria (minimum 8)"

# 2c. Early termination (failed):
python .claude/scripts/update_qa_status.py <job_id> \
  --status failed --result rejected --notes "Early termination: suspicious diff"
```

## Verification Protocol

After running the script:
1. Parse JSON output
2. Check `"success": true`
3. If success=false → ABORT, log error, do NOT cleanup
4. If success=true → proceed to next step

**Never:**
- Write inline Python to update database
- Update database without checking success
- Cleanup instance before database update succeeds

---

# Immediate Reject Conditions

Reject immediately if any condition holds:

- `rubric.json` cannot be parsed.
- Rubric contains fewer than **8 criteria** (minimum is 8, more allowed).
- `score_mean` outside **0.4–0.8**.
- `num_oscillating` is present, numeric, and **< 3**.
- **Repo average** score (mean of `score_mean` across ALL instances of the same repo in database) outside **[0.4, 0.8]**. Computed once at pipeline start by `prepare_instance.py prefilter`.
- **Duplicate bug location**: any rubric criterion targets the same (file, function/symbol) as an already-accepted instance of the same repo (stored in `data/repo_store.json`).
- `injected_repo/` missing.
- More than 25% of **source** files differ structurally (excluding packaging artifacts).
- Entire **source** directories deleted (not tests/docs/CI — those are packaging artifacts).
- Diff classified as **suspicious rewrite** (based on real source changes, not packaging artifacts).
- `metadata.json` missing required fields.

**Not rejection triggers** (warnings only):
- `__init__.py` renamed to `_init_.py` — packaging artifact.
- Individual test files modified/removed because they assert values changed by the injected bug — expected and acceptable.
- `.github/`, docs missing — TAIGA stripping artifact.
- `Dockerfile` added — extraction artifact.

**Suspicious test changes (flag for rejection):**
- Entire `tests/` directory deleted when original repo has one — the test suite covers many un-bugged functions; wholesale deletion is not justified by 8 bugs.
- Test assertions weakened or removed for bugs NOT covered by the rubric — suggests intentional test evasion.

If rejected during pre-check:

- Set `qa_result = rejected`
- Add clear reason in `qa_notes`
- Set `status = done`
- Set `processed_at`
- Update database
- Remove lock
- Stop execution

---

# Diff Size Estimation Rules

The diff-analyzer must classify modification scale.

**Terminology:** `acceptable` = `localized` OR `moderate`. `suspicious` → reject.

## Acceptable (localized | moderate)
- 8–20 files modified
- Changes must be small, localized modifications consistent with subtle bug injection
- No large refactors, file rewrites, or structural reorganizations

## Suspicious
- >30% repository files changed
- Large refactors
- File rewrites
- Structural reorganizations
- Entire modules replaced
- Massive line insertions/deletions
- Formatting-wide rewrites

If classified as suspicious → reject.

---

# QA Phases

Phases are organized for maximum parallelization. Independent agents run concurrently; dependent agents wait for upstream results.

---

## Phase 1 — Parallel Analysis (run simultaneously)

Launch **all three** of these checks in parallel:

### 1a. Dataset Loader
**Script:** `.claude/scripts/validate_structure.py` (replaces dataset-loader agent)

**Usage:**
```bash
python .claude/scripts/validate_structure.py instances/<problem_id>/<job_id>
```

Responsibilities:
- Count rubric criteria
- Extract metadata
- Confirm directory structure
- Compute file counts

Output (canonical schema):
- problem_id, average_score, rubric_entry_count, rubric_criteria
- files_in_injected_repo, directory_size_ratio
- rubric_valid, rubric_message, parse_errors (if applicable)

**Performance:** ~50-100ms (script) vs ~3-5s (agent)

**⚠️ IMPORTANT:** Run the script and parse its JSON output. Do NOT manually verify with inline Python or grep commands unless the script fails.

### 1b. Diff Analysis
**Script:** `.claude/scripts/analyze_diff.py` (replaces diff-analyzer agent)

**Usage:**
```bash
python .claude/scripts/analyze_diff.py instances/<problem_id>/<job_id>
```

Responsibilities:
- Compare injected vs original (if available)
- Count changed files (source files only, excluding packaging artifacts)
- Estimate line modifications
- Detect structural rewrites
- Classify diff: `localized` | `moderate` (= acceptable) or `suspicious`

Output:
- changed_files, changed_lines_estimate, diff_classification, flags, packaging_artifacts

**Performance:** ~100-200ms (script) vs ~5-7s (agent)

**⚠️ IMPORTANT:** Trust the script's classification. Do NOT manually verify unless the output is unclear.

### 1c. Score Validation
**Script:** `.claude/scripts/check_score.py` (replaces scoring-engine agent)

**Usage:**
```bash
python .claude/scripts/check_score.py --metadata instances/<problem_id>/<job_id>/metadata.json
```

Responsibilities:
- Confirm score within 0.4–0.8
- Classify band and produce remark

Output:
- score_classification, score_band, remark

**Performance:** ~10ms (script) vs ~2-3s (agent)

**⚠️ IMPORTANT:** Use the script output directly. Do NOT manually recalculate scores.

---

## Early Termination Gate (after Phase 1)

Before proceeding to Phase 2, check for **hard failures** that make further analysis unnecessary:

| Condition | Source | Action |
|-----------|--------|--------|
| `diff_classification == "suspicious"` | diff-analyzer | **REJECT immediately** — skip remaining phases |
| `score_classification == "outside_range"` | scoring-engine | **REJECT immediately** — skip remaining phases |
| `rubric_entry_count < 8` | dataset-loader | **REJECT immediately** — skip remaining phases |
| `parse_errors` non-empty | dataset-loader | **REJECT immediately** — skip remaining phases |
| `files_changed_count > 20` AND structural flags present | diff-analyzer | **REJECT immediately** — skip remaining phases |

If early termination triggers:
1. Set `qa_result = rejected`, `status = done` in database.
2. Write `result.json` to instance directory with reason.
3. Remove lock and move to next instance.

This saves significant time by avoiding rubric deep-analysis and LLM reasoning on clearly-broken instances.

---

## Phase 2 — Rubric Deep Validation (requires diff-analyzer output)

**Script:** `.claude/scripts/validate_rubric.py` (replaces rubric-validator agent for structural checks)

**Usage:**
```bash
python .claude/scripts/validate_rubric.py \
  instances/<problem_id>/<job_id>/rubric.json \
  instances/<problem_id>/<job_id>/injected_repo
```

Responsibilities:
- Confirm 8 criteria with valid structure
- Ensure each criterion references a real file
- **Critical: Identify core files (top 20-25% by importance) and verify rubric targets them**
- Core file detection uses: LOC, import frequency, directory depth, naming patterns
- Pattern-based quality checks (line numbers, diff language, self-describing keywords)
- Apply multi-bug-per-file rule (soft — allowed for small repos)
- Check for early termination triggers (missing weights, invalid paths)

Output:
- rubric_valid: true/false
- early_termination: true/false
- core_analysis: {core_files_count, rubric_targets_core, core_targeting_percentage}
- needs_manual_review: true if warnings present

**Performance:** ~50-100ms (script) for structural validation

**Note:** Returns exit code 2 if needs manual LLM review for subjective quality checks

**⚠️ IMPORTANT:** If exit code is 0, accept the validation. If exit code is 2, perform manual LLM review (but still use script output as baseline).

---

## Phase 3 — Final Decision (requires all upstream outputs)

**Responsibilities:**
- Consume outputs from validate_structure.py, analyze_diff.py, check_score.py, validate_rubric.py
- Apply deterministic acceptance matrix
- Apply qualitative QA Playbook checks (manual LLM reasoning)
- **Update database BEFORE writing result.json**
- Write `result.json` to instance directory (optional audit log)

| Condition | Verdict |
|-----------|---------|
| suspicious diff | reject |
| rubric hard failure (early_termination) | reject |
| score out of range | reject |
| structural deletion | reject |
| duplicate bugs (same exact mutation) | reject |
| all checks pass | accept |

**CRITICAL ORDER OF OPERATIONS:**

1. Complete evaluation (accept or reject decision)
2. **Update database** using:
   ```bash
   python .claude/scripts/update_qa_status.py <job_id> \
     --status done \
     --result accepted \
     --notes "Instance accepted. Score 0.46 in ideal range [0.4, 0.5]. All 12 rubric criteria reference real files..."
   ```
3. **Verify database update succeeded** (parse JSON output, check `"success": true`)
4. If accepted: Add rubric to repo store using `db_helper.add_accepted_rubric(repo_id, problem_id, rubric)`
5. Write `result.json` to instance directory (optional audit log)
6. Remove `QA_LOCK` file
7. Run cleanup: `.claude/scripts/cleanup_instance.sh <problem_id> <job_id>`

**⚠️ CRITICAL:** If step 3 verification fails (success=false), ABORT immediately:
- Do NOT add rubric to repo store
- Do NOT write result.json
- Do NOT remove QA_LOCK
- Do NOT cleanup instance
- Log the error for debugging

---

# Final Quality Gate

Before marking as accepted, confirm:

1. At least 8 rubric criteria (8 minimum, more allowed).
2. Diff classified as acceptable (localized | moderate).
3. No structural rewrite (small, localized modifications only; no large refactors, file rewrites, or structural reorganizations).
4. Score within range.
5. Rubric valid (or soft-valid with acceptable multi-bug-per-file for small repos).
6. All agents produced structured output.
7. No unexpected repository deletion.
8. No **duplicate** bugs (same exact mutation). Similar bugs (same category) are acceptable.

If any check fails → force reject.

---

# Auto-Advance Batch Mode

The pipeline supports automatic sequential processing of all pending instances.

## Invocation

```bash
/qa-runner                                                # Process ALL pending instances
/qa-runner --limit 10                                     # Process next 10 pending instances
/qa-runner --offset 55 --limit 55                         # Process rows 55-109 (for parallel workers)
/qa-runner 483570b9-b936-47bc-8e26-107a70bd808f           # Process single instance by job_id
```

## Parallel Worker Distribution

For team-based parallel processing, divide the database records into chunks using `--offset` and `--limit`:

```
Person 1: /qa-runner --offset 0   --limit 55    # rows 0-54
Person 2: /qa-runner --offset 55  --limit 55    # rows 55-109
Person 3: /qa-runner --offset 110 --limit 55    # rows 110-164
Person 4: /qa-runner --offset 165 --limit 55    # rows 165-219
Person 5: /qa-runner --offset 220 --limit 52    # rows 220-271
```

Each worker operates on independent rows. Database transactions ensure no conflicts.

## Batch Loop Logic

When no explicit `problem_id` is provided:

```
OUTER LOOP (continuous polling):
  1. Query database for fresh data
  2. Filter rows where: status is empty AND download_status == "downloaded"
  3. Apply --offset and --limit if specified
  4. If no rows found:
     - Print "No pending instances, waiting for downloads..."
     - Sleep 10 seconds
     - GOTO step 1 (reload database and check again)

  5. FOR EACH pending row (in order):
     a. Read problem_id and job_id from row (job_id is PRIMARY KEY for processing)
     b. Check instance directory exists (instances/<problem_id>/<job_id>/)
        - If missing → mark as rejected by job_id ("instance directory not found"), continue
     c. Set status = "in_progress" for this job_id, update database
     d. Create QA_LOCK at instances/<problem_id>/<job_id>/QA_LOCK
     e. Run full QA pipeline (Phase 1 → Early Termination check → Phase 2 → Phase 3)
     f. Update database with verdict (by job_id)
     g. Write result.json to instances/<problem_id>/<job_id>/result.json
     h. Remove QA_LOCK
     i. **Cleanup processed instance:** Run `.claude/scripts/cleanup_instance.sh <problem_id> <job_id>`
     j. Print progress: "[N processed] <problem_id> (job: <job_id>) → verdict (Xs)"
     k. **Query database and check for new downloads** (GOTO step 1)

  6. After processing all available rows:
     - Print current summary: "X accepted, Y rejected, Z total processed"
     - GOTO step 1 (query database and check for more)

EXIT CONDITIONS:
  - User manually interrupts (Ctrl+C)
  - Never exits automatically (continuous polling mode)
```

## Progress Tracking

After each instance, print:

```
[Processed: 3] zarr-developers__zarr-python-07 (job: a1b2c3d4-...) → REJECTED (early termination: 47s)
[Processed: 4] scipy__scipy-12 (job: e5f6g7h8-...) → ACCEPTED (full pipeline: 92s)
```

Print summary every 10 instances:
```
=== QA Summary (10 instances processed) ===
Accepted: 6 | Rejected: 4 | Rate: 0.60
Waiting for more downloads...
```

## Error Recovery

If an instance fails unexpectedly:
1. Mark as `rejected` (by job_id) with `qa_notes = "error: <message>"`
2. Remove QA_LOCK at instances/<problem_id>/<job_id>/QA_LOCK
3. **Continue to next instance** (do not halt the batch)

## Stop Conditions

The pipeline runs in **continuous polling mode** and only stops when:
- User manually interrupts (Ctrl+C)

**IMPORTANT:** The pipeline never exits automatically. It continuously:
1. Processes all available downloaded instances
2. Waits 10 seconds when queue is empty
3. Queries database to check for new downloads
4. Repeats indefinitely

This allows qa-runner to work in parallel with prepare_instance.py downloads.

---

# Logging Rules

Each run must:

- Print structured summary.
- Record reasoning in qa_notes (2–4 sentences).
- Avoid verbose token-heavy analysis in database records.

---

# Deliverable

After completion:

- `status = done` (updated by job_id)
- `qa_result` set
- `qa_notes` concise and clear
- `processed_at` ISO timestamp
- Lock removed
- `instances/<problem_id>/<job_id>/result.json` written (full evaluation log)

The `result.json` file persists in the instance directory and contains the complete evaluation record: verdict, confidence, reason codes, score details, diff summary, rubric status, and timestamp. This serves as an audit log and enables re-evaluation without re-running agents.

When the instance-evaluator marks an instance **accepted**, it must also call `db_helper.add_accepted_rubric()` to store the rubric in the database (keyed by problem_id for duplicate checking across jobs of the same problem).

Instance is now finalized for Meta 800 selection pipeline.

---

# Common Mistakes to Avoid

**⚠️ CRITICAL: See `.claude/lessons_learned.md` for detailed examples of common agent failures and how to avoid them.**

Key reminders:
- ✅ **Use existing scripts** (`.claude/scripts/*.py`) - NEVER write inline `python -c "..."` for operations
- ✅ **Verify database updates** - Check `"success": true` before proceeding to cleanup
- ✅ **Database first, cleanup last** - Update database BEFORE any destructive operations
- ✅ **Trust script outputs** - Don't over-verify with 20+ manual commands
- ✅ **Use correct primary key** - Always use `job_id` (not `problem_id`) for updates
- ✅ **Follow state machine** - Don't skip states (must go: empty → in_progress → done/failed)

**Read `.claude/lessons_learned.md` at the start of complex tasks to avoid repeating past mistakes.**
