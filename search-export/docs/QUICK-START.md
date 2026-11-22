# Quick start

Abstract Search & Export finds the events you want in your Abstract tenant and exports all of them, with proof the export is complete. You need Python 3.8 or newer, an Abstract API key and your vendor account ID. Nothing else to install. On Windows, use `py` in place of `python3`.

The same steps, with the same names, in all three ways to use it:

## Browser app

1. Run `python3 abstract_search.py --web`. Keep the terminal window open.
2. Choose **Connect** and enter your vendor account ID and API key. The **Start** tab asks what you want to do; the **Tenant** tab shows what your tenant supports.
3. Build the search in one of three tabs: **Builder** (pick a **Dataset**, add **Filters**, choose a **Time range** and **Where to search**), **KQL** (write the query; a badge says how it runs here) or **Plain English** (describe it; Abstract's assistant writes the KQL for you to check). Or choose **Use a search from Abstract…**.
4. Choose **Preview** (a sample of at most 50 events), then **Validate**. Fix anything marked FAIL and read anything marked WARN. Export stays off until Validate passes.
5. Open the **Export** tab and choose **Start export**. When it is complete, choose **Download** or **Open folder**.

## Guided terminal

1. Run `python3 abstract_search.py`.
2. Follow the steps: **Connect → Dataset → Filters → Time → Fields → Review → Export**. The Review step previews and validates before anything is exported.
3. At the Dataset step you can also **Describe it in plain English** or **Write a KQL query**.
4. Press `m` at any step for the main menu, which has every tool: What this tenant supports, Fieldsets in Abstract, Exports, Saved searches and more.

## Command line

1. Set `ABSTRACT_VENDOR_ACCOUNT_ID` and one source for the key in the environment: `ABSTRACT_API_KEY_COMMAND` (recommended, for example `op read "op://Security/Abstract API/credential"` for 1Password or `security find-generic-password -s abstract-api -a api-key -w` for the macOS keychain), `ABSTRACT_API_KEY_FILE` or `ABSTRACT_API_KEY`.
2. Run the steps:

```
python3 abstract_search.py status
python3 abstract_search.py capabilities
python3 abstract_search.py datasets
python3 abstract_search.py preview  --dataset "<your dataset>" --last 24h
python3 abstract_search.py validate --dataset "<your dataset>" --last 24h
python3 abstract_search.py export   --dataset "<your dataset>" --last 24h --merge
```

Filter with `--where`, for example `--where "user_name !contains example.com"` or `--where dest_port=22,3389`, or give the whole search as KQL with `--kql "events | where …"`. `status` shows which tenant you are connected to; every command that connects also prints a `Connected to …` line first. `kql check "…"` says how a query runs here; `nl "…"` turns plain English into KQL. Every command shows each call it makes to Abstract's API as it happens, the key masked; `--hide-api` turns that off.

To schedule a search, let the tool write the job: `automate cron --from-saved "<name>" --every 1d --out <folder>`, or in the browser app **Export → Run it on a schedule**. Exit code 3 means "Partial: recent data only. Older data was not searched."

## Fieldsets and saved views

The Fieldsets and Saved views tabs (terminal: main menu; command line: `fieldsets`, `views save|update|delete`) build, change and delete them in Abstract. Every change shows exactly what it will do and who it reaches, and waits for your confirm (on the command line, `--yes`). There is no undo: export a bundle first (`content export`). See FIELDSETS-AND-VIEWS.md.

## From an AI assistant

`python3 abstract_search.py mcp` lets an AI assistant on your computer use these same steps. It is read-only unless started with `--allow-writes`, and the events the assistant asks to see go to the assistant and its provider. See MCP.md.

## Where exports go

Each export is a folder under `~/AbstractExports` (on Windows, `C:\Users\<you>\AbstractExports`). It holds the data file, `export.ndjson.gz` by default, a `manifest.json` that records exactly what was exported, and a `run.log` of every step and API call. Errors are also kept in the `logs` folder there. Set `ABX_OUT_ROOT` to use another folder. Every export is a new copy of your data outside Abstract: delete it when you no longer need it.

## More help

The User guide covers the browser app and the terminal side by side. Command line and automation covers every command. The same help is in the browser app (**Help**) and the terminal main menu (**Help**).
