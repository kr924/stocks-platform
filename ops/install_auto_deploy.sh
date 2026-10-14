#!/usr/bin/env bash
#
# One-time setup: run ops/auto_deploy.sh every 5 minutes under systemd.
#
#   cd ~/stocks && git pull origin main && sudo bash ops/install_auto_deploy.sh
#
# Installs stocks-auto-deploy.service (one deploy attempt, as the repo's owner)
# and stocks-auto-deploy.timer (every 5 minutes), then runs one attempt
# straight away. Re-running is safe; --uninstall removes both units.
#
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo bash $0"; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_AS="${SUDO_USER:-$(stat -c %U "$REPO")}"
RUN_HOME="$(getent passwd "$RUN_AS" | cut -d: -f6)"
UNIT=/etc/systemd/system/stocks-auto-deploy

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl disable --now stocks-auto-deploy.timer 2>/dev/null || true
    rm -f "$UNIT.service" "$UNIT.timer"
    systemctl daemon-reload
    echo "Auto-deploy removed."
    exit 0
fi

# The script uses `sudo docker`, exactly as the manual deploy and retunnel.sh do.
sudo -u "$RUN_AS" sudo -n true 2>/dev/null \
    || { echo "$RUN_AS needs passwordless sudo for docker (the default on Oracle's Ubuntu image)."; exit 1; }

cat > "$UNIT.service" <<EOF
[Unit]
Description=Deploy origin/main of the stocks platform if it changed
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_AS
Environment=HOME=$RUN_HOME
Environment=STOCKS=$REPO
WorkingDirectory=$REPO
ExecStart=/bin/bash $REPO/ops/auto_deploy.sh
# The image build (npm + pip) can take several minutes on the ARM VM.
TimeoutStartSec=45min
EOF

cat > "$UNIT.timer" <<EOF
[Unit]
Description=Check for a new stocks platform commit every 5 minutes

[Timer]
OnBootSec=3min
OnCalendar=*:0/5
Persistent=false

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now stocks-auto-deploy.timer

echo "Installed. Running the first check now (this builds the image if main is new — a few minutes)..."
systemctl start stocks-auto-deploy.service || true
echo
sudo -u "$RUN_AS" env HOME="$RUN_HOME" STOCKS="$REPO" bash "$REPO/ops/auto_deploy.sh" --status
echo
echo "Log:    tail -f $REPO/deploy.log"
echo "Timer:  systemctl list-timers stocks-auto-deploy.timer"
