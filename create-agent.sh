#!/usr/bin/env bash
# Create (or preview) the Retell LLM + agent.
#   bash create-agent.sh            # creates on Retell (needs RETELL_API_KEY in .env)
#   bash create-agent.sh --dry-run  # prints payloads, no account needed
set -e

cd "$(dirname "$0")"

source venv/Scripts/activate 2>/dev/null || source venv/bin/activate

if [ -f ".env" ]; then
    set -a; source .env; set +a
fi

python -m app.provision "$@"
