# syntax=docker/dockerfile:1
FROM python:3.12-slim

# ── System deps ────────────────────────────────────────────────────────────────
# curl: used in entrypoint to wait for Ollama health endpoint (LLM_PROVIDER=ollama)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps ────────────────────────────────────────────────────────────────
COPY examples/microsoft_agent_framework/newsroom/docker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ── Application source ─────────────────────────────────────────────────────────
# We copy the full repo because:
#   - shared.py lives at examples/microsoft_agent_framework/shared.py
#   - quadro itself lives at src/quadro/
# Both are on sys.path via main.py's sys.path.insert() calls.
WORKDIR /app
COPY . .

# Install quadro in editable mode from the copied source
RUN pip install --no-cache-dir -e ".[dev]"

# ── Runtime config ─────────────────────────────────────────────────────────────
# These are defaults; override them via docker-compose.yml or .env.
# LLM_PROVIDER controls whether the entrypoint runs Ollama health/pull logic.
ENV LLM_PROVIDER=ollama \
    NEWSROOM_TARGET=5 \
    NEWSROOM_CYCLES=500 \
    NEWSROOM_CHOREOGRAPHY="" \
    OLLAMA_MODEL=gpt-oss:20b \
    OLLAMA_BASE_URL=http://localhost:11434 \
    UI_PORT=8080

# Board UI port
EXPOSE 8080

# ── Entrypoint ─────────────────────────────────────────────────────────────────
COPY examples/microsoft_agent_framework/newsroom/docker/entrypoint.sh /entrypoint.sh
COPY examples/microsoft_agent_framework/newsroom/docker/entrypoint.debug.sh /entrypoint.debug.sh
RUN chmod +x /entrypoint.sh /entrypoint.debug.sh

WORKDIR /app/examples/microsoft_agent_framework/newsroom

ENTRYPOINT ["/entrypoint.sh"]
