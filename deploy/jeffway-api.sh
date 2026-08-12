#!/usr/bin/env bash
# Manage the Jeffway API systemd service (survives SSH disconnect).
set -euo pipefail

SERVICE=jeffway-api.service
UNIT_SRC="$(cd "$(dirname "$0")" && pwd)/jeffway-api.service"
UNIT_DST="/etc/systemd/system/${SERVICE}"

usage() {
  cat <<EOF
Usage: $0 {install|start|stop|restart|status|logs|uninstall}

  install   Copy unit file, enable on boot, start service
  start     Start the service
  stop      Stop the service
  restart   Restart the service
  status    Show service status
  logs      Follow journal logs (Ctrl+C to exit)
  uninstall Stop, disable, remove unit file
EOF
}

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run as root (or with sudo): sudo $0 $*" >&2
    exit 1
  fi
}

cmd_install() {
  require_root
  cp "$UNIT_SRC" "$UNIT_DST"
  systemctl daemon-reload
  systemctl enable "$SERVICE"
  systemctl restart "$SERVICE"
  systemctl --no-pager status "$SERVICE" || true
  echo ""
  echo "API should be on http://0.0.0.0:8010 (check: curl -s http://127.0.0.1:8010/health)"
}

cmd_uninstall() {
  require_root
  systemctl stop "$SERVICE" 2>/dev/null || true
  systemctl disable "$SERVICE" 2>/dev/null || true
  rm -f "$UNIT_DST"
  systemctl daemon-reload
  echo "Removed $SERVICE"
}

case "${1:-}" in
  install)   cmd_install ;;
  start)     require_root; systemctl start "$SERVICE" ;;
  stop)      require_root; systemctl stop "$SERVICE" ;;
  restart)   require_root; systemctl restart "$SERVICE" ;;
  status)    systemctl status "$SERVICE" --no-pager || true ;;
  logs)      journalctl -u "$SERVICE" -f ;;
  uninstall) cmd_uninstall ;;
  *) usage; exit 1 ;;
esac
