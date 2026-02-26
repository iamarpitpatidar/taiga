#!/usr/bin/env python3
"""
Rubric Validator - Structural validation + Core File Analysis

Critical: Ensures rubric bugs target the CORE 20-25% of the repository,
not peripheral/edge files. Core files are identified by:
- LOC (lines of code)
- Import frequency
- Directory depth
- Naming patterns (core, main, base, utils, etc.)
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple
from collections import defaultdict


SOURCE_EXTENSIONS = {
    '.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.rs', '.java', '.c', '.cpp', '.h', '.hpp',
    '.rb', '.php', '.swift', '.kt', '.scala', '.sh', '.bash', '.sql', '.r', '.m', '.mm', '.cs'
}

CORE_FILE_INDICATORS = {
    'core', 'main', 'base', 'app', 'index', 'client', 'server',
    'engine', 'manager', 'controller', 'handler', 'service', 'api'
}

PERIPHERAL_INDICATORS = {
    'test', 'spec', 'mock', 'fixture', 'example', 'demo', 'sample',
    'migration', 'script', 'tool', 'util', 'helper', 'config'
}


def count_lines_of_code(file_path: Path) -> int:
    """Count non-empty, non-comment lines in a file."""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = 0
            for line in f:
                stripped = line.strip()
                # Skip empty lines and comment-only lines
                if stripped and not stripped.startswith('#') and not stripped.startswith('//'):
                    lines += 1
            return lines
    except:
        return 0


def analyze_imports(file_path: Path, repo_root: Path) -> Set[str]:
    """Extract imported files from source code."""
    imports = set()

    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

            # Python imports
            for match in re.finditer(r'from\s+([\w.]+)\s+import', content):
                imports.add(match.group(1).replace('.', '/'))

            for match in re.finditer(r'import\s+([\w.]+)', content):
                imports.add(match.group(1).replace('.', '/'))

            # JS/TS imports
            for match in re.finditer(r'from\s+["\']([^"\']+)["\']', content):
                imports.add(match.group(1))

            for match in re.finditer(r'import\s+["\']([^"\']+)["\']', content):
                imports.add(match.group(1))

    except:
        pass

    return imports


def identify_core_files(repo_path: Path) -> Dict:
    """
    Identify core files (top 20-25% by importance).

    Scoring factors:
    - LOC (lines of code) - more lines = more important
    - Import frequency - imported by many files = central
    - Directory depth - shallower = more core
    - Naming patterns - core/main/base indicators
    """

    files_data = {}
    import_graph = defaultdict(set)  # file -> set of files that import it

    # First pass: collect file data
    for root, dirs, files in os.walk(repo_path):
        # Skip test and hidden directories
        dirs[:] = [d for d in dirs if not d.startswith('.') and 'test' not in d.lower()]

        for file in files:
            if not any(file.endswith(ext) for ext in SOURCE_EXTENSIONS):
                continue

            file_path = Path(root) / file
            rel_path = file_path.relative_to(repo_path)

            # Skip test files
            if 'test' in str(rel_path).lower():
                continue

            loc = count_lines_of_code(file_path)
            depth = len(rel_path.parts) - 1
            imports = analyze_imports(file_path, repo_path)

            # Build import graph
            for imported in imports:
                import_graph[imported].add(str(rel_path))

            files_data[str(rel_path)] = {
                "loc": loc,
                "depth": depth,
                "imports": imports,
                "path": rel_path
            }

    # Second pass: calculate scores
    for file_path, data in files_data.items():
        score = 0

        # LOC factor (normalize to 0-100)
        loc = data["loc"]
        if loc > 0:
            score += min(loc / 5, 100)  # Cap at 100

        # Import frequency (how many files import this one)
        imported_by_count = len(import_graph.get(file_path, set()))
        score += imported_by_count * 20

        # Directory depth penalty (shallower = more important)
        depth_penalty = data["depth"] * 10
        score -= depth_penalty

        # Naming pattern bonus
        file_name = Path(file_path).stem.lower()
        if any(indicator in file_name for indicator in CORE_FILE_INDICATORS):
            score += 50

        # Peripheral penalty
        if any(indicator in file_name for indicator in PERIPHERAL_INDICATORS):
            score -= 30

        data["importance_score"] = max(score, 0)

    # Sort by importance
    sorted_files = sorted(files_data.items(), key=lambda x: x[1]["importance_score"], reverse=True)

    # Top 25% are core files
    core_count = max(1, int(len(sorted_files) * 0.25))
    core_files = set(f[0] for f in sorted_files[:core_count])

    return {
        "all_files": files_data,
        "core_files": core_files,
        "core_percentage": 25.0,
        "total_files": len(files_data),
        "core_count": core_count,
        "sorted_by_importance": [
            {"file": f[0], "score": f[1]["importance_score"], "loc": f[1]["loc"]}
            for f in sorted_files[:20]  # Top 20 for reference
        ]
    }


def extract_file_path_from_criterion(criterion_text: str) -> List[str]:
    """Extract file paths from criterion text."""
    # Pattern: path/to/file.ext
    pattern = r'(?:^|[\s`\(])((?:[a-zA-Z0-9_\-]+/)+[a-zA-Z0-9_\-]+\.[a-z]{1,4})(?:[\s`\)\,\.]|$)'
    matches = re.findall(pattern, criterion_text)

    # Normalize paths
    return [m.lower().lstrip('./') for m in matches]


def extract_function_from_criterion(criterion_text: str) -> List[str]:
    """Extract function/method names from criterion text."""
    patterns = [
        r'([A-Z][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)\s*\(',  # Class.method()
        r'([a-zA-Z_][a-zA-Z0-9_]*)\s*\(',  # function()
    ]

    functions = set()
    for pattern in patterns:
        matches = re.findall(pattern, criterion_text)
        functions.update(m.lower() for m in matches)

    return list(functions)


def validate_rubric(rubric_path: str, injected_repo_path: str) -> Dict:
    """
    Comprehensive rubric validation including core file targeting.
    """

    rubric_path = Path(rubric_path)
    repo_path = Path(injected_repo_path)

    issues = []
    warnings = []

    # Load rubric
    try:
        with open(rubric_path, 'r') as f:
            rubric_data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        return {
            "rubric_valid": False,
            "early_termination": True,
            "error": f"Failed to load rubric: {e}",
            "issues": [f"Cannot parse rubric.json: {e}"]
        }

    # Structural checks
    if "rubric" not in rubric_data:
        return {
            "rubric_valid": False,
            "early_termination": True,
            "issues": ["Missing top-level 'rubric' key"]
        }

    rubric = rubric_data["rubric"]

    if not isinstance(rubric, list):
        return {
            "rubric_valid": False,
            "early_termination": True,
            "issues": ["'rubric' is not an array"]
        }

    if len(rubric) < 8:
        return {
            "rubric_valid": False,
            "early_termination": True,
            "issues": [f"Rubric has {len(rubric)} entries, minimum is 8"]
        }

    if len(rubric) > 8:
        warnings.append(f"Rubric has {len(rubric)} entries (more than standard 8)")

    # Identify core files
    core_analysis = identify_core_files(repo_path)
    core_files = core_analysis["core_files"]

    # Track which files are targeted by rubric
    targeted_files = []
    files_in_core = 0

    # Validate each criterion
    for i, criterion in enumerate(rubric):
        criterion_num = i + 1

        # Check structure
        if not isinstance(criterion, dict):
            issues.append(f"Criterion {criterion_num}: not a dict")
            continue

        if "criterion" not in criterion:
            issues.append(f"Criterion {criterion_num}: missing 'criterion' field")
            continue

        if "weight" not in criterion:
            issues.append(f"Criterion {criterion_num}: missing 'weight' field")
            continue

        # Check weight
        if criterion["weight"] != 1:
            issues.append(f"Criterion {criterion_num}: weight={criterion['weight']}, expected 1")

        criterion_text = criterion["criterion"]

        # Pattern-based checks
        if re.search(r'\bline\s+\d+\b', criterion_text, re.I):
            warnings.append(f"Criterion {criterion_num}: mentions line numbers")

        if re.search(r'changed\s+from.*to', criterion_text, re.I):
            warnings.append(f"Criterion {criterion_num}: diff-specific language")

        # Extract file paths
        file_paths = extract_file_path_from_criterion(criterion_text)

        if not file_paths:
            warnings.append(f"Criterion {criterion_num}: no clear file path found")
        else:
            # Check if files exist
            for file_path in file_paths:
                full_path = repo_path / file_path
                if not full_path.exists():
                    issues.append(f"Criterion {criterion_num}: file not found: {file_path}")
                else:
                    targeted_files.append(file_path)

                    # Check if file is in core
                    if file_path in core_files:
                        files_in_core += 1

    # CRITICAL CHECK: Are rubric bugs targeting core files?
    core_targeting_percentage = (files_in_core / len(rubric)) * 100 if len(rubric) > 0 else 0

    if core_targeting_percentage < 50:
        warnings.append(
            f"Only {files_in_core}/8 rubric bugs target core files ({core_targeting_percentage:.1f}%). "
            f"Bugs should target the most important 20-25% of the codebase."
        )

    # Check for duplicate file targeting
    file_counts = {}
    for file in targeted_files:
        file_counts[file] = file_counts.get(file, 0) + 1

    multi_bug_files = {f: c for f, c in file_counts.items() if c > 1}
    if multi_bug_files and core_analysis["total_files"] > 5:
        for file, count in multi_bug_files.items():
            warnings.append(f"Multiple bugs ({count}) target same file: {file}")

    # Determine validity
    rubric_valid = len(issues) == 0
    needs_review = len(warnings) > 0 or core_targeting_percentage < 50

    return {
        "rubric_valid": rubric_valid,
        "early_termination": not rubric_valid,
        "issues": issues,
        "warnings": warnings,
        "rubric_entry_count": len(rubric),
        "core_analysis": {
            "total_files": core_analysis["total_files"],
            "core_files_count": core_analysis["core_count"],
            "rubric_targets_core": files_in_core,
            "core_targeting_percentage": round(core_targeting_percentage, 1),
            "top_importance_files": core_analysis["sorted_by_importance"][:10]
        },
        "targeted_files": list(set(targeted_files)),
        "needs_manual_review": needs_review
    }


def main():
    parser = argparse.ArgumentParser(description="Validate rubric structure and core file targeting")
    parser.add_argument("rubric_path", help="Path to rubric.json")
    parser.add_argument("injected_repo", help="Path to injected_repo directory")
    parser.add_argument("--output", choices=["json", "summary"], default="summary")

    args = parser.parse_args()

    result = validate_rubric(args.rubric_path, args.injected_repo)

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        # Human-readable summary
        print(f"Rubric Entries: {result['rubric_entry_count']}/8")
        print(f"Structurally Valid: {result['rubric_valid']}")

        if result.get("issues"):
            print(f"\n❌ Issues ({len(result['issues'])}):")
            for issue in result['issues']:
                print(f"  - {issue}")

        if result.get("warnings"):
            print(f"\n⚠️  Warnings ({len(result['warnings'])}):")
            for warning in result['warnings']:
                print(f"  - {warning}")

        # Core file analysis
        core = result["core_analysis"]
        print(f"\n📊 Core File Analysis:")
        print(f"  Total Source Files: {core['total_files']}")
        print(f"  Core Files (top 25%): {core['core_files_count']}")
        print(f"  Rubric Targets Core: {core['rubric_targets_core']}/8 ({core['core_targeting_percentage']}%)")

        if core['core_targeting_percentage'] < 50:
            print(f"\n  ⚠️  WARNING: Less than 50% of bugs target core files!")
            print(f"      Bugs should focus on the most important parts of the codebase.")

        print(f"\nTop 10 Most Important Files:")
        for item in core['top_importance_files']:
            print(f"  - {item['file']} (score: {item['score']:.0f}, LOC: {item['loc']})")

        if result.get("targeted_files"):
            print(f"\nFiles Targeted by Rubric:")
            for file in result['targeted_files']:
                in_core = " [CORE]" if file in result.get("core_files", set()) else " [peripheral]"
                print(f"  - {file}{in_core}")

        if result["early_termination"]:
            print(f"\n❌ REJECT: Hard structural failure")
            sys.exit(1)
        elif not result["rubric_valid"]:
            print(f"\n❌ REJECT: Validation failed")
            sys.exit(1)
        elif result.get("needs_manual_review"):
            print(f"\n⚠️  NEEDS REVIEW: Passed structure but has warnings")
            sys.exit(2)
        else:
            print(f"\n✅ PASS: All checks passed")
            sys.exit(0)


if __name__ == "__main__":
    main()
