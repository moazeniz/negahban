#!/usr/bin/env bash
# Install the MeowMeow platform jobs (synthetic monitor + disk backups) on the monitoring host.
#
#   sudo ops/systemd/install.sh                 copy code + units, write credentials, verify; timers stay DISABLED
#   sudo ops/systemd/install.sh --enable        ... and start both timers
#   sudo ops/systemd/install.sh --enable monitor   (or: backup) start just one
#
# Secret inputs (read here, never printed; copied to root-only files that systemd hands to the units
# through LoadCredential=, so the jobs run as unprivileged DynamicUser accounts):
#   LIARA_ENV_SRC   default /root/.liara.env      -> /etc/meow-platform/liara.env (3 variables only)
#   INGEST_ENV_SRC  default /root/.negahban-admin.env    -> /etc/meow-platform/status-ingest-token
set -euo pipefail
cd "$(dirname "$0")/../.."
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
PREFIX=/opt/meow-platform CONF=/etc/meow-platform
LIARA_ENV_SRC=${LIARA_ENV_SRC:-/root/.liara.env}
INGEST_ENV_SRC=${INGEST_ENV_SRC:-/root/.negahban-admin.env}

install -d -m 0755 "$PREFIX" "$PREFIX/infra" "$PREFIX/ops/monitor" "$PREFIX/ops/backup"
install -m 0644 infra/liara_api.py infra/liara.yaml "$PREFIX/infra/"
install -m 0755 infra/liara_deploy.py infra/liara_apply.py "$PREFIX/infra/"   # runbook tooling
install -m 0755 ops/monitor/probe.py "$PREFIX/ops/monitor/"
install -m 0755 ops/backup/liara_backup.py "$PREFIX/ops/backup/"
install -d -m 0700 "$CONF"

pick() { grep -E "^($1)=" "$2" || true; }
if [ -r "$LIARA_ENV_SRC" ]; then
  umask 077; pick 'LIARA_API_TOKEN|LIARA_TEAM_ID|LIARA_API_BASE' "$LIARA_ENV_SRC" > "$CONF/liara.env"
  echo "wrote $CONF/liara.env ($(wc -l < "$CONF/liara.env") variables)"
else
  echo "warning: $LIARA_ENV_SRC not readable; create $CONF/liara.env (LIARA_API_TOKEN, LIARA_TEAM_ID, LIARA_API_BASE)"
  : > "$CONF/liara.env"; chmod 600 "$CONF/liara.env"
fi
if [ -r "$INGEST_ENV_SRC" ]; then
  umask 077; pick 'STATUS_INGEST_TOKEN' "$INGEST_ENV_SRC" | cut -d= -f2- | tr -d "\"'" > "$CONF/status-ingest-token"
else
  : > "$CONF/status-ingest-token"; chmod 600 "$CONF/status-ingest-token"
fi
grep -q '^INTERNAL_VANTAGE_URL=' "$CONF/monitor.env" 2>/dev/null || [ ! -f "$CONF/monitor.env" ] || \
  printf '%s\n' '# Second vantage: Negahban in-Liara probe results (same bearer token as the ingest)' \
                 'INTERNAL_VANTAGE_URL=https://negahban-sre.liara.run/api/status/vantage' >> "$CONF/monitor.env"
[ -f "$CONF/monitor.env" ] || cat > "$CONF/monitor.env" <<'EOF'
# Non-secret monitor settings
STATUS_INGEST_URL=https://negahban-sre.liara.run/api/status/ingest
# Second vantage: Negahban's in-Liara probe results (same bearer token as the ingest)
INTERNAL_VANTAGE_URL=https://negahban-sre.liara.run/api/status/vantage
# ALERT_WEBHOOK_URL=https://hooks.example.com/...   (Slack/Mattermost-compatible {"text": ...})
EOF
chmod 600 "$CONF/monitor.env"

install -m 0644 ops/systemd/meow-monitor.service ops/systemd/meow-monitor.timer \
                ops/systemd/meow-backup.service ops/systemd/meow-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/meow-{monitor,backup}.{service,timer}
echo "units installed and verified"

case "${1:-}" in
  --enable)
    for t in ${2:-monitor backup}; do systemctl enable --now "meow-$t.timer"; done
    systemctl list-timers 'meow-*' --no-pager ;;
  *)
    for t in monitor backup; do echo "meow-$t.timer: $(systemctl is-enabled meow-$t.timer 2>/dev/null || true)"; done
    echo "Enable a disabled timer with:"
    echo "  systemctl enable --now meow-monitor.timer   # probe every minute + status page"
    echo "  systemctl enable --now meow-backup.timer    # daily disk backups at 02:30 UTC" ;;
esac
