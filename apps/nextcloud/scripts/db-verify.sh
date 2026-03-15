#!/bin/bash

set -e

# Load environment variables
set -a
source /home/ashwinn/apps/nextcloud/.env
set +a

BACKUP_DIR="/mnt/nas/nextcloud/backups/db"
LATEST=$(ls -t $BACKUP_DIR | head -1)
FILE="$BACKUP_DIR/$LATEST"

echo "*********************************************"
echo "Performing weekly restore test on  $FILE"

# Restore test (silent unless error)
if ! docker exec -i nextcloud-postgres-1 psql -U $POSTGRES_USER -d $POSTGRES_DB -c "CREATE DATABASE restore_test;" >/dev/null 2>&1; then
    echo "Failed to create restore_test database."
    exit 1
fi

if ! cat "$FILE" | docker exec -i nextcloud-postgres-1 psql -U $POSTGRES_USER -d restore_test >/dev/null 2>&1; then
    echo "Restore into restore_test failed."
    docker exec -i nextcloud-postgres-1 psql -U $POSTGRES_USER -d $POSTGRES_DB -c "DROP DATABASE restore_test;" >/dev/null 2>&1
    exit 1
fi

docker exec -i nextcloud-postgres-1 psql -U $POSTGRES_USER -d $POSTGRES_DB -c "DROP DATABASE restore_test;" >/dev/null 2>&1

echo "Restore test successful."
echo "*********************************************"
