# Abstract Search & Export as a container: the browser app by default, and the same command line and MCP server.
#
#   docker build -t abstract-search-export .
#   docker run --rm -p 127.0.0.1:8765:8765 -v "$HOME/AbstractExports:/exports" abstract-search-export
#
# Python's standard library only: nothing is installed from the network at build time or at run time.
FROM python:3.13-alpine

ARG VERSION=dev
ARG REVISION=unknown
ARG SOURCE=
LABEL org.opencontainers.image.title="Abstract Search & Export" \
      org.opencontainers.image.description="Search, check and export events from an Abstract Security tenant: browser app, guided terminal, command line and MCP server." \
      org.opencontainers.image.vendor="Abstract Security" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.source="${SOURCE}" \
      org.opencontainers.image.licenses="MIT"

# An unprivileged user, and the one folder the app writes to: exports, saved searches and logs all live under it.
RUN adduser -D -u 10001 -h /home/abx abx \
 && mkdir -p /exports \
 && chown abx:abx /exports

WORKDIR /app
COPY . /app
RUN python -m compileall -q /app

ENV ABX_OUT_ROOT=/exports \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/abx

USER abx
VOLUME ["/exports"]
EXPOSE 8765

# Healthy when the browser app answers. A one-off command or the MCP server has no page to check, so those pass.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request\nif b'--web' not in open('/proc/1/cmdline','rb').read(): sys.exit(0)\nurllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"]

ENTRYPOINT ["python", "/app/abstract_search.py"]
CMD ["--web", "--bind", "0.0.0.0", "--port", "8765", "--no-browser"]
