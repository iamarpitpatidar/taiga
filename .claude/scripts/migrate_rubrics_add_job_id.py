#!/usr/bin/env python3
"""
Migration: Add job_id to accepted_rubrics table and update schema.

This allows storing multiple accepted jobs per problem_id for proper duplicate checking.
"""

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = PROJECT_ROOT / "data" / "database.sqlite"


def migrate():
    """Add job_id column and update UNIQUE constraint."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    try:
        # Check if migration already done
        cur.execute("PRAGMA table_info(accepted_rubrics)")
        columns = [row['name'] for row in cur.fetchall()]

        if 'job_id' in columns:
            print("Migration already applied. Skipping.")
            return True

        print("Starting migration: Adding job_id to accepted_rubrics...")

        # Step 1: Drop old index (will be recreated with new name)
        cur.execute("DROP INDEX IF EXISTS idx_repo_id")

        # Step 2: Create new table with updated schema
        cur.execute("""
            CREATE TABLE accepted_rubrics_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_id TEXT NOT NULL,
                problem_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                rubric_json TEXT NOT NULL,
                score REAL,
                UNIQUE(job_id),
                FOREIGN KEY(repo_id) REFERENCES repositories(repo_id)
            )
        """)

        # Step 3: Copy existing data (set job_id = problem_id for old entries as placeholder)
        cur.execute("""
            INSERT INTO accepted_rubrics_new (id, repo_id, problem_id, job_id, rubric_json, score)
            SELECT id, repo_id, problem_id, problem_id || '-placeholder', rubric_json, score
            FROM accepted_rubrics
        """)

        # Step 4: Drop old table
        cur.execute("DROP TABLE accepted_rubrics")

        # Step 5: Rename new table
        cur.execute("ALTER TABLE accepted_rubrics_new RENAME TO accepted_rubrics")

        # Step 6: Create indexes (use unique names to avoid conflicts with instances table)
        cur.execute("CREATE INDEX idx_rubrics_repo_id ON accepted_rubrics(repo_id)")
        cur.execute("CREATE INDEX idx_rubrics_problem_id ON accepted_rubrics(problem_id)")
        cur.execute("CREATE INDEX idx_rubrics_job_id ON accepted_rubrics(job_id)")

        conn.commit()
        print("✓ Migration completed successfully")
        print(f"  - Added job_id column")
        print(f"  - Updated UNIQUE constraint to (job_id)")
        print(f"  - Added indexes for problem_id and job_id")

        return True

    except Exception as e:
        conn.rollback()
        print(f"✗ Migration failed: {e}", file=sys.stderr)
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    success = migrate()
    sys.exit(0 if success else 1)
