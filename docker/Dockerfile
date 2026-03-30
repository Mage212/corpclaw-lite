# CorpClaw Agent Sandbox Image
#
# This image is the per-user execution sandbox.
# It runs agent_worker.py which:
#   1. Reads a signed JSON tool-call from stdin (via docker exec)
#   2. Verifies the HMAC signature using CORPCLAW_IPC_SECRET
#   3. Executes the requested tool (file ops / exec_script)
#   4. Returns a signed JSON result to stdout
#
# Security posture:
#   - Runs as non-root 'agent' user
#   - No network access (enforced by docker run --network none in ContainerPolicies)
#   - Read-only root filesystem (rw only on /workspace and /tmp)
#   - All capabilities dropped + seccomp profile applied at runtime
#
# Build:
#   make build-agent
# or:
#   docker build -f docker/Dockerfile -t corpclaw-agent-base:latest .

FROM python:3.12-slim

# Install only system deps needed for tool execution (no LLM libraries)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        bash \
        coreutils \
        findutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy package metadata and source
COPY pyproject.toml ./
COPY src/ ./src/

# Install the package (agent_worker only needs the container/ and extensions/tools/ modules)
# We install without optional deps to keep the image small
RUN pip install --no-cache-dir -e ".[container]" 2>/dev/null || \
    pip install --no-cache-dir -e . && \
    # Remove cache to shrink image
    find /usr/local/lib/python3.12 -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# Create non-root agent user
RUN useradd --no-create-home --shell /bin/false --uid 1001 agent

# /workspace is the bind-mounted user directory (rw via volume)
# /tmp is tmpfs (64m, rw via --tmpfs flag at runtime)
RUN mkdir -p /workspace && chown agent:agent /workspace

USER agent

# Default command — runs as the IPC worker for docker exec
# In practice, docker exec overrides this with:
#   docker exec -i corpclaw_agent_{user_id} python -m corpclaw_lite.container.agent_worker
CMD ["python", "-m", "corpclaw_lite.container.agent_worker"]
