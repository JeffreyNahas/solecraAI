#!/usr/bin/env bash
# Nightly Postgres backup. Run from the repo directory on the server via cron:
#   15 3 * * * cd /opt/solecra && ./scripts/backup.sh >> backups/backup.log 2>&1
# Keeps 14 days locally. Copy the backups/ folder to object storage (see RUNBOOK.md).
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; source .env; set +a

mkdir -p backups
file="backups/solecra_$(date +%Y%m%d_%H%M%S).sql.gz"
docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-solecra}" "${POSTGRES_DB:-solecra}" | gzip > "$file"
echo "$(date -Is) backup written: $file ($(du -h "$file" | cut -f1))"

find backups -name 'solecra_*.sql.gz' -mtime +14 -delete
