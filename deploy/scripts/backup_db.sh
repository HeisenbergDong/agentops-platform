#!/usr/bin/env bash
set -euo pipefail

mkdir -p backups
docker exec agentops-postgres pg_dump -U agentops agentops > "backups/agentops-$(date +%Y%m%d-%H%M%S).sql"
