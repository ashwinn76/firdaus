#!/bin/bash

set -e

# Load environment variables
set -a
source /home/ashwinn/apps/nextcloud/.env
set +a

DATE=$(date +%F-%H%M)
BACKUP_ROOT="/mnt/nas/nextcloud/backups"
DB_CONTAINER="nextcloud-postgres-1"

mkdir -p "$BACKUP_ROOT/db"
mkdir -p "$BACKUP_ROOT/config"

echo "============================================="
echo "$DATE"

echo "Backing up database..."
docker exec "$DB_CONTAINER" pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" \
  > "$BACKUP_ROOT/db/nextcloud-db-$DATE.sql"

echo "Backing up config..."
tar -czf "$BACKUP_ROOT/config/nextcloud-config-$DATE.tar.gz" \
  /home/ashwinn/apps/nextcloud/html/config/ \
  /home/ashwinn/apps/nextcloud/.env \
  /home/ashwinn/apps/nextcloud/docker-compose.yml \
  /home/ashwinn/apps/nextcloud/scripts/

# Keep only last 14 days
find "$BACKUP_ROOT/db" -name "nextcloud-db-*.sql" -mtime +5 -delete
find "$BACKUP_ROOT/config" -name "nextcloud-config-*.tar.gz" -mtime +5 -delete

echo "Backup complete."

FILE="$BACKUP_ROOT/db/nextcloud-db-$DATE.sql"

echo "Verifying $FILE"

# Check file size (>1 MB)
SIZE=$(stat -c%s "$FILE")
if [ "$SIZE" -lt 1000000 ]; then
    echo "Backup too small!"
    exit 1
fi

# Check header
head -n 5 "$FILE" | grep -q "PostgreSQL database dump" || {
    echo "Invalid dump header!"
    exit 1
}

echo "Daily verification successful."
echo "============================================="
