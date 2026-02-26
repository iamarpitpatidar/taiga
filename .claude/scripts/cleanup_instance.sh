#!/usr/bin/env bash
# Cleanup script for processed QA instances
#
# Usage: cleanup_instance.sh <problem_id> <job_id>
#
# Deletes the instance directory after QA processing.
# If no sibling instances remain, deletes the parent problem_id directory.

set -euo pipefail

if [ $# -ne 2 ]; then
    echo "Usage: $0 <problem_id> <job_id>" >&2
    exit 1
fi

PROBLEM_ID="$1"
JOB_ID="$2"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTANCES_DIR="$PROJECT_ROOT/instances"

PROBLEM_DIR="$INSTANCES_DIR/$PROBLEM_ID"
INSTANCE_DIR="$PROBLEM_DIR/$JOB_ID"

# Check if instance directory exists
if [ ! -d "$INSTANCE_DIR" ]; then
    echo "Skip: Instance directory not found: $INSTANCE_DIR" >&2
    exit 0
fi

# Delete the instance directory
rm -rf "$INSTANCE_DIR"
echo "Deleted instance: $PROBLEM_ID/$JOB_ID"

# Check for remaining siblings
if [ ! -d "$PROBLEM_DIR" ]; then
    # Already deleted (shouldn't happen but handle it)
    exit 0
fi

SIBLINGS=$(find "$PROBLEM_DIR" -mindepth 1 -maxdepth 1 -type d | wc -l)

if [ "$SIBLINGS" -eq 0 ]; then
    # No siblings remaining, delete parent
    rm -rf "$PROBLEM_DIR"
    echo "  → No siblings remaining, deleted parent directory"
else
    echo "  → $SIBLINGS sibling(s) remaining"
fi
