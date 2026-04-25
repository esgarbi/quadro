#!/usr/bin/env sh
# entrypoint.debug.sh — Debug variant of entrypoint.sh.
#
# Differences from entrypoint.sh:
#   - Installs debugpy at runtime (keeps prod image clean)
#   - Launches main.py under debugpy, waiting for the IDE to attach
#   - Set DEBUGPY_WAIT=0 to skip waiting (debugpy still listens)
#
# Attach from VS Code / Cursor with the "Attach: Ordering Docker" launch config.

set -e

DEBUGPY_PORT="${DEBUGPY_PORT:-5679}"
DEBUGPY_WAIT="${DEBUGPY_WAIT:-1}"
LLM_PROVIDER="${LLM_PROVIDER:-ollama}"

# ── Install debugpy if missing ────────────────────────────────────────────────
python -c "import debugpy" 2>/dev/null || pip install --quiet debugpy

# ── 1. LLM provider setup ─────────────────────────────────────────────────────
if [ "$LLM_PROVIDER" = "ollama" ]; then
    OLLAMA_HEALTH="${OLLAMA_BASE_URL}/api/tags"

    echo "[debug-entrypoint] LLM_PROVIDER=ollama — waiting for Ollama at ${OLLAMA_BASE_URL} ..."
    RETRIES=60
    until curl -sf "${OLLAMA_HEALTH}" > /dev/null 2>&1; do
        RETRIES=$((RETRIES - 1))
        if [ "$RETRIES" -le 0 ]; then
            echo "[debug-entrypoint] ERROR: Ollama did not become healthy in time." >&2
            exit 1
        fi
        sleep 2
    done
    echo "[debug-entrypoint] Ollama is ready."

    echo "[debug-entrypoint] Checking for model: ${OLLAMA_MODEL}"
    if ! curl -sf "${OLLAMA_BASE_URL}/api/tags" | grep -q "${OLLAMA_MODEL}"; then
        echo "[debug-entrypoint] Pulling ${OLLAMA_MODEL} ..."
        curl -sf -X POST "${OLLAMA_BASE_URL}/api/pull" \
            -H "Content-Type: application/json" \
            -d "{\"name\": \"${OLLAMA_MODEL}\"}" | tail -1
        echo "[debug-entrypoint] Pull complete."
    else
        echo "[debug-entrypoint] Model already present."
    fi
else
    echo "[debug-entrypoint] LLM_PROVIDER=${LLM_PROVIDER} — skipping Ollama health check"
    if [ -z "${OPENAI_API_KEY}" ]; then
        echo "[debug-entrypoint] ERROR: OPENAI_API_KEY is required when LLM_PROVIDER=${LLM_PROVIDER}" >&2
        exit 1
    fi
    echo "[debug-entrypoint] OPENAI_BASE_URL=${OPENAI_BASE_URL}"
    echo "[debug-entrypoint] OPENAI_MODEL_ID=${OPENAI_MODEL_ID}"
fi

# ── 2. Clean slate ────────────────────────────────────────────────────────────
DB_PATH="/app/examples/microsoft_agent_framework/ordering_system/orders.db"
if [ -f "${DB_PATH}" ]; then
    echo "[debug-entrypoint] Removing stale orders.db"
    rm -f "${DB_PATH}"
fi

# ── 3. Start Board UI in background ──────────────────────────────────────────
echo "[debug-entrypoint] Starting Board UI on port ${UI_PORT} ..."
python -m quadro.ui \
    "${DB_PATH}" \
    --host 0.0.0.0 \
    --port "${UI_PORT}" \
    --wait 30 \
    &
UI_PID=$!
echo "[debug-entrypoint] Board UI pid=${UI_PID} — open http://localhost:${UI_PORT}"

# ── 4. Launch main.py under debugpy ──────────────────────────────────────────
ARGS="--target ${ORDERING_TARGET} --cycles ${ORDERING_CYCLES}"
if [ -n "${ORDERING_CHOREOGRAPHY}" ]; then
    ARGS="${ARGS} --choreography ${ORDERING_CHOREOGRAPHY}"
elif [ -n "${ORDERING_PROFILE}" ]; then
    ARGS="${ARGS} --profile ${ORDERING_PROFILE}"
fi

cd /app/examples/microsoft_agent_framework/ordering_system

if [ "$DEBUGPY_WAIT" = "1" ]; then
    echo "[debug-entrypoint] debugpy listening on 0.0.0.0:${DEBUGPY_PORT} — WAITING for debugger to attach ..."
    # shellcheck disable=SC2086
    exec python -m debugpy --listen 0.0.0.0:${DEBUGPY_PORT} --wait-for-client main_pipeline.py ${ARGS}
else
    echo "[debug-entrypoint] debugpy listening on 0.0.0.0:${DEBUGPY_PORT} (not waiting for attach)"
    # shellcheck disable=SC2086
    exec python -m debugpy --listen 0.0.0.0:${DEBUGPY_PORT} main_pipeline.py ${ARGS}
fi
