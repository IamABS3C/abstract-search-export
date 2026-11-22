---
name: abstract-search-export
description: Search, count, preview, validate and export Abstract events at scale, check and run KQL, turn a plain-English question into KQL, and read, build and change saved views and fieldsets in an Abstract tenant, using the Abstract Search & Export tool in this repo (its command line or its MCP tools). Use whenever someone wants every event matching a search exported, a large or archive-range export, a KQL query checked or its result table saved, a question turned into KQL, or Abstract saved views and fieldsets listed, built, copied or changed, even if they only say "pull the logs", "get me everything for this user" or "save this as a view".
---

# Abstract Search & Export

The tool lives in `search-export/`. It is a companion to Abstract for jobs the UI is not built for: exporting every
matching event over long ranges (recent data and the archive), automating exports, and driving search from scripts
or an AI assistant. Prefer its MCP tools (`abstract-search` in `.mcp.json`) when they are connected; otherwise use the
command line, `python3 search-export/abstract_search.py …`, with `--json` for output you read back.

## Before anything

1. Check the connection: `status` tool, or `capabilities`. It names the tenant. If it says a variable is missing,
   tell the user which one to set (`ABSTRACT_VENDOR_ACCOUNT_ID`, `ABSTRACT_EXPECT_TENANT`, and a key source:
   `ABSTRACT_API_KEY_COMMAND`, `ABSTRACT_API_KEY_FILE`, `ABSTRACT_API_KEY`, or in a cloud environment a network
   secret with `ABSTRACT_API_KEY_FROM_NETWORK=1`). Never ask the user to paste a key into the chat.
2. Never print the key or the key command's output.

## Find and check

| Need | MCP tool | Command line |
|---|---|---|
| What the tenant supports (KQL, archive) | `capabilities` | `capabilities` |
| Datasets, fields, values | `datasets`, `fields`, `field_values` | `datasets`, `fields --dataset NAME` |
| How a KQL query runs here | `kql_check` | `kql check "QUERY"` |
| Plain English to KQL (draft only) | `nl_to_kql` | `nl "TEXT"` |

`kql_check` answers one of: **exact export** (converted to filters, every event re-checked), **runs as KQL**
(on Abstract as written), **KQL result table** (`summarize`, computed `project`, `extend` …: preview or save, not an
event export), **cannot run** (with the reason and the platform's suggestion). Show the user the query and that
answer before running anything. A query from `nl_to_kql` is a draft: run it only after the user agrees.

## Run

- Preview: `preview` / `kql_preview` (first rows and the count). Result tables: `kql_save` / `kql save "QUERY"`.
- Validate before a large export: `validate` (every check an export runs, plus an estimated time).
- Export: `export_start` then `export_status`, or `export --kql "QUERY" --last 24h --merge`
  (`--dataset/--where`, `--from-view`, `--from-saved` also work). Exports are resumable: `export --resume FOLDER`.
- When a search matches nothing, report the window and filter it searched. A zero is a claim, not a fact.

## Change saved views and fieldsets (the only tenant writes)

1. Plan: `fieldset_plan` / `view_plan`, or the command without `--yes` (exits 3 with the review).
2. Show the user exactly what will change (added, removed, reordered, before → after, the request line, warnings).
3. Apply only after an explicit yes for that change: `apply_change` with the plan's token, or the same command with
   `--yes`. A plan works once and expires after 10 minutes; if the item changed since the review, it is refused and
   must be reviewed again.

## Guardrails

- The tenant guard (`ABSTRACT_EXPECT_TENANT`) is never bypassed.
- Exported events are real data: do not paste them into chat, commits or issues.
- Help topics: `search-export/docs/` (SEARCH-REFERENCE.md, FIELDSETS-AND-VIEWS.md, MCP.md, CLI.md, DOCKER.md, SECURITY.md,
  TROUBLESHOOTING.md).
