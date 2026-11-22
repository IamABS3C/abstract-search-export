#!/bin/sh
# Printed when a dev container, codespace or DevPod workspace is attached: what is here and how to start it.
cd "$(dirname "$0")/.." || exit 0
version=$(python3 search-export/abstract_search.py --version 2>/dev/null || echo "Abstract Search & Export")
connected="not set: connect in the app, or add them as secrets"
if [ -n "${ABSTRACT_VENDOR_ACCOUNT_ID:-}" ] && [ -n "${ABSTRACT_API_KEY:-}${ABSTRACT_API_KEY_COMMAND:-}${ABSTRACT_API_KEY_FILE:-}" ]; then
  connected="set for vendor account ${ABSTRACT_VENDOR_ACCOUNT_ID}"
fi
cat <<TXT

  $version is ready.

    Guided terminal   python3 search-export/abstract_search.py
    Browser app       python3 search-export/abstract_search.py --web --port 8765 --no-browser
    Command line      python3 search-export/abstract_search.py --help
    Tests             cd search-export/tests && python3 -m unittest discover -q

  Credentials: $connected.
  The browser app opens through the forwarded port in VS Code desktop. In a browser-only codespace, use the terminal.

TXT
