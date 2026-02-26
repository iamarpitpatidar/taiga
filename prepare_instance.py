"""
Meta 800 — TAIGA Instance Downloader
#swe-bench-taiga-meta-800

Downloads and prepares dataset instances from TAIGA for QA evaluation.
Reads rubrics directly from JSON files (no job metadata API call).
Only downloads injected_repo (no git clone of original repo).

Optimizations:
- Rubric extraction from JSON files (no API call for job metadata)
- Removed git clone step (only injected_repo needed for core 20% analysis)
- Cached JSON data loading

Author: brian.k @ Turing
"""

import os
import sys
import json
import shutil
import tarfile
import requests
import subprocess
from pathlib import Path
from dotenv import load_dotenv
import pandas as pd
import time
from datetime import datetime
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# ---------------------------------------------------
# CONFIG
# ---------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
INSTANCES_DIR = PROJECT_ROOT / "instances"
CSV_PATH = PROJECT_ROOT / "instances.csv"

MIN_OSCILLATING = 3

load_dotenv(PROJECT_ROOT / "env")

TAIGA_BASE_URL = os.getenv("TAIGA_BASE_URL")
API_TOKEN = os.getenv("API_TOKEN")
TAIGA_IAP_CLIENT_ID = os.getenv("TAIGA_IAP_CLIENT_ID")
SERVICE_ACCOUNT_FILE = PROJECT_ROOT / os.getenv("TAIGA_SERVICE_ACCOUNT", "taiga-service-account.json")

if not TAIGA_BASE_URL:
    raise ValueError("Missing TAIGA_BASE_URL in env file.")

# ---------------------------------------------------
# IAP AUTH
# ---------------------------------------------------

_iap_token_cache = {"token": None, "expiry": None}

def get_iap_token():
    """Generate an OIDC token for IAP using the service account."""
    now = time.time()
    if _iap_token_cache["token"] and _iap_token_cache["expiry"] and now < _iap_token_cache["expiry"]:
        return _iap_token_cache["token"]

    credentials = service_account.IDTokenCredentials.from_service_account_file(
        str(SERVICE_ACCOUNT_FILE),
        target_audience=TAIGA_IAP_CLIENT_ID,
    )
    credentials.refresh(Request())
    _iap_token_cache["token"] = credentials.token
    _iap_token_cache["expiry"] = now + 3500  # tokens last ~1hr
    return credentials.token

def get_auth_headers():
    """Return auth headers. Uses IAP OIDC token if service account is available, falls back to API_TOKEN."""
    if TAIGA_IAP_CLIENT_ID and SERVICE_ACCOUNT_FILE.exists():
        token = get_iap_token()
        return {"Authorization": f"Bearer {token}"}
    if API_TOKEN:
        return {"Authorization": f"Bearer {API_TOKEN}"}
    raise ValueError("No authentication method available. Need IAP service account or API_TOKEN.")

INSTANCES_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------
# UI HELPERS
# ---------------------------------------------------

class Colors:
    """ANSI color codes for terminal output."""
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'

    # Status colors
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GRAY = '\033[90m'

    # Background colors
    BG_GREEN = '\033[102m\033[30m'
    BG_YELLOW = '\033[103m\033[30m'
    BG_RED = '\033[101m\033[30m'
    BG_BLUE = '\033[104m\033[30m'

def format_size(bytes_val):
    """Format bytes as human-readable size."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:6.1f}{unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:6.1f}TB"

def format_duration(seconds):
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        return f"{seconds/60:.1f}m"
    else:
        return f"{seconds/3600:.1f}h"

def print_header(title, width=80):
    """Print a styled header."""
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'='*width}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{title.center(width)}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'='*width}{Colors.RESET}")

def print_section(title, width=80):
    """Print a section divider."""
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'-'*width}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{title}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'-'*width}{Colors.RESET}")

def print_footer(width=80):
    """Print a footer line."""
    print(f"{Colors.BOLD}{Colors.CYAN}{'='*width}{Colors.RESET}\n")

def format_status(status, problem_id, details, progress_bar):
    """Format a status line with colors."""
    status_colors = {
        'OK': Colors.GREEN,
        'PASS': Colors.GREEN,
        'SKIP': Colors.YELLOW,
        'CACHE': Colors.CYAN,
        'REJECT': Colors.RED,
        'FAIL': Colors.RED,
    }

    color = status_colors.get(status, Colors.RESET)
    status_text = f"{color}{status:6s}{Colors.RESET}"
    problem_text = f"{Colors.DIM}{problem_id:<50s}{Colors.RESET}"
    details_text = f"{Colors.GRAY}({details}){Colors.RESET}"

    return f"{progress_bar} {status_text} {problem_text} {details_text}"

# ---------------------------------------------------
# RETRY WRAPPER
# ---------------------------------------------------

def safe_request(func, *args, retries=2, delay=2):
    for attempt in range(retries + 1):
        try:
            return func(*args)
        except Exception as e:
            if attempt == retries:
                raise
            print(f"Retrying after error: {e} ({attempt+1}/{retries})")
            time.sleep(delay)

# ---------------------------------------------------
# JSON DATA LOADING
# ---------------------------------------------------

_json_data_cache = None

def load_json_data():
    """Load all JSON files into memory for fast lookup (cached)."""
    global _json_data_cache
    if _json_data_cache is not None:
        return _json_data_cache

    json_sources = [
        PROJECT_ROOT / "data" / "opus-1-700-filtered.json",
        PROJECT_ROOT / "data" / "opus-701-1200-filtered.json",
        PROJECT_ROOT / "data" / "opus-1201-1817-filtered.json",
    ]

    json_data = {}  # job_id -> job_data

    for json_path in json_sources:
        if not json_path.exists():
            continue

        with open(json_path, 'r') as f:
            data = json.load(f)

        jobs = data if isinstance(data, list) else [data]
        for job in jobs:
            job_id = job.get('job_id')
            if job_id:
                json_data[job_id] = job

    _json_data_cache = json_data
    return json_data

# ---------------------------------------------------
# HELPERS
# ---------------------------------------------------

def extract_rubric_from_json(job_id, problem_id):
    """Extract rubric directly from JSON data (no API call)."""
    json_data = load_json_data()
    job = json_data.get(job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found in JSON data")

    responses = job.get('response', [])
    for resp in responses:
        if resp.get('problem_id') == problem_id:
            rewards = resp.get('rewards', [])
            for reward in rewards:
                if reward.get('grading_strategy_type') == 'rubric':
                    metadata = reward.get('metadata', {})
                    rubric = metadata.get('rubric', [])
                    if rubric:
                        return rubric

    raise ValueError(f"No rubric found for problem_id {problem_id} in job {job_id}")

def get_problem_version_id(job_id, problem_id):
    """Get problem_uuid directly from JSON data (no API call)."""
    json_data = load_json_data()
    job = json_data.get(job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found in JSON data")

    responses = job.get('response', [])
    for resp in responses:
        if resp.get('problem_id') == problem_id:
            return resp.get('problem_uuid')

    raise ValueError(f"No problem_uuid found for problem_id {problem_id} in job {job_id}")


def download_preloaded_files(problem_version_id, dest_path):
    """Download the injected repo tar.gz via TAIGA API."""
    headers = get_auth_headers()
    url = f"{TAIGA_BASE_URL}/api/problem-crud/{problem_version_id}/download-preloaded-files"
    r = requests.get(url, headers=headers, timeout=300)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(r.content)
    return len(r.content)


def extract_injected_repo(tar_path, dest_dir):
    """Extract the repo from the tar.gz, flattening the deep path structure."""
    dest_dir.mkdir(exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tar:
        members = tar.getmembers()
        submit_prefix = None
        for m in members:
            if "/submit/" in m.name:
                idx = m.name.index("/submit/")
                submit_prefix = m.name[:idx + len("/submit/")]
                break

        if not submit_prefix:
            submit_prefix = ""

        for m in members:
            if m.name.startswith(submit_prefix) and m.name != submit_prefix.rstrip("/"):
                m.name = m.name[len(submit_prefix):]
                if m.name:
                    tar.extract(m, dest_dir, filter='data')

    post_extraction_fixes(dest_dir)


def post_extraction_fixes(repo_dir):
    """Fix common extraction artifacts that break QA analysis."""
    fixes_applied = []

    # Fix 1: Flatten nested path structures (e.g. Attachments/Problem/tmp/files/repo-main/)
    # If repo_dir contains a single subdirectory tree leading to actual source, flatten it.
    nested_markers = ["Attachments", "Problem", "tmp"]
    current = repo_dir
    while True:
        entries = list(current.iterdir())
        if len(entries) == 1 and entries[0].is_dir() and entries[0].name in nested_markers + [d.name for d in entries]:
            current = entries[0]
        else:
            break

    if current != repo_dir:
        import tempfile
        with tempfile.TemporaryDirectory(dir=repo_dir.parent) as tmp:
            tmp_path = Path(tmp) / "move"
            shutil.move(str(current), str(tmp_path))
            for item in repo_dir.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            for item in tmp_path.iterdir():
                shutil.move(str(item), str(repo_dir / item.name))
        fixes_applied.append(f"flattened nested path: {current.relative_to(repo_dir)}")

    # Fix 2: Rename _init_.py back to __init__.py (common extraction corruption)
    for bad_init in repo_dir.rglob("_init_.py"):
        correct = bad_init.parent / "__init__.py"
        if not correct.exists():
            bad_init.rename(correct)
            fixes_applied.append(f"renamed _init_.py → __init__.py in {bad_init.parent.relative_to(repo_dir)}")

    # Fix 3: Remove stale extraction artifacts
    for artifact in ["Dockerfile", ".dockerignore"]:
        artifact_path = repo_dir / artifact
        if artifact_path.exists() and artifact_path.stat().st_size < 100:
            artifact_path.unlink()
            fixes_applied.append(f"removed empty artifact: {artifact}")

    if fixes_applied:
        print(f"  Post-extraction fixes applied ({len(fixes_applied)}):")
        for fix in fixes_applied:
            print(f"    - {fix}")


def validate_extraction(injected_dir):
    """Check for common extraction issues that would break QA analysis."""
    issues = []

    injected_files = set(
        str(p.relative_to(injected_dir)) for p in injected_dir.rglob("*") if p.is_file()
    )

    if not injected_files:
        issues.append("injected_repo is empty after extraction")
        return issues

    # Check for _init_.py corruption (already fixed by post_extraction_fixes, but verify)
    bad_inits = [f for f in injected_files if f.endswith("_init_.py")]
    if bad_inits:
        issues.append(f"{len(bad_inits)} _init_.py files still present (should be __init__.py)")

    # Check for nested path artifacts still present
    for f in injected_files:
        if "Attachments/" in f or "Problem/tmp/" in f:
            issues.append(f"nested path artifact still present: {f}")
            break

    return issues


# Git clone removed - we only need injected_repo for core 20% analysis

# ---------------------------------------------------
# MAIN
# ---------------------------------------------------

def prepare_one(problem_id, job_id, avg_score, show_details=False):
    """Prepare a single instance: extract rubric from JSON, download injected repo.

    Args:
        show_details: If True, print verbose step-by-step output. If False, minimal output.

    Returns:
        dict: Summary statistics (size_mb, file_count, issues, warnings)
    """
    instance_dir = INSTANCES_DIR / problem_id / job_id

    if instance_dir.exists():
        shutil.rmtree(instance_dir)

    instance_dir.mkdir(parents=True)

    # Step 1: Extract rubric directly from JSON (NO API CALL)
    rubric_entries = extract_rubric_from_json(job_id, problem_id)

    if not rubric_entries:
        raise ValueError("No rubric found in JSON data for this problem_id.")

    rubric_path = instance_dir / "rubric.json"
    with open(rubric_path, "w") as f:
        json.dump({"rubric": rubric_entries}, f, indent=2)

    # Step 2: Get problem_version_id from JSON (NO API CALL)
    problem_version_id = get_problem_version_id(job_id, problem_id)

    if not problem_version_id:
        raise ValueError("Could not find problem_version_id in JSON data.")

    # Step 3: Download injected repo
    tar_path = instance_dir / "repo.tar.gz"
    size = safe_request(download_preloaded_files, problem_version_id, tar_path)

    # Step 4: Extract injected repo
    injected_repo_dir = instance_dir / "injected_repo"
    extract_injected_repo(tar_path, injected_repo_dir)
    tar_path.unlink()

    file_count = sum(1 for _ in injected_repo_dir.rglob("*") if _.is_file())

    # Step 5: Validate extraction integrity
    issues = validate_extraction(injected_repo_dir)

    # Validate rubric structure
    warnings = []
    if len(rubric_entries) != 8:
        warnings.append(f"rubric has {len(rubric_entries)} criteria (expected 8)")

    for i, entry in enumerate(rubric_entries):
        if "criterion" not in entry or "weight" not in entry:
            warnings.append(f"rubric entry {i} missing criterion or weight")

    if show_details:
        print(f"    Extracted {file_count} files ({size / 1024 / 1024:.1f} MB)")
        if issues:
            print(f"    Extraction issues: {issues}")
        if warnings:
            print(f"    Warnings: {warnings}")

    # Write metadata
    metadata = {
        "problem_id": problem_id,
        "job_id": job_id,
        "average_score": avg_score,
        "problem_version_id": problem_version_id,
        "rubric_count": len(rubric_entries),
        "injected_file_count": file_count,
        "extraction_issues": issues,
    }

    with open(instance_dir / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    return {
        "size_mb": size / 1024 / 1024,
        "file_count": file_count,
        "issues": len(issues),
        "warnings": len(warnings)
    }


def _get_score(row):
    """Return float score from CSV row (score_mean column)."""
    for col in ("score_mean", "average_score"):
        v = row.get(col)
        if v is not None and not (isinstance(v, float) and pd.isna(v)):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _should_skip_oscillating(row):
    """Return (True, reason) if row should be skipped for num_oscillating < MIN_OSCILLATING."""
    v = row.get("num_oscillating")
    if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ("", "nan"):
        return False, None
    try:
        n = int(float(v))
        if n < MIN_OSCILLATING:
            return True, f"num_oscillating={n} < {MIN_OSCILLATING}"
    except (ValueError, TypeError):
        pass
    return False, None


def main(limit=None):
    """Read CSV, process rows sequentially. Skip already-downloaded instances.
    Applies same score/num_oscillating/repo-average filters as prefilter so we
    only download instances that can pass prefilter."""
    from repo_store import build_store_from_csv, get_repo_id, repo_average_ok

    if not CSV_PATH.exists():
        raise FileNotFoundError("instances_output.csv not found.")

    df = pd.read_csv(CSV_PATH)

    if "download_status" not in df.columns:
        df["download_status"] = ""

    # Ensure download_status column is string type to avoid dtype warnings
    df["download_status"] = df["download_status"].astype(str).replace('nan', '')

    # Build repo store once so we can filter by repo average
    store = build_store_from_csv(CSV_PATH)

    total = len(df)
    to_process = df[df["download_status"] != "downloaded"]

    if limit:
        to_process = to_process.head(limit)

    print_header("TAIGA Instance Downloader", 80)
    print(f"{Colors.BOLD}Dataset Overview:{Colors.RESET}")
    print(f"  Total instances in CSV:  {Colors.CYAN}{total:,}{Colors.RESET}")
    print(f"  Already downloaded:      {Colors.GREEN}{total - len(to_process):,}{Colors.RESET}")
    print(f"  To process:              {Colors.YELLOW}{len(to_process):,}{Colors.RESET}{f' {Colors.DIM}(limit={limit}){Colors.RESET}' if limit else ''}")
    print_footer(80)

    stats = {
        "succeeded": 0,
        "failed": 0,
        "skipped_no_score": 0,
        "skipped_score": 0,
        "skipped_oscillating": 0,
        "skipped_repo_average": 0,
        "already_downloaded": 0,
        "total_size_mb": 0,
        "total_files": 0,
    }

    processed_count = 0
    start_time = time.time()
    last_print_time = time.time()

    for idx in to_process.index:
        processed_count += 1
        row = df.loc[idx]
        problem_id = row["problem_id"]
        job_id = row["job_id"]
        avg_score = _get_score(row)

        # Progress indicator
        progress_pct = (processed_count / len(to_process)) * 100
        progress_bar = f"[{processed_count}/{len(to_process)}] {progress_pct:5.1f}%"

        if avg_score is None:
            print(f"{progress_bar} SKIP  {problem_id:<45} (no score)")
            df.at[idx, "download_status"] = "skipped_no_score"
            df.to_csv(CSV_PATH, index=False)
            stats["skipped_no_score"] += 1
            continue

        if not (0.4 <= avg_score <= 0.8):
            print(f"{progress_bar} SKIP  {problem_id:<45} (score={avg_score:.3f})")
            df.at[idx, "download_status"] = "skipped_score"
            df.to_csv(CSV_PATH, index=False)
            stats["skipped_score"] += 1
            continue

        skip_osc, reason_osc = _should_skip_oscillating(row)
        if skip_osc:
            print(f"{progress_bar} SKIP  {problem_id:<45} ({reason_osc})")
            df.at[idx, "download_status"] = "skipped_oscillating"
            df.to_csv(CSV_PATH, index=False)
            stats["skipped_oscillating"] += 1
            continue

        repo_id = get_repo_id(problem_id)
        avg_ok, avg_val = repo_average_ok(store, repo_id)
        if not avg_ok:
            print(f"{progress_bar} SKIP  {problem_id:<45} (repo_avg={avg_val:.3f})")
            df.at[idx, "download_status"] = "skipped_repo_average"
            df.to_csv(CSV_PATH, index=False)
            stats["skipped_repo_average"] += 1
            continue

        instance_dir = INSTANCES_DIR / problem_id / job_id
        if instance_dir.exists() and (instance_dir / "metadata.json").exists():
            print(f"{progress_bar} CACHE {problem_id:<45} (already downloaded)")
            df.at[idx, "download_status"] = "downloaded"
            df.to_csv(CSV_PATH, index=False)
            stats["already_downloaded"] += 1
            continue

        try:
            result = prepare_one(problem_id, job_id, avg_score, show_details=False)
            print(f"{progress_bar} OK    {problem_id:<45} ({result['size_mb']:.1f}MB, {result['file_count']} files)")
            df.at[idx, "download_status"] = "downloaded"
            df.to_csv(CSV_PATH, index=False)
            stats["succeeded"] += 1
            stats["total_size_mb"] += result["size_mb"]
            stats["total_files"] += result["file_count"]
        except Exception as e:
            error_msg = str(e)[:50]
            print(f"{progress_bar} FAIL  {problem_id:<45} ({error_msg})")
            df.at[idx, "download_status"] = f"error: {str(e)[:100]}"
            df.to_csv(CSV_PATH, index=False)
            stats["failed"] += 1

    print(f"\n{'='*70}")
    print(f"Download Complete")
    print(f"{'='*70}")
    print(f"Downloaded:        {stats['succeeded']:4d} instances ({stats['total_size_mb']:.1f} MB, {stats['total_files']} files)")
    print(f"Failed:            {stats['failed']:4d} instances")
    print(f"Already cached:    {stats['already_downloaded']:4d} instances")
    print(f"Skipped (score):   {stats['skipped_score']:4d} instances")
    print(f"Skipped (osc):     {stats['skipped_oscillating']:4d} instances")
    print(f"Skipped (repo):    {stats['skipped_repo_average']:4d} instances")
    print(f"Skipped (no data): {stats['skipped_no_score']:4d} instances")
    print(f"{'='*70}")


# ---------------------------------------------------
# PRE-FILTER: Fast deterministic rejection before agents
# ---------------------------------------------------

def prefilter_instance(instance_dir):
    """Run fast deterministic checks that can reject without invoking agents.

    Returns (pass, reason) where pass=True means proceed to agents,
    pass=False means reject immediately with the given reason.
    """
    problem_id = instance_dir.name

    # Check 1: metadata.json exists and is readable
    meta_path = instance_dir / "metadata.json"
    if not meta_path.exists():
        return False, "metadata.json missing"

    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        return False, f"metadata.json unreadable: {e}"

    # Check 2: Score in range [0.4, 0.8]
    score = meta.get("average_score")
    if score is None or not isinstance(score, (int, float)):
        return False, "average_score missing or non-numeric"
    if not (0.4 <= score <= 0.8):
        return False, f"score {score} outside [0.4, 0.8]"

    # Check 3: rubric.json exists and has exactly 8 valid entries
    rubric_path = instance_dir / "rubric.json"
    if not rubric_path.exists():
        return False, "rubric.json missing"

    try:
        with open(rubric_path) as f:
            rubric_data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        return False, f"rubric.json unreadable: {e}"

    rubric = rubric_data.get("rubric")
    if not isinstance(rubric, list):
        return False, "rubric.json: 'rubric' key missing or not an array"
    if len(rubric) != 8:
        return False, f"rubric has {len(rubric)} entries (expected 8)"

    for i, entry in enumerate(rubric):
        if not isinstance(entry, dict):
            return False, f"rubric entry {i} is not an object"
        if "criterion" not in entry or not entry["criterion"]:
            return False, f"rubric entry {i} missing or empty 'criterion'"
        if "weight" not in entry:
            return False, f"rubric entry {i} missing 'weight'"
        if entry["weight"] != 1 and entry["weight"] != 1.0:
            return False, f"rubric entry {i} has weight={entry['weight']} (expected 1)"

    # Check 4: Required directories exist
    if not (instance_dir / "injected_repo").is_dir():
        return False, "injected_repo/ directory missing"

    return True, "passed all pre-filter checks"


def run_prefilter(limit=None):
    """Run pre-filter on all downloaded instances. Applies:
      1. Standard structural checks (metadata, rubric, dirs)
      2. num_oscillating >= 3 (reject if present & numeric & < 3)
      3. Repo average score in [0.4, 0.8] (all instances per repo)
      4. Cross-instance rubric duplication (same file+function location)
    """
    from datetime import datetime, timezone
    from repo_store import (
        build_store_from_csv,
        repo_average_ok,
        get_repo_id,
        get_prior_rubrics,
        check_duplicate_criteria,
        add_accepted_rubric,
    )

    if not CSV_PATH.exists():
        raise FileNotFoundError("CSV not found.")

    df = pd.read_csv(CSV_PATH, dtype={"status": str, "qa_result": str, "qa_notes": str, "processed_at": str, "download_status": str})

    for col in ["status", "qa_result", "qa_notes", "processed_at", "download_status"]:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)

    # Build repo store with averages from full CSV (once at start)
    store = build_store_from_csv(CSV_PATH)
    print("Repo averages computed from full CSV and persisted to data/repo_store.json\n")

    candidates = df[
        (df["download_status"] == "downloaded") &
        ((df["status"] == "") | (df["status"].isna()))
    ]

    if limit:
        candidates = candidates.head(limit)

    print(f"\n{'='*70}")
    print(f"Pre-filter: Deterministic Quality Checks")
    print(f"{'='*70}")
    print(f"Candidates to check: {len(candidates)}")
    print(f"{'='*70}\n")

    stats = {
        "passed": 0,
        "rejected_oscillating": 0,
        "rejected_repo_avg": 0,
        "rejected_structural": 0,
        "rejected_duplicate": 0,
    }

    processed_count = 0

    def _reject(idx, problem_id, reason, instance_dir, category):
        stats[category] += 1
        now = datetime.now(timezone.utc).isoformat()
        df.at[idx, "status"] = "done"
        df.at[idx, "qa_result"] = "rejected"
        df.at[idx, "qa_notes"] = f"Pre-filter reject: {reason}"
        df.at[idx, "processed_at"] = now

        result = {
            "problem_id": problem_id,
            "verdict": "rejected",
            "confidence": "high",
            "early_termination": True,
            "early_termination_reason": f"prefilter: {reason}",
            "reason_codes": ["prefilter_reject"],
            "qa_notes": f"Pre-filter reject: {reason}",
            "phases_completed": ["prefilter"],
            "phases_skipped": ["dataset-loader", "diff-analyzer", "scoring-engine",
                               "rubric-validator", "instance-evaluator"],
            "processed_at": now,
        }
        if instance_dir.exists():
            with open(instance_dir / "result.json", "w") as f:
                json.dump(result, f, indent=2)

    for idx in candidates.index:
        processed_count += 1
        row = df.loc[idx]
        problem_id = row["problem_id"]
        job_id = row["job_id"]
        instance_dir = INSTANCES_DIR / problem_id / job_id

        progress_pct = (processed_count / len(candidates)) * 100
        progress_bar = f"[{processed_count}/{len(candidates)}] {progress_pct:5.1f}%"

        # --- Check 0: num_oscillating ---
        try:
            n_osc = row.get("num_oscillating")
            if n_osc is not None and str(n_osc).strip() not in ("", "nan"):
                n_osc_int = int(float(n_osc))
                if n_osc_int < MIN_OSCILLATING:
                    reason = f"num_osc={n_osc_int}<{MIN_OSCILLATING}"
                    print(f"{progress_bar} REJECT {problem_id:<45} ({reason})")
                    _reject(idx, problem_id, reason, instance_dir, "rejected_oscillating")
                    continue
        except (ValueError, TypeError):
            pass

        # --- Check 1: Repo average in [0.4, 0.8] ---
        repo_id = get_repo_id(problem_id)
        avg_ok, avg_val = repo_average_ok(store, repo_id)
        if not avg_ok:
            reason = f"repo_avg={avg_val:.3f}"
            print(f"{progress_bar} REJECT {problem_id:<45} ({reason})")
            _reject(idx, problem_id, reason, instance_dir, "rejected_repo_avg")
            continue

        # --- Check 2: Standard structural checks ---
        if not instance_dir.exists():
            reason = "instance dir missing"
            print(f"{progress_bar} REJECT {problem_id:<45} ({reason})")
            _reject(idx, problem_id, reason, instance_dir, "rejected_structural")
            continue

        ok, reason = prefilter_instance(instance_dir)
        if not ok:
            print(f"{progress_bar} REJECT {problem_id:<45} ({reason[:35]})")
            _reject(idx, problem_id, reason, instance_dir, "rejected_structural")
            continue

        # --- Check 3: Cross-instance rubric duplication ---
        rubric_path = instance_dir / "rubric.json"
        try:
            with open(rubric_path) as f:
                rubric_data = json.load(f)
            current_rubric = rubric_data.get("rubric", [])
        except Exception:
            current_rubric = []

        if current_rubric:
            prior = get_prior_rubrics(store, repo_id, exclude_problem_id=problem_id)
            is_dup, dup_details = check_duplicate_criteria(current_rubric, prior)
            if is_dup:
                reason = "duplicate bug location"
                print(f"{progress_bar} REJECT {problem_id:<45} ({reason})")
                _reject(idx, problem_id, reason + ": " + "; ".join(dup_details), instance_dir, "rejected_duplicate")
                continue

        print(f"{progress_bar} PASS   {problem_id:<45} (ready for agent QA)")
        stats["passed"] += 1

    df.to_csv(CSV_PATH, index=False)

    print(f"\n{'='*70}")
    print(f"Pre-filter Complete")
    print(f"{'='*70}")
    print(f"Passed:                {stats['passed']:4d} instances (ready for agent QA)")
    print(f"Rejected (oscillating):{stats['rejected_oscillating']:4d} instances")
    print(f"Rejected (repo avg):   {stats['rejected_repo_avg']:4d} instances")
    print(f"Rejected (structural): {stats['rejected_structural']:4d} instances")
    print(f"Rejected (duplicate):  {stats['rejected_duplicate']:4d} instances")
    print(f"{'='*70}")


# ---------------------------------------------------

def mark_accepted(problem_id: str, job_id: str):
    """After an instance is accepted by agents, store its rubric for future dup checks."""
    from repo_store import get_repo_id, load_store, add_accepted_rubric

    instance_dir = INSTANCES_DIR / problem_id / job_id
    rubric_path = instance_dir / "rubric.json"
    if not rubric_path.exists():
        print(f"ERROR: {rubric_path} not found")
        return

    with open(rubric_path) as f:
        rubric_data = json.load(f)
    rubric = rubric_data.get("rubric", [])

    store = load_store()
    repo_id = get_repo_id(problem_id)
    add_accepted_rubric(store, repo_id, problem_id, rubric)
    print(f"Stored rubric for {problem_id} ({job_id}) under repo {repo_id}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Meta 800 — TAIGA Instance Downloader & Pre-filter")
    parser.add_argument("action", nargs="?", default="download",
                        choices=["download", "prefilter", "accept"],
                        help="'download' to fetch instances, "
                             "'prefilter' to run deterministic rejection checks, "
                             "'accept' to store rubric after agent acceptance")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max rows to process")
    parser.add_argument("--problem-id", type=str, default=None,
                        help="problem_id (required for 'accept' action)")
    parser.add_argument("--job-id", type=str, default=None,
                        help="job_id (required for 'accept' action)")
    args = parser.parse_args()

    if args.action == "download":
        print(f"Downloading instances (limit={args.limit})")
        main(limit=args.limit)
    elif args.action == "prefilter":
        print(f"Running pre-filter (limit={args.limit})")
        run_prefilter(limit=args.limit)
    elif args.action == "accept":
        if not args.problem_id or not args.job_id:
            parser.error("--problem-id and --job-id required for 'accept' action")
        mark_accepted(args.problem_id, args.job_id)