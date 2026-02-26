"""
Meta 800 — TAIGA Instance Downloader
#swe-bench-taiga-meta-800

Downloads and prepares dataset instances from TAIGA for QA evaluation.
Reads rubrics directly from JSON files (no job metadata API call).
Only downloads injected_repo (no git clone of original repo).

Optimizations:
- Rubric extraction from JSON files (no API call for job metadata)
- Core 20% analysis: Rubric tells us which files have bugs, no need for git diff
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
import time
from datetime import datetime
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from threading import Lock, Thread

# ---------------------------------------------------
# CONFIG
# ---------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent

# Import from src/
sys.path.insert(0, str(PROJECT_ROOT / "src"))
import db_helper
INSTANCES_DIR = PROJECT_ROOT / "instances"
DB_PATH = PROJECT_ROOT / "instances.db"

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

def clear_lines(n):
    """Clear n lines from terminal."""
    if n <= 0:
        return
    for _ in range(n):
        sys.stdout.write('\033[F')  # Move cursor up
        sys.stdout.write('\033[K')  # Clear line
    sys.stdout.flush()

def print_active_downloads(active_downloads, active_lock):
    """Print currently active downloads."""
    with active_lock:
        if not active_downloads:
            return 0

        print(f"{Colors.BOLD}{Colors.BLUE}{'='*80}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.BLUE}Active Downloads ({len(active_downloads)} running){Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.BLUE}{'-'*80}{Colors.RESET}")

        lines = 3  # Header lines
        for problem_id, status_info in list(active_downloads.items())[:10]:  # Show max 10
            elapsed = time.time() - status_info['start_time']
            status = status_info.get('status', 'starting')
            retry = status_info.get('retry', 0)
            retry_str = f" {Colors.YELLOW}[retry:{retry}]{Colors.RESET}" if retry > 0 else ""
            print(f"  {Colors.CYAN}\u2022{Colors.RESET} {problem_id:<45s} {Colors.DIM}{status:<30s}{Colors.RESET} {Colors.GRAY}({elapsed:.0f}s){Colors.RESET}{retry_str}")
            lines += 1

        if len(active_downloads) > 10:
            print(f"  {Colors.DIM}... and {len(active_downloads) - 10} more{Colors.RESET}")
            lines += 1

        print(f"{Colors.BOLD}{Colors.BLUE}{'='*80}{Colors.RESET}")
        lines += 1

        sys.stdout.flush()  # Force flush to ensure immediate display
        return lines

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


def download_preloaded_files(problem_version_id, dest_path, progress_callback=None):
    """Download the injected repo tar.gz via TAIGA API with progress reporting."""
    headers = get_auth_headers()
    url = f"{TAIGA_BASE_URL}/api/problem-crud/{problem_version_id}/download-preloaded-files"

    # Stream download to show progress
    r = requests.get(url, headers=headers, timeout=300, stream=True)
    r.raise_for_status()

    total_size = int(r.headers.get('content-length', 0))
    downloaded = 0

    with open(dest_path, "wb") as f:
        last_update = 0
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                # Update progress every 256KB to avoid too many callbacks
                if progress_callback and total_size > 0 and (downloaded - last_update) >= 262144:
                    progress_callback(downloaded, total_size)
                    last_update = downloaded
        # Final update
        if progress_callback and total_size > 0:
            progress_callback(downloaded, total_size)

    return downloaded


def extract_injected_repo(tar_path, dest_dir, silent=True):
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

    post_extraction_fixes(dest_dir, silent=silent)


def post_extraction_fixes(repo_dir, silent=True):
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

    if fixes_applied and not silent:
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
# The rubric already tells us which files have bugs, no need for git diff

# ---------------------------------------------------
# MAIN
# ---------------------------------------------------

def prepare_one(problem_id, job_id, avg_score, show_details=False, progress_callback=None):
    """Prepare a single instance: extract rubric from JSON, download injected repo.

    Args:
        show_details: If True, print verbose step-by-step output. If False, minimal output.
        progress_callback: Optional callback(current, total, status) for download progress.

    Returns:
        dict: Summary statistics (size_mb, file_count, issues, warnings)
    """
    instance_dir = INSTANCES_DIR / problem_id / job_id

    if instance_dir.exists():
        shutil.rmtree(instance_dir)

    instance_dir.mkdir(parents=True)

    # Step 1: Extract rubric directly from JSON (NO API CALL)
    if progress_callback:
        progress_callback(0, 100, "extracting rubric")
    rubric_entries = extract_rubric_from_json(job_id, problem_id)

    if not rubric_entries:
        raise ValueError("No rubric found in JSON data for this problem_id.")

    rubric_path = instance_dir / "rubric.json"
    with open(rubric_path, "w") as f:
        json.dump({"rubric": rubric_entries}, f, indent=2)

    # Step 2: Get problem_version_id from JSON (NO API CALL)
    if progress_callback:
        progress_callback(10, 100, "resolving version")
    problem_version_id = get_problem_version_id(job_id, problem_id)

    if not problem_version_id:
        raise ValueError("Could not find problem_version_id in JSON data.")

    # Step 3: Download injected repo with progress
    tar_path = instance_dir / "repo.tar.gz"

    def download_progress(downloaded, total):
        if progress_callback:
            pct = int(10 + (downloaded / total) * 80)  # 10-90% for download
            progress_callback(pct, 100, f"downloading {downloaded/(1024*1024):.1f}/{total/(1024*1024):.1f}MB")

    size = download_preloaded_files(problem_version_id, tar_path, progress_callback=download_progress)

    # Step 4: Extract injected repo
    if progress_callback:
        progress_callback(90, 100, "extracting repo")
    injected_repo_dir = instance_dir / "injected_repo"
    extract_injected_repo(tar_path, injected_repo_dir, silent=True)
    tar_path.unlink()

    file_count = sum(1 for _ in injected_repo_dir.rglob("*") if _.is_file())

    # Step 5: Validate extraction integrity
    if progress_callback:
        progress_callback(95, 100, "validating")
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

    if progress_callback:
        progress_callback(100, 100, "complete")

    return {
        "size_mb": size / 1024 / 1024,
        "file_count": file_count,
        "issues": len(issues),
        "warnings": len(warnings)
    }


def _get_score(row):
    """Return float score from database row (score_mean column)."""
    for col in ("score_mean", "average_score"):
        v = row.get(col)
        if v is not None:
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


def process_single_instance(row, stats_lock, stats, processed_count_lock, processed_count_ref, total, start_time, max_retries, active_downloads, active_lock):
    """Process a single instance with retry logic and live progress tracking."""
    problem_id = row["problem_id"]
    job_id = row["job_id"]
    avg_score = _get_score(row)

    # Check skip conditions
    if avg_score is None:
        with stats_lock:
            stats["skipped_no_score"] += 1
        db_helper.update_download_status(problem_id, "skipped_no_score")
        return ('SKIP', problem_id, 'no score', None)

    if not (0.4 <= avg_score <= 0.8):
        with stats_lock:
            stats["skipped_score"] += 1
        db_helper.update_download_status(problem_id, "skipped_score")
        return ('SKIP', problem_id, f'score={avg_score:.3f}', None)

    skip_osc, reason_osc = _should_skip_oscillating(row)
    if skip_osc:
        with stats_lock:
            stats["skipped_oscillating"] += 1
        db_helper.update_download_status(problem_id, "skipped_oscillating")
        return ('SKIP', problem_id, reason_osc, None)

    repo_id = db_helper.get_repo_id(problem_id)
    avg_ok, avg_val = db_helper.repo_average_ok(repo_id)
    if not avg_ok:
        with stats_lock:
            stats["skipped_repo_average"] += 1
        db_helper.update_download_status(problem_id, "skipped_repo_average")
        return ('SKIP', problem_id, f'repo_avg={avg_val:.3f}', None)

    instance_dir = INSTANCES_DIR / problem_id / job_id
    if instance_dir.exists() and (instance_dir / "metadata.json").exists():
        with stats_lock:
            stats["already_downloaded"] += 1
        db_helper.update_download_status(problem_id, "downloaded")
        return ('CACHE', problem_id, 'already downloaded', None)

    # Track this download
    with active_lock:
        active_downloads[problem_id] = {
            'start_time': time.time(),
            'status': 'starting',
            'retry': 0
        }

    # Try download with retries
    last_error = None
    for attempt in range(max_retries):
        try:
            retry_msg = f"retry:{attempt+1}" if attempt > 0 else ""

            # Update retry count
            with active_lock:
                if problem_id in active_downloads:
                    active_downloads[problem_id]['retry'] = attempt

            # Progress tracking with live updates
            def progress_callback(current, total_size, status):
                with active_lock:
                    if problem_id in active_downloads:
                        active_downloads[problem_id]['status'] = status

            result = prepare_one(problem_id, job_id, avg_score, show_details=False, progress_callback=progress_callback)

            # Remove from active tracking
            with active_lock:
                if problem_id in active_downloads:
                    del active_downloads[problem_id]

            with stats_lock:
                stats["succeeded"] += 1
                stats["total_size_mb"] += result["size_mb"]
                stats["total_files"] += result["file_count"]

            db_helper.update_download_status(problem_id, "downloaded")
            details = f"{format_size(result['size_mb'] * 1024 * 1024)}, {result['file_count']:,} files"
            if retry_msg:
                details = f"{details} {retry_msg}"
            return ('OK', problem_id, details, result)

        except Exception as e:
            last_error = str(e)
            if attempt < max_retries - 1:
                with active_lock:
                    if problem_id in active_downloads:
                        active_downloads[problem_id]['status'] = f'retrying (attempt {attempt+2}/{max_retries})'
                time.sleep(2 ** attempt)  # Exponential backoff
                continue
            else:
                # Remove from active tracking
                with active_lock:
                    if problem_id in active_downloads:
                        del active_downloads[problem_id]

                with stats_lock:
                    stats["failed"] += 1
                error_msg = last_error[:40]
                db_helper.update_download_status(problem_id, f"error: {last_error[:100]}")
                return ('FAIL', problem_id, f"{error_msg} (retry:{max_retries})", None)

def main(limit=None, parallel=10, max_retries=3):
    """Process instances from database in parallel with retry logic.
    Applies same score/num_oscillating/repo-average filters as prefilter so we
    only download instances that can pass prefilter.

    Args:
        limit: Max instances to process
        parallel: Number of parallel workers (default 10)
        max_retries: Max retry attempts per instance (default 3)
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}\nRun: python migrate_to_sqlite.py")

    # Build repo store from database
    db_helper.build_repo_store()

    total = db_helper.get_instance_count()
    to_process = db_helper.get_instances_to_process(limit=limit)
    already_downloaded = db_helper.get_downloaded_count()

    print_header("TAIGA Instance Downloader (Parallel Mode)", 80)
    print(f"{Colors.BOLD}Dataset Overview:{Colors.RESET}")
    print(f"  Total instances:         {Colors.CYAN}{total:,}{Colors.RESET}")
    print(f"  Already downloaded:      {Colors.GREEN}{already_downloaded:,}{Colors.RESET}")
    print(f"  To process:              {Colors.YELLOW}{len(to_process):,}{Colors.RESET}{f' {Colors.DIM}(limit={limit}){Colors.RESET}' if limit else ''}")
    print(f"  Parallel workers:        {Colors.CYAN}{parallel}{Colors.RESET}")
    print(f"  Max retries:             {Colors.CYAN}{max_retries}{Colors.RESET}")
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

    stats_lock = Lock()
    processed_count = [0]  # Use list for shared counter
    processed_count_lock = Lock()
    start_time = time.time()

    # Active downloads tracking for live status
    active_downloads = {}  # problem_id -> status_info
    active_lock = Lock()
    last_display_lines = [0]  # Track lines for clearing
    stop_display = [False]  # Signal to stop display thread

    def submit_task(executor, row):
        """Submit a task and track it."""
        future = executor.submit(
            process_single_instance,
            row, stats_lock, stats,
            processed_count_lock, processed_count, len(to_process), start_time, max_retries,
            active_downloads, active_lock
        )
        return future

    def display_progress():
        """Background thread to display active downloads."""
        while not stop_display[0]:
            # Check if there are active downloads without locking
            has_active = False
            with active_lock:
                has_active = len(active_downloads) > 0

            if has_active:
                # Clear previous display
                if last_display_lines[0] > 0:
                    clear_lines(last_display_lines[0])
                # Print new display
                last_display_lines[0] = print_active_downloads(active_downloads, active_lock)
            else:
                # Clear display if no active downloads
                if last_display_lines[0] > 0:
                    clear_lines(last_display_lines[0])
                    last_display_lines[0] = 0

            time.sleep(1)  # Update every second

    # Start display thread
    display_thread = Thread(target=display_progress, daemon=True)
    display_thread.start()

    with ThreadPoolExecutor(max_workers=parallel) as executor:
        # Submit initial batch
        futures = {}
        instances_iter = iter(to_process)

        # Keep submitting tasks to maintain full pool
        def submit_more_tasks(count):
            """Submit up to 'count' new tasks."""
            submitted = 0
            for _ in range(count):
                try:
                    row = next(instances_iter)
                    future = submit_task(executor, row)
                    futures[future] = row['problem_id']
                    submitted += 1
                except StopIteration:
                    break
            return submitted

        # Submit initial batch
        submit_more_tasks(parallel)

        # Process results as they complete
        while futures:  # Continue while there are pending futures
            # Wait for at least one to complete
            done, pending = wait(futures.keys(), return_when='FIRST_COMPLETED', timeout=1)

            for future in done:
                with processed_count_lock:
                    processed_count[0] += 1
                    current_count = processed_count[0]

                # Get result
                try:
                    status, problem_id, details, result = future.result()
                except Exception as e:
                    status = 'FAIL'
                    problem_id = f"unknown_{futures.get(future, 'unknown')}"
                    details = f"exception: {str(e)[:40]}"
                    result = None

                # Remove completed future from tracking
                del futures[future]

                # Progress indicator
                progress_pct = (current_count / len(to_process)) * 100
                progress_bar = f"{Colors.DIM}[{current_count:4d}/{len(to_process):4d}]{Colors.RESET} {Colors.BOLD}{progress_pct:6.2f}%{Colors.RESET}"

                # Estimate time remaining
                elapsed = time.time() - start_time
                if current_count > 0:
                    avg_time_per_item = elapsed / current_count
                    remaining_items = len(to_process) - current_count
                    eta_seconds = avg_time_per_item * remaining_items
                    eta_str = f"{Colors.DIM}ETA: {format_duration(eta_seconds)}{Colors.RESET}"
                else:
                    eta_str = ""

                # Clear display and print result
                if last_display_lines[0] > 0:
                    clear_lines(last_display_lines[0])
                    last_display_lines[0] = 0

                line = format_status(status, problem_id, details, progress_bar)
                if status == 'OK':
                    line += f" {eta_str}"
                print(line, flush=True)

            # After processing completed futures, submit more to keep pool full
            slots_available = parallel - len(futures)
            if slots_available > 0:
                submit_more_tasks(slots_available)

    # Stop display thread
    stop_display[0] = True
    time.sleep(1.1)  # Wait for last display update
    if last_display_lines[0] > 0:
        clear_lines(last_display_lines[0])

    elapsed_total = time.time() - start_time

    print_header("Download Complete", 80)
    print(f"{Colors.BOLD}Results Summary:{Colors.RESET}")
    print(f"  {Colors.GREEN}Downloaded:{Colors.RESET}        {stats['succeeded']:5,} instances  {Colors.DIM}({format_size(stats['total_size_mb'] * 1024 * 1024)}, {stats['total_files']:,} files){Colors.RESET}")
    if stats['failed'] > 0:
        print(f"  {Colors.RED}Failed:{Colors.RESET}            {stats['failed']:5,} instances")
    if stats['already_downloaded'] > 0:
        print(f"  {Colors.CYAN}Cached:{Colors.RESET}            {stats['already_downloaded']:5,} instances")
    print(f"\n{Colors.BOLD}Skipped:{Colors.RESET}")
    if stats['skipped_score'] > 0:
        print(f"  {Colors.YELLOW}Score filter:{Colors.RESET}     {stats['skipped_score']:5,} instances  {Colors.DIM}(outside 0.4-0.8 range){Colors.RESET}")
    if stats['skipped_oscillating'] > 0:
        print(f"  {Colors.YELLOW}Oscillating:{Colors.RESET}      {stats['skipped_oscillating']:5,} instances  {Colors.DIM}(< 3 oscillations){Colors.RESET}")
    if stats['skipped_repo_average'] > 0:
        print(f"  {Colors.YELLOW}Repo average:{Colors.RESET}     {stats['skipped_repo_average']:5,} instances  {Colors.DIM}(repo avg outside range){Colors.RESET}")
    if stats['skipped_no_score'] > 0:
        print(f"  {Colors.YELLOW}No score data:{Colors.RESET}    {stats['skipped_no_score']:5,} instances")
    print(f"\n{Colors.BOLD}Performance:{Colors.RESET}")
    print(f"  Total time:           {Colors.CYAN}{format_duration(elapsed_total)}{Colors.RESET}")
    if stats['succeeded'] > 0:
        print(f"  Avg time per download: {Colors.CYAN}{elapsed_total/stats['succeeded']:.1f}s{Colors.RESET}")
        print(f"  Download rate:        {Colors.CYAN}{(stats['total_size_mb'] / elapsed_total * 60):.1f} MB/min{Colors.RESET}")
    print_footer(80)


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

    # Check duplicate criteria helper
    def check_duplicate_criteria(current_rubric, prior_rubrics):
        """Check if rubric criteria duplicate any prior accepted rubrics."""
        duplicates = []
        for entry in current_rubric:
            file_path = entry.get('criterion', {}).get('file_path', '')
            func_name = entry.get('criterion', {}).get('function_name', '')
            if not file_path:
                continue

            for prior_pid, prior_rubric in prior_rubrics.items():
                for prior_entry in prior_rubric:
                    prior_file = prior_entry.get('criterion', {}).get('file_path', '')
                    prior_func = prior_entry.get('criterion', {}).get('function_name', '')
                    if file_path == prior_file and func_name == prior_func:
                        duplicates.append(f"{file_path}:{func_name} (matches {prior_pid})")

        return (len(duplicates) > 0, duplicates)

    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}\nRun: python migrate_to_sqlite.py")

    # Build repo store from database
    db_helper.build_repo_store()

    # Get candidates
    candidates = db_helper.get_instances_for_prefilter(limit=limit)

    print_header("Pre-filter: Deterministic Quality Checks", 80)
    print(f"{Colors.BOLD}Configuration:{Colors.RESET}")
    print(f"  Repo store loaded:   {Colors.GREEN}data/repo_store.db{Colors.RESET}")
    print(f"  Candidates to check: {Colors.CYAN}{len(candidates):,}{Colors.RESET}")
    if limit:
        print(f"  Limit applied:       {Colors.YELLOW}{limit:,}{Colors.RESET}")
    print(f"\n{Colors.BOLD}Quality Checks:{Colors.RESET}")
    print(f"  {Colors.DIM}1. Structural integrity (metadata, rubric, directories){Colors.RESET}")
    print(f"  {Colors.DIM}2. Oscillating count >= {MIN_OSCILLATING}{Colors.RESET}")
    print(f"  {Colors.DIM}3. Repo average score in [0.4, 0.8]{Colors.RESET}")
    print(f"  {Colors.DIM}4. Cross-instance bug location deduplication{Colors.RESET}")
    print_footer(80)

    stats = {
        "passed": 0,
        "rejected_oscillating": 0,
        "rejected_repo_avg": 0,
        "rejected_structural": 0,
        "rejected_duplicate": 0,
    }

    processed_count = 0
    start_time = time.time()

    def _reject(problem_id, reason, instance_dir, category):
        stats[category] += 1
        now = datetime.now(timezone.utc).isoformat()
        db_helper.update_qa_status(problem_id, "rejected", f"Pre-filter reject: {reason}", now)

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

    for row in candidates:
        processed_count += 1
        problem_id = row["problem_id"]
        job_id = row["job_id"]
        instance_dir = INSTANCES_DIR / problem_id / job_id

        progress_pct = (processed_count / len(candidates)) * 100
        progress_bar = f"{Colors.DIM}[{processed_count:4d}/{len(candidates):4d}]{Colors.RESET} {Colors.BOLD}{progress_pct:6.2f}%{Colors.RESET}"

        # Estimate time remaining
        elapsed = time.time() - start_time
        if processed_count > 0:
            avg_time_per_item = elapsed / processed_count
            remaining_items = len(candidates) - processed_count
            eta_seconds = avg_time_per_item * remaining_items
            eta_str = f"{Colors.DIM}ETA: {format_duration(eta_seconds)}{Colors.RESET}"
        else:
            eta_str = ""

        # --- Check 0: num_oscillating ---
        try:
            n_osc = row.get("num_oscillating")
            if n_osc is not None and str(n_osc).strip() not in ("", "nan"):
                n_osc_int = int(float(n_osc))
                if n_osc_int < MIN_OSCILLATING:
                    reason = f"num_osc={n_osc_int}<{MIN_OSCILLATING}"
                    print(format_status('REJECT', problem_id, reason, progress_bar))
                    _reject(problem_id, reason, instance_dir, "rejected_oscillating")
                    continue
        except (ValueError, TypeError):
            pass

        # --- Check 1: Repo average in [0.4, 0.8] ---
        repo_id = db_helper.get_repo_id(problem_id)
        avg_ok, avg_val = db_helper.repo_average_ok(repo_id)
        if not avg_ok:
            reason = f"repo_avg={avg_val:.3f}"
            print(format_status('REJECT', problem_id, reason, progress_bar))
            _reject(problem_id, reason, instance_dir, "rejected_repo_avg")
            continue

        # --- Check 2: Standard structural checks ---
        if not instance_dir.exists():
            reason = "instance dir missing"
            print(format_status('REJECT', problem_id, reason, progress_bar))
            _reject(problem_id, reason, instance_dir, "rejected_structural")
            continue

        ok, reason = prefilter_instance(instance_dir)
        if not ok:
            reason_short = reason[:40] + "..." if len(reason) > 40 else reason
            print(format_status('REJECT', problem_id, reason_short, progress_bar))
            _reject(problem_id, reason, instance_dir, "rejected_structural")
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
            prior = db_helper.get_prior_rubrics(repo_id, exclude_problem_id=problem_id)
            is_dup, dup_details = check_duplicate_criteria(current_rubric, prior)
            if is_dup:
                reason = "duplicate bug location"
                print(format_status('REJECT', problem_id, reason, progress_bar))
                _reject(problem_id, reason + ": " + "; ".join(dup_details), instance_dir, "rejected_duplicate")
                continue

        print(format_status('PASS', problem_id, 'ready for agent QA', progress_bar) + f" {eta_str}")
        stats["passed"] += 1

    elapsed_total = time.time() - start_time
    total_checked = sum(stats.values())

    print_header("Pre-filter Complete", 80)
    print(f"{Colors.BOLD}Results Summary:{Colors.RESET}")
    print(f"  {Colors.GREEN}Passed:{Colors.RESET}            {stats['passed']:5,} instances  {Colors.DIM}(ready for agent QA){Colors.RESET}")
    if stats['rejected_oscillating'] + stats['rejected_repo_avg'] + stats['rejected_structural'] + stats['rejected_duplicate'] > 0:
        print(f"\n{Colors.BOLD}Rejected:{Colors.RESET}")
        if stats['rejected_oscillating'] > 0:
            print(f"  {Colors.RED}Oscillating:{Colors.RESET}      {stats['rejected_oscillating']:5,} instances  {Colors.DIM}(< {MIN_OSCILLATING} oscillations){Colors.RESET}")
        if stats['rejected_repo_avg'] > 0:
            print(f"  {Colors.RED}Repo average:{Colors.RESET}     {stats['rejected_repo_avg']:5,} instances  {Colors.DIM}(outside [0.4, 0.8]){Colors.RESET}")
        if stats['rejected_structural'] > 0:
            print(f"  {Colors.RED}Structural:{Colors.RESET}       {stats['rejected_structural']:5,} instances  {Colors.DIM}(missing/invalid data){Colors.RESET}")
        if stats['rejected_duplicate'] > 0:
            print(f"  {Colors.RED}Duplicate:{Colors.RESET}        {stats['rejected_duplicate']:5,} instances  {Colors.DIM}(same bug location){Colors.RESET}")
    print(f"\n{Colors.BOLD}Performance:{Colors.RESET}")
    print(f"  Total time:           {Colors.CYAN}{format_duration(elapsed_total)}{Colors.RESET}")
    if total_checked > 0:
        print(f"  Avg time per check:   {Colors.CYAN}{elapsed_total/total_checked:.2f}s{Colors.RESET}")
        print(f"  Throughput:           {Colors.CYAN}{total_checked / elapsed_total * 60:.1f} checks/min{Colors.RESET}")
    print_footer(80)


# ---------------------------------------------------

def mark_accepted(problem_id: str, job_id: str):
    """After an instance is accepted by agents, store its rubric for future dup checks."""
    instance_dir = INSTANCES_DIR / problem_id / job_id
    rubric_path = instance_dir / "rubric.json"
    if not rubric_path.exists():
        print(f"ERROR: {rubric_path} not found")
        return

    with open(rubric_path) as f:
        rubric_data = json.load(f)
    rubric = rubric_data.get("rubric", [])

    repo_id = db_helper.get_repo_id(problem_id)
    db_helper.add_accepted_rubric(repo_id, problem_id, rubric)
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
    parser.add_argument("--parallel", type=int, default=10,
                        help="Number of parallel workers for downloads (default: 10)")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="Max retry attempts per instance (default: 3)")
    parser.add_argument("--problem-id", type=str, default=None,
                        help="problem_id (required for 'accept' action)")
    parser.add_argument("--job-id", type=str, default=None,
                        help="job_id (required for 'accept' action)")
    args = parser.parse_args()

    if args.action == "download":
        main(limit=args.limit, parallel=args.parallel, max_retries=args.max_retries)
    elif args.action == "prefilter":
        run_prefilter(limit=args.limit)
    elif args.action == "accept":
        if not args.problem_id or not args.job_id:
            parser.error("--problem-id and --job-id required for 'accept' action")
        mark_accepted(args.problem_id, args.job_id)