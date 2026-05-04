#!/usr/bin/env bash
# up.sh — build and start the Quadro ordering system.
#
# Usage:
#   ./up.sh                        # uses defaults from .env or docker-compose.yml
#   ./up.sh --target 5             # ship 5 orders
#   ./up.sh --target 3 --cycles 200
#   ./up.sh --profile burst
#   ./up.sh --choreography wave_study
#
# The Board UI is available at http://localhost:8081 once the container starts.
# Ctrl+C stops the logs but leaves the container running.
# Use ./down.sh to stop everything.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TARGET=""
CYCLES=""
PROFILE=""
CHOREOGRAPHY=""

# ── Parse arguments ────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)         TARGET="$2";        shift 2 ;;
        --cycles)         CYCLES="$2";        shift 2 ;;
        --profile)        PROFILE="$2";       shift 2 ;;
        --choreography)   CHOREOGRAPHY="$2";  shift 2 ;;
        *)
            echo "Unknown argument: $1"
            echo "Usage: $0 [--target N] [--cycles N] [--profile NAME] [--choreography NAME]"
            exit 1 ;;
    esac
done

# ── Build env overrides ────────────────────────────────────────────────────────
ENV_OVERRIDES=()
[[ -n "$TARGET" ]]        && ENV_OVERRIDES+=("ORDERING_TARGET=$TARGET")
[[ -n "$CYCLES" ]]        && ENV_OVERRIDES+=("ORDERING_CYCLES=$CYCLES")
[[ -n "$PROFILE" ]]       && ENV_OVERRIDES+=("ORDERING_PROFILE=$PROFILE")
[[ -n "$CHOREOGRAPHY" ]]  && ENV_OVERRIDES+=("ORDERING_CHOREOGRAPHY=$CHOREOGRAPHY")

echo "=========================================="
echo "  Quadro Ordering System"
echo "=========================================="
echo "  Board UI   : http://localhost:${UI_PORT:-8081}"
[[ -n "$TARGET" ]]        && echo "  Target     : $TARGET orders"
[[ -n "$CYCLES" ]]        && echo "  Cycles     : $CYCLES"
[[ -n "$PROFILE" ]]       && echo "  Profile    : $PROFILE"
[[ -n "$CHOREOGRAPHY" ]]  && echo "  Choreography: $CHOREOGRAPHY"
echo "=========================================="
echo ""

# ── Start ──────────────────────────────────────────────────────────────────────
if [[ ${#ENV_OVERRIDES[@]} -gt 0 ]]; then
    env "${ENV_OVERRIDES[@]}" docker compose up --build
else
    docker compose up --build
fi
