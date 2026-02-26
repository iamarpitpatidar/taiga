#!/usr/bin/env python3
"""
Score Classifier (replaces scoring-engine agent)

Fast deterministic classification of TAIGA detection scores.
Determines if score is within acceptable range [0.4, 0.8] for Meta 800.
"""

import argparse
import json
import sys
from typing import Dict, Any, Optional


def classify_score(score: float) -> Dict[str, Any]:
    """
    Classify TAIGA detection score for Meta 800 inclusion.

    Args:
        score: TAIGA average detection score (score_mean)

    Returns:
        Structured classification result matching scoring-engine agent output
    """
    if score is None:
        return {
            "average_score": None,
            "score_classification": "outside_range",
            "score_band": "unknown",
            "remark": "Score data unavailable — cannot evaluate",
            "notes": "Missing score_mean value"
        }

    # Determine classification
    if score < 0.4:
        classification = "outside_range"
        band = "too_easy"
        remark = "Rejected — detection score too low, suggesting bugs are too hard to find or scoring anomaly."
        notes = f"Score {score:.3f} is below minimum threshold of 0.4"

    elif score > 0.8:
        classification = "outside_range"
        band = "too_hard"
        remark = "Rejected — detection score too high, suggesting bugs are trivially obvious or evasion patterns present."
        notes = f"Score {score:.3f} exceeds maximum threshold of 0.8"

    elif 0.4 <= score <= 0.5:
        classification = "within_range"
        band = "ideal"
        remark = "Strong candidate — bugs are well-calibrated: detectable with effort but not trivially obvious."
        notes = f"Score {score:.3f} is in ideal range [0.4, 0.5]"

    elif 0.5 < score <= 0.7:
        classification = "within_range"
        band = "acceptable"
        remark = "Solid instance — moderate detection difficulty, suitable for Meta 800 inclusion."
        notes = f"Score {score:.3f} is in acceptable range (0.5, 0.7]"

    else:  # 0.7 < score <= 0.8
        classification = "within_range"
        band = "borderline"
        remark = "Borderline — bugs may be too easy to detect. Requires strong realism/subtlety from other checks to compensate."
        notes = f"Score {score:.3f} is in borderline range (0.7, 0.8] — needs careful review"

    return {
        "average_score": score,
        "score_classification": classification,
        "score_band": band,
        "remark": remark,
        "notes": notes
    }


def load_score_from_metadata(metadata_path: str) -> Optional[float]:
    """Load score from metadata.json file."""
    try:
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
        return metadata.get("average_score") or metadata.get("score_mean")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Classify TAIGA detection score for Meta 800 inclusion"
    )

    # Accept score as argument or read from metadata
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--score",
        type=float,
        help="Score value to classify (e.g., 0.428)"
    )
    input_group.add_argument(
        "--metadata",
        help="Path to metadata.json file containing score"
    )

    parser.add_argument(
        "--output",
        choices=["json", "summary"],
        default="json",
        help="Output format: 'json' (full structured output) or 'summary' (human-readable)"
    )

    args = parser.parse_args()

    # Get score from argument or metadata file
    if args.score is not None:
        score = args.score
    else:
        score = load_score_from_metadata(args.metadata)
        if score is None:
            print(f"Error: Could not load score from {args.metadata}", file=sys.stderr)
            sys.exit(1)

    result = classify_score(score)

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        # Human-readable summary
        print(f"Score: {result['average_score']}")
        print(f"Classification: {result['score_classification']}")
        print(f"Band: {result['score_band']}")
        print(f"\n{result['remark']}")
        print(f"\nNotes: {result['notes']}")

    # Exit with error code if score is outside range
    sys.exit(0 if result['score_classification'] == "within_range" else 1)


if __name__ == "__main__":
    main()
