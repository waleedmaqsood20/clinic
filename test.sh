#!/usr/bin/env bash
# Run both test suites. Expects a venv with requirements.txt installed,
# or run from run.sh which sets that up.
set -e

cd "$(dirname "$0")"

export PYTHONIOENCODING=utf-8

echo "==== Offline tests ===="
python tests/test_offline.py

echo ""
echo "==== Stage 1 integration tests ===="
python tests/test_stage1.py
