#!/bin/bash
# BamDude — Docker volume migration from Bambuddy HE
# Run this ONCE before upgrading from Bambuddy HE to BamDude

set -e

OLD_DATA="bambuddy_he_data"
OLD_LOGS="bambuddy_he_logs"
NEW_DATA="bamdude_data"
NEW_LOGS="bamdude_logs"

# Check old volumes exist
if ! docker volume inspect "$OLD_DATA" &>/dev/null; then
  echo "No old Bambuddy HE volumes found — nothing to migrate."
  echo "If this is a fresh install, just run: docker compose up -d"
  exit 0
fi

# Check new volumes don't already exist
if docker volume inspect "$NEW_DATA" &>/dev/null; then
  echo "New volumes already exist — migration may have been done already."
  echo "Remove them first if you want to re-migrate: docker volume rm $NEW_DATA $NEW_LOGS"
  exit 1
fi

echo "Migrating Docker volumes from Bambuddy HE to BamDude..."
echo "  $OLD_DATA -> $NEW_DATA"
echo "  $OLD_LOGS -> $NEW_LOGS"
echo ""

docker volume create "$NEW_DATA"
docker volume create "$NEW_LOGS"

echo "Copying data (this may take a moment)..."
docker run --rm -v "$OLD_DATA":/from:ro -v "$NEW_DATA":/to alpine sh -c "cp -a /from/. /to/"
docker run --rm -v "$OLD_LOGS":/from:ro -v "$NEW_LOGS":/to alpine sh -c "cp -a /from/. /to/"

echo ""
echo "Migration complete!"
echo "Old volumes kept as backup. Remove when ready:"
echo "  docker volume rm $OLD_DATA $OLD_LOGS"
