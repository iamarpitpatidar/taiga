#!/usr/bin/env python3
"""
Rubric Duplicate Checker - Database-backed cross-instance duplicate detection

Handles:
- Checking for duplicate rubric locations across instances of the same repo
- Storing accepted rubrics for future duplicate checking
- Quality-based duplicate resolution (keep better scoring instance)

Uses database (`rubrics` table) for persistent storage.
"""

import re
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# Add src to path for db_helper import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from db_helper import (
    get_prior_rubrics,
    add_accepted_rubric,
    get_repo_id,
    get_instance_by_problem_id,
    get_repo_average,
    repo_average_ok
)


def extract_location_from_criterion(criterion_text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract (file_path, function_name) from criterion text.

    Args:
        criterion_text: Full criterion description

    Returns:
        Tuple of (file_path, function_name). Either can be None if not found.
    """
    # Extract file path (e.g., src/module/file.py, path/to/file.h)
    file_pattern = r'(?:^|\s)([\w\-./]+\.\w+)'
    file_match = re.search(file_pattern, criterion_text)
    file_path = file_match.group(1) if file_match else None

    # Normalize file path
    if file_path:
        file_path = file_path.lower().lstrip('./')

    # Extract function/method name (e.g., ClassName.method(), function_name())
    func_pattern = r'\b(\w+(?:\.\w+)?)\s*\('
    func_match = re.search(func_pattern, criterion_text)
    function_name = func_match.group(1) if func_match else None

    # Normalize function name
    if function_name:
        function_name = function_name.lower()

    return file_path, function_name


def check_duplicate_locations(
    repo_id: str,
    current_rubric: List[Dict],
    current_problem_id: str,
    current_score: float
) -> Tuple[bool, List[str]]:
    """
    Check if current rubric has duplicate bug locations with prior accepted instances.

    Args:
        repo_id: Repository identifier (e.g., "owner__repo")
        current_rubric: List of criterion dicts with 'criterion' and 'weight' keys
        current_problem_id: Current instance problem_id
        current_score: Current instance score_mean

    Returns:
        Tuple of (has_duplicates: bool, duplicate_details: List[str])
        duplicate_details contains strings describing which prior instances have conflicts
    """
    # Get all prior accepted rubrics for this repo (excluding current instance)
    prior_rubrics = get_prior_rubrics(repo_id, exclude_problem_id=current_problem_id)

    if not prior_rubrics:
        return False, []

    # Extract locations from current rubric
    current_locations = []
    for i, entry in enumerate(current_rubric):
        criterion_text = entry.get("criterion", "")
        file_path, function_name = extract_location_from_criterion(criterion_text)
        if file_path or function_name:
            current_locations.append({
                "index": i,
                "file": file_path,
                "function": function_name,
                "text": criterion_text[:80]  # abbreviated for display
            })

    # Check against each prior rubric
    duplicate_details = []
    for prior_problem_id, prior_rubric in prior_rubrics.items():
        # Extract locations from prior rubric
        prior_locations = []
        for entry in prior_rubric:
            criterion_text = entry.get("criterion", "")
            file_path, function_name = extract_location_from_criterion(criterion_text)
            if file_path or function_name:
                prior_locations.append({
                    "file": file_path,
                    "function": function_name
                })

        # Check for matches
        for curr_loc in current_locations:
            for prior_loc in prior_locations:
                # Match on (file, function)
                if curr_loc["file"] and prior_loc["file"] and curr_loc["file"] == prior_loc["file"]:
                    if curr_loc["function"] and prior_loc["function"] and curr_loc["function"] == prior_loc["function"]:
                        duplicate_details.append(
                            f"Criterion {curr_loc['index']+1} matches {prior_problem_id}: "
                            f"{curr_loc['file']}::{curr_loc['function']}"
                        )
                    elif not curr_loc["function"] and not prior_loc["function"]:
                        # Both have file but no function
                        duplicate_details.append(
                            f"Criterion {curr_loc['index']+1} matches {prior_problem_id}: "
                            f"{curr_loc['file']} (no function specified)"
                        )
                # Match on function only (if no file match)
                elif curr_loc["function"] and prior_loc["function"] and curr_loc["function"] == prior_loc["function"]:
                    if not (curr_loc["file"] and prior_loc["file"]):
                        duplicate_details.append(
                            f"Criterion {curr_loc['index']+1} matches {prior_problem_id}: "
                            f"function {curr_loc['function']} (no file path to compare)"
                        )

    has_duplicates = len(duplicate_details) > 0
    return has_duplicates, duplicate_details


def compare_quality(score1: float, score2: float) -> int:
    """
    Compare quality of two instances based on distance from ideal score (0.5).

    Args:
        score1: First instance score
        score2: Second instance score

    Returns:
        -1 if score1 is better (closer to 0.5)
         0 if equal quality
         1 if score2 is better
    """
    ideal = 0.5
    dist1 = abs(score1 - ideal)
    dist2 = abs(score2 - ideal)

    if abs(dist1 - dist2) < 0.01:  # Within 0.01 considered equal
        return 0
    return -1 if dist1 < dist2 else 1


def store_accepted_rubric(problem_id: str, rubric: List[Dict]) -> None:
    """
    Store an accepted rubric in the database for future duplicate checking.

    Args:
        problem_id: Instance problem_id (e.g., "owner__repo-07")
        rubric: List of criterion dicts
    """
    repo_id = get_repo_id(problem_id)
    add_accepted_rubric(repo_id, problem_id, rubric)


def check_instance_for_duplicates(problem_id: str, rubric_path: str) -> Dict:
    """
    Check if an instance has duplicate rubric locations.

    Args:
        problem_id: Instance problem_id
        rubric_path: Path to rubric.json file

    Returns:
        Dict with keys:
        - has_duplicates: bool
        - duplicate_details: List[str]
        - repo_id: str
        - repo_average: float or None
        - repo_average_ok: bool
    """
    repo_id = get_repo_id(problem_id)

    # Get instance score
    instance = get_instance_by_problem_id(problem_id)
    if not instance:
        return {
            "error": f"Instance {problem_id} not found in database",
            "has_duplicates": False,
            "duplicate_details": [],
            "repo_id": repo_id,
            "repo_average": None,
            "repo_average_ok": False
        }

    current_score = instance.get("score_mean")
    if current_score is None:
        return {
            "error": f"Instance {problem_id} has no score_mean",
            "has_duplicates": False,
            "duplicate_details": [],
            "repo_id": repo_id,
            "repo_average": None,
            "repo_average_ok": False
        }

    # Load rubric
    try:
        with open(rubric_path, 'r') as f:
            rubric_data = json.load(f)
        rubric = rubric_data.get("rubric", [])
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return {
            "error": f"Failed to load rubric: {e}",
            "has_duplicates": False,
            "duplicate_details": [],
            "repo_id": repo_id,
            "repo_average": None,
            "repo_average_ok": False
        }

    # Check for duplicates
    has_duplicates, duplicate_details = check_duplicate_locations(
        repo_id, rubric, problem_id, current_score
    )

    # Get repo average
    repo_avg = get_repo_average(repo_id)
    avg_ok, _ = repo_average_ok(repo_id)

    return {
        "has_duplicates": has_duplicates,
        "duplicate_details": duplicate_details,
        "repo_id": repo_id,
        "repo_average": repo_avg,
        "repo_average_ok": avg_ok,
        "instance_score": current_score
    }


def main():
    """CLI for checking duplicate rubrics."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Check for duplicate rubric locations across instances"
    )
    parser.add_argument("problem_id", help="Instance problem_id (e.g., simdutf__simdutf-07)")
    parser.add_argument("rubric_path", help="Path to rubric.json file")
    parser.add_argument("--output", choices=["json", "summary"], default="summary")

    args = parser.parse_args()

    result = check_instance_for_duplicates(args.problem_id, args.rubric_path)

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        # Human-readable summary
        print(f"Problem ID: {args.problem_id}")
        print(f"Repo ID: {result['repo_id']}")
        print(f"Repo Average: {result['repo_average']}")
        print(f"Repo Average OK: {result['repo_average_ok']}")
        print(f"Instance Score: {result.get('instance_score')}")

        if "error" in result:
            print(f"\n❌ Error: {result['error']}")
            sys.exit(1)

        print(f"\nDuplicate Check: {'❌ DUPLICATES FOUND' if result['has_duplicates'] else '✅ No duplicates'}")

        if result['duplicate_details']:
            print(f"\nDuplicate locations ({len(result['duplicate_details'])}):")
            for detail in result['duplicate_details']:
                print(f"  - {detail}")
            sys.exit(1)
        else:
            print("\n✅ All rubric locations are unique for this repo")
            sys.exit(0)


if __name__ == "__main__":
    main()
