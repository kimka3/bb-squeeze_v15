#!/usr/bin/env bash
# Install only; no service/campaign is started by this script.
set -euo pipefail
if [[ "$EUID" -ne 0 ]]; then
    echo 'Run with sudo after reviewing scripts/bb-squeeze-paper.service.' >&2
    exit 1
fi
REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$REPO_DIR" != '/opt/bb-squeeze' ]]; then
    echo 'This service template requires the repository at /opt/bb-squeeze.' >&2
    exit 1
fi
if ! id bbpaper >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/bb-squeeze --shell /usr/sbin/nologin bbpaper
fi
test -x "$REPO_DIR/.venv/bin/python"
test -f "$REPO_DIR/paper.config.json"
install -d -m 0750 -o bbpaper -g bbpaper /var/lib/bb-squeeze
install -d -m 0750 -o bbpaper -g bbpaper "$REPO_DIR/data"
# Prepared public market data are the only writable repository subtree.
chown -R bbpaper:bbpaper "$REPO_DIR/data"
install -m 0644 "$REPO_DIR/scripts/bb-squeeze-paper.service" /etc/systemd/system/bb-squeeze-paper.service
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/bb-squeeze-paper.service
echo 'Installed. Run the bbpaper preflight, then explicitly enable --now when ready.'
