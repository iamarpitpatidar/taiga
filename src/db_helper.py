"""
SQLite database helpers for TAIGA instance management.
Thread-safe connection pooling and utilities.
"""

import sqlite3
import json
from pathlib import Path
from threading import Lock
from contextlib import contextmanager

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "database.sqlite"

# Thread-safe connection lock
_db_lock = Lock()


@contextmanager
def get_db():
    """Get a thread-safe database connection."""
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row  # Access columns by name
        try:
            yield conn
        finally:
            conn.close()


# ---------------------------------------------------
# Instance Database Operations
# ---------------------------------------------------

def get_instances_to_process(limit=None):
    """Get instances that need to be downloaded."""
    with get_db() as conn:
        query = """
            SELECT * FROM instances
            WHERE download_status != 'downloaded' OR download_status = ''
            ORDER BY id
        """
        if limit:
            query += f" LIMIT {limit}"

        cursor = conn.execute(query)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_instance_by_problem_id(problem_id):
    """Get a single instance by problem_id."""
    with get_db() as conn:
        cursor = conn.execute("SELECT * FROM instances WHERE problem_id = ?", (problem_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_download_status(problem_id, status):
    """Update the download status for an instance."""
    with get_db() as conn:
        conn.execute(
            "UPDATE instances SET download_status = ? WHERE problem_id = ?",
            (status, problem_id)
        )
        conn.commit()


def update_qa_status(problem_id, qa_result, qa_notes, processed_at):
    """Update QA status for an instance."""
    with get_db() as conn:
        conn.execute(
            """UPDATE instances
               SET status = 'done', qa_result = ?, qa_notes = ?, processed_at = ?
               WHERE problem_id = ?""",
            (qa_result, qa_notes, processed_at, problem_id)
        )
        conn.commit()


def get_instances_for_prefilter(limit=None):
    """Get downloaded instances that need pre-filtering."""
    with get_db() as conn:
        query = """
            SELECT * FROM instances
            WHERE download_status = 'downloaded'
              AND (status = '' OR status IS NULL)
            ORDER BY id
        """
        if limit:
            query += f" LIMIT {limit}"

        cursor = conn.execute(query)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_instance_count():
    """Get total instance count."""
    with get_db() as conn:
        cursor = conn.execute("SELECT COUNT(*) as count FROM instances")
        return cursor.fetchone()['count']


def get_downloaded_count():
    """Get count of downloaded instances."""
    with get_db() as conn:
        cursor = conn.execute("SELECT COUNT(*) as count FROM instances WHERE download_status = 'downloaded'")
        return cursor.fetchone()['count']


# ---------------------------------------------------
# Repo Store Database Operations
# ---------------------------------------------------

def get_repo_average(repo_id):
    """Get average score for a repo."""
    with get_db() as conn:
        cursor = conn.execute("SELECT average_score FROM repositories WHERE repo_id = ?", (repo_id,))
        row = cursor.fetchone()
        return row['average_score'] if row else None


def repo_average_ok(repo_id):
    """Check if repo average is in acceptable range [0.4, 0.8]."""
    avg = get_repo_average(repo_id)
    if avg is None:
        return False, None
    return (0.4 <= avg <= 0.8), avg


def get_prior_rubrics(repo_id, exclude_problem_id=None):
    """Get all accepted rubrics for a repo (excluding specific problem_id)."""
    with get_db() as conn:
        if exclude_problem_id:
            cursor = conn.execute(
                "SELECT problem_id, rubric_json FROM accepted_rubrics WHERE repo_id = ? AND problem_id != ?",
                (repo_id, exclude_problem_id)
            )
        else:
            cursor = conn.execute(
                "SELECT problem_id, rubric_json FROM accepted_rubrics WHERE repo_id = ?",
                (repo_id,)
            )

        rows = cursor.fetchall()
        return {row['problem_id']: json.loads(row['rubric_json']) for row in rows}


def add_accepted_rubric(repo_id, problem_id, rubric):
    """Add an accepted rubric to the store."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO accepted_rubrics (repo_id, problem_id, rubric_json) VALUES (?, ?, ?)",
            (repo_id, problem_id, json.dumps(rubric))
        )
        conn.commit()


def update_repo_average(repo_id, average_score, instance_count):
    """Update repo average score."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO repositories (repo_id, average_score, instance_count) VALUES (?, ?, ?)",
            (repo_id, average_score, instance_count)
        )
        conn.commit()


def get_repo_id(problem_id):
    """Extract repo_id from problem_id (format: owner__repo-NN)."""
    return problem_id.rsplit('-', 1)[0] if '-' in problem_id else problem_id


# ---------------------------------------------------
# Repo Store Building (from instances)
# ---------------------------------------------------

def build_repo_store():
    """Build/update repo store from instances database."""
    from collections import defaultdict

    repo_scores = defaultdict(list)

    # Get all instances with scores
    with get_db() as conn:
        cursor = conn.execute("SELECT problem_id, score_mean FROM instances")
        for row in cursor:
            problem_id = row['problem_id']
            score = row['score_mean']

            if score is not None:
                repo_id = get_repo_id(problem_id)
                repo_scores[repo_id].append(float(score))

    # Calculate averages and update repo store
    with get_db() as conn:
        for repo_id, scores in repo_scores.items():
            avg_score = sum(scores) / len(scores)
            conn.execute(
                "INSERT OR REPLACE INTO repositories (repo_id, average_score, instance_count) VALUES (?, ?, ?)",
                (repo_id, avg_score, len(scores))
            )
        conn.commit()

    return len(repo_scores)
