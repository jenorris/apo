# Sandbox image for Hermes Agent's Docker code-execution backend
# (terminal.backend: docker — e.g. profiles/lyra/config.yaml): python:3.12-slim
# (matches Lyra's actual base) plus jq and the apo MCP client, so
# execute_code/terminal sessions can run `apo <cmd> | jq ...` pipelines
# directly inside the sandbox.
#
# Build (from repo root):  docker build -t apo-sandbox:latest -f client/Dockerfile .
# Point a profile's terminal.docker_image at apo-sandbox:latest to use it.
# apo reaches a bare-metal apo-engine via
# APO_SERVER_URL=http://host.docker.internal:<port>/mcp — the Hermes Agent
# docker backend already adds host.docker.internal on Linux for every
# container it launches.

FROM golang:1.26-alpine AS apobuild
WORKDIR /build
COPY client/go.mod client/go.sum ./
RUN go mod download
COPY client/*.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /apo .

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/*
COPY --from=apobuild /apo /usr/local/bin/apo
