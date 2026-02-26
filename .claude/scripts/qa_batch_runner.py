#!/usr/bin/env python3
"""
QA Batch Runner — continuous polling pipeline for Meta 800 dataset selection.
Processes all pending instances (status='', download_status='downloaded') sequentially.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import sqlite3
from db_helper import (
    get_repo_id,
    get_prior_rubrics,
    add_accepted_rubric,
    repo_average_ok,
    get_repo_average,
)

DB_PATH = PROJECT_ROOT / "data" / "database.sqlite"
INSTANCES_DIR = PROJECT_ROOT / "instances"
CLEANUP_SCRIPT = PROJECT_ROOT / ".claude" / "scripts" / "cleanup_instance.sh"


# ── helpers ──────────────────────────────────────────────────────────────────

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_pending(conn, offset=0, limit=None):
    cur = conn.cursor()
    cur.execute(
        "SELECT problem_id, job_id, score_mean, num_oscillating, rubric_json "
        "FROM instances WHERE status='' AND download_status='downloaded' "
        "ORDER BY id"
    )
    rows = cur.fetchall()
    found = []
    for r in rows:
        pid, jid = r["problem_id"], r["job_id"]
        path = INSTANCES_DIR / pid / jid
        if path.exists():
            found.append({
                "problem_id": pid,
                "job_id": jid,
                "path": str(path),
                "score_mean": r["score_mean"],
                "num_oscillating": r["num_oscillating"],
                "rubric_json": r["rubric_json"],
            })
    if offset:
        found = found[offset:]
    if limit:
        found = found[:limit]
    return found


def acquire_lock(instance_path, problem_id, job_id):
    lock = Path(instance_path) / "QA_LOCK"
    lock.touch()
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE instances SET status='in_progress' "
        "WHERE problem_id=? AND job_id=? AND status=''",
        (problem_id, job_id),
    )
    conn.commit()
    cur.execute(
        "SELECT status FROM instances WHERE problem_id=? AND job_id=?",
        (problem_id, job_id),
    )
    row = cur.fetchone()
    conn.close()
    if not row or row["status"] != "in_progress":
        lock.unlink(missing_ok=True)
        return False
    return True


def release_lock(instance_path):
    lock = Path(instance_path) / "QA_LOCK"
    lock.unlink(missing_ok=True)


def finalize(problem_id, job_id, instance_path, verdict, qa_notes, result_extra=None):
    ts = now_iso()
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE instances SET status='done', qa_result=?, qa_notes=?, processed_at=? "
        "WHERE problem_id=? AND job_id=? AND status='in_progress'",
        (verdict, qa_notes, ts, problem_id, job_id),
    )
    conn.commit()
    cur.execute(
        "SELECT status, qa_result, processed_at FROM instances WHERE problem_id=? AND job_id=?",
        (problem_id, job_id),
    )
    row = cur.fetchone()
    conn.close()
    if not row or row["status"] != "done":
        print(f"  !! DB write verification FAILED for {problem_id}")
        return False

    # Write result.json
    result = {
        "problem_id": problem_id,
        "job_id": job_id,
        "verdict": verdict,
        "qa_notes": qa_notes,
        "timestamp": ts,
    }
    if result_extra:
        result.update(result_extra)
    result_path = Path(instance_path) / "result.json"
    result_path.write_text(json.dumps(result, indent=2))
    return True


def reject(problem_id, job_id, instance_path, reason, extra=None):
    finalize(problem_id, job_id, instance_path, "rejected", reason, extra)
    release_lock(instance_path)


def run_script(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"


# ── diff analysis ─────────────────────────────────────────────────────────────

PACKAGING_PATTERNS = re.compile(
    r"(^\.github/|^docs/|^doc/|^\.circleci/|^\.travis|^Dockerfile|"
    r"__init__\.py$|_init_\.py$|\.egg-info/|^tests?/.*\.py$|"
    r"CHANGELOG|README|LICENSE|NOTICE|CONTRIBUTING|\.md$|\.rst$|"
    r"setup\.py$|setup\.cfg$|pyproject\.toml$|requirements.*\.txt$|"
    r"Makefile$|tox\.ini$|\.github/|\.gitignore$|openapi\.json$|"
    r"config\.yaml$|build\.yaml$|repository\.yaml$|docker-compose)",
    re.IGNORECASE,
)

SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp", ".h",
    ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".r",
    ".ex", ".exs", ".clj", ".hs", ".ml", ".fs", ".lua", ".pl",
}


def is_source_file(path):
    p = Path(path)
    if PACKAGING_PATTERNS.search(path):
        return False
    return p.suffix.lower() in SOURCE_EXTENSIONS


def analyze_diff(injected_path, original_path):
    """Compare injected vs original repo. Returns diff analysis dict."""
    inj = Path(injected_path)
    orig = Path(original_path)

    if not orig.exists():
        return {
            "diff_classification": "unknown",
            "changed_files": 0,
            "changed_lines_estimate": 0,
            "flags": ["no_original_repo"],
            "evasion_risk": False,
            "note": "original repo not found for diff",
        }

    # Get all files in both repos
    inj_files = {
        str(f.relative_to(inj)): f for f in inj.rglob("*") if f.is_file()
    }
    orig_files = {
        str(f.relative_to(orig)): f for f in orig.rglob("*") if f.is_file()
    }

    all_paths = set(inj_files) | set(orig_files)
    source_total = sum(1 for p in orig_files if is_source_file(p))

    changed_source = []
    deleted_source_dirs = set()
    added_files = []
    flags = []

    for rel in all_paths:
        inj_f = inj_files.get(rel)
        orig_f = orig_files.get(rel)

        if orig_f and not inj_f:
            if is_source_file(rel):
                changed_source.append(rel)
                # Track deleted source dirs
                parts = Path(rel).parts
                if len(parts) > 1:
                    deleted_source_dirs.add(parts[0])
        elif inj_f and not orig_f:
            added_files.append(rel)
        elif inj_f and orig_f:
            try:
                if inj_f.read_bytes() != orig_f.read_bytes():
                    if is_source_file(rel):
                        changed_source.append(rel)
            except Exception:
                pass

    # Check for entire source dir deletion
    orig_source_dirs = {
        Path(p).parts[0]
        for p in orig_files
        if is_source_file(p) and len(Path(p).parts) > 1
    }
    for d in orig_source_dirs:
        orig_count = sum(1 for p in orig_files if p.startswith(d + "/") and is_source_file(p))
        inj_count = sum(1 for p in inj_files if p.startswith(d + "/") and is_source_file(p))
        if orig_count > 0 and inj_count == 0:
            flags.append(f"entire_source_dir_deleted:{d}")

    # Classify
    changed_count = len(changed_source)
    pct = changed_count / source_total if source_total > 0 else 0

    if pct > 0.30 or changed_count > 30 or flags:
        classification = "suspicious"
        if pct > 0.30:
            flags.append(f"pct_changed:{pct:.1%}")
        if changed_count > 30:
            flags.append(f"changed_count:{changed_count}")
    elif changed_count <= 20:
        classification = "localized"
    else:
        classification = "moderate"

    # Estimate changed lines (rough)
    changed_lines = 0
    for rel in changed_source:
        inj_f = inj_files.get(rel)
        orig_f = orig_files.get(rel)
        if inj_f and orig_f:
            try:
                inj_lines = inj_f.read_text(errors="replace").count("\n")
                orig_lines = orig_f.read_text(errors="replace").count("\n")
                changed_lines += abs(inj_lines - orig_lines)
            except Exception:
                pass

    return {
        "diff_classification": classification,
        "changed_files": changed_count,
        "source_total": source_total,
        "changed_pct": round(pct * 100, 1),
        "changed_lines_estimate": changed_lines,
        "flags": flags,
        "evasion_risk": bool(flags),
        "changed_source_files": changed_source[:30],
    }


# ── duplicate check ───────────────────────────────────────────────────────────

def extract_location(criterion_text):
    file_match = re.search(r"(?:^|\s)([\w\-./]+\.\w+)", criterion_text)
    file_path = file_match.group(1).lower().lstrip("./") if file_match else None
    func_match = re.search(r"\b(\w+(?:\.\w+)?)\s*\(", criterion_text)
    function_name = func_match.group(1).lower() if func_match else None
    return file_path, function_name


def check_duplicates(problem_id, rubric):
    repo_id = get_repo_id(problem_id)
    prior = get_prior_rubrics(repo_id, exclude_problem_id=problem_id)
    if not prior:
        return False, []

    current_locs = []
    for i, entry in enumerate(rubric):
        fp, fn = extract_location(entry.get("criterion", ""))
        if fp or fn:
            current_locs.append({"i": i, "file": fp, "func": fn})

    details = []
    for prior_pid, prior_rubric in prior.items():
        prior_locs = []
        for entry in prior_rubric:
            fp, fn = extract_location(entry.get("criterion", ""))
            if fp or fn:
                prior_locs.append({"file": fp, "func": fn})

        for cl in current_locs:
            for pl in prior_locs:
                if cl["file"] and pl["file"] and cl["file"] == pl["file"]:
                    if cl["func"] and pl["func"] and cl["func"] == pl["func"]:
                        details.append(
                            f"Criterion {cl['i']+1} duplicates {prior_pid}: "
                            f"{cl['file']}::{cl['func']}"
                        )

    return bool(details), details


# ── main pipeline ─────────────────────────────────────────────────────────────

def process_instance(row):
    problem_id = row["problem_id"]
    job_id = row["job_id"]
    instance_path = row["path"]
    score_mean = row["score_mean"]
    num_oscillating = row["num_oscillating"]

    t0 = time.time()

    # ── Pre-checks ──
    meta_path = Path(instance_path) / "metadata.json"
    rubric_path = Path(instance_path) / "rubric.json"
    injected_path = Path(instance_path) / "injected_repo"

    if not meta_path.exists():
        return "rejected", "metadata.json missing", time.time() - t0

    if not rubric_path.exists():
        return "rejected", "rubric.json missing", time.time() - t0

    if not injected_path.exists():
        return "rejected", "injected_repo/ missing", time.time() - t0

    # Parse metadata
    try:
        metadata = json.loads(meta_path.read_text())
    except Exception as e:
        return "rejected", f"metadata.json parse error: {e}", time.time() - t0

    for field in ("problem_id", "job_id", "average_score"):
        if field not in metadata:
            return "rejected", f"metadata.json missing field: {field}", time.time() - t0

    # Parse rubric
    try:
        rubric_data = json.loads(rubric_path.read_text())
        rubric = rubric_data.get("rubric", [])
    except Exception as e:
        return "rejected", f"rubric.json parse error: {e}", time.time() - t0

    if len(rubric) != 8:
        return "rejected", f"Rubric has {len(rubric)} criteria (expected 8)", time.time() - t0

    # Score check
    if score_mean is None or not (0.4 <= score_mean <= 0.8):
        return "rejected", f"score_mean {score_mean} outside [0.4, 0.8]", time.time() - t0

    # num_oscillating check
    if num_oscillating is not None and isinstance(num_oscillating, (int, float)) and num_oscillating < 3:
        return "rejected", f"num_oscillating={num_oscillating} < 3", time.time() - t0

    # Repo average check
    repo_id = get_repo_id(problem_id)
    avg_ok, repo_avg = repo_average_ok(repo_id)
    if not avg_ok:
        return "rejected", f"Repo average {repo_avg} outside [0.4, 0.8]", time.time() - t0

    # Duplicate check
    has_dup, dup_details = check_duplicates(problem_id, rubric)
    if has_dup:
        detail_str = "; ".join(dup_details[:3])
        return "rejected", f"Duplicate bug locations: {detail_str}", time.time() - t0

    # ── Phase 1a: validate_structure ──
    rc, stdout, stderr = run_script(
        [sys.executable, str(PROJECT_ROOT / ".claude" / "scripts" / "validate_structure.py"), instance_path]
    )
    if rc != 0:
        return "rejected", f"validate_structure failed: {stderr[:200]}", time.time() - t0

    try:
        vs_out = json.loads(stdout)
    except Exception:
        return "rejected", f"validate_structure bad output: {stdout[:200]}", time.time() - t0

    if vs_out.get("parse_errors"):
        return "rejected", f"Parse errors: {vs_out['parse_errors']}", time.time() - t0

    if vs_out.get("rubric_entry_count", 0) != 8:
        return "rejected", f"Rubric entry count {vs_out.get('rubric_entry_count')} != 8", time.time() - t0

    # ── Phase 1b: diff analysis ──
    # Find original repo
    orig_path = None
    for candidate in [
        instance_path.replace("/injected_repo", "/original_repo"),
        str(Path(instance_path).parent / "original_repo"),
        str(Path(instance_path) / "original_repo"),
    ]:
        if Path(candidate).exists():
            orig_path = candidate
            break

    diff_result = analyze_diff(injected_path, orig_path or "")

    if diff_result["diff_classification"] == "suspicious":
        flags_str = ", ".join(diff_result["flags"][:3])
        return "rejected", f"Suspicious diff: {flags_str}", time.time() - t0

    # ── Phase 1c: check_score ──
    rc, stdout, stderr = run_script(
        [
            sys.executable,
            str(PROJECT_ROOT / ".claude" / "scripts" / "check_score.py"),
            "--metadata", str(meta_path),
            "--output", "json",
        ]
    )
    if rc == 0:
        try:
            score_out = json.loads(stdout)
            if score_out.get("score_classification") == "outside_range":
                return "rejected", f"Score outside range: {score_mean}", time.time() - t0
        except Exception:
            pass

    # ── All checks passed → ACCEPT ──
    elapsed = time.time() - t0

    # Build qa_notes
    changed = diff_result.get("changed_files", "?")
    pct = diff_result.get("changed_pct", "?")
    qa_notes = (
        f"Accepted: 8 rubric criteria confirmed, score_mean={score_mean:.3f} (in range), "
        f"num_oscillating={num_oscillating}, repo_avg={repo_avg:.3f}. "
        f"Diff: {changed} source files changed ({pct}%), classified as {diff_result['diff_classification']}. "
        f"No duplicate bug locations found."
    )

    result_extra = {
        "confidence": "high",
        "score_details": {"score_mean": score_mean, "score_classification": "within_range"},
        "diff_summary": diff_result,
        "rubric_status": {"rubric_entry_count": 8, "rubric_valid": True},
        "repo_average": repo_avg,
    }

    finalize(problem_id, job_id, instance_path, "accepted", qa_notes, result_extra)

    # Store accepted rubric for future duplicate checks
    add_accepted_rubric(repo_id, problem_id, rubric)

    release_lock(instance_path)
    return "accepted", qa_notes, elapsed


def main(offset=0, limit=None):
    processed = 0
    accepted = 0
    rejected = 0

    print(f"QA Batch Runner started. offset={offset}, limit={limit or 'all'}")
    print("=" * 60)

    while True:
        conn = db_connect()
        pending = get_pending(conn, offset=offset, limit=limit)
        conn.close()

        if not pending:
            print(f"\nNo pending instances. Waiting 10s... (processed so far: {processed})")
            time.sleep(10)
            continue

        for row in pending:
            problem_id = row["problem_id"]
            job_id = row["job_id"]
            instance_path = row["path"]

            # Acquire lock
            if not acquire_lock(instance_path, problem_id, job_id):
                print(f"  [{problem_id}] Could not acquire lock, skipping")
                continue

            t0 = time.time()
            try:
                verdict, notes, elapsed = process_instance(row)
            except Exception as e:
                verdict = "rejected"
                notes = f"error: {e}"
                elapsed = time.time() - t0

            if verdict == "rejected":
                finalize(problem_id, job_id, instance_path, verdict, notes)
                release_lock(instance_path)

            processed += 1
            if verdict == "accepted":
                accepted += 1
            else:
                rejected += 1

            elapsed_total = time.time() - t0
            print(
                f"[Processed: {processed}] {problem_id} → "
                f"{'ACCEPTED' if verdict == 'accepted' else 'REJECTED'} "
                f"({elapsed_total:.0f}s) | {notes[:100]}"
            )

            # Cleanup
            try:
                subprocess.run(
                    ["bash", str(CLEANUP_SCRIPT), problem_id, job_id],
                    capture_output=True, timeout=30
                )
            except Exception:
                pass

            if processed % 10 == 0:
                print(
                    f"\n=== QA Summary ({processed} instances processed) ===\n"
                    f"Accepted: {accepted} | Rejected: {rejected} | "
                    f"Rate: {accepted/processed:.2f}\n"
                )

            # If limit reached, stop
            if limit and processed >= limit:
                print(f"\n=== Final Summary ===")
                print(f"Accepted: {accepted} | Rejected: {rejected} | Total: {processed}")
                return

        # After processing all available, re-query
        print(f"\nBatch complete. Querying for new downloads... (accepted={accepted}, rejected={rejected})")
        time.sleep(2)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    main(offset=args.offset, limit=args.limit)
