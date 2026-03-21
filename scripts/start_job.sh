#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x "venv/bin/python" ]; then
  echo "Missing venv at ./venv. Create it with: python3 -m venv venv"
  exit 1
fi

# Run Job Search app on dedicated port to avoid collisions with other local apps.
export HOST=127.0.0.1
export PORT=8010
export DEBUG=false

exec ./venv/bin/python run.py
