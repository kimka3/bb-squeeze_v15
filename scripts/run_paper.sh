#!/usr/bin/env bash
# Fixed UTC campaign, durable status and bounded recovery live in Python.
# Usage: bash scripts/run_paper.sh /absolute/campaign /absolute/paper.config.json
set -euo pipefail
REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
PYTHON_BIN="${PYTHON_BIN:-$REPO_DIR/.venv/bin/python}"
CAMPAIGN_DIR="${1:-$REPO_DIR/paper_runs/campaign-01}"
CONFIG_PATH="${2:-$REPO_DIR/paper.config.json}"
exec "$PYTHON_BIN" src/live/supervisor.py start --campaign-dir "$CAMPAIGN_DIR" --config "$CONFIG_PATH"
