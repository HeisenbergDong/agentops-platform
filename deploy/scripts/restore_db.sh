#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: restore_db.sh backup.sql" >&2
  exit 1
fi

cat "$1" | docker exec -i agentops-postgres psql -U agentops agentops
