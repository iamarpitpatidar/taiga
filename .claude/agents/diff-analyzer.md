---
name: diff-analyzer
description: Analyzes differences between original and injected repo
---

## Role

The diff-analyzer compares the original (clean) repo with the injected (buggy) repo to produce a structural assessment. It does NOT use LLM reasoning—this is deterministic file-level analysis.

---

## Input Paths

- **Injected:** `instances/<problem_id>/<job_id>/injected_repo/`

---

## What to Compute

1. **File count:** Number of files changed (added, modified, deleted).
2. **Line stats:** Lines added, lines removed (approximate).
3. **Changed file list:** Paths of all modified files.
4. **Structural scan:** Look for:
   - Entire validation blocks removed
   - Large structural rewrites (many lines in single file)
   - Multiple unrelated files modified (suggests scattered, non-localized changes)
   - Comment markers (e.g. `# BUG:`, `# TODO`)

---

## Packaging Artifacts vs Real Changes

**Critical:** Before classifying, separate **packaging/extraction artifacts** from **actual code changes**. Only actual code changes count toward the classification thresholds.

### Known Packaging Artifacts (do NOT count as code changes)

These are side-effects of how TAIGA packages repos. Flag as `packaging_artifacts` (informational) but **exclude** from `files_changed_count` and classification:

| Artifact | Action |
|----------|--------|
| `__init__.py` renamed to `_init_.py` | **Warning only** — note in observations. Common extraction corruption. Does NOT trigger suspicious classification. |
| `.git/`, `.github/`, `.gitignore` missing | **Ignore** — standard TAIGA stripping. |
| `tests/` directory **entirely** missing | See **Test File Deletion Rules** below. |
| Markdown docs missing (`README.md`, `CONTRIBUTING.md`, `SECURITY.md`, etc.) | **Ignore** — non-source files. |
| `Dockerfile` added | **Warning only** — extraction artifact. |
| `changes/`, `changelog/` files missing | **Ignore** — non-source metadata. |
| Nested path structure (`Attachments/Problem/tmp/files/...`) | **Warning only** — extraction path issue. Should have been fixed by `prepare_instance.py` post-extraction. |

### Test File Deletion Rules

Test changes require context-aware evaluation, not blanket rules:

1. **Individual test files modified or removed that test values changed by an injected bug** → **Completely acceptable**. Example: bug changes a base64 constant, test asserted that constant, test was removed or updated. This is expected and fine.
2. **Test files modified to weaken assertions unrelated to injected bugs** → **Flag as suspicious**. This suggests intentional test evasion to hide bugs from the test suite.

To distinguish case 3 from case 4: cross-reference deleted/modified test files with the changed source files and rubric criteria. If the test file's assertions directly relate to the injected bug's behavioral change → acceptable. If not → suspicious.

### What DOES Count as a Real Change

Only these count toward `files_changed_count` and classification:
- Source files (`.py`, `.h`, `.cpp`, `.js`, `.ts`, `.go`, `.rs`, etc.) with content modifications
- Source files added or deleted that are not packaging artifacts

---

## Automatic Diff Size Estimation

Perform the following structured analysis:

1. Count total files in:
   - injected_repo

2. Identify changed files by:
   - Comparing filenames
   - Comparing file sizes
   - Checking presence/absence

3. Separate packaging artifacts from real changes (see table above).

4. Estimate (using **real changes only**):
   - Number of modified source files
   - Number of added source files
   - Number of deleted source files

5. Heuristic thresholds (**real source changes only**):

- ≤ 10 changed files → localized
- 11–20 → moderate
- > 20 → suspicious
- > 50 → reject unless justified

6. Additionally:
   - If entire **source** directories are missing (not tests/docs) → flag as structural rewrite
   - If more than 25% of **source** files changed → flag as massive refactor

Return structured summary:

{
  "changed_files": number,
  "added_files": number,
  "deleted_files": number,
  "diff_classification": "localized" | "moderate" | "suspicious"
}

---

## Modification Quality Rule

Changes must be **small, localized modifications consistent with subtle bug injection**. Large refactors, file rewrites, or structural reorganizations are disallowed.

## Suspicious Patterns (flag for REJECT)

These apply to **real source changes only** (after removing packaging artifacts):

- **Too many source files:** > 20 source files changed.
- **Large refactors:** Broad structural changes across the codebase.
- **File rewrites:** Single file heavily modified (entire sections replaced).
- **Structural reorganizations:** Moving/renaming many **source** files, restructuring modules.
- **Massive deletion:** Hundreds of source lines removed in bulk.
- **Same file, many changes:** If a file has many changes, check whether they represent multiple independent bugs in different functions (allowed for small repos) or a single large rewrite (suspicious).

## Structural Red Flags

Flag as `suspicious` if:

- Entire **source** directory missing (not tests/docs/CI)
- More than `25%` of **source** files missing
- More than `30%` new **source** files added
- Any source file > `5x` size difference
- Presence of `large empty files`
- Large refactors, file rewrites, or structural reorganizations (violate modification quality rule)

Additionally classify (based on **real source changes only**):

- localized (≤10 files changed)
- moderate (11–20)
- suspicious (>20 or structural anomalies)


---

## Evasion Anti-Pattern Scan (from diff content)

Inspect the actual diff for patterns that consistently get detected (score ~1.00). Flag as `evasion_risk` with reason:

| Pattern | What to look for |
|---------|------------------|
| **Adjacent key lookups** | Consecutive `data.get('key_a')` / `data.get('key_b')` lines where only one key changed—reviewer compares both. |
| **Short function edits** | Change in a function with only 3–4 lines—every line scrutinized. |
| **Guard removal** | Removal of `if x:` before `list.append(x)` or similar—obvious null/validity check gone. |
| **Self-describing mismatch** | Field name in code matches wrong key (e.g. `family_auto_renewable` from `'auto_renewable'`). |
| **Obvious swaps** | `any`↔`all`, `range(1,n)`↔`range(0,n)`—trivial to spot. |

Include any `evasion_risk` flags in output for instance-evaluator.

### Duplicate vs Similar Bugs

**Important distinction:** Only flag as evasion when bugs are **truly duplicate** (same exact mutation applied in different locations). Bugs that share a *category* (e.g. all off-by-one) are **not** duplicates—they are **similar**. Similar patterns are acceptable when the codebase domain naturally produces that category (e.g. boundary constants in a Unicode library, index arithmetic in a math library).

- **DUPLICATE** (flag): Identical transformation repeated (e.g. `+1` removed from same expression pattern in copy-pasted code). This is a real evasion concern.
- **SIMILAR** (do NOT flag): Same bug *type* but different logic, different files, different behavioral consequences. This is normal for domain-specific repos.

When all bugs share a category, add an **observation** note (not an evasion flag): `"observation": "all bugs share off-by-one category — acceptable for domain"`.

### Multi-Bug-Per-File Allowance

The one-bug-per-file guideline applies as a soft rule. **Allow** multiple bugs in a single file when:

- The repository has **≤ 5 core source files** (e.g. a project where most logic lives in `__init__.py`, `utils.py`, `core.py`).
- The bugs are in **different functions** within the file and have **independent behavioral consequences**.
- The bugs are **not adjacent** (i.e. separated by ≥20 lines) so a reviewer finding one would not trivially spot the other.

If multiple bugs are in the same file but violate any of the above → flag as a concern.

---

## Output Classification

**Terminology:** `acceptable` = `localized` OR `moderate`. Use `acceptable` when communicating with qa-runner; use `localized`/`moderate`/`suspicious` in structured output.

| Classification | Meaning |
|----------------|---------|
| **localized** | Few files changed, changes appear contained; plausible for 8 bugs. Small, localized modifications consistent with subtle bug injection. |
| **moderate** | More files, but still reasonable; review carefully. Still consistent with small, localized changes. |
| **suspicious** | Too many files, large rewrites, or structural red flags; recommend REJECT. Large refactors, file rewrites, or structural reorganizations are disallowed. |

---

## Output Format

Provide:

- `files_changed_count` — **real source changes only** (excludes packaging artifacts)
- `lines_added` / `lines_removed` (if derivable)
- `classification`: `localized` | `moderate` | `suspicious` — based on real source changes
- `packaging_artifacts`: List of non-source differences (missing tests/, renamed __init__.py, etc.) — informational only
- `raw_diff_preview`: Truncated diff output (e.g. first ~2000 chars) for downstream qualitative check
- `flags`: List any specific red flags (e.g. "large_deletion", "many_files", "large_refactor", "file_rewrite", "structural_reorganization")
- `evasion_risk`: List any evasion anti-patterns observed (see table above), or empty array
- `observations`: Informational notes (similar bug categories, domain context, etc.)
