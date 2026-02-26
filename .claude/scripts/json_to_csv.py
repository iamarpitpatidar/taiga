"""
Convert TAIGA JSON response files to CSV format for Meta 800 dataset.

Reads JSON files containing problem runs and aggregates scores to create
a CSV with job_id, problem_id, problem_uuid, scores, and rubric data.

Usage:
    python json_to_csv.py data/opus-1-700-filtered.json --output instances.csv
    python json_to_csv.py data/*.json --output instances.csv
"""

import json
import argparse
from pathlib import Path
import pandas as pd
from typing import List, Dict, Any
import statistics


def extract_rubric_from_response(response_item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract rubric from a problem run response."""
    rewards = response_item.get("rewards", [])
    for reward in rewards:
        if reward.get("grading_strategy_type") == "rubric":
            metadata = reward.get("metadata", {})
            rubric = metadata.get("rubric", [])
            if rubric:
                return rubric
    return []


def process_json_file(json_path: Path) -> List[Dict[str, Any]]:
    """Process a single JSON file and extract problem data."""
    print(f"Processing {json_path}...")
    
    with open(json_path) as f:
        data = json.load(f)
    
    # Handle both single job dict and list of jobs
    if isinstance(data, list):
        jobs = data
    else:
        jobs = [data]
    
    all_rows = []
    for job_data in jobs:
        rows = process_job(job_data)
        all_rows.extend(rows)
    
    print(f"  Extracted {len(all_rows)} problems from {json_path.name}")
    return all_rows


def process_job(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Process a single job and extract problem data."""
    job_id = data.get("job_id")
    responses = data.get("response", [])

    # Group responses by problem_id
    problems = {}
    for resp in responses:
        problem_id = resp.get("problem_id")
        if not problem_id:
            continue

        if problem_id not in problems:
            problems[problem_id] = {
                "job_id": job_id,
                "problem_id": problem_id,
                "problem_uuid": resp.get("problem_uuid"),
                "scores": [],
                "attempts": [],
                "created_dates": [],
                "rubric": None,
                "rubric_scores": {},  # rubric_name -> [scores across runs]
            }

        problems[problem_id]["scores"].append(resp.get("final_score", 0.0))
        problems[problem_id]["attempts"].append(resp.get("attempt_number", 0))
        problems[problem_id]["created_dates"].append(resp.get("created_at", ""))

        # Extract rubric from first response (they should all be the same)
        if problems[problem_id]["rubric"] is None:
            rubric = extract_rubric_from_response(resp)
            if rubric:
                problems[problem_id]["rubric"] = rubric

        # Collect rubric scores per run for oscillation detection
        rewards = resp.get("rewards", [])
        for reward in rewards:
            if reward.get("grading_strategy_type") == "rubric":
                subscores = reward.get("subscores", [])
                for subscore in subscores:
                    rubric_name = subscore.get("name")
                    score = subscore.get("score", 0.0)
                    max_score = subscore.get("max_score", 1.0)
                    weight = subscore.get("weight", 1.0)

                    # Normalize score: (score / max_score) * weight
                    normalized = (score / max_score) * weight if max_score > 0 else 0.0

                    if rubric_name not in problems[problem_id]["rubric_scores"]:
                        problems[problem_id]["rubric_scores"][rubric_name] = []
                    problems[problem_id]["rubric_scores"][rubric_name].append(normalized)

    # Aggregate data for each problem
    rows = []
    for problem_id, problem_data in problems.items():
        scores = problem_data["scores"]
        attempts = problem_data["attempts"]
        created_dates = sorted(problem_data["created_dates"])

        if not scores:
            continue

        # Calculate oscillating rubrics: rubrics where scores are NOT all the same
        num_oscillating = 0
        rubric_scores = problem_data["rubric_scores"]
        for rubric_name, rubric_score_list in rubric_scores.items():
            if len(rubric_score_list) > 1:
                # Check if all scores are the same (with small tolerance for floating point)
                first_score = rubric_score_list[0]
                is_oscillating = any(abs(s - first_score) > 1e-6 for s in rubric_score_list[1:])
                if is_oscillating:
                    num_oscillating += 1
        
        row = {
            "job_id": problem_data["job_id"],
            "problem_id": problem_id,
            "problem_uuid": problem_data["problem_uuid"],
            "num_attempts": len(scores),
            "score_mean": statistics.mean(scores),
            "score_max": max(scores),
            "score_min": min(scores),
            "score_median": statistics.median(scores),
            "score_stdev": statistics.stdev(scores) if len(scores) > 1 else 0.0,
            "created_at_first": created_dates[0] if created_dates else "",
            "created_at_last": created_dates[-1] if created_dates else "",
            "num_oscillating": num_oscillating,
            "rubric_json": json.dumps(problem_data["rubric"]) if problem_data["rubric"] else "",
            "rubric_count": len(problem_data["rubric"]) if problem_data["rubric"] else 0,
            "download_status": "",
            "status": "",
            "qa_result": "",
            "qa_notes": "",
            "processed_at": "",
        }
        rows.append(row)
    
    return rows


def main():
    parser = argparse.ArgumentParser(description="Convert TAIGA JSON to CSV")
    parser.add_argument("json_files", nargs="+", help="JSON file(s) to convert")
    parser.add_argument("--output", "-o", default="instances.csv", help="Output CSV file")
    args = parser.parse_args()
    
    all_rows = []
    for json_file_str in args.json_files:
        json_path = Path(json_file_str)
        if not json_path.exists():
            print(f"WARNING: {json_path} does not exist, skipping")
            continue
        rows = process_json_file(json_path)
        all_rows.extend(rows)
    
    if not all_rows:
        print("ERROR: No data extracted from JSON files")
        return
    
    # Create DataFrame and save to CSV
    df = pd.DataFrame(all_rows)
    
    # Sort by problem_id
    df = df.sort_values("problem_id")
    
    # Save to CSV
    output_path = Path(args.output)
    df.to_csv(output_path, index=False)
    
    print(f"\n{'='*60}")
    print(f"Conversion complete!")
    print(f"  Total problems: {len(df)}")
    print(f"  Output file: {output_path}")
    print(f"  Columns: {', '.join(df.columns)}")
    print(f"\nScore distribution:")
    print(f"  Mean: {df['score_mean'].mean():.3f}")
    print(f"  Min: {df['score_mean'].min():.3f}")
    print(f"  Max: {df['score_mean'].max():.3f}")
    print(f"\nProblems in target range [0.4, 0.8]: {len(df[(df['score_mean'] >= 0.4) & (df['score_mean'] <= 0.8)])}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
