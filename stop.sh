#!/usr/bin/env bash
# Stop the badk stack.
#   ./stop.sh          stop containers, keep volumes
#   ./stop.sh --clean  also drop volumes and locally built images
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" = "--clean" ]; then
  docker compose --profile cloud down -v --remove-orphans --rmi local
else
  docker compose --profile cloud down --remove-orphans
fi
