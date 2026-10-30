#!/usr/bin/env bash
# 하루 한 번 DB를 덤프하고 14일 넘은 것을 지운다. 원문(data/snapshots)은 크고 다시 받기 어려우니
# 따로 옮겨 두는 것을 권한다 (docs/deploy.md).
set -euo pipefail
DEST=${BACKUP_DIR:-/var/backups/noticedelta}
mkdir -p "$DEST"
docker exec noticedelta-postgres-1 pg_dump -U noticedelta -Fc noticedelta > "$DEST/db-$(date +%Y%m%d).dump"
find "$DEST" -name 'db-*.dump' -mtime +14 -delete
