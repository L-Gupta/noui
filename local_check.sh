#!/usr/bin/env bash
set -euo pipefail

PYTHON=".venv/bin/python"
RUN="$PYTHON -m"

echo "=== ruff check ==="
$RUN ruff check .

echo ""
echo "=== ruff format ==="
$RUN ruff format --check .

echo ""
echo "=== mypy ==="
$RUN mypy .

echo ""
echo "=== pytest ==="
$RUN pytest -v

echo ""
echo "All checks passed."
