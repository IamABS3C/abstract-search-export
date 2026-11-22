#!/bin/sh
# Start Abstract Search & Export in Docker from a checkout of this repository. Same commands as
# search-export/run-docker.sh: ./run.sh, ./run.sh link, ./run.sh stop, ./run.sh terminal, ./run.sh <command ...>
exec sh "$(dirname "$0")/search-export/run-docker.sh" "$@"
