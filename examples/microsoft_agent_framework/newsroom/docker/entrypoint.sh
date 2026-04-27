#!/usr/bin/env sh
# entrypoint.sh — start the Board UI in the background, then run the newsroom.
#
# Process model:
#   1. (LLM_PROVIDER=ollama only) Wait for Ollama health, pull model if needed
#   2. (LLM_PROVIDER=openai) Validate OPENAI_API_KEY is set
#   3. Remove any stale newsroom.db so each container start is a fresh run
#   4. Start Board UI in the background (python -m quadro.ui newsroom.db)
#   5. Foreground main.py — when it exits the container stops
#
# Environment variables (set in docker-compose.yml or .env):
#   LLM_PROVIDER             "ollama" or "openai"          (default "ollama")
#   NEWSROOM_TARGET          number of articles to publish  (default 5)
#   NEWSROOM_CYCLES          max chief decision cycles      (default 500)
#   NEWSROOM_CHOREOGRAPHY    named choreography or empty    (default "")
#   OLLAMA_MODEL             Ollama model name              (only when LLM_PROVIDER=ollama)
#   OLLAMA_BASE_URL          Ollama API base URL            (only when LLM_PROVIDER=ollama)
#   OPENAI_API_KEY           API key for OpenAI-compat API  (required when LLM_PROVIDER=openai)
#   OPENAI_BASE_URL          OpenAI-compat base URL         (used by Python always)
#   OPENAI_MODEL_ID          model identifier               (used by Python always)
#   UI_PORT                  board UI port                  (default 8080)

set -e

LLM_PROVIDER="${LLM_PROVIDER:-ollama}"

# ── 1. LLM provider setup ─────────────────────────────────────────────────────
if [ "$LLM_PROVIDER" = "ollama" ]; then
    OLLAMA_HEALTH="${OLLAMA_BASE_URL}/api/tags"

    echo "[entrypoint] LLM_PROVIDER=ollama — waiting for Ollama at ${OLLAMA_BASE_URL} ..."
    RETRIES=60
    until curl -sf "${OLLAMA_HEALTH}" > /dev/null 2>&1; do
        RETRIES=$((RETRIES - 1))
        if [ "$RETRIES" -le 0 ]; then
            echo "[entrypoint] ERROR: Ollama did not become healthy in time. Exiting." >&2
            exit 1
        fi
        sleep 2
    done
    echo "[entrypoint] Ollama is ready."

    echo "[entrypoint] Checking for model: ${OLLAMA_MODEL}"
    if ! curl -sf "${OLLAMA_BASE_URL}/api/tags" | grep -q "${OLLAMA_MODEL}"; then
        echo "[entrypoint] Pulling ${OLLAMA_MODEL} ..."
        curl -sf -X POST "${OLLAMA_BASE_URL}/api/pull" \
            -H "Content-Type: application/json" \
            -d "{\"name\": \"${OLLAMA_MODEL}\"}" | tail -1
        echo "[entrypoint] Pull complete."
    else
        echo "[entrypoint] Model already present."
    fi
else
    echo "[entrypoint] LLM_PROVIDER=${LLM_PROVIDER} — skipping Ollama health check"
    if [ -z "${OPENAI_API_KEY}" ]; then
        echo "[entrypoint] ERROR: OPENAI_API_KEY is required when LLM_PROVIDER=${LLM_PROVIDER}" >&2
        exit 1
    fi
    echo "[entrypoint] OPENAI_BASE_URL=${OPENAI_BASE_URL}"
    echo "[entrypoint] OPENAI_MODEL_ID=${OPENAI_MODEL_ID}"
fi

# ── 2. Clean slate — remove stale db files ────────────────────────────────────
EXAMPLE_DIR="/app/examples/microsoft_agent_framework/newsroom"
DB_PATH="${EXAMPLE_DIR}/newsroom.db"
echo "[entrypoint] Removing stale *.db files in ${EXAMPLE_DIR}"
rm -f "${EXAMPLE_DIR}"/*.db 2>/dev/null || true

# ── 3. Start Board UI in background ───────────────────────────────────────────
echo "[entrypoint] Starting Board UI on port ${UI_PORT} ..."
python -m quadro.ui \
    "${DB_PATH}" \
    --host 0.0.0.0 \
    --port "${UI_PORT}" \
    --wait 30 \
    &
UI_PID=$!
echo "[entrypoint] Board UI pid=${UI_PID} — open http://localhost:${UI_PORT}"

# ── 4. Build main.py arguments and run ────────────────────────────────────────
ARGS="--target ${NEWSROOM_TARGET} --cycles ${NEWSROOM_CYCLES}"
if [ -n "${NEWSROOM_CHOREOGRAPHY}" ]; then
    ARGS="${ARGS} --choreography ${NEWSROOM_CHOREOGRAPHY}"
fi

echo "[entrypoint] Starting newsroom: python main.py ${ARGS}"
cd /app/examples/microsoft_agent_framework/newsroom

# Use exec so main.py replaces this shell and receives signals cleanly.
# shellcheck disable=SC2086
exec python main_pipeline.py ${ARGS}
