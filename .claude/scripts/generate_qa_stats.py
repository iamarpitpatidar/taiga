#!/usr/bin/env python3
"""
QA Statistics Generator - Database analytics for processed instances
Pure SQL queries, no LLM needed.
"""

import argparse
import json
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
from db_helper import get_db


def get_qa_summary():
    """Get QA processing statistics from database."""
    with get_db() as conn:
        cursor = conn.execute("""
            SELECT
                COUNT(*) as total_processed,
                SUM(CASE WHEN qa_result = 'accepted' THEN 1 ELSE 0 END) as accepted,
                SUM(CASE WHEN qa_result = 'rejected' THEN 1 ELSE 0 END) as rejected,
                AVG(CASE WHEN qa_result = 'accepted' THEN score_mean ELSE NULL END) as avg_score_accepted,
                AVG(CASE WHEN qa_result = 'rejected' THEN score_mean ELSE NULL END) as avg_score_rejected,
                MIN(score_mean) as min_score,
                MAX(score_mean) as max_score
            FROM instances
            WHERE status = 'done' AND qa_result IS NOT NULL AND qa_result != ''
        """)

        row = cursor.fetchone()

        total = row['total_processed'] or 0
        accepted = row['accepted'] or 0
        rejected = row['rejected'] or 0

        return {
            "total_processed": total,
            "accepted": accepted,
            "rejected": rejected,
            "acceptance_rate": round(accepted / total * 100, 2) if total > 0 else 0.0,
            "avg_score_accepted": round(row['avg_score_accepted'], 4) if row['avg_score_accepted'] else None,
            "avg_score_rejected": round(row['avg_score_rejected'], 4) if row['avg_score_rejected'] else None,
            "min_score": round(row['min_score'], 4) if row['min_score'] else None,
            "max_score": round(row['max_score'], 4) if row['max_score'] else None
        }


def get_pending_stats():
    """Get statistics on pending instances."""
    with get_db() as conn:
        cursor = conn.execute("""
            SELECT
                COUNT(*) as total_pending,
                SUM(CASE WHEN download_status = 'downloaded' THEN 1 ELSE 0 END) as ready_for_qa,
                SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress
            FROM instances
            WHERE status != 'done' OR status IS NULL OR status = ''
        """)

        row = cursor.fetchone()
        return {
            "total_pending": row['total_pending'] or 0,
            "ready_for_qa": row['ready_for_qa'] or 0,
            "in_progress": row['in_progress'] or 0
        }


def get_score_distribution():
    """Get distribution of scores by band."""
    with get_db() as conn:
        cursor = conn.execute("""
            SELECT
                CASE
                    WHEN score_mean < 0.4 THEN 'too_easy'
                    WHEN score_mean >= 0.4 AND score_mean <= 0.5 THEN 'ideal'
                    WHEN score_mean > 0.5 AND score_mean <= 0.7 THEN 'acceptable'
                    WHEN score_mean > 0.7 AND score_mean <= 0.8 THEN 'borderline'
                    WHEN score_mean > 0.8 THEN 'too_hard'
                END as band,
                COUNT(*) as count,
                AVG(score_mean) as avg_score
            FROM instances
            WHERE status = 'done' AND score_mean IS NOT NULL
            GROUP BY band
            ORDER BY avg_score
        """)

        distribution = {}
        for row in cursor.fetchall():
            distribution[row['band']] = {
                "count": row['count'],
                "avg_score": round(row['avg_score'], 4)
            }

        return distribution


def main():
    parser = argparse.ArgumentParser(description="Generate QA processing statistics")
    parser.add_argument("--output", choices=["json", "summary", "dashboard"], default="dashboard")
    parser.add_argument("--include-pending", action="store_true", help="Include pending instance stats")
    parser.add_argument("--score-distribution", action="store_true", help="Include score distribution")

    args = parser.parse_args()

    # Get main stats
    stats = get_qa_summary()

    if args.include_pending:
        stats["pending"] = get_pending_stats()

    if args.score_distribution:
        stats["score_distribution"] = get_score_distribution()

    if args.output == "json":
        print(json.dumps(stats, indent=2))

    elif args.output == "summary":
        print(f"Total Processed: {stats['total_processed']}")
        print(f"Accepted: {stats['accepted']}")
        print(f"Rejected: {stats['rejected']}")
        print(f"Acceptance Rate: {stats['acceptance_rate']}%")
        print(f"Avg Score (Accepted): {stats['avg_score_accepted']}")
        print(f"Avg Score (Rejected): {stats['avg_score_rejected']}")

        if args.include_pending:
            print(f"\nPending: {stats['pending']['total_pending']}")
            print(f"Ready for QA: {stats['pending']['ready_for_qa']}")
            print(f"In Progress: {stats['pending']['in_progress']}")

    else:  # dashboard
        print("=" * 60)
        print("QA PROCESSING DASHBOARD".center(60))
        print("=" * 60)
        print()
        print(f"  Total Processed:    {stats['total_processed']:>6}")
        print(f"  ✓ Accepted:         {stats['accepted']:>6} ({stats['acceptance_rate']:>5}%)")
        print(f"  ✗ Rejected:         {stats['rejected']:>6}")
        print()
        print(f"  Avg Score (Accepted):  {stats['avg_score_accepted'] or 'N/A'}")
        print(f"  Avg Score (Rejected):  {stats['avg_score_rejected'] or 'N/A'}")
        print(f"  Score Range:           {stats['min_score']} - {stats['max_score']}")

        if args.include_pending:
            print()
            print("-" * 60)
            print("PENDING INSTANCES".center(60))
            print("-" * 60)
            print()
            print(f"  Total Pending:      {stats['pending']['total_pending']:>6}")
            print(f"  Ready for QA:       {stats['pending']['ready_for_qa']:>6}")
            print(f"  In Progress:        {stats['pending']['in_progress']:>6}")

        if args.score_distribution:
            print()
            print("-" * 60)
            print("SCORE DISTRIBUTION".center(60))
            print("-" * 60)
            print()
            for band, data in stats['score_distribution'].items():
                print(f"  {band:15} {data['count']:>4} instances (avg: {data['avg_score']})")

        print()
        print("=" * 60)


if __name__ == "__main__":
    main()
