#!/usr/bin/env bash
# Install dependencies (first run) and start the server on port 4242.
set -e

cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
    python -m venv venv
fi

# Activate — Scripts/ on Windows, bin/ on Linux/Mac
source venv/Scripts/activate 2>/dev/null || source venv/bin/activate

pip install -q -r requirements.txt

uvicorn app.server:app --host 0.0.0.0 --port 4242 --reload
