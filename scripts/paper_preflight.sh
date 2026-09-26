#!/usr/bin/env bash
# Gate a NEW campaign. Existing campaigns must be able to mark their deadline
# complete and recover without depending on a fresh network preflight each boot.
set -euo pipefail
CAMPAIGN_DIR="${1:?campaign directory is required}"
if [[ -f "$CAMPAIGN_DIR/campaign.json" ]]; then
    exit 0
fi
exec /opt/bb-squeeze/.venv/bin/python /opt/bb-squeeze/src/live/run.py \
    --mode paper --config /opt/bb-squeeze/paper.config.json \
    --state-dir /var/lib/bb-squeeze/preflight --preflight
