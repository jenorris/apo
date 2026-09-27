# Apo MCP server (apo_engine.mcp.server, installed as the `apo-mcp` console
# script), streamable-HTTP transport.
#
# Vault data is NOT baked into this image — bind-mount it (see docker-compose
# below, or `docker run -v`). The index (~/.apo/index-*.db) is regenerable
# from the vault's Markdown at any time, but mount a volume over the apo
# user's home directory too so it isn't rebuilt from scratch on every
# container restart.
#
# Build:  docker build -t apo-engine:0.27.0 .
# Run:    docker run -p 8878:8878 \
#           -v /path/to/vault:/vaults/myvault:rw \
#           -v apo-state:/home/apo \
#           -e APO_COLLECTION_ROOT=/vaults \
#           -e APO_DEFAULT_VAULT=myvault \
#           -e APO_EMBED_BACKEND=ollama -e APO_OLLAMA_URL=http://host.docker.internal:11434 \
#           apo-engine:0.27.0
#
# Optional Google-OIDC auth (apo_engine.mcp_auth, see
# docs/systemd/apo-desma-mcp.service.example for what each env var does):
# mount a real mcp-auth.json and add -e APO_MCP_AUTH=google
# -e APO_MCP_AUTH_CONFIG=/config/mcp-auth.json -v /path/to/mcp-auth.json:/config/mcp-auth.json:ro
# — omit both and the server is unauthenticated, same as every existing
# stdio/loopback deployment (Claude Code, Cursor, each Hermes gateway).

FROM python:3.12-slim AS builder
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /build
COPY engine/pyproject.toml ./engine/pyproject.toml
COPY engine/src ./engine/src
RUN pip install --no-cache-dir "./engine[mcp]"

FROM python:3.12-slim
RUN useradd --create-home --uid 1000 apo
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
RUN chown -R apo:apo /app
ENV PATH="/opt/venv/bin:$PATH"
USER apo

EXPOSE 8878
ENV APO_MCP_TRANSPORT=http \
    APO_MCP_HOST=0.0.0.0 \
    APO_MCP_PORT=8878

ENTRYPOINT ["apo-mcp"]
