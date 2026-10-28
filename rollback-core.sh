#!/bin/bash
# Explicit core-code rollback. New events, snapshots and recordings are preserved.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi
backup=/var/backups/camera-monitor/pre-fix-20261028-2314/project.tar
test -r "$backup"
stage=$(mktemp -d /var/tmp/camera-monitor-rollback-XXXXXX)
chmod 700 "$stage"
tar -xf "$backup" -C "$stage" \
  opt/camera-ai/camera_ai.py opt/camera-recorder/server.py opt/camera-timestamp/server.py \
  var/www/camera-monitor/index.html var/www/camera-monitor/motion.html \
  var/www/camera-monitor/recordings.html var/www/camera-monitor/camera-ai-events.js \
  etc/nginx/sites-enabled/camera-monitor
systemctl disable --now camera-health.timer camera-backup.timer camera-cleanup.timer
systemctl stop camera-health.service camera-backup.service camera-cleanup.service
systemctl stop camera-ai camera-recorder-api camera-timestamp
for path in opt/camera-ai/camera_ai.py opt/camera-recorder/server.py opt/camera-timestamp/server.py \
  var/www/camera-monitor/index.html var/www/camera-monitor/motion.html \
  var/www/camera-monitor/recordings.html var/www/camera-monitor/camera-ai-events.js \
  etc/nginx/sites-enabled/camera-monitor; do
  install -o root -g root -m 644 "$stage/$path" "/$path"
done
nginx -t
systemctl reload nginx
systemctl start camera-ai camera-recorder-api camera-timestamp
echo "Core code restored. New data was preserved. Extracted recovery files remain at $stage"
echo "Maintenance timers remain stopped pending review; audio readiness helpers remain installed."
