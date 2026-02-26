# Lessons Learned - Agent Best Practices

**⚠️ CRITICAL: Read this file before starting any task. This is a living document updated with lessons from real agent failures.**

---

## Lesson 1: NEVER Write Inline Python for Operations with Existing Scripts

**Date:** 2026-02-28
**Context:** QA Runner agent trace showed agent writing tons of `python -c "..."` commands instead of using provided scripts

### ❌ DON'T DO THIS:
```bash
# WRONG: Writing inline Python
python -c "import sqlite3; conn = sqlite3.connect('data/database.sqlite'); cur = conn.cursor(); cur.execute('UPDATE instances SET status=\"in_progress\" WHERE job_id=...')"

# WRONG: Manual queries instead of using scripts
python -c "import json; with open('rubric.json') as f: print(len(json.load(f)))"

# WRONG: Multiple inline commands for database operations
python -c "cur.execute('SELECT status FROM instances WHERE job_id=...')"
python -c "cur.execute('UPDATE instances SET ...')"
python -c "cur.execute('SELECT status FROM instances WHERE job_id=...')"  # verification
```

### ✅ DO THIS:
```bash
# CORRECT: Use the provided scripts
python .claude/scripts/update_qa_status.py <job_id> --status in_progress
python .claude/scripts/validate_structure.py <instance_dir>
python .claude/scripts/check_score.py --metadata <metadata.json>
```

### Why This Matters:
- Each `python -c` command creates a NEW database connection
- Multiple connections cause lost updates (SQLite transaction isolation)
- Scripts handle transaction management correctly
- Scripts include verification and error handling
- Inline Python wastes tokens and is hard to debug
- Makes behavior unpredictable and hard to maintain

### Rule:
**If a script exists for an operation, ALWAYS use it. NEVER write inline Python as a substitute.**

---

## Lesson 2: Database Updates MUST Be Verified Before Proceeding

**Date:** 2026-02-28
**Context:** QA Runner left database status as 'in_progress' because updates weren't verified before cleanup

### ❌ DON'T DO THIS:
```bash
# WRONG: Update database without checking result
python .claude/scripts/update_qa_status.py <job_id> --status done --result accepted --notes "..."

# Proceed to cleanup without verification
bash .claude/scripts/cleanup_instance.sh <problem_id> <job_id>
```

### ✅ DO THIS:
```bash
# CORRECT: Update database and capture output
python .claude/scripts/update_qa_status.py <job_id> --status done --result accepted --notes "..."

# Parse the JSON output and check "success": true
# If success is false, ABORT immediately - do NOT proceed to cleanup

# Only after verifying success:
bash .claude/scripts/cleanup_instance.sh <problem_id> <job_id>
```

### Why This Matters:
- If database update fails, cleanup destroys evidence
- Instance directory gets deleted but DB shows 'in_progress'
- Can't re-run QA because files are gone
- Database becomes inconsistent and unreliable
- Debugging becomes impossible

### Rule:
**ALWAYS verify critical operations succeed before proceeding. If a script returns JSON with "success" field, check it. If false, ABORT.**

---

## Lesson 3: Update Database BEFORE Cleanup, Never After

**Date:** 2026-02-28
**Context:** QA Runner trace showed result.json written before database update, then cleanup happened even though update failed

### ❌ DON'T DO THIS:
```bash
# WRONG ORDER:
# 1. Write result.json
echo '{"verdict": "accepted"}' > result.json

# 2. Try to update database (might fail)
python .claude/scripts/update_qa_status.py ...

# 3. Cleanup happens regardless
bash cleanup_instance.sh ...
```

### ✅ DO THIS:
```bash
# CORRECT ORDER:
# 1. Update database FIRST and verify success
python .claude/scripts/update_qa_status.py <job_id> --status done --result accepted --notes "..."
# → Check "success": true in output

# 2. Only if success, proceed with remaining steps
db_helper.add_accepted_rubric(...)  # if accepted

# 3. Write result.json (optional audit log)
echo '{"verdict": "accepted"}' > result.json

# 4. Remove lock
rm QA_LOCK

# 5. Cleanup last
bash cleanup_instance.sh ...
```

### Why This Matters:
- Database is the source of truth, not files
- If database update fails but cleanup succeeds, you lose all evidence
- Proper order ensures database always reflects true state
- If update fails, you can debug because files still exist

### Rule:
**Database updates MUST succeed before any destructive operations (cleanup, file deletion, etc.). Database first, cleanup last.**

---

## Lesson 4: Trust Script Outputs - Don't Over-Verify

**Date:** 2026-02-28
**Context:** QA Runner did 20+ manual grep/sed/cat commands after scripts already validated everything

### ❌ DON'T DO THIS:
```bash
# Scripts already ran and returned structured output:
python .claude/scripts/validate_structure.py <dir>  # Returns: rubric_entry_count: 12
python .claude/scripts/validate_rubric.py ...       # Returns: rubric_valid: true

# WRONG: Manual verification of what scripts already checked
python -c "import json; with open('rubric.json') as f: print(len(json.load(f)['rubric']))"
grep -n "get_current_cache" injected_repo/autogen/cache/cache.py
sed -n '185,205p' injected_repo/autogen/cache/cache.py
grep -n "pickle.loads" injected_repo/autogen/cache/redis_cache.py
sed -n '65,85p' injected_repo/autogen/cache/redis_cache.py
# ... 15 more manual commands ...
```

### ✅ DO THIS:
```bash
# CORRECT: Run scripts and trust their output
python .claude/scripts/validate_structure.py <dir>
# Output: {"rubric_entry_count": 12, "rubric_valid": true, ...}

python .claude/scripts/validate_rubric.py <rubric> <repo>
# Output: {"rubric_valid": true, "early_termination": false, ...}

# Make decision based on structured output
# Only do manual verification if:
# - Script returned an error or unexpected result
# - Script output is ambiguous or unclear
# - Manual review is explicitly required (e.g., exit code 2 for subjective checks)
```

### Why This Matters:
- Wastes time and tokens
- Creates very long transcripts that are hard to review
- Makes behavior unpredictable
- Scripts are optimized and tested - manual checks are not
- If you don't trust scripts, improve the scripts instead of bypassing them

### Rule:
**Scripts are the source of truth. Use their structured output to make decisions. Only do manual verification when scripts fail or request manual review.**

---

## Lesson 5: Use Correct Primary Keys for Database Operations

**Date:** 2026-02-28
**Context:** QA Runner uses `job_id` as PRIMARY KEY, but old helper function updated by `problem_id`

### ❌ DON'T DO THIS:
```python
# WRONG: Using problem_id when job_id is the primary key
def update_qa_status(problem_id, qa_result, qa_notes):
    conn.execute(
        "UPDATE instances SET status='done', qa_result=?, qa_notes=? WHERE problem_id=?",
        (qa_result, qa_notes, problem_id)
    )
```

### ✅ DO THIS:
```python
# CORRECT: Use job_id (PRIMARY KEY)
def update_qa_status(job_id, qa_result, qa_notes):
    conn.execute(
        "UPDATE instances SET status='done', qa_result=?, qa_notes=? WHERE job_id=?",
        (qa_result, qa_notes, job_id)
    )
```

### Why This Matters:
- Multiple jobs can exist for the same `problem_id`
- Updating by `problem_id` could update the wrong row
- Primary key ensures you update exactly one row
- Prevents data corruption and race conditions

### Rule:
**Always use the PRIMARY KEY (job_id) for database updates. Check the schema documentation if unsure.**

---

## Lesson 6: Follow Transaction State Machines - Don't Skip States

**Date:** 2026-02-28
**Context:** QA Runner tried to set status='done' when status was still empty (not 'in_progress')

### ❌ DON'T DO THIS:
```bash
# WRONG: Skipping in_progress state
# Status is currently: '' (empty)
python .claude/scripts/update_qa_status.py <job_id> --status done --result accepted --notes "..."
# This will fail because status must be 'in_progress' before setting to 'done'
```

### ✅ DO THIS:
```bash
# CORRECT: Follow state machine
# Status: '' → in_progress
python .claude/scripts/update_qa_status.py <job_id> --status in_progress

# Run QA pipeline...

# Status: in_progress → done
python .claude/scripts/update_qa_status.py <job_id> --status done --result accepted --notes "..."
```

### Status State Machine:
```
'' (empty) → in_progress → done
                         ↘ failed
```

Valid transitions:
- `'' → in_progress` (start processing)
- `in_progress → done` (complete successfully)
- `in_progress → failed` (early termination)

Invalid transitions:
- `'' → done` ❌ (can't skip in_progress)
- `'' → failed` ❌ (can't skip in_progress)
- `done → in_progress` ❌ (can't restart after completion)

### Why This Matters:
- State machines prevent race conditions
- Ensures proper concurrency control
- Makes debugging easier (you know where things failed)
- Prevents accidental overwrites

### Rule:
**Follow the documented state machine. Don't skip states. If a transition fails, investigate why instead of forcing it.**

---

## Lesson 7: Read Documentation Before Creating Custom Solutions

**Date:** 2026-02-28
**Context:** Agent created custom inline scripts instead of checking if scripts already existed

### ❌ DON'T DO THIS:
```bash
# WRONG: Writing custom solution without checking for existing tools
python -c "
import sqlite3
conn = sqlite3.connect('data/database.sqlite')
cur = conn.cursor()
cur.execute('UPDATE instances SET status=...')
conn.commit()
cur.execute('SELECT status FROM instances WHERE ...')
print(cur.fetchone())
conn.close()
"
```

### ✅ DO THIS:
```bash
# CORRECT: Check documentation first
# 1. Read qa-runner.md → see it mentions .claude/scripts/
# 2. Check what scripts exist:
ls .claude/scripts/*.py

# 3. Use the appropriate script:
python .claude/scripts/update_qa_status.py <job_id> --status in_progress
```

### Why This Matters:
- Existing scripts are tested and handle edge cases
- Custom solutions often miss error handling
- Wastes time reinventing the wheel
- Creates technical debt

### Rule:
**Before writing custom code, check:**
1. **Command files** (`.claude/commands/*.md`) - task-specific instructions
2. **Scripts directory** (`.claude/scripts/`) - utility scripts
3. **Helper modules** (`src/db_helper.py`, etc.) - shared functions
4. **This file** (`.claude/lessons_learned.md`) - documented patterns

**If a tool exists, use it. If the tool is inadequate, improve it (don't bypass it).**

---

## Quick Reference Card

### ✅ ALWAYS DO:
- Use existing scripts instead of inline Python
- Verify critical operations (check `"success": true`)
- Update database BEFORE cleanup/destructive operations
- Use correct primary key (job_id, not problem_id)
- Follow documented state machines
- Trust script outputs unless they fail
- Read documentation before creating solutions

### ❌ NEVER DO:
- Write `python -c "..."` if a script exists
- Proceed to cleanup without verifying database update
- Skip status states (must go through in_progress)
- Update database by problem_id when job_id is PRIMARY KEY
- Over-verify with 20+ manual commands after scripts run
- Write result.json before updating database

### 🚨 RED FLAGS:
- Multiple `python -c` commands in sequence → Use a script
- Database update followed immediately by cleanup → Verify success first
- Manual grep/sed after scripts ran → Trust the scripts
- Custom SQLite code → Use db_helper.py or scripts
- Skipping from empty status directly to done → Must set in_progress first

---

## How to Use This File

### For Agents:
1. **Read this file at the start of complex tasks**
2. **Before writing custom code, check if your pattern is an anti-pattern here**
3. **If you catch yourself about to do something listed under "DON'T", stop and use the "DO" approach**
4. **If unsure, ask the user rather than guessing**

### For Developers:
1. **Add new lessons as they're discovered**
2. **Include date, context, and clear examples**
3. **Keep examples concise and actionable**
4. **Update when patterns change**

---

## Version History

- **2026-02-28**: Initial version with 7 lessons from QA Runner agent trace
  - Lesson 1: Don't write inline Python
  - Lesson 2: Verify database updates
  - Lesson 3: Database before cleanup
  - Lesson 4: Trust script outputs
  - Lesson 5: Use correct primary keys
  - Lesson 6: Follow state machines
  - Lesson 7: Read documentation first

---

**Last Updated:** 2026-02-28
**Next Review:** Add lessons as discovered
