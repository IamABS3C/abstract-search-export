#!/bin/sh
# Abstract Search & Export in Docker, for macOS and Linux.
#
#   ./run-docker.sh                 start the browser app and show the link to open
#   ./run-docker.sh link            show a new link (the app keeps running)
#   ./run-docker.sh logs            follow the app's log
#   ./run-docker.sh stop            stop the app
#   ./run-docker.sh terminal        the guided terminal instead of the browser app
#   ./run-docker.sh mcp             the MCP server on standard input and output, for an AI assistant
#   ./run-docker.sh <command ...>   one command, for example: ./run-docker.sh datasets
#
# Settings, all optional:
#   ABX_IMAGE      the image to run. Default: built from this folder when it holds a Dockerfile, otherwise pulled.
#   ABX_PORT       the port on this computer (default 8765)
#   ABX_EXPORTS    where exports are written on this computer (default ~/AbstractExports)
# To connect without typing in the page, set these in your shell first: ABSTRACT_VENDOR_ACCOUNT_ID,
# ABSTRACT_EXPECT_TENANT, ABSTRACT_API_KEY. Docker keeps them in the container's settings until it is removed
# ("stop" removes it); typing the key in the page, or a mounted secret file, keeps it out of them.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
NAME=${ABX_CONTAINER:-abstract-search}
PORT=${ABX_PORT:-8765}
OUT=${ABX_EXPORTS:-$HOME/AbstractExports}
PUBLISHED=${ABX_PUBLISHED_IMAGE:-ghcr.io/iamabs3c/abstract-search-export:latest}
IMAGE=${ABX_IMAGE:-}

say() { printf '%s\n' "$*"; }
die() { say "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "Docker was not found. Install Docker Desktop (https://www.docker.com/products/docker-desktop/), start it, then run this again."
docker info >/dev/null 2>&1 || die "Docker is installed but not running. Start Docker Desktop (or the Docker service), wait until it says it is running, then run this again."

running() { [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)" = "true" ]; }

image() {
  [ -n "$IMAGE" ] && return 0
  if [ -f "$HERE/Dockerfile" ] && [ -f "$HERE/abstract_search.py" ]; then
    IMAGE="abstract-search-export:$(tr -d ' \r\n' < "$HERE/VERSION")"
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
      say "Building the image from this folder (about a minute, the first time only)..."
      docker build -q -t "$IMAGE" "$HERE" >/dev/null
    fi
  else
    IMAGE=$PUBLISHED
    say "Getting $IMAGE..."
    docker pull -q "$IMAGE" >/dev/null
  fi
}

# The flags every container gets: this computer's port only, the exports folder, no privileges, nothing written
# anywhere else. On Linux the container runs as you, so the exported files are yours.
common() {
  set -- --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
    -v "$OUT:/exports" \
    -e ABSTRACT_VENDOR_ACCOUNT_ID -e ABSTRACT_EXPECT_TENANT -e ABSTRACT_API_KEY -e ABSTRACT_BASE_URL
  if [ "$(uname -s)" = "Linux" ]; then set -- "$@" --user "$(id -u):$(id -g)"; fi
  COMMON=$(printf '%s\n' "$@")
}

last_link() { docker logs "$NAME" 2>&1 | grep -o 'http[s]*://[^ ]*?code=[A-Za-z0-9_-]*' | tail -n 1; }

show_link() {
  link=$(last_link)
  [ -n "$link" ] || die "The app did not print a link. See its log: docker logs $NAME"
  say ""
  say "  Abstract Search & Export is running. Open this link in your browser:"
  say ""
  say "    $link"
  say ""
  say "  The link works once. For another one: $0 link      To stop the app: $0 stop"
  say "  Exports are written to: $OUT"
  say ""
  if [ "${ABX_NO_BROWSER:-}" != "1" ]; then
    if command -v open >/dev/null 2>&1; then open "$link" >/dev/null 2>&1 || true
    elif command -v xdg-open >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then xdg-open "$link" >/dev/null 2>&1 || true
    fi
  fi
}

new_link() {
  running || die "The app is not running. Start it with: $0"
  before=$(last_link)
  docker exec "$NAME" python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8765/api/relink', data=b'{}', headers={'Content-Type': 'application/json'}), timeout=5)" >/dev/null
  i=0
  while [ "$i" -lt 20 ] && [ "$(last_link)" = "$before" ]; do sleep 0.3; i=$((i + 1)); done
  show_link
}

start() {
  if running; then say "The app is already running."; new_link; return 0; fi
  image
  mkdir -p "$OUT"
  docker rm "$NAME" >/dev/null 2>&1 || true
  common
  # shellcheck disable=SC2086
  ( IFS='
'; docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8765" -e "ABX_WEB_URL=http://127.0.0.1:$PORT/" $COMMON "$IMAGE" >/dev/null )
  i=0
  while [ "$i" -lt 60 ]; do
    [ -n "$(last_link)" ] && break
    running || { docker logs "$NAME" 2>&1 | tail -n 20 >&2; die "The app stopped while starting. Its last messages are above."; }
    sleep 0.5; i=$((i + 1))
  done
  show_link
}

once() {            # one command in its own container, removed afterwards. $1: terminal, or cli followed by the command
  mode=$1; shift
  image
  mkdir -p "$OUT"
  common
  tty=-i; [ -t 0 ] && [ -t 1 ] && tty=-it
  if [ "$mode" = terminal ]; then
    # shellcheck disable=SC2086
    ( IFS='
'; exec docker run --rm $tty $COMMON --entrypoint python "$IMAGE" /app/abstract_search.py )
  else
    # shellcheck disable=SC2086
    ( IFS='
'; exec docker run --rm $tty $COMMON "$IMAGE" "$@" )
  fi
}

case "${1:-start}" in
  start) start ;;
  link) new_link ;;
  logs) exec docker logs -f "$NAME" ;;
  stop) if running; then docker rm -f "$NAME" >/dev/null; say "Stopped."; else docker rm "$NAME" >/dev/null 2>&1 || true; say "The app was not running."; fi ;;
  terminal) once terminal ;;
  -h|--help|help) sed -n '2,/^set -eu/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//' ;;
  *) once cli "$@" ;;
esac
