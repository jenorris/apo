# Apo MCP server (apo_engine.mcp.server, installed as the `apo-mcp` console
# script), streamable-HTTP transport.
#
# Vault data is NOT baked into this image — bind-mount it (see docker-compose
# below, or `docker run -v`). The index (~/.apo/index-*.db) is regenerable
# from the vault's Markdown at any time, but mount a volume over the apo
# user's home directory too so it isn't rebuilt from scratch on every
# container restart.
#
# Build:  docker build -t apo-engine .
# Run the MCP server on loopback inside the container. A watcher is a second
# process; this image does not drain the deferred queue.
# Do not set APO_MCP_HOST=0.0.0.0 unless APO_MCP_AUTH is configured.
# Run:    docker run -p 127.0.0.1:8878:8878 \
#           -v /path/to/vault:/vaults/myvault:rw \
#           -v apo-state:/home/apo \
#           -e APO_COLLECTION_ROOT=/vaults \
#           -e APO_DEFAULT_VAULT=myvault \
#           -e APO_EMBED_BACKEND=ollama -e APO_OLLAMA_URL=http://host.docker.internal:11434 \
#           apo-engine
#
# Optional OIDC auth: mount mcp-auth.json and set APO_MCP_AUTH=google
# and APO_MCP_AUTH_CONFIG. See engine/docs/systemd/apo-mcp.service.example.

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
# Install the locked environment into /opt/venv. The system interpreter stays
# the only Python (no uv download), and the project installs non-editable so
# the venv does not point back at /build.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /build/engine
COPY engine/pyproject.toml engine/uv.lock engine/README.md ./
COPY engine/src ./src
RUN uv sync --locked --extra mcp --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --create-home --uid 1000 apo
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
RUN chown -R apo:apo /app
ENV PATH="/opt/venv/bin:$PATH"
USER apo

EXPOSE 8878
ENV APO_MCP_TRANSPORT=http \
    APO_MCP_HOST=127.0.0.1 \
    APO_MCP_PORT=8878

ENTRYPOINT ["apo-mcp"]
