"""
Repo store management for Meta 800 dataset.

Tracks repository-level statistics and accepted rubrics to enable:
- Repo-level average score filtering
- Cross-instance duplicate bug detection

Author: brian.k @ Turing
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Tuple, Any
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent
STORE_PATH = PROJECT_ROOT / "data" / "repo_store.json"


def get_repo_id(problem_id: str) -> str:
    """Extract repo_id from problem_id by removing trailing -NN suffix.
    
    Example: needle-di__needle-di-01 -> needle-di__needle-di
    """
    parts = problem_id.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return problem_id


def build_store_from_csv(csv_path: Path) -> Dict[str, Any]:
    """Build repo store from CSV, calculating repo-level average scores.
    
    Returns a dict mapping repo_id -> {
        "repo_id": str,
        "average_score": float,
        "instance_count": int,
        "processed_rubrics": []  # List of accepted rubrics
    }
    """
    df = pd.read_csv(csv_path)
    
    # Group by repo_id and calculate averages
    store = {}
    
    for _, row in df.iterrows():
        problem_id = row.get("problem_id")
        if not problem_id:
            continue
            
        repo_id = get_repo_id(problem_id)
        
        # Get score
        score = None
        for col in ("score_mean", "average_score"):
            if col in row and pd.notna(row[col]):
                try:
                    score = float(row[col])
                    break
                except (TypeError, ValueError):
                    pass
        
        if score is None:
            continue
        
        if repo_id not in store:
            store[repo_id] = {
                "repo_id": repo_id,
                "scores": [],
                "instance_count": 0,
                "processed_rubrics": []
            }
        
        store[repo_id]["scores"].append(score)
        store[repo_id]["instance_count"] += 1
    
    # Calculate averages
    for repo_id, data in store.items():
        scores = data["scores"]
        data["average_score"] = sum(scores) / len(scores) if scores else 0.0
        del data["scores"]  # Remove raw scores, keep only average
    
    # Load existing processed_rubrics from disk if available
    existing_store = load_store()
    for repo_id, repo_data in existing_store.items():
        if repo_id in store:
            store[repo_id]["processed_rubrics"] = repo_data.get("processed_rubrics", [])
    
    # Save to disk
    save_store(store)
    
    return store


def load_store() -> Dict[str, Any]:
    """Load repo store from disk."""
    if not STORE_PATH.exists():
        return {}
    
    try:
        with open(STORE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_store(store: Dict[str, Any]):
    """Save repo store to disk."""
    STORE_PATH.parent.mkdir(exist_ok=True)
    with open(STORE_PATH, "w") as f:
        json.dump(store, f, indent=2)


def repo_average_ok(store: Dict[str, Any], repo_id: str) -> Tuple[bool, float]:
    """Check if repo average score is in acceptable range [0.4, 0.8].
    
    Returns (is_ok, average_score)
    """
    repo_data = store.get(repo_id)
    if not repo_data:
        return False, 0.0
    
    avg = repo_data.get("average_score", 0.0)
    return 0.4 <= avg <= 0.8, avg


def get_prior_rubrics(store: Dict[str, Any], repo_id: str, exclude_problem_id: str = None) -> List[Dict[str, Any]]:
    """Get list of accepted rubrics for a repo.

    Returns list of {problem_id, rubric, score} dicts from previously accepted instances.
    If exclude_problem_id is provided, excludes that instance from results.
    """
    repo_data = store.get(repo_id, {})
    all_rubrics = repo_data.get("processed_rubrics", [])

    if exclude_problem_id:
        return [r for r in all_rubrics if r.get("problem_id") != exclude_problem_id]

    return all_rubrics


def _extract_file_and_function(criterion_text: str) -> List[Tuple[str, str]]:
    """Extract file path and function/symbol references from criterion text.

    Returns list of (normalized_file_path, function_name) tuples.
    Conservative approach: only extracts clear file paths and function references.
    """
    if not criterion_text:
        return []

    locations = set()  # Use set to avoid duplicates

    # Pattern 1: File paths with 2+ path segments (to avoid false positives like "file.py")
    # Matches: path/to/file.py, src/module/file.ts, ./folder/file.js
    file_pattern = r'(?:^|[\s`\(])((?:[a-zA-Z0-9_\-]+/)+[a-zA-Z0-9_\-]+\.[a-z]{1,4})(?:[\s`\)\,]|$)'

    files_found = set()
    for match in re.finditer(file_pattern, criterion_text):
        file_path = match.group(1)
        # Normalize path
        file_path = file_path.lower().lstrip('./')
        files_found.add(file_path)

    # Pattern 2: Function/method references with parentheses or explicit keywords
    # Be conservative: only match clear function calls
    function_patterns = [
        r'([A-Z][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)\s*\(',  # Class.method()
        r'([a-zA-Z_][a-zA-Z0-9_]*)\s*\(',                       # function()
        r'(?:function|method|class)\s+([a-zA-Z_][a-zA-Z0-9_]*)', # "function foo" or "method bar"
    ]

    functions_found = set()
    for pattern in function_patterns:
        for match in re.finditer(pattern, criterion_text):
            func = match.group(1).lower()
            # Filter out common noise words that look like functions
            if func not in ('the', 'that', 'this', 'data', 'file', 'path', 'in', 'from'):
                functions_found.add(func)

    # Build location tuples
    # Strategy: If we have both files and functions, pair the FIRST file with FIRST function
    # This is more conservative than pairing all combinations
    if files_found and functions_found:
        # Take first of each (sorted for determinism)
        file_list = sorted(files_found)
        func_list = sorted(functions_found)
        locations.add((file_list[0], func_list[0]))
    elif files_found:
        # File-only matches
        for f in files_found:
            locations.add((f, ""))
    elif functions_found:
        # Function-only matches
        for fn in functions_found:
            locations.add(("", fn))

    return list(locations)


def check_duplicate_criteria(
    current_rubric: List[Dict[str, Any]],
    prior_rubrics: List[Dict[str, Any]]
) -> Tuple[bool, List[str]]:
    """Check if any rubric criteria duplicate previously accepted bugs.

    Args:
        current_rubric: List of criterion dicts to check
        prior_rubrics: List of {problem_id, rubric, score} dicts from accepted instances

    Returns (is_duplicate, list_of_duplicate_details)
    """
    if not prior_rubrics:
        return False, []

    duplicates = []

    # Build index of all prior locations
    prior_locations = {}  # (file, function) -> [(problem_id, criterion_idx)]
    for prior_entry in prior_rubrics:
        prior_problem_id = prior_entry.get("problem_id", "unknown")
        prior_rubric = prior_entry.get("rubric", [])

        for k, prior_criterion in enumerate(prior_rubric):
            prior_text = prior_criterion.get("criterion", "")
            prior_locs = _extract_file_and_function(prior_text)

            for loc in prior_locs:
                if loc not in prior_locations:
                    prior_locations[loc] = []
                prior_locations[loc].append((prior_problem_id, k))

    # Check current rubric against prior locations
    for i, criterion in enumerate(current_rubric):
        criterion_text = criterion.get("criterion", "")
        current_locs = _extract_file_and_function(criterion_text)

        for loc in current_locs:
            if loc in prior_locations:
                for prior_problem_id, prior_idx in prior_locations[loc]:
                    duplicates.append(
                        f"criterion {i} location {loc} matches instance {prior_problem_id} criterion {prior_idx}"
                    )

    return len(duplicates) > 0, duplicates


def check_duplicate_with_quality(
    current_rubric: List[Dict[str, Any]],
    current_score: float,
    current_problem_id: str,
    prior_rubrics: List[Dict[str, Any]]
) -> Tuple[str, List[str], List[str]]:
    """Check for duplicates and determine action based on quality.

    Returns (action, duplicate_details, instances_to_mark_denied)
    where action is one of:
    - "accept": no duplicates found, or current is better than all duplicates
    - "deny": duplicates found and current is not better than existing ones
    - "accept_and_deny_prior": current is better, accept it and mark prior instances as denied

    duplicate_details: list of human-readable duplicate descriptions
    instances_to_mark_denied: list of problem_ids that should be marked as denied
    """
    if not prior_rubrics:
        return "accept", [], []

    is_dup, dup_details = check_duplicate_criteria(current_rubric, prior_rubrics)

    if not is_dup:
        return "accept", [], []

    # Find which prior instances have duplicates
    conflicting_instances = {}  # problem_id -> {score, num_duplicates}
    for detail in dup_details:
        # Parse "criterion X location Y matches instance Z criterion W"
        parts = detail.split(" matches instance ")
        if len(parts) == 2:
            prior_problem_id = parts[1].split(" criterion ")[0]
            if prior_problem_id not in conflicting_instances:
                # Find score for this instance
                prior_score = None
                for prior_entry in prior_rubrics:
                    if prior_entry.get("problem_id") == prior_problem_id:
                        prior_score = prior_entry.get("score")
                        break

                conflicting_instances[prior_problem_id] = {
                    "score": prior_score if prior_score is not None else 0.5,  # Default mid-range
                    "num_duplicates": 0
                }
            conflicting_instances[prior_problem_id]["num_duplicates"] += 1

    # Decision logic:
    # 1. Count how many rubric criteria are duplicated
    # 2. If current score > all conflicting instances' scores, accept current and deny prior
    # 3. Otherwise, deny current

    num_duplicated_criteria = len(set(
        int(d.split("criterion ")[1].split(" ")[0]) for d in dup_details
    ))

    # If only 1-2 criteria overlap, might be acceptable (similar bugs in different files)
    # But for now, let's be strict: any location duplicate is a conflict

    instances_to_deny = []
    should_accept_current = True

    # Compare current score against each conflicting instance
    for prior_id, prior_data in conflicting_instances.items():
        prior_score = prior_data["score"]

        # Current is better if score is higher (we want higher scores)
        # WAIT - check the spec: [0.4, 0.8] means we want scores closer to 0.5-0.6 (ideal range)
        # Actually, looking at scoring-engine, "ideal" is around 0.4-0.6
        # Let's use absolute distance from 0.5 as quality metric

        current_distance = abs(current_score - 0.5)
        prior_distance = abs(prior_score - 0.5)

        if current_distance < prior_distance:
            # Current is better quality (closer to ideal 0.5)
            instances_to_deny.append(prior_id)
        elif current_distance > prior_distance:
            # Prior is better, deny current
            should_accept_current = False
        else:
            # Equal quality - keep first one (deny current)
            should_accept_current = False

    if should_accept_current and instances_to_deny:
        return "accept_and_deny_prior", dup_details, instances_to_deny
    elif should_accept_current:
        return "accept", dup_details, []
    else:
        return "deny", dup_details, []


def remove_rubric(store: Dict[str, Any], repo_id: str, problem_id: str):
    """Remove a rubric from the store (when marking an instance as denied due to better duplicate)."""
    if repo_id not in store:
        return

    processed = store[repo_id].get("processed_rubrics", [])
    store[repo_id]["processed_rubrics"] = [
        r for r in processed if r.get("problem_id") != problem_id
    ]

    save_store(store)


def add_accepted_rubric(
    store: Dict[str, Any],
    repo_id: str,
    problem_id: str,
    rubric: List[Dict[str, Any]],
    score: float = None
):
    """Add an accepted instance's rubric to the store for future duplicate checking."""
    if repo_id not in store:
        store[repo_id] = {
            "repo_id": repo_id,
            "average_score": 0.0,
            "instance_count": 0,
            "processed_rubrics": []
        }

    # Remove any existing entry for this problem_id (in case of re-evaluation)
    store[repo_id]["processed_rubrics"] = [
        r for r in store[repo_id]["processed_rubrics"]
        if r.get("problem_id") != problem_id
    ]

    # Add rubric with metadata
    entry = {
        "problem_id": problem_id,
        "rubric": rubric
    }
    if score is not None:
        entry["score"] = score

    store[repo_id]["processed_rubrics"].append(entry)

    # Save updated store
    save_store(store)
