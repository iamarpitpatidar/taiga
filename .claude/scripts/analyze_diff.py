#!/usr/bin/env python3
"""
Diff Analyzer - Deterministic file-level diff analysis
Compares injected vs original repo, classifies changes as localized/moderate/suspicious
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple


# Source file extensions
SOURCE_EXTENSIONS = {
    '.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.rs', '.java', '.c', '.cpp', '.h', '.hpp',
    '.cs', '.rb', '.php', '.swift', '.kt', '.scala', '.sh', '.bash', '.sql', '.r', '.m', '.mm'
}

# Packaging artifacts to ignore
PACKAGING_ARTIFACTS = {
    '.git', '.github', '.gitignore', '.gitattributes',
    'README.md', 'CONTRIBUTING.md', 'LICENSE', 'CHANGELOG.md', 'SECURITY.md',
    'Dockerfile', '.dockerignore', 'docker-compose.yml',
    '.circleci', '.travis.yml', '.gitlab-ci.yml',
    'changes', 'changelog', '.vscode', '.idea',
    '__pycache__', '.pytest_cache', 'node_modules', '.tox', 'venv', '.venv'
}


def is_source_file(path: str) -> bool:
    """Check if file is a source code file."""
    return Path(path).suffix.lower() in SOURCE_EXTENSIONS


def is_packaging_artifact(path: str) -> bool:
    """Check if path is a packaging artifact."""
    path_parts = Path(path).parts
    for part in path_parts:
        if part in PACKAGING_ARTIFACTS or part.startswith('.'):
            return True
    return False


def count_files_recursive(directory: Path, include_tests=False) -> Dict:
    """Count files in directory, separating source from other files."""
    source_files = []
    test_files = []
    other_files = []

    for root, dirs, files in os.walk(directory):
        # Skip hidden and package directories
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in {'__pycache__', 'node_modules', '.tox', 'venv'}]

        rel_root = Path(root).relative_to(directory)

        for file in files:
            rel_path = str(rel_root / file)

            if is_packaging_artifact(rel_path):
                continue

            # Check if test file
            is_test = 'test' in rel_path.lower() or '/tests/' in rel_path or '/test/' in rel_path

            if is_source_file(file):
                if is_test:
                    test_files.append(rel_path)
                else:
                    source_files.append(rel_path)
            else:
                other_files.append(rel_path)

    return {
        "source_files": source_files,
        "test_files": test_files,
        "other_files": other_files,
        "total_source": len(source_files),
        "total_test": len(test_files)
    }


def compare_files(injected_dir: Path, original_dir: Path = None) -> Dict:
    """Compare injected repo against original (if available)."""

    injected_files = count_files_recursive(injected_dir)

    if not original_dir or not original_dir.exists():
        # No original available - can only count files
        return {
            "has_original": False,
            "injected_source_count": injected_files["total_source"],
            "injected_files": injected_files["source_files"],
            "changed_files": [],
            "added_files": [],
            "deleted_files": [],
            "packaging_artifacts": []
        }

    original_files = count_files_recursive(original_dir)

    # Convert to sets for comparison
    injected_set = set(injected_files["source_files"])
    original_set = set(original_files["source_files"])

    added = injected_set - original_set
    deleted = original_set - injected_set
    common = injected_set & original_set

    # Check which common files actually changed
    changed = []
    for file in common:
        injected_path = injected_dir / file
        original_path = original_dir / file

        try:
            # Quick size check first
            if injected_path.stat().st_size != original_path.stat().st_size:
                changed.append(file)
            else:
                # Same size, check content
                with open(injected_path, 'rb') as f1, open(original_path, 'rb') as f2:
                    if f1.read() != f2.read():
                        changed.append(file)
        except (OSError, IOError):
            # Assume changed if can't read
            changed.append(file)

    # Check for packaging artifacts
    artifacts = []
    if '_init_.py' in str(injected_dir):
        artifacts.append("__init__.py renamed to _init_.py")

    if len(original_files["test_files"]) > 0 and len(injected_files["test_files"]) == 0:
        artifacts.append("entire tests/ directory removed")

    return {
        "has_original": True,
        "injected_source_count": injected_files["total_source"],
        "original_source_count": original_files["total_source"],
        "changed_files": sorted(changed),
        "added_files": sorted(list(added)),
        "deleted_files": sorted(list(deleted)),
        "packaging_artifacts": artifacts
    }


def classify_diff(changed_count: int, added_count: int, deleted_count: int,
                  total_source: int, flags: List[str]) -> str:
    """Classify diff as localized, moderate, or suspicious."""

    total_changes = changed_count + added_count + deleted_count

    # Check for suspicious flags
    if flags:
        return "suspicious"

    # Check percentage of files changed
    if total_source > 0:
        pct_changed = (total_changes / total_source) * 100
        if pct_changed > 25:
            return "suspicious"

    # Apply thresholds
    if total_changes <= 10:
        return "localized"
    elif total_changes <= 20:
        return "moderate"
    else:
        return "suspicious"


def detect_structural_flags(diff_data: Dict) -> List[str]:
    """Detect structural issues that indicate suspicious changes."""
    flags = []

    changed = len(diff_data["changed_files"])
    added = len(diff_data["added_files"])
    deleted = len(diff_data["deleted_files"])
    total_changes = changed + added + deleted

    # Too many files changed
    if total_changes > 20:
        flags.append(f"many_files: {total_changes} files changed")

    if total_changes > 50:
        flags.append("massive_changes: >50 files modified")

    # High percentage of files changed
    if diff_data.get("has_original") and diff_data.get("original_source_count"):
        original_count = diff_data["original_source_count"]
        if original_count > 0:
            pct = (total_changes / original_count) * 100
            if pct > 25:
                flags.append(f"high_percentage: {pct:.1f}% of files changed")

    # Many deletions
    if deleted > 10:
        flags.append(f"many_deletions: {deleted} files deleted")

    # Many additions
    if added > 15:
        flags.append(f"many_additions: {added} files added")

    return flags


def analyze_diff(injected_dir: str, original_dir: str = None) -> Dict:
    """Main diff analysis function."""

    injected_path = Path(injected_dir)
    original_path = Path(original_dir) if original_dir else None

    if not injected_path.exists():
        return {
            "error": f"Injected repo not found: {injected_dir}",
            "files_changed_count": 0,
            "classification": "error"
        }

    # Compare files
    diff_data = compare_files(injected_path, original_path)

    changed_count = len(diff_data["changed_files"])
    added_count = len(diff_data["added_files"])
    deleted_count = len(diff_data["deleted_files"])

    # Detect structural flags
    flags = detect_structural_flags(diff_data)

    # Classify
    classification = classify_diff(
        changed_count, added_count, deleted_count,
        diff_data["injected_source_count"], flags
    )

    return {
        "has_original": diff_data["has_original"],
        "files_changed_count": changed_count,
        "files_added_count": added_count,
        "files_deleted_count": deleted_count,
        "total_changes": changed_count + added_count + deleted_count,
        "injected_source_count": diff_data["injected_source_count"],
        "classification": classification,
        "flags": flags,
        "packaging_artifacts": diff_data["packaging_artifacts"],
        "changed_files": diff_data["changed_files"][:20],  # Limit output
        "evasion_risk": []  # Could add pattern detection here
    }


def main():
    parser = argparse.ArgumentParser(description="Analyze diff between injected and original repos")
    parser.add_argument("injected_repo", help="Path to injected_repo directory")
    parser.add_argument("--original", help="Path to original repo (optional)")
    parser.add_argument("--output", choices=["json", "summary"], default="summary")

    args = parser.parse_args()

    result = analyze_diff(args.injected_repo, args.original)

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        # Human-readable summary
        if "error" in result:
            print(f"❌ Error: {result['error']}")
            sys.exit(1)

        print(f"Classification: {result['classification'].upper()}")
        print(f"Total Changes: {result['total_changes']} files")
        print(f"  Changed: {result['files_changed_count']}")
        print(f"  Added:   {result['files_added_count']}")
        print(f"  Deleted: {result['files_deleted_count']}")
        print(f"Total Source Files: {result['injected_source_count']}")

        if result.get("packaging_artifacts"):
            print(f"\n⚠️  Packaging Artifacts:")
            for artifact in result["packaging_artifacts"]:
                print(f"  - {artifact}")

        if result.get("flags"):
            print(f"\n🚩 Structural Flags:")
            for flag in result["flags"]:
                print(f"  - {flag}")

        if result["classification"] == "suspicious":
            print(f"\n❌ REJECT: Diff classified as suspicious")
            sys.exit(1)
        else:
            print(f"\n✓ Diff classified as acceptable ({result['classification']})")
            sys.exit(0)


if __name__ == "__main__":
    main()
