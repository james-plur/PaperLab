#!/bin/sh
set -eu
root=${PAPERLAB_ROOT:-/srv/paperlab-publish}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
destination="$root/backups/daily/$stamp"
mkdir -p "$destination"
sqlite3 "$root/data/papers.db" ".timeout 10000" ".backup '$destination/papers.db'"
sqlite3 "$destination/papers.db" "PRAGMA integrity_check;" | grep -qx ok
zstd -q --rm "$destination/papers.db"
find "$root/backups/daily" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf -- {} +
if [ "$(date -u +%u)" = "7" ]; then
  mkdir -p "$root/backups/weekly"
  cp "$destination/papers.db.zst" "$root/backups/weekly/$stamp.db.zst"
fi
find "$root/backups/weekly" -type f -mtime +28 -delete 2>/dev/null || true
if command -v rclone >/dev/null 2>&1 && [ -n "${PAPERLAB_BACKUP_REMOTE:-}" ]; then
  rclone copy "$destination" "$PAPERLAB_BACKUP_REMOTE/$stamp"
  rclone sync "$root/pdfs" "$PAPERLAB_BACKUP_REMOTE/objects/pdfs"
  rclone sync "$root/reports" "$PAPERLAB_BACKUP_REMOTE/objects/reports"
  [ ! -d "$root/figures" ] || rclone sync "$root/figures" "$PAPERLAB_BACKUP_REMOTE/objects/figures"
  [ ! -d "$root/reviews" ] || rclone sync "$root/reviews" "$PAPERLAB_BACKUP_REMOTE/objects/reviews"
fi
