#!/bin/bash
# Abstract Search and Export: double-click to open the browser app.
# Keep this window open while you use the app. Close it, or press Ctrl+C, to stop.
cd "$(dirname "$0")" || exit 1
if [ -f app/abstract_search.py ]; then
  cd app || exit 1
fi

PY=""
for c in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
    PY="$c"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "Abstract Search and Export needs Python 3.8 or newer, and it was not found."
  echo "Install it from https://www.python.org/downloads/ and then double-click Start-Mac.command again."
  read -r -p "Press Enter to close this window. " _
  exit 1
fi

if [ "$#" -eq 0 ]; then
  set -- --web
fi
"$PY" abstract_search.py "$@"
status=$?
if [ "$status" -ne 0 ] && [ "$status" -ne 130 ]; then
  read -r -p "The app stopped with an error (shown above). Press Enter to close this window. " _
fi
exit "$status"
