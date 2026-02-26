"""
Test script for duplicate checker logic.
Run with: python test_duplicate_checker.py
"""

import sys
from repo_store import (
    _extract_file_and_function,
    check_duplicate_criteria,
    check_duplicate_with_quality,
)


def test_location_extraction():
    """Test that file/function extraction works correctly."""
    print("=" * 60)
    print("TEST: Location Extraction")
    print("=" * 60)

    test_cases = [
        (
            "Identifies that Foo.bar() in path/to/file.py reads field X",
            "path/to/file.py",  # Must extract this file
            True,  # Should extract some function
        ),
        (
            "The function process_data() in src/utils/helper.py returns None",
            "src/utils/helper.py",
            True,
        ),
        (
            "Bug in api/routes.py without specific function",
            "api/routes.py",
            False,  # No function expected
        ),
    ]

    passed = 0
    failed = 0

    for criterion_text, expected_file, should_have_function in test_cases:
        result = _extract_file_and_function(criterion_text)

        # Check if expected file is found in any tuple
        files_found = [loc[0] for loc in result if loc[0]]
        functions_found = [loc[1] for loc in result if loc[1]]

        has_file = expected_file in files_found
        has_function = len(functions_found) > 0

        if has_file and (has_function == should_have_function):
            print(f"✓ PASS: {criterion_text[:50]}...")
            print(f"  Found: {result}")
            passed += 1
        else:
            print(f"✗ FAIL: {criterion_text[:50]}...")
            print(f"  Expected file: {expected_file}, Found: {has_file}")
            print(f"  Expected function: {should_have_function}, Found: {has_function}")
            print(f"  Result: {result}")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed\n")
    return failed == 0


def test_first_instance():
    """Test that first instance (no prior rubrics) passes."""
    print("=" * 60)
    print("TEST: First Instance (No Prior Rubrics)")
    print("=" * 60)

    current_rubric = [
        {"criterion": "Bug in file.py function foo()", "weight": 1}
    ]

    prior_rubrics = []  # No prior instances

    is_dup, details = check_duplicate_criteria(current_rubric, prior_rubrics)

    if not is_dup:
        print("✓ PASS: First instance passes (no duplicates)")
        return True
    else:
        print(f"✗ FAIL: First instance marked as duplicate: {details}")
        return False


def test_duplicate_detection():
    """Test that duplicates are correctly detected."""
    print("=" * 60)
    print("TEST: Duplicate Detection")
    print("=" * 60)

    current_rubric = [
        {"criterion": "Bug in Foo.bar() in path/to/file.py", "weight": 1}
    ]

    prior_rubrics = [
        {
            "problem_id": "repo-01",
            "rubric": [
                {"criterion": "Different wording for Foo.bar() in path/to/file.py", "weight": 1}
            ],
            "score": 0.6
        }
    ]

    is_dup, details = check_duplicate_criteria(current_rubric, prior_rubrics)

    if is_dup and len(details) > 0:
        print(f"✓ PASS: Duplicate detected: {details[0]}")
        return True
    else:
        print("✗ FAIL: Duplicate not detected")
        return False


def test_quality_comparison():
    """Test quality-based duplicate resolution."""
    print("=" * 60)
    print("TEST: Quality-Based Duplicate Resolution")
    print("=" * 60)

    current_rubric = [
        {"criterion": "Bug in Foo.bar() in path/to/file.py", "weight": 1}
    ]

    prior_rubrics = [
        {
            "problem_id": "repo-01",
            "rubric": [
                {"criterion": "Different wording for Foo.bar() in path/to/file.py", "weight": 1}
            ],
            "score": 0.6  # Distance from 0.5 = 0.1
        }
    ]

    # Test 1: Current is better (closer to 0.5)
    current_score = 0.48  # Distance from 0.5 = 0.02 (better)
    action, details, to_deny = check_duplicate_with_quality(
        current_rubric, current_score, "repo-02", prior_rubrics
    )

    if action == "accept_and_deny_prior" and "repo-01" in to_deny:
        print("✓ PASS: Current (0.48) beats prior (0.6) - current accepted, prior denied")
    else:
        print(f"✗ FAIL: Expected accept_and_deny_prior, got {action}")
        return False

    # Test 2: Prior is better
    current_score = 0.7  # Distance from 0.5 = 0.2 (worse than 0.1)
    action, details, to_deny = check_duplicate_with_quality(
        current_rubric, current_score, "repo-02", prior_rubrics
    )

    if action == "deny":
        print("✓ PASS: Prior (0.6) beats current (0.7) - current denied")
    else:
        print(f"✗ FAIL: Expected deny, got {action}")
        return False

    # Test 3: No duplicates
    current_rubric_unique = [
        {"criterion": "Bug in Bar.baz() in other/file.py", "weight": 1}
    ]
    action, details, to_deny = check_duplicate_with_quality(
        current_rubric_unique, 0.5, "repo-03", prior_rubrics
    )

    if action == "accept":
        print("✓ PASS: No duplicates - current accepted")
        return True
    else:
        print(f"✗ FAIL: Expected accept, got {action}")
        return False


def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("DUPLICATE CHECKER TEST SUITE")
    print("=" * 60 + "\n")

    all_passed = True

    all_passed &= test_location_extraction()
    all_passed &= test_first_instance()
    all_passed &= test_duplicate_detection()
    all_passed &= test_quality_comparison()

    print("\n" + "=" * 60)
    if all_passed:
        print("ALL TESTS PASSED ✓")
    else:
        print("SOME TESTS FAILED ✗")
    print("=" * 60 + "\n")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
