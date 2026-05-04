#!/usr/bin/env bash
# up.sh — build and start the Quadro newsroom.
#
# Usage:
#   ./up.sh                        # uses defaults from .env or docker-compose.yml
#   ./up.sh --target 10            # publish 10 articles
#   ./up.sh --target 3 --cycles 200
#   ./up.sh --choreography sleep_study
#
# The Board UI is available at http://localhost:8080 once the container starts.
# Ctrl+C stops the logs but leaves the container running.
# Use ./down.sh to stop everything.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TARGET=""
CYCLES=""
CHOREOGRAPHY=""

# ── Parse arguments ────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)         TARGET="$2";        shift 2 ;;
        --cycles)         CYCLES="$2";        shift 2 ;;
        --choreography)   CHOREOGRAPHY="$2";  shift 2 ;;
        *)
            echo "Unknown argument: $1"
            echo "Usage: $0 [--target N] [--cycles N] [--choreography NAME]"
            exit 1 ;;
    esac
done

# ── Build env overrides ────────────────────────────────────────────────────────
ENV_OVERRIDES=()
[[ -n "$TARGET" ]]        && ENV_OVERRIDES+=("NEWSROOM_TARGET=$TARGET")
[[ -n "$CYCLES" ]]        && ENV_OVERRIDES+=("NEWSROOM_CYCLES=$CYCLES")
[[ -n "$CHOREOGRAPHY" ]]  && ENV_OVERRIDES+=("NEWSROOM_CHOREOGRAPHY=$CHOREOGRAPHY")

echo "=========================================="
echo "  Quadro Newsroom"
echo "=========================================="
echo "  Board UI   : http://localhost:${UI_PORT:-8080}"
[[ -n "$TARGET" ]]        && echo "  Target     : $TARGET articles"
[[ -n "$CYCLES" ]]        && echo "  Cycles     : $CYCLES"
[[ -n "$CHOREOGRAPHY" ]]  && echo "  Choreography: $CHOREOGRAPHY"
echo "=========================================="
echo ""

# ── Start ──────────────────────────────────────────────────────────────────────
if [[ ${#ENV_OVERRIDES[@]} -gt 0 ]]; then
    env "${ENV_OVERRIDES[@]}" docker compose up --build
else
    docker compose up --build
fi
