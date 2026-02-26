# QA Runner

Orchestrates the full QA validation pipeline for Meta 800 dataset selection. Uses the database (`instances` table) as the single source of truth.

This command is the single source of truth for evaluating a prepared TAIGA instance.

---

## Command Format

```bash
/qa-runner <problem_id>
```

### Example
```bash
/qa-runner simdutf__simdutf-07
```

---

# Working Directory

**Project Root**  
`MetaCursorAgent/`

**Instance Directory**  
`instances/<problem_id>/`

**Injected Repository**
`<instance_dir>/injected_repo/`

**Database**
`instances` table in the project database

**Repo store (automatic):**
`data/repo_store.json` — built and used by `prepare_instance.py`; do not edit by hand.

---

# Full Pipeline Flow (End-to-End)

Use this order for a full run. `repo_store.py` is **never run as a script**; it is a **module** imported and used only by `prepare_instance.py` as below.

| Step | Command / Action                                                                                                                                                                                                                                                               | Who invokes | What uses repo_store |
|------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------|----------------------|
| **1. Download** | `python boot.py download [--limit N]`                                                                                                                                                                                                                                          | User or CI | **Yes** — at start, builds `data/repo_store.json` from database (repo averages). Skips rows where per-instance score not in [0.4, 0.8], **num_oscillating** present and < 3, or **repo average** (all instances of that repo) not in [0.4, 0.8]. Downloads rubric + injected repo, clones original; writes `download_status`. |
| **2. Prefilter** | `python boot.py prefilter [--limit N]`                                                                                                                                                                                                                             | User or CI | **Yes** — (re)builds repo store from database; for each downloaded instance with empty `status`, checks num_oscillating ≥ 3, repo average in [0.4, 0.8], structural checks (metadata, rubric, dirs), and **cross-instance duplicate** (same file+function as an already-accepted instance of same repo). Rejects with `status=done`, `qa_result=rejected`, writes `result.json`. |
| **3. QA pipeline** | Run validate_structure.py → diff-analyzer → check_score.py → rubric-validator → instance-evaluator (see Phases below)                                                                                                                                                                 | User / qa-runner / orchestrator | **No** — scripts/agents read instance dirs and database only. |
| **4. Update database + repo store** | Set `status=done`, `qa_result=accepted` or `rejected`, `qa_notes`, `processed_at` in database. **If accepted**, the instance-evaluator must also update `data/repo_store.json`: add this instance’s rubric to `processed_rubrics` for its repo (see instance-evaluator agent). | Instance-evaluator (or orchestrator) | **Yes when accepted** — instance-evaluator writes the accepted rubric into the store so prefilter/rubric-validator can detect duplicates later. No manual step. |

**Summary:** Repo store is updated **automatically**: (1) repo averages in steps 1 and 2 (download and prefilter); (2) **processed rubrics** when the instance-evaluator marks an instance **accepted** (it must write the rubric to `data/repo_store.json` in the same turn as the database update).

---

# Pre-Run Checklist (Mandatory)

Before beginning evaluation, verify:

1. `instances/<problem_id>/` exists.
2. The following files exist:
   - `injected_repo/`
   - `rubric.json`
   - `metadata.json`
3. Database contains exactly one row matching `problem_id`.
4. Row `status` is NOT `"done"`.
5. No `QA_LOCK` file exists inside instance directory.
6. `metadata.json` contains:
   - `problem_id`
   - `job_id`
   - `average_score` (or `score_mean` from database)

If any check fails → **abort immediately**.

---

# Database Schema (`instances` table)

**Source of truth:** `instances` table in the project database. All columns below must be present.

| Column                | Description |
|-----------------------|-------------|
| job_id                | TAIGA job identifier (UUID) |
| problem_id            | Instance ID: `owner__repo-NN` (e.g. `simdutf__simdutf-07`) |
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

1. Create `instances/<problem_id>/QA_LOCK`.
2. Query database for current record.
3. Confirm `status` is empty.
4. Set `status = in_progress`.
5. Update database.
6. Query again and verify write succeeded.

If verification fails → abort and remove lock.

After processing:

- Remove `QA_LOCK`.
- Update database atomically.

---

# Atomic Database Update Rules

Before writing:

1. Query database for fresh data.
2. Confirm exactly one row matches.
3. Confirm row is still `in_progress`.
4. Apply changes only to that row.
5. Update database record.
6. Immediately query again and verify:
   - status == done
   - qa_result set
   - processed_at valid ISO timestamp

If verification fails → abort and notify.

Never modify other rows.

---

# Immediate Reject Conditions

Reject immediately if any condition holds:

- `rubric.json` cannot be parsed.
- Rubric does not contain exactly **8 criteria**.
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
**Script:** `scripts/validate_structure.py` (replaces dataset-loader agent)

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

### 1b. Diff Analysis
**Agent:** diff-analyzer

Responsibilities:
- Compare injected vs original
- Compute changed file count
- Estimate line modifications
- Detect structural rewrite
- Classify diff: `localized` | `moderate` (= acceptable) or `suspicious`

Output:
- changed_files, changed_lines_estimate, diff_classification, flags, evasion_risk

### 1c. Score Validation
**Script:** `scripts/check_score.py` (replaces scoring-engine agent)

Responsibilities:
- Confirm score within 0.4–0.8
- Classify band and produce remark

Output:
- score_classification, score_band, remark

**Performance:** ~10ms (script) vs ~2-3s (agent)

---

## Early Termination Gate (after Phase 1)

Before proceeding to Phase 2, check for **hard failures** that make further analysis unnecessary:

| Condition | Source | Action |
|-----------|--------|--------|
| `diff_classification == "suspicious"` | diff-analyzer | **REJECT immediately** — skip remaining phases |
| `score_classification == "outside_range"` | scoring-engine | **REJECT immediately** — skip remaining phases |
| `rubric_entry_count != 8` | dataset-loader | **REJECT immediately** — skip remaining phases |
| `parse_errors` non-empty | dataset-loader | **REJECT immediately** — skip remaining phases |
| `files_changed_count > 20` AND structural flags present | diff-analyzer | **REJECT immediately** — skip remaining phases |

If early termination triggers:
1. Set `qa_result = rejected`, `status = done` in database.
2. Write `result.json` to instance directory with reason.
3. Remove lock and move to next instance.

This saves significant time by avoiding rubric deep-analysis and LLM reasoning on clearly-broken instances.

---

## Phase 2 — Rubric Deep Validation (requires diff-analyzer output)

**Agent:** rubric-validator

Responsibilities:
- Confirm 8 criteria with valid structure
- Ensure each criterion references a real file
- Map criteria to changed files from diff-analyzer
- Apply multi-bug-per-file rule (soft — allowed for small repos)
- Check for early termination triggers (missing weights, invalid paths)

Output:
- rubric_valid: true/false
- early_termination: true/false
- rubric_issues: list

---

## Phase 3 — Final Decision (requires all upstream outputs)

**Agent:** instance-evaluator

Responsibilities:
- Consume outputs from dataset-loader, diff-analyzer, rubric-validator, scoring-engine
- Apply deterministic acceptance matrix
- Apply qualitative QA Playbook checks
- Write `result.json` to instance directory

| Condition | Verdict |
|-----------|---------|
| suspicious diff | reject |
| rubric hard failure (early_termination) | reject |
| score out of range | reject |
| structural deletion | reject |
| duplicate bugs (same exact mutation) | reject |
| all checks pass | accept |

Output:
- verdict: accepted/rejected
- reasoning: structured explanation
- result.json written to instance directory

---

# Final Quality Gate

Before marking as accepted, confirm:

1. Exactly 8 rubric criteria.
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
/qa-runner                              # Process ALL pending instances
/qa-runner --limit 10                   # Process next 10 pending instances
/qa-runner --offset 55 --limit 55       # Process rows 55-109 (for parallel workers)
/qa-runner simdutf__simdutf-07          # Process single instance
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
     a. Read problem_id and job_id from row
     b. Check instance directory exists (instances/<problem_id>/<job_id>/)
        - If missing → mark as rejected ("instance directory not found"), continue
     c. Set status = "in_progress", update database
     d. Create QA_LOCK
     e. Run full QA pipeline (Phase 1 → Early Termination check → Phase 2 → Phase 3)
     f. Update database with verdict
     g. Write result.json
     h. Remove QA_LOCK
     i. **Cleanup processed instance:** Run `.claude/scripts/cleanup_instance.sh <problem_id> <job_id>`
     j. Print progress: "[N processed] problem_id → verdict (Xs)"
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
[Processed: 3] zarr-developers__zarr-python-07 → REJECTED (early termination: 47s)
[Processed: 4] scipy__scipy-12 → ACCEPTED (full pipeline: 92s)
```

Print summary every 10 instances:
```
=== QA Summary (10 instances processed) ===
Accepted: 6 | Rejected: 4 | Rate: 0.60
Waiting for more downloads...
```

## Error Recovery

If an instance fails unexpectedly:
1. Mark as `rejected` with `qa_notes = "error: <message>"`
2. Remove QA_LOCK
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

- `status = done`
- `qa_result` set
- `qa_notes` concise and clear
- `processed_at` ISO timestamp
- Lock removed
- `instances/<problem_id>/result.json` written (full evaluation log)

The `result.json` file persists in the instance directory and contains the complete evaluation record: verdict, confidence, reason codes, score details, diff summary, rubric status, and timestamp. This serves as an audit log and enables re-evaluation without re-running agents.

When the instance-evaluator marks an instance **accepted**, it must also update `data/repo_store.json` (add this instance’s rubric to `processed_rubrics` for its repo) so future instances can be duplicate-checked—no separate manual step.

Instance is now finalized for Meta 800 selection pipeline.

--limit 1
