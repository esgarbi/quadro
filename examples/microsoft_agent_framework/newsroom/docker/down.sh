#!/usr/bin/env bash
# down.sh — stop the Quadro newsroom containers.
#
# Usage:
#   ./down.sh           # stop containers, keep volumes (articles + model weights)
#   ./down.sh --clean   # stop containers AND remove all volumes (fresh slate)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CLEAN=false
if [[ "${1:-}" == "--clean" ]]; then
    CLEAN=true
fi

if $CLEAN; then
    echo "Stopping containers and removing volumes..."
    docker compose down --volumes
    echo "Done. Model weights and articles have been removed."
else
    echo "Stopping containers (volumes retained)..."
    docker compose down
    echo "Done. Articles and model weights are preserved in Docker volumes."
    echo "Run './down.sh --clean' to remove them."
fi
