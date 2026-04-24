#!/usr/bin/env bash
# Bootstrap memento-tools: create a Python venv and install dependencies.
# Usage: ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
echo "Done. Activate with: source .venv/bin/activate"
