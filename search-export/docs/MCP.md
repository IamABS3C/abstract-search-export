# Use the tool from an AI assistant (MCP)

MCP mode lets an AI assistant use this tool for you. The assistant can see what your tenant supports, find datasets and fields, check and run KQL, turn a question into KQL, preview and validate searches, start and follow exports, and read, build and change saved views and fieldsets. It works with any assistant that supports the Model Context Protocol (MCP) and can start a program on your computer: Claude Desktop, Claude Code, GitHub Copilot in VS Code, Cursor and others.

The assistant talks only to this tool, running on your computer. The tool talks to Abstract with your API key, exactly as the command line does. The key is never sent to the assistant, never a tool argument and never part of a result.

**The tool is read-only for an assistant unless you turn changes on.** By default the assistant can search, check, preview, export and plan a change, but it cannot change anything in your tenant. To let it create, change or delete saved views and fieldsets, start the tool with `--allow-writes` (add it to the arguments in the assistant's settings) or set `ABSTRACT_MCP_ALLOW_WRITES=1` in its environment. This is a setting you make, not something the assistant can ask for. The tool cannot check that you typed "yes" in the chat; only the person who starts the tool decides whether changes are allowed.

**Use a read-only API key for an assistant** unless you need it to make changes. Then even a server started with `--allow-writes` cannot change anything, because Abstract refuses the change.

## How it works

```
python3 abstract_search.py mcp                  the assistant starts this program and talks to it over standard input and output
python3 abstract_search.py mcp --http 8765      a local web address instead, for assistants that connect to a URL
python3 abstract_search.py mcp --allow-writes   also let the assistant change saved views and fieldsets
```

When the tool starts it prints one line to standard error saying whether changes are on, and how many rows a preview returns.

You do not normally run either yourself: you tell your assistant how to start the tool, once, in its settings (below). On Windows, use `py` in place of `python3` if `python3` is not on your PATH.

## Credentials

The tool reads the same three settings as the command line (see [CLI.md](CLI.md), Credentials), from the environment the assistant starts it with:

| Setting | Value |
|---|---|
| `ABSTRACT_VENDOR_ACCOUNT_ID` | Your vendor account ID. |
| `ABSTRACT_EXPECT_TENANT` | The vendor account ID you expect. The tool refuses to work if the key belongs to another tenant. |
| `ABSTRACT_API_KEY_COMMAND` | Recommended: a command that prints the key from your secret manager or keychain (examples in the README, Where the API key lives). The tool runs it once, without a shell, when it connects. |
| `ABSTRACT_API_KEY` | Only if you cannot use a key command. The assistant's settings file then holds the key in plain text, so protect that file. |
| `ABSTRACT_BASE_URL` | Only if Abstract gave you an API address other than `https://api.abstractsecurity.app`. |
| `ABSTRACT_MCP_ALLOW_WRITES` | `1` lets the assistant change saved views and fieldsets (the same as `--allow-writes`). Leave it out to keep the tool read-only. |
| `ABSTRACT_MCP_MAX_ROWS` | The most rows a preview returns to the assistant: 50 when not set, never more than 500. Rows hold your events, and every row is sent to the assistant's service. |

Exports go to `~/AbstractExports` unless you also set `ABX_OUT_ROOT`.

Ask the assistant to "check the Abstract connection" first. It calls `status`, which names the tenant (or says exactly which setting is missing), says whether changes are on, and gives the row limit.

## Claude Desktop

Open Claude Desktop, then Settings, Developer, Edit Config. That opens `claude_desktop_config.json`:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add the tool under `mcpServers`, with the full path to `abstract_search.py` in the `app` folder of the kit. To see the full path on a Mac, drag `abstract_search.py` into a Terminal window; on Windows, Shift + right-click it and choose Copy as path.

```json
{
  "mcpServers": {
    "abstract-search": {
      "command": "python3",
      "args": ["/full/path/to/abstract-search-export-0.5.0/app/abstract_search.py", "mcp"],
      "env": {
        "ABSTRACT_VENDOR_ACCOUNT_ID": "AbC123xYz0",
        "ABSTRACT_EXPECT_TENANT": "AbC123xYz0",
        "ABSTRACT_API_KEY_COMMAND": "security find-generic-password -s abstract-api -a api-key -w"
      }
    }
  }
}
```

On Windows, use `"command": "py"` and write each backslash in the path twice, for example `"C:\\Tools\\abstract-search-export-0.5.0\\app\\abstract_search.py"`. Quit and reopen Claude Desktop. Claude Desktop is not available for Linux; use Claude Code there.

## Claude Code

From any folder:

```
claude mcp add abstract-search -s user \
  -e ABSTRACT_VENDOR_ACCOUNT_ID=AbC123xYz0 \
  -e ABSTRACT_EXPECT_TENANT=AbC123xYz0 \
  -e ABSTRACT_API_KEY_COMMAND='op read "op://Security/Abstract API/credential"' \
  -- python3 /path/to/abstract-search-export-0.5.0/app/abstract_search.py mcp
```

`-s user` makes it available in every project. Check it with `claude mcp list`.

## GitHub Copilot in VS Code

Create `.vscode/mcp.json` in your workspace (or run MCP: Add Server from the Command Palette):

```json
{
  "servers": {
    "abstract-search": {
      "type": "stdio",
      "command": "python3",
      "args": ["/path/to/abstract-search-export-0.5.0/app/abstract_search.py", "mcp"],
      "env": {
        "ABSTRACT_VENDOR_ACCOUNT_ID": "AbC123xYz0",
        "ABSTRACT_EXPECT_TENANT": "AbC123xYz0",
        "ABSTRACT_API_KEY_COMMAND": "op read \"op://Security/Abstract API/credential\""
      }
    }
  }
}
```

Do not commit this file if it holds a key. Start the server from the `mcp.json` editor or the MCP: List Servers command, and use Copilot Chat in agent mode.

## Cursor and other assistants

Most other assistants take the same three things: a command (`python3`, or `py` on Windows), arguments (the full path to `abstract_search.py`, then `mcp`) and environment variables. In Cursor, add them under `mcpServers` in `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project), in the same shape as the Claude Desktop example.

## ChatGPT and other cloud assistants

ChatGPT's connectors reach MCP servers on the internet over HTTPS. They cannot start a program on your computer, and they cannot reach `127.0.0.1`. This tool runs only on your computer and only listens on `127.0.0.1`, so ChatGPT cannot use it directly. Putting it on the internet would expose a tool that holds your Abstract key, and this guide does not cover that. Use an assistant that runs on your computer, or use the browser app.

## The local web address (`--http`)

For an assistant on your computer that connects to a URL instead of starting a program:

```
python3 abstract_search.py mcp --http 8765
```

It prints the address, `http://127.0.0.1:8765/mcp`, and a bearer token once. Give both to the assistant: it sends the token as `Authorization: Bearer <token>`. The server listens on `127.0.0.1` only, refuses requests without the token, and refuses requests from web pages on other sites. The token changes every time you start it. Press Ctrl+C to stop it.

## What the assistant can do

| Tool | What it does |
|---|---|
| `status` | The connected tenant (or what to set), whether changes are on, and the row limit. |
| `capabilities` | What the tenant supports, checked live: KQL, the archive, complete events. |
| `datasets`, `fields`, `field_values` | Datasets with volume; the field catalogue, or a dataset's fields and how often each is filled; the common values of a field. |
| `kql_check` | How a KQL query would run, without running it: Abstract's own check, then exact export, native KQL, a result table, or refused with the reason. |
| `nl_to_kql` | Abstract's assistant writes KQL from a description. Nothing runs. |
| `kql_preview`, `kql_save` | Run KQL for a preview (up to `ABSTRACT_MCP_MAX_ROWS` rows, 50 by default); save a result table as CSV or JSON. |
| `preview`, `validate` | The same preview and checks as the browser app. |
| `export_start`, `export_status`, `export_cancel`, `exports_list` | Run an export in the background, follow it, stop it, list exports. |
| `views_list`, `view_import` | Saved views, and a view loaded as a search. |
| `fieldsets_list`, `fieldset_get` | Fieldsets on the tenant. |
| `content_export`, `content_diff` | Export saved views and fieldsets as a bundle (`abstract.content/v1`); compare a bundle with the tenant. Read only. |
| `automation_recipe` | The files that run a search on a schedule (cron, launchd, systemd, Windows Task Scheduler, GitHub Actions, containers), the same as `automate` on the command line. Nothing is installed. |
| `view_plan`, `fieldset_plan`, `content_plan` | Plan creating, updating or deleting saved views and fieldsets (one, or a whole bundle). Works when the tool is read-only. |
| `apply_change` | Make a planned change. Refused unless the tool was started with `--allow-writes`. |
| `endpoints` | The Abstract API endpoints the tool uses and what it saw from each. |

The tool also offers one resource and two ready-made prompts:

| Name | What it is |
|---|---|
| `abstract://fields` | The connected tenant's field catalogue. Listed once the tool is connected. |
| `exact-export` | A prompt that walks the assistant through a validated export of every matching event, from a description of what to export. |
| `question-to-kql` | A prompt that drafts KQL for a question, checks it, and previews it only after you agree. |

## A worked example

What an assistant did with the tool against a demo tenant ("Example Corp", invented data), asked to check a query, preview it, and create a fieldset. Abbreviated; the tool's answers are quoted as they came back.

1. `status` → connected to Example Corp (`DEMO0EXAMPLE`), `"writes": false`, `"max_rows": 50`.
2. `kql_check` with `events | where data_stream.dataset =~ "Okta" and event.outcome == "failure"` → `"valid": true`, `"runs_as": "filters"`, "exact export: converted into filters the tool re-checks on every event", readable as: dataset is "Okta" (any case); event.outcome is one of "failure" (exact case).
3. `preview` with the search built from that answer, last 24 hours, 5 rows → `"total": 724`, the window it searched, and the first 5 events.
4. `fieldset_plan` to create "Okta triage" with `@timestamp`, `user_name`, `action`, `event.outcome` → the plan: `POST /v1/streamviewer/field-set`, the changes, and "Only you will see this in Abstract. An API key with views permission on this tenant can still read it."
5. `apply_change` with the plan's token → refused: "Changes to your Abstract tenant are switched off for this MCP server, so nothing was changed. The tool cannot check a "yes" typed in the chat, so writes are turned on only where the server is started …"

The assistant then reported that the search found 724 events and that the fieldset was planned but not created, because the tool was read-only. Started with `--allow-writes`, step 5 creates the fieldset, once you have said yes.

## Changes to your tenant

Reading never changes anything. Creating, changing or deleting a saved view or a fieldset takes two steps, and the second works only when you started the tool with `--allow-writes`:

1. The assistant asks for a plan (`view_plan`, `fieldset_plan` or `content_plan`). Nothing changes. The plan lists exactly what would change: fields added, removed or reordered, the name, the search, the time range, and which saved views a fieldset delete affects.
2. The assistant shows you the plan and asks you. Only after you say yes does it call `apply_change` with the plan's token. On a read-only tool, `apply_change` is refused and the assistant tells you how to turn changes on, or you can make the same change yourself in the browser app, the terminal or on the command line.

A token works once, for ten minutes, and only in the same running tool that made the plan. If someone else changed the item after the plan, the change is refused and the assistant has to plan again. Every change is written to `logs/changes-YYYY-MM-DD.log` in your exports folder (what changed, never the key).

Assistants can make mistakes. Read the plan before you say yes, and read KQL before you let the assistant run it.

## Problems

- **The assistant says it is not connected.** Ask it to call `status` and set what it names. Restart the assistant after changing its settings: the tool reads the environment once when it starts.
- **Nothing happens, or the server will not start.** Run the same command yourself in a terminal: `python3 /path/to/app/abstract_search.py mcp`. It waits silently for input (press Ctrl+C to stop). An error printed instead tells you what is wrong, for example the wrong Python or a wrong path.
- **Where are the details.** The tool writes what it does, including every API call with the key masked, to standard error, which most assistants show in their MCP log. Errors also go to the daily log in the `logs` folder of your exports folder.
