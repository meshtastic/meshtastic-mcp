# Containerized MCP server (core capability — device admin/recorder over serial/TCP).
# The firmware + emulator capabilities need a firmware checkout / the android CLI and
# are not part of this image; mount a firmware tree + set MESHTASTIC_FIRMWARE_ROOT to
# enable build/flash tools.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /src
COPY . .
# .dockerignore excludes .git (small context), so hatch-vcs can't derive the
# version — pass it explicitly: `docker build --build-arg VERSION=$(git describe --tags)`.
ARG VERSION=0.0.0.dev0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}
RUN uv build --wheel

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/meshtastic/meshtastic-mcp"
LABEL org.opencontainers.image.description="Meshtastic MCP server"
LABEL org.opencontainers.image.licenses="GPL-3.0-only"
COPY --from=build /src/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
# Serial device access (USB radios) needs `--device`/`--privileged` at run time;
# TCP nodes (meshtasticd) work without it.
ENTRYPOINT ["meshtastic-mcp"]
