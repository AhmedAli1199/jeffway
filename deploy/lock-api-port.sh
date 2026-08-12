#!/usr/bin/env bash
# Restrict port 8010 to trusted client IPs (n8n) via ufw.
set -euo pipefail

N8N_IP="${N8N_IP:-144.126.195.119}"
API_PORT="${API_PORT:-8010}"

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run as root: sudo $0" >&2
    exit 1
  fi
}

require_root

if ! command -v ufw >/dev/null 2>&1; then
  echo "ufw not installed — install with: apt-get install -y ufw" >&2
  exit 1
fi

# Keep SSH open before enabling the firewall.
ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp comment 'SSH'

# Idempotent: delete old Jeffway API rules if re-run.
while ufw status numbered | grep -q 'Jeffway API'; do
  num=$(ufw status numbered | grep 'Jeffway API' | head -1 | sed -n 's/^\[\s*\([0-9]*\)\].*/\1/p')
  [[ -n "$num" ]] && ufw --force delete "$num" || break
done

ufw allow from "$N8N_IP" to any port "$API_PORT" proto tcp comment 'Jeffway API n8n'
ufw deny "$API_PORT"/tcp comment 'Jeffway API block scanners'

ufw --force enable
ufw status verbose

echo ""
echo "Port $API_PORT is open only to $N8N_IP (plus localhost)."
echo "To allow another IP, add to .env API_ALLOWED_IPS and run:"
echo "  N8N_IP=<ip> sudo $0"
