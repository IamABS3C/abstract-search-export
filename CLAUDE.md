# Abstract Search & Export — working in this repo

This repo holds Abstract Search & Export (`search-export/`), a companion to Abstract for large, automated, API- or
MCP-driven search and export. It replaces nothing in Abstract: use Abstract's own UI for day-to-day search, and this
tool when a job is too big, too repetitive or too scripted for the UI.

## Running it

- Command line: `python3 search-export/abstract_search.py --help` (Python 3.8+, standard library only, nothing to install).
- Browser app: `python3 search-export/abstract_search.py --web --port 8765 --no-browser`. A cloud or self-hosted
  session has no port a person can open: use the MCP tools or the command line there.
- MCP server: `.mcp.json` registers it as `abstract-search` (`python3 search-export/abstract_search.py mcp`). Its tools
  are the same steps as the command line.
- Credentials come from the environment only: `ABSTRACT_VENDOR_ACCOUNT_ID`, `ABSTRACT_EXPECT_TENANT`, and one source
  for the key: `ABSTRACT_API_KEY_COMMAND` (preferred: a secret-manager command that prints the key),
  `ABSTRACT_API_KEY_FILE` (a mounted secret file), `ABSTRACT_API_KEY`, or `ABSTRACT_API_KEY_FROM_NETWORK=1` where the
  environment adds the key to requests itself (a cloud environment's network secret; see
  `docs/claude-environments.md`). Optional `ABSTRACT_BASE_URL` (default `https://api.abstractsecurity.app`).
- When the session starts, `scripts/session-start.sh` prints whether credentials are set and what is missing. If
  something is missing, tell the user which variable or secret to add; never ask them to paste a key into the chat.

## Rules when you use it for someone

1. **Tenant guard.** Every command refuses to run unless `ABSTRACT_EXPECT_TENANT` names the tenant the key belongs to.
   Never work around that check. Say which tenant you are connected to before you search.
2. **Never show the key.** Do not print, echo, log, paste or write the API key or the output of the key command. Do not
   put a key in any file in this repo. The tool masks it in every API trace; keep it that way.
3. **Check KQL first.** Run `kql check` (or the `kql_check` tool) before running a query. Show the user the query and
   how it will run: exact export, runs as KQL, KQL result table, or cannot run (with the reason).
4. **Plain English is a draft.** `nl` / `nl_to_kql` writes a query; show it to the user and run it only after they agree.
5. **Changes to the tenant need a yes.** Fieldsets and saved views are the only things this tool changes. Always plan
   first (`fieldsets … ` or `views …` without `--yes`, or a `*_plan` tool), show the user exactly what will change, and
   apply (`--yes` or `apply_change`) only after an explicit yes for that change. A plan works once and expires.
6. **A zero is a claim.** When a search matches nothing, say which time window and filter it searched; never report
   "no events" without the window.
7. **Exports hold real data.** They go to `~/AbstractExports` (or `ABX_OUT_ROOT`). In a codespace they stay in GitHub's
   cloud until deleted. Do not copy exported events into chat, commits or issues.

## Working on the code

- Offline tests: `cd search-export/tests && python3 -m unittest discover -q` (no network, no key).
- `python3 search-export/tools/coverage_matrix.py` fails when a screen, menu entry, command, MCP tool or help topic
  is not documented and shown. Run it after changing any of them.
- The container build is `search-export/Dockerfile`; `./run.sh` builds and starts it. The single-file build is
  `scripts/build-binary.py`. A tag `v<version>` that matches `search-export/VERSION` publishes both.
- This repository is public and holds no tenant data: no exports, no profiles, no keys, no customer names. Keep
  examples generic (Example Corp, `AbC123xYz0`, `example.com`).
