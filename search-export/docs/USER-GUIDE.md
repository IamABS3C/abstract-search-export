# User guide

This tool finds the events you want and exports all of them, with proof the export is complete. It is a companion to search in Abstract, not a replacement.

The browser app and the guided terminal offer the same steps under the same names. This chapter describes them side by side; the table of every step in all three is in Start here. The command line has the same steps too: see CLI.md.

## Concepts

- **Dataset:** one source of events, the name of an integration configuration such as your firewall or identity feed. Use the full name; case does not matter, but a partial name matches nothing.
- **Filter:** a condition on a field, such as `user_name` is one of `alice, bob`, or `user_name` does not contain `example.com`.
- **At least one of:** a group of conditions of which at least one must hold. It comes from a search from Abstract (or `--where`) that mixes `and` and `or`.
- **Match:** with more than one filter, either every filter must match (the default) or at least one must. The dataset always applies.
- **Time range:** last 15 minutes to last 30 days, or a custom UTC start and end. Relative ranges end 10 minutes ago, so late events have settled.
- **Where to search:** recent data, the archive, or both (see below).
- **Fields:** the columns you export. `@timestamp` and `id` are always included. Every field of every event exports complete events instead.
- **Validate:** runs every check an export runs, without exporting. See VALIDATION.md.
- **Fieldset:** a named list of columns, kept in Abstract, that saved views use.
- **Saved view:** a named search with its time range and columns, kept in Abstract.

## Connect

Enter your vendor account ID and API key. Leave the API URL as it is unless Abstract gave you another one. The key stays in memory for this session only; it is never saved. If `ABSTRACT_API_KEY` and `ABSTRACT_VENDOR_ACCOUNT_ID` are set in the environment, both front ends connect by themselves.

In the browser app, the top bar then shows your tenant, Connect becomes Switch tenant, and Disconnect removes the key from memory.

## What this tenant supports

Right after you connect, the tool runs a few small read-only searches to find out what your tenant supports. It takes a few seconds and nothing is stored.

| Item | What it means when available |
|---|---|
| KQL searches | A search copied from Abstract that cannot be converted into filters can run as KQL, as written. |
| Archive (older data) | Older events can be searched and exported. |
| Complete-event search | Complete events (every field) can be exported. |

Each item reads available, not available or unknown. In the browser app it is the **Tenant** tab; **Check again** runs the check again. In the terminal it is printed after connecting, and the main menu item **What this tenant supports** checks again. The command line has `capabilities`.

## The Start screen

In the browser app, the **Start** tab asks what you want to do (find events, ask in plain English, write KQL, export every match, build a fieldset, manage saved views, use a search from Abstract) and opens the right place.

## Build the search

There are three ways in, and all three make the same search, checked against your tenant before anything runs: the **Builder** (below), **KQL** and **Plain English**. In the browser app they are tabs above the search panel. In the terminal's guided flow, the Dataset step asks how you want to start; the main menu has Describe what you want in plain English and Write or paste a KQL query.

- **Dataset:** pick from the suggestions.
- **Filters:** add a filter, pick a field, then a condition that suits the field's type, then the values. The tool asks your tenant for each field's type and offers every condition Abstract has for it: text, lists, IP addresses, numbers and true/false. Text ignores upper and lower case unless you choose **Match case** (the terminal asks "match upper and lower case exactly?"). For a "not" condition, **or has no value** (terminal: "also keep events where … has no value?") keeps events where the field is empty, which "not" conditions otherwise leave out, as Abstract's search does. See SEARCH-REFERENCE.md.
- **Time range:** a preset, or a custom UTC start and end, picked with a date and time picker (browser app) or from a calendar (terminal), or typed.
- **Fields to export:** Learn fields samples recent events of the dataset (10–30 seconds) and shows which fields it uses and how often. Use suggested fields picks the useful ones. Every field of every event exports complete events.

## KQL and plain English

- **KQL:** write the query, starting with `events`. The browser app suggests operators, field names and a field's common values as you type. Before anything runs, the tool asks Abstract's own KQL check and says how the query runs here: **Exact export** (converted into filters and re-checked on every event), **Runs as KQL** (on Abstract, as written), **KQL result table** (`summarize`, `extend` and the like: preview the table and save it as CSV or JSON) or **Cannot run** (with the reason and Abstract's suggestion).
- **Plain English:** describe what you want. Abstract's KQL assistant writes the query and explains it in one line, and the same check runs. Nothing searches until you use the query and choose Preview. Read it first: the assistant can pick a different field or value than you meant.

SEARCH-REFERENCE.md has the details, under "How a KQL or plain-English search is checked before it runs".

## Every vs at least one filter

With more than one filter, choose **Every filter must match** (the default) or **At least one filter must match**. The dataset always applies.

- Browser app: **With more than one filter**, under the filters.
- Terminal: the **Match: …** item in the Filters step, or main menu, Build or edit the search, Filters.
- Command line: `--match all` or `--match any`.

## Where to search

One choice, with two options. In the browser app it is **Where to search**, under Time range, with a one-line hint. In the terminal it is asked in the Time step, and in the main menu under Build or edit the search, Where to search.

| Option | What it does |
|---|---|
| Automatic (recommended) | Recent data, plus the archive for anything older (that part runs as archive jobs). |
| Recent data only | Fastest. Older events kept in the archive are not included, and the result says so. |

Recent data keeps your tenant's most recent days (31 on many tenants); the archive keeps older events (365 days on many tenants). Searching the archive runs an **archive job** on your tenant: it takes minutes for a day and roughly 45 minutes for a month of data on a busy tenant. The browser app and the terminal show the job's progress. Archive jobs appear in your tenant's archive job list under the key's user, like the ones you run in Abstract, and your key needs permission to run them.

If your tenant has no archive, the browser app's hint says so: only recent data is searched.

## Use a search from Abstract

Start from one of your tenant's saved views, or paste a search copied from Abstract's search box.

Every saved view is listed: views with a KQL search, views built with Abstract's filter builder, replay views (the search their replay ran), and views that hold only columns and a time range. A view's own time range and columns come with it. A view with only columns and a time range loads them, and you add a dataset or a filter.

- Browser app: **Use a search from Abstract…**, then **Load views** and pick a view (grouped by kind), or paste a search and choose **Convert**. **Check every view** lists each view as ready, columns only, or not convertible, with the reason. **Save all as saved searches** copies every view that loads into your saved searches. **Use this search** fills in the left panel.
- Terminal: at the Dataset step, choose **Use a search from Abstract**. The main menu item **Saved views from Abstract** lists every view with its kind; its first items **Check every view** and **Save every view as a saved search** do the same as in the browser app.
- Command line: `views` lists them all, `views --save` saves them all, `export --from-view "NAME"` runs one.

You always see **This will run as …** before anything runs. Saved views are read live and never stored unless you save them; saving again updates the copies and never replaces a saved search of your own. Details: SEARCH-REFERENCE.md, Use a search from Abstract.

## KQL from a search from Abstract

A search from Abstract is converted into a dataset and filters whenever it can be: every operator converts, as do `or`, `not(…)` and brackets. If it cannot be converted (for example it uses a function such as `strlen()`) and What this tenant supports shows KQL searches as available, it runs as KQL, as written, and shows as "runs as KQL: …". Otherwise it is refused, with the reason.

- Validate shows a "Filter proof: KQL" row, and every export of it checks first that the tenant still applies KQL.
- A search that runs as KQL reads recent data only; use a dataset and filters to reach the archive.

## Preview, Validate, Export

- **Preview** shows how many events match and the first events: 50 in the browser app, 25 in the terminal. It is a sample, not the export.
- **Validate** runs every check an export runs: fields, a proof for each filter, a second independent count, the export plan and an estimated time. Fix anything marked FAIL. VALIDATION.md explains each row. Ctrl+C in the terminal, or Stop validating in the browser app, stops it; nothing is exported.
- **Export** writes every matching event to `~/AbstractExports/<folder>`. Choose NDJSON.gz (default), NDJSON or CSV. The browser app combines the slice files into one file when Combine into one file when complete is ticked (the default); the terminal always combines them.

In the browser app, Export is enabled once Validate passes for exactly this search. Changing the search or the tenant needs a new validation; changing the file format does not. A validation is honoured for 15 minutes, and the export then uses exactly the window Validate checked. In the terminal, the Review step previews and validates before the Export step, and an unchanged search reuses its checks for 10 minutes.

If nothing matched, both front ends say **Nothing matched** and offer to export anyway, which writes an empty file after you confirm.

Closing the browser tab does not stop an export: it runs in the terminal that started the app. Closing that terminal, or the computer sleeping, stops it. Resume then continues.

## Exports

Every export on this computer, with its status: COMPLETE, PARTIAL (recent data only), INCOMPLETE, FAILED, RUNNING or Planned, not started.

- **Resume** continues an unfinished export from the first slice that did not finish.
- **Delete** removes a finished or stopped export folder after you confirm. It never deletes a running export.
- **Open folder** shows the files. For multi-GB files, use it instead of Download, which makes a second copy.

## Delete an export in the terminal

Main menu, **Export history: list, resume, open folder**, pick the export, then **Delete this export**, and confirm. The item is not offered for a running export. On the command line: `exports --delete NAME`.

## Fieldsets and saved views in Abstract

The tool can create, change and delete fieldsets and saved views in Abstract. Back up first with a bundle (`content export`): there is no undo. Every change opens a review that lists exactly what will be created, changed or removed, the request that will be sent, and who the change reaches: a new private fieldset only you see in Abstract, a new saved view everyone on the tenant who can open saved views sees, and any update or delete changes the item for everyone who can see it. Nothing happens until you confirm, and if someone else changed the item after your review the change is refused so you can review again. Each change is recorded in `logs/changes-YYYY-MM-DD.log`.

- **Fieldsets.** Browser app: the **Fieldsets** tab, with a builder that starts from a dataset's fields and how often each is filled. Terminal: main menu, **Fieldsets in Abstract**. Command line: `fieldsets`. A fieldset holds stored fields only, never a column computed by KQL.
- **Saved views.** Browser app: the **Saved views** tab (Save the current search as a view, Update, Delete). Terminal: main menu, **Saved views from Abstract**, then **Save the current search as a view, or change or delete a view**. Command line: `views save`, `views update`, `views delete`.

FIELDSETS-AND-VIEWS.md has the details.

## Platform endpoints

The browser app's **Tenant** tab lists every Abstract API endpoint the tool can use, what it is for, whether the tool needs it, and what this session saw. On the command line: `endpoints`.

## Saved searches

Save the current search under a name, load it later, or delete it. Saved searches keep the dataset, filters, match, time range, where to search, fields and format. They are JSON files in `~/AbstractExports/saved-searches/` and never contain your API key. They do contain your dataset name and filter values. A few example searches are added the first time.

## Copy as command line

To run a search on a schedule, build and validate it here, then turn it into a command line:

- Browser app: **Export** tab, **Run it on a schedule**, **Copy as command line**. The command is shown and copied to the clipboard.
- Terminal: main menu, **Saved searches**, **Show the current search as a command line (for scheduling)**.

The output is an `export ABSTRACT_EXPECT_TENANT=…` line and the command. A search made of a dataset and "is one of" filters becomes `--dataset`, `--where`, `--match` and `--storage`. Any other search (other operators, exact case, "at least one of" groups, or a search that runs as KQL) becomes `export --from-saved "<name>"`, which runs your saved copy: save the search first. Set `ABSTRACT_API_KEY` and `ABSTRACT_VENDOR_ACCOUNT_ID` in the environment before you run it. On Windows PowerShell use `py` instead of `python3`, and keep the quotes. See CLI.md, Scheduling.

## API calls and logs

Every call the tool makes to Abstract's API is part of its live output, whatever you do:

- Browser app: **Live API calls**, under every tab, shows each call as it happens; during an export the calls are in the Technical log too. The **API calls** tab lists them all: open one to see its headers (key masked), the request it sent, and **Copy curl** for macOS / Linux, Windows PowerShell or Windows Command Prompt.
- Terminal and command line: each call is printed as it happens, starting with the headers every call carries, the key shown as `********`. `--hide-api` turns this off for one command; `--quiet` does too unless you add `--show-api`; `ABX_SHOW_API=0` turns it off everywhere.

The API key is never shown, printed or written: it always appears as `********`. The events that come back are not kept.

To run one of the calls yourself with **Copy curl**:

1. On the **API calls** tab, choose your terminal at the top: **macOS / Linux terminal**, **Windows PowerShell** or **Windows Command Prompt**.
2. Open a call. For a call that sends a request body, choose **Download request.json** and put the file in the folder where you will run curl.
3. Set `ABSTRACT_API_KEY` in that terminal, as for the command line.
4. Choose **Copy curl** and paste it into the terminal. The command reads the key from `ABSTRACT_API_KEY`; it never contains it.

Every error the tool shows is also written to `logs/abstract-search-YYYY-MM-DD.log` in your exports folder, with any call that failed; the message ends "kept in the log: …". Logs older than 30 days are deleted. Each export folder also has `run.log`: every step and API call of that export, including after a resume. Send the log, and for an export its `run.log` and `manifest.json`, when you ask for help. They contain your search, never your key.

## The terminal main menu

The main menu holds every tool the terminal has. Press `m` from any guided step (or type `:m` in a text prompt), or start with `python3 abstract_search.py --menu`. In plain mode, with piped input, or in a console without arrow keys, it opens first.

| # | Item |
|---|---|
| 1 | Guided search & export (step by step, recommended) |
| 2 | Connect / switch tenant |
| 3 | What this tenant supports (KQL, archive, complete events) |
| 4 | Build or edit the search: Dataset, Filters, Time range, Where to search, Fields to export, Use a search from Abstract, Start a new search |
| 5 | Describe what you want in plain English (Abstract's assistant writes the KQL; you check it first) |
| 6 | Write or paste a KQL query (checked live: exact export where it converts) |
| 7 | Preview results (first 25 events) |
| 8 | Validate the search (fields, filter proof, cross-check, export plan) |
| 9 | Export all results (reads Watch the running export while one runs) |
| 10 | Export history: list, resume, open folder |
| 11 | Saved views from Abstract (every view on this tenant, read live; save, change or delete one) |
| 12 | Saved searches (yours, on this computer) |
| 13 | Fieldsets in Abstract (build, change and save column lists) |
| 14 | Views and fieldsets as code (back up, compare, restore, copy to another tenant) |
| 15 | Run a search on a schedule (cron, launchd, systemd, Windows, GitHub Actions, containers) |
| 16 | Browse fields of a dataset or of the tenant |
| 17 | Learn the fields of every dataset (live, kept in memory only) |
| 18 | List every dataset |
| 19 | Open the browser app |
| 20 | Help |
| 21 | Quit |

## Terminal keys

Up and down arrows move, Enter or the right arrow chooses, left arrow, Esc or b goes back, m opens the main menu, q quits. Digits jump to an item. In a text prompt, type `<` to go back or `:m` for the main menu. Ctrl+C during an export asks whether to stop it.

The API key is typed without echo. With no terminal to read it from safely (piped input, or `ssh` without `-t`), the tool refuses to ask: set `ABSTRACT_API_KEY`, or use `ssh -t`. In Git Bash (mintty) on Windows, run `winpty python abstract_search.py`.

Where arrow keys are not available, the main menu opens first as numbered choices; choose 1 for the guided flow. `ABX_PLAIN=1` gives plain numbered prompts with no colour or redrawing, which suits screen readers. `NO_COLOR=1` turns colour off.

## Fields of every dataset

The tool can learn which fields each dataset uses, and how often each has a value, by sampling recent events (about 10–30 seconds per dataset). The result is kept in memory for this session only, never on disk, and holds field names and coverage, never event values.

- Browser app: the Fields tab, Fields of every dataset, Learn fields for every dataset.
- Terminal: main menu, Learn the fields of every dataset.
- Command line: `fields --all`, or `fields --dataset NAME` for one dataset.

## What this tool does not do

- It does not delete, mask, redact or stop the collection of any data in Abstract. Every export is a new copy of your data outside Abstract; delete exports when you no longer need them.
- No charts or detections. Counts and groupings come from a KQL `summarize` saved as a result table, not from the export.
- No functions in filters (such as `strlen()`): write the search in KQL; it runs as KQL where your tenant supports it.
- No changes to your tenant other than fieldsets and saved views, and none of those without your confirm.
- No exporting every event in the tenant: a search needs a dataset or a filter.

For large exports and the limits of each front end, see EXPORTS.md, Large exports.
