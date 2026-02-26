#!/usr/bin/env python3
"""
Dataset Structure Validator (replaces dataset-loader agent)

Fast deterministic validation of instance directory structure.
Checks for required files, validates rubric format, and extracts metadata.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Any


def extract_file_paths_and_functions(criterion_text: str) -> tuple[List[str], List[str]]:
    """Extract file paths and function names from criterion text using regex."""
    # Match file paths (e.g., src/module/file.py, path/to/file.h)
    file_pattern = r'(?:^|\s)([\w\-./]+\.\w+)'
    files = re.findall(file_pattern, criterion_text)

    # Match function/method names (e.g., ClassName.method(), function_name())
    func_pattern = r'\b(\w+(?:\.\w+)?)\s*\('
    functions = re.findall(func_pattern, criterion_text)

    return files, functions


def abbreviate_criterion(criterion: str, max_length: int = 80) -> str:
    """Abbreviate a criterion for display purposes."""
    if len(criterion) <= max_length:
        return criterion
    return criterion[:max_length - 3] + "..."


def validate_instance_structure(instance_dir: str, problem_id: str = None) -> Dict[str, Any]:
    """
    Validate instance directory structure and extract metadata.

    Args:
        instance_dir: Path to instance directory (e.g., instances/repo-01/)
        problem_id: Optional problem_id override

    Returns:
        Structured validation result matching dataset-loader agent output
    """
    instance_path = Path(instance_dir)
    parse_errors = []

    # Initialize result
    result = {
        "problem_id": problem_id or instance_path.name,
        "average_score": None,
        "rubric_entry_count": 0,
        "rubric_criteria": [],
        "files_in_injected_repo": 0,
        "directory_size_ratio": 0.0,
        "rubric_valid": False,
        "rubric_message": "",
        "parse_errors": []
    }

    # Check instance directory exists
    if not instance_path.exists():
        parse_errors.append(f"Instance directory does not exist: {instance_dir}")
        result["parse_errors"] = parse_errors
        result["rubric_message"] = "Instance directory not found"
        return result

    # Check for metadata.json
    metadata_path = instance_path / "metadata.json"
    if metadata_path.exists():
        try:
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)
            result["problem_id"] = metadata.get("problem_id", result["problem_id"])
            result["average_score"] = metadata.get("average_score") or metadata.get("score_mean")

            # Check for extraction_issues
            if "extraction_issues" in metadata and metadata["extraction_issues"]:
                parse_errors.extend(metadata["extraction_issues"])
        except (json.JSONDecodeError, OSError) as e:
            parse_errors.append(f"Failed to parse metadata.json: {e}")
    else:
        parse_errors.append("metadata.json not found")

    # Check for injected_repo/
    injected_repo_path = instance_path / "injected_repo"
    if not injected_repo_path.exists():
        parse_errors.append("injected_repo/ directory not found")
    else:
        # Count files in injected_repo
        try:
            file_count = sum(1 for _ in injected_repo_path.rglob('*') if _.is_file())
            result["files_in_injected_repo"] = file_count
        except OSError as e:
            parse_errors.append(f"Failed to count files in injected_repo: {e}")

    # Check for rubric.json (REQUIRED)
    rubric_path = instance_path / "rubric.json"
    if not rubric_path.exists():
        parse_errors.append("rubric.json not found")
        result["rubric_message"] = "Rubric file missing"
        result["parse_errors"] = parse_errors
        return result

    # Validate rubric structure
    try:
        with open(rubric_path, 'r') as f:
            rubric_data = json.load(f)

        # Check for top-level "rubric" key
        if "rubric" not in rubric_data:
            parse_errors.append("rubric.json missing top-level 'rubric' key")
            result["rubric_message"] = "Malformed rubric: missing 'rubric' key"
        elif not isinstance(rubric_data["rubric"], list):
            parse_errors.append("'rubric' key is not an array")
            result["rubric_message"] = "Malformed rubric: 'rubric' is not an array"
        else:
            rubric = rubric_data["rubric"]
            result["rubric_entry_count"] = len(rubric)

            # Check 8 criteria
            if len(rubric) < 8:
                parse_errors.append(f"Rubric has {len(rubric)} entries; expected 8")
                result["rubric_message"] = f"Invalid count: {len(rubric)} entries (need 8)"
            else:
                # Validate each entry
                all_valid = True
                for i, entry in enumerate(rubric):
                    # Check for required fields
                    if "criterion" not in entry:
                        parse_errors.append(f"Criterion {i+1} missing 'criterion' field")
                        all_valid = False
                    elif not entry["criterion"] or not entry["criterion"].strip():
                        parse_errors.append(f"Criterion {i+1} has empty 'criterion' text")
                        all_valid = False

                    if "weight" not in entry:
                        parse_errors.append(f"Criterion {i+1} missing 'weight' field")
                        all_valid = False
                    elif entry["weight"] != 1:
                        parse_errors.append(f"Criterion {i+1} has weight={entry['weight']}; expected 1")
                        all_valid = False

                    # Extract abbreviated criterion for output
                    if "criterion" in entry:
                        criterion_text = entry["criterion"]
                        abbreviated = abbreviate_criterion(criterion_text)
                        result["rubric_criteria"].append(abbreviated)

                        # Extract file paths and functions (for downstream use)
                        files, functions = extract_file_paths_and_functions(criterion_text)
                        # Store in entry for potential future use
                        entry["_extracted_files"] = files
                        entry["_extracted_functions"] = functions

                if all_valid:
                    result["rubric_valid"] = True
                    result["rubric_message"] = "Valid: 8 criteria, all with weight=1"
                else:
                    result["rubric_message"] = "Structural validation failed"

    except (json.JSONDecodeError, OSError) as e:
        parse_errors.append(f"Failed to parse rubric.json: {e}")
        result["rubric_message"] = f"Parse error: {e}"

    # Check for summary.json (optional)
    summary_path = instance_path / "summary.json"
    if summary_path.exists():
        try:
            with open(summary_path, 'r') as f:
                summary = json.load(f)
            # Use summary data if metadata missing
            if result["average_score"] is None:
                result["average_score"] = summary.get("average_score")
        except (json.JSONDecodeError, OSError):
            # Summary is optional, so don't fail on error
            pass

    result["parse_errors"] = parse_errors
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Validate TAIGA instance directory structure"
    )
    parser.add_argument(
        "instance_dir",
        help="Path to instance directory (e.g., instances/simdutf__simdutf-07/)"
    )
    parser.add_argument(
        "--problem-id",
        help="Override problem_id (default: inferred from directory name)"
    )
    parser.add_argument(
        "--output",
        choices=["json", "summary"],
        default="json",
        help="Output format: 'json' (full structured output) or 'summary' (human-readable)"
    )

    args = parser.parse_args()

    result = validate_instance_structure(args.instance_dir, args.problem_id)

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        # Human-readable summary
        print(f"Problem ID: {result['problem_id']}")
        print(f"Average Score: {result['average_score']}")
        print(f"Rubric Entries: {result['rubric_entry_count']}/8")
        print(f"Rubric Valid: {result['rubric_valid']}")
        print(f"Files in Repo: {result['files_in_injected_repo']}")

        if result['parse_errors']:
            print(f"\n⚠️  Errors ({len(result['parse_errors'])}):")
            for error in result['parse_errors']:
                print(f"  - {error}")
        else:
            print("\n✓ All structural checks passed")

    # Exit with error code if validation failed
    sys.exit(0 if not result['parse_errors'] else 1)


if __name__ == "__main__":
    main()
