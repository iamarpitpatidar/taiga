#!/usr/bin/env python3
"""
Update QA status, result, and notes in the database for a specific job_id.
Updates status, qa_result, qa_notes, and processed_at atomically.

Status enum: in_progress | failed | done
Result enum: accepted | rejected | "" (empty for in_progress/failed)
"""

import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = PROJECT_ROOT / "data" / "database.sqlite"


def update_status(job_id, status, qa_result="", qa_notes=""):
    """
    Update status, result, and notes for an instance by job_id.

    Args:
        job_id: The job UUID (PRIMARY KEY)
        status: "in_progress" | "failed" | "done"
        qa_result: "accepted" | "rejected" | "" (required for done/failed)
        qa_notes: Explanation (required for done/failed, optional for in_progress)

    Returns:
        dict with success status and updated row
    """
    # Validate inputs
    valid_statuses = ["in_progress", "failed", "done"]
    valid_results = ["accepted", "rejected", ""]

    if status not in valid_statuses:
        return {
            "success": False,
            "error": f"Invalid status: {status}. Must be one of {valid_statuses}"
        }

    if qa_result and qa_result not in valid_results:
        return {
            "success": False,
            "error": f"Invalid qa_result: {qa_result}. Must be one of {valid_results}"
        }

    # Require qa_result and qa_notes for done/failed
    if status in ["done", "failed"]:
        if not qa_result:
            return {
                "success": False,
                "error": f"qa_result is required when status is '{status}'"
            }
        if not qa_notes:
            return {
                "success": False,
                "error": f"qa_notes is required when status is '{status}'"
            }

    processed_at = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    try:
        # Verify row exists
        cur.execute("SELECT status, problem_id FROM instances WHERE job_id = ?", (job_id,))
        row = cur.fetchone()

        if not row:
            return {
                "success": False,
                "error": f"No instance found with job_id: {job_id}"
            }

        current_status = row['status']
        problem_id = row['problem_id']

        # Validation rules:
        # - in_progress: can only be set if status is empty
        # - done/failed: can be set from in_progress
        if status == "in_progress" and current_status and current_status != '':
            return {
                "success": False,
                "error": f"Cannot set to in_progress: current status is '{current_status}' (expected empty)",
                "current_status": current_status,
                "job_id": job_id,
                "problem_id": problem_id
            }

        if status in ["done", "failed"] and current_status != "in_progress":
            return {
                "success": False,
                "error": f"Cannot set to '{status}': current status is '{current_status}' (expected in_progress)",
                "current_status": current_status,
                "job_id": job_id,
                "problem_id": problem_id
            }

        # Update the row
        cur.execute(
            """UPDATE instances
               SET status = ?, qa_result = ?, qa_notes = ?, processed_at = ?
               WHERE job_id = ?""",
            (status, qa_result, qa_notes, processed_at, job_id)
        )
        conn.commit()

        # Verify the update
        cur.execute(
            "SELECT status, qa_result, qa_notes, processed_at, problem_id FROM instances WHERE job_id = ?",
            (job_id,)
        )
        updated_row = cur.fetchone()

        if not updated_row:
            return {
                "success": False,
                "error": "Update failed: row not found after commit"
            }

        # Verify fields match
        if updated_row['status'] != status:
            return {
                "success": False,
                "error": "Update failed: status verification mismatch",
                "expected": status,
                "actual": updated_row['status']
            }

        return {
            "success": True,
            "job_id": job_id,
            "problem_id": updated_row['problem_id'],
            "status": updated_row['status'],
            "qa_result": updated_row['qa_result'],
            "qa_notes": updated_row['qa_notes'],
            "processed_at": updated_row['processed_at']
        }

    except Exception as e:
        conn.rollback()
        return {
            "success": False,
            "error": str(e)
        }
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Update QA status, result, and notes in database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Set status to in_progress (after creating QA_LOCK):
  %(prog)s <job_id> --status in_progress

  # Mark as done with result accepted:
  %(prog)s <job_id> --status done --result accepted --notes "Instance accepted. Score 0.46 in ideal range..."

  # Mark as failed:
  %(prog)s <job_id> --status failed --result rejected --notes "Early termination: suspicious diff"

Status enum: in_progress | failed | done
Result enum: accepted | rejected | "" (empty)
        """
    )
    parser.add_argument("job_id", help="Job UUID (PRIMARY KEY)")
    parser.add_argument("--status", required=True, choices=["in_progress", "failed", "done"],
                        help="Status to set")
    parser.add_argument("--result", dest="qa_result", choices=["accepted", "rejected"],
                        help="QA result (required for done/failed)")
    parser.add_argument("--notes", dest="qa_notes",
                        help="QA notes (required for done/failed)")

    args = parser.parse_args()

    result = update_status(
        job_id=args.job_id,
        status=args.status,
        qa_result=args.qa_result or "",
        qa_notes=args.qa_notes or ""
    )

    print(json.dumps(result, indent=2))
    sys.exit(0 if result["success"] else 1)
