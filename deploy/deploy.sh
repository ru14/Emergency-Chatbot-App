#!/usr/bin/env bash
# Copy the app to a Linode and (re)start it as a systemd service.
#   bash deploy/deploy.sh 172.232.188.234
# Run from the repo folder. Needs SSH key access as root and a filled-in .env.
set -euo pipefail
IP="${1:?usage: bash deploy/deploy.sh LINODE_IP}"
SSH=(ssh -i "$HOME/.ssh/id_ed25519" -o IdentitiesOnly=yes -o ConnectTimeout=15 "root@$IP")
SCP=(scp -i "$HOME/.ssh/id_ed25519" -o IdentitiesOnly=yes -o ConnectTimeout=15)

"${SSH[@]}" 'mkdir -p /root/copilot && chmod 700 /root/copilot'
"${SCP[@]}" .env app.py core.py index.html deploy/app.service "root@$IP:/root/copilot/"
"${SSH[@]}" 'chmod 600 /root/copilot/.env &&
  cp /root/copilot/app.service /etc/systemd/system/copilot.service &&
  systemctl daemon-reload && systemctl enable copilot >/dev/null 2>&1 && systemctl restart copilot &&
  for i in $(seq 1 60); do curl -fsS -o /dev/null http://127.0.0.1/health && break; sleep 2; done;
  systemctl is-active copilot; journalctl -u copilot -n 5 --no-pager'
echo "Health from the internet:"
curl -fsS "http://$IP/health"; echo
