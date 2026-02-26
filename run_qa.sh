#!/usr/bin/env bash
# Run QA engine using the project venv.
# Use this when Cursor's integrated terminal intercepts Python.
cd "$(dirname "$0")"
VENV_SITE="$PWD/.venv/lib/python3.14/site-packages"
export PYTHONPATH="$VENV_SITE${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3.14 -m qa_engine.main
