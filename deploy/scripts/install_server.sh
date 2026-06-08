#!/usr/bin/env bash
set -euo pipefail

apt-get update
apt-get install -y docker.io docker-compose-v2 git curl ca-certificates
systemctl enable --now docker
docker compose version
