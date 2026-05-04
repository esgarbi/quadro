#!/usr/bin/env bash
# down.sh — stop the Quadro ordering system containers.
#
# Usage:
#   ./down.sh           # stop containers
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
    echo "Done."
else
    echo "Stopping containers..."
    docker compose down
    echo "Done."
    echo "Run './down.sh --clean' to also remove volumes."
fi
