#!/bin/sh
# Runs when a Claude Code session starts in this repository (see .claude/settings.json). It installs nothing: the
# tool is Python's standard library only. It prints a few lines so the session knows the tool is here, whether it can
# reach a tenant, and what is still missing. It never prints a key.
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/..}" || exit 0
command -v python3 >/dev/null 2>&1 || { echo "Abstract Search & Export: python3 was not found (it needs Python 3.8 or newer)."; exit 0; }
version=$(python3 search-export/abstract_search.py --version 2>/dev/null) || { echo "Abstract Search & Export: could not start (it needs Python 3.8 or newer)."; exit 0; }

key="none"
[ "${ABSTRACT_API_KEY_FROM_NETWORK:-}" = "1" ] && key="added by the network"
[ -n "${ABSTRACT_API_KEY_FILE:-}" ] && key="a mounted file"
[ -n "${ABSTRACT_API_KEY_COMMAND:-}" ] && key="a secret-manager command"
[ -n "${ABSTRACT_API_KEY:-}" ] && key="an environment variable"

echo "$version is in search-export/ (MCP server: abstract-search; command line: python3 search-export/abstract_search.py)."
if [ -z "${ABSTRACT_VENDOR_ACCOUNT_ID:-}" ] || [ -z "${ABSTRACT_EXPECT_TENANT:-}" ] || [ "$key" = "none" ]; then
  echo "It is not connected to a tenant yet. Missing:"
  [ -z "${ABSTRACT_VENDOR_ACCOUNT_ID:-}" ] && echo "  ABSTRACT_VENDOR_ACCOUNT_ID (the vendor account ID)"
  [ -z "${ABSTRACT_EXPECT_TENANT:-}" ] && echo "  ABSTRACT_EXPECT_TENANT (the same ID: the tenant guard)"
  if [ "$key" = "none" ]; then
    if [ "${CLAUDE_CODE_REMOTE:-}" = "true" ]; then
      echo "  the API key: add it to this environment as a network secret for api.abstractsecurity.app, then set ABSTRACT_API_KEY_FROM_NETWORK=1 (docs/claude-environments.md)"
    else
      echo "  the API key: ABSTRACT_API_KEY_COMMAND (a secret-manager command), ABSTRACT_API_KEY_FILE or ABSTRACT_API_KEY"
    fi
  fi
else
  echo "Credentials are set for vendor account ${ABSTRACT_VENDOR_ACCOUNT_ID} (key: $key). Check the connection with: python3 search-export/abstract_search.py capabilities"
fi
exit 0
