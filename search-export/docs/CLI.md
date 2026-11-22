# Command line and automation

The command line offers the same steps as the browser app and the guided terminal, with the same names: see what your tenant supports, find datasets, preview, validate, export. Use it for scheduled jobs and scripts.

The easiest way to write a command is not to write it at all: build the search in the browser app, then choose **Export → Run it on a schedule → Copy as command line**. In the guided terminal, use the main menu: **Saved searches → Show the current search as a command line (for scheduling)**.

## Credentials

Set two things in the environment: your vendor account ID and one source for the key. Never put the key in a script or a scheduler definition: inject it from your secret store.

| Variable | Value |
|---|---|
| `ABSTRACT_API_KEY` | The API key. Or set `ABSTRACT_API_KEY_COMMAND` instead: a command that prints the key from your secret manager or keychain, run once when the tool connects (see README, Where the API key lives). |
| `ABSTRACT_API_KEY_FILE` | Instead of the two above: a file that holds the key on one line, where a server mounts secrets as files (for example `/run/secrets/abstract_api_key`). |
| `ABSTRACT_VENDOR_ACCOUNT_ID` | Your vendor account ID. |
| `ABSTRACT_EXPECT_TENANT` | Optional. Pins one tenant, for scheduled jobs: every command refuses to run when the key belongs to another. |

macOS or Linux (the key is not echoed and stays out of your shell history):

```
printf 'API key: '; read -rs ABSTRACT_API_KEY; echo; export ABSTRACT_API_KEY
export ABSTRACT_VENDOR_ACCOUNT_ID=AbC123xYz0
```

Windows PowerShell:

```
$env:ABSTRACT_API_KEY = [Net.NetworkCredential]::new('', (Read-Host -AsSecureString 'API key')).Password
$env:ABSTRACT_VENDOR_ACCOUNT_ID = "AbC123xYz0"
```

With a key command instead of a typed key, for example 1Password or the macOS keychain:

```
export ABSTRACT_API_KEY_COMMAND='op read "op://Security/Abstract API/credential"'           # 1Password
export ABSTRACT_API_KEY_COMMAND='security find-generic-password -s abstract-api -a api-key -w'  # or the macOS keychain
```

When the tool connects, the key is checked against `ABSTRACT_VENDOR_ACCOUNT_ID`, and Abstract refuses a key that belongs to another vendor account. `ABSTRACT_EXPECT_TENANT` (or `--expect-tenant ID`) adds a second pin: when it is set, it must match, otherwise the command stops with `TENANT MISMATCH`. Jobs written by `automate` set it.

Every command that connects prints one line first, on standard error, so `--json` output stays clean:

```
Connected to Example Corp (AbC123xYz0) · production
```

For a non-production API it names the API host instead of `production`. `--quiet` hides it.

On Windows, run the tool as `py abstract_search.py` if `python3` is not on your PATH. If Abstract gave you an API address other than `https://api.abstractsecurity.app`, also set `ABSTRACT_BASE_URL`. Exports go to `~/AbstractExports` unless you set `ABX_OUT_ROOT`.

## The one-screen help

`python3 abstract_search.py --help` prints everything on one screen (shortened here):

```
Start here
  python3 abstract_search.py              guided search & export in this terminal (m = main menu, q = quit)
  python3 abstract_search.py --menu       open the terminal's main menu first
  python3 abstract_search.py --web        the browser app  [--port N] [--no-browser]

Commands (the same steps as the browser app; add --json for machine-readable output)
  status                                  which tenant this computer connects to, and where the key comes from
  capabilities                            what this tenant supports: KQL, archive, complete events
  datasets                                every dataset, with 24-hour volume
  views                                   every saved view in Abstract, and how it runs here
  searches                                your saved searches (on this computer)
  exports [--delete NAME]                 the exports on this computer (resume one: export --resume FOLDER)
  fields --dataset NAME | --all           the fields a dataset uses, learned live
  preview  <search>                       how many events match, and the first 25
  validate <search>                       every check an export runs, without exporting
  export   <search> [--format F --merge]  export every matching event to this computer

KQL, plain English, fieldsets and saved views
  kql check|preview|save "QUERY"          how a KQL query runs here; run it; save its result table
  nl "WHAT YOU WANT" [--preview]          plain English to KQL (shown first, never run unless you ask)
  fieldsets [list|show|create|update|delete]   build and keep Abstract fieldsets (column lists)
  views save|update|delete …              save a search as a saved view in Abstract, change or delete one
  endpoints                               the platform endpoints this tool uses and what it saw
  content export|diff|apply DIR          saved views and fieldsets as files: back up, compare, restore, copy
  automate TARGET --from-saved NAME       the files that run a search on a schedule
  mcp [--http PORT]                       let an AI assistant on this computer use this tool over MCP
  Changes to Abstract show exactly what they will do and stop; add --yes to make them.

<search> is one of
  --dataset NAME [--where CONDITION …] [--match all|any]
  --kql "events | where …" | --kql-file FILE
  --from-view "NAME" | --from-file FILE | --from-saved NAME
and a time range: --last 24h | --start … --end …   (UTC)
plus, if you need them: --storage auto|recent   --fields a,b,c | --every-field
```

`python3 abstract_search.py export --help` lists every export option. `--version` prints the version and the kit's build record.

## The commands, in the order you use them

### 1. Check the connection, and what this tenant supports

`status` shows which tenant the environment connects to, the API address, where the key comes from and whether a tenant pin is set. It never shows the key. For `ABSTRACT_API_KEY_COMMAND` it names only the program the command runs.

```
$ python3 abstract_search.py status
  Tenant          Example Corp
  Vendor account  AbC123xYz0
  API             https://api.abstractsecurity.app (production)
  Key from        ABSTRACT_API_KEY_COMMAND (runs op when the key is needed) · checked, never shown or stored
  Pinned          no; the key is checked against the vendor account ID
  Version         0.6.0
```

With `--json` it prints `connected`, `tenant`, `vendor_account_id`, `base_url`, `production`, `key_from`, `pinned_tenant` and `version`. When the vendor account ID or a key source is missing, it says what to set and exits 2.

`capabilities` shows what the tenant and key support:

```
$ python3 abstract_search.py capabilities
  KQL searches           available      searches copied from Abstract can also run as KQL
  Archive (older data)   available      data older than about 31 days is searched in the archive (kept 365 days); that part takes minutes to an hour
  Complete-event search  available      complete events (every field) can be exported
```

Each line reads available, not available or unknown. The check runs a few small read-only searches and stores nothing.

### 2. Find datasets, saved views and fields

```
$ python3 abstract_search.py views
  4 saved views on Example Corp: 2 ready to export, 1 with columns and a time range only, 1 cannot be converted

  ready    Firewall RDP and SSH  (vw-rdp, filter builder)
             • dataset is "Palo Alto Firewall" (any case)
             • dest_port is one of 22, 3389
  ready    Okta failed sign-ins  (vw-okta, KQL)
             • dataset is "Okta" (any case)
             • event.outcome is one of "failure" (exact case)
  columns  Firewall columns  (vw-cols, columns only)
             holds no filter, only columns and a time range: load it, add a dataset or a filter, and save it
  cannot   Admin lookups  (vw-model, filter builder)
             the condition on user_name is a model condition (such as a model lookup), which this tool cannot run
```

- `datasets`: every dataset, busiest first. Use the full name with `--dataset`; upper and lower case do not matter.
- `views`: every saved view in Abstract, read live: views with a KQL search, views built with the filter builder, replay views (their search is read from the replay job), and views that hold only columns and a time range. "ready" shows what it runs as; "columns" can be loaded and needs a dataset or a filter; "cannot" gives the reason. `views --save` saves every view that loads as a saved search on this computer (run it again to update them; a saved search of your own with the same name is kept, and the view is saved as "NAME (view)").
- `export --from-view "NAME"` runs a ready view with its own time range and columns; add `--last 24h` or `--start`/`--end` to use another range.
- `fields --dataset NAME`: the fields a dataset uses and how often each has a value, with a ready-to-paste `--fields` list. `fields --all` does every dataset.
- `searches`: your saved searches, stored on this computer.

### KQL and plain English

```
python3 abstract_search.py kql check 'events | where data_stream.dataset =~ "okta" and event.outcome == "failure"'
python3 abstract_search.py export --kql 'events | where data_stream.dataset =~ "okta" and event.outcome == "failure"' --last 24h --merge
python3 abstract_search.py kql save 'events | summarize n = count() by data_stream.dataset | top 10 by n' --last 24h --format csv
python3 abstract_search.py nl "failed logins from outside the US" --preview --last 24h
```

`kql check` says how the query runs here, without running it: **Exact export** (converted into filters, re-checked on every event), **Runs as KQL** (on Abstract, as written, where your tenant applies KQL), **KQL result table** (`summarize`, `extend` and the like: use `kql preview` or `kql save`, not `export`) or **Cannot run** (with the reason). `--kql` works on `preview`, `validate`, `export` and `views save`. `nl` asks Abstract's KQL assistant to write the query and shows it with the same check; it runs it only with `--preview`. SEARCH-REFERENCE.md explains each answer.

In Windows PowerShell 5.1, use `--kql-file query.txt` instead of `--kql`: PowerShell removes the double quotes inside an argument.

### KQL preview and the endpoint list

```
python3 abstract_search.py kql preview 'events | where data_stream.dataset =~ "okta" | summarize n = count() by event.outcome' --last 24h
python3 abstract_search.py endpoints                     # every platform endpoint the tool uses, and what this run saw
```

`kql preview` runs a query and shows the first rows, or the whole result table for a query that summarizes. `endpoints` lists each Abstract API endpoint the tool uses, whether it is required or optional, and the status this run of the tool saw (ok, off, error or unknown).

### Fieldsets and saved views

```
python3 abstract_search.py fieldsets
python3 abstract_search.py fieldsets show "Okta triage"
python3 abstract_search.py fieldsets create --name "Okta triage" --from-dataset "Okta"
python3 abstract_search.py fieldsets create --name "Okta triage" --from-dataset "Okta" --yes
python3 abstract_search.py fieldsets update "Okta triage" --add ext.client_zone --remove message --yes
python3 abstract_search.py views save --name "Failed Okta logins" --kql 'events | where data_stream.dataset =~ "okta" and event.outcome == "failure"' --last 24h --fieldset "Okta triage" --yes
python3 abstract_search.py views update "Failed Okta logins" --name "Failed Okta logins (24h)" --yes
python3 abstract_search.py views delete "Failed Okta logins" --yes
python3 abstract_search.py fieldsets delete "Okta triage" --yes
```

Without `--yes`, a command that changes your tenant prints exactly what it would change and exits with code 3, so a script can show it for approval first. With `--yes` it makes the change; just before sending, it reads the item again and stops if someone changed it since. Every change is written to `logs/changes-YYYY-MM-DD.log`. FIELDSETS-AND-VIEWS.md has the details.

### Saved views and fieldsets as code

```
python3 abstract_search.py content export ./abstract-content
python3 abstract_search.py content diff ./abstract-content
python3 abstract_search.py content apply ./abstract-content            # the review only (exit code 3)
python3 abstract_search.py content apply ./abstract-content --yes      # make the changes
```

A bundle is one file per view and fieldset, with no ids, so it can live in git and apply to any tenant. `apply` is one review for the whole bundle; `--prune` also deletes what is not in the bundle and says so first. CONTENT-AS-CODE.md has the details.

### Run a search on a schedule

```
python3 abstract_search.py automate cron --from-saved "Okta failures" --every 1d --at 00:20 --out ./okta-job
```

Targets: `cron`, `launchd`, `systemd`, `windows-task`, `github-actions`, `container-cron`. The files never hold the key, set the tenant guard, export back-to-back windows and alert `ABX_ALERT_WEBHOOK` on failure. AUTOMATION.md has the steps for each.

### 3. Preview

```
python3 abstract_search.py preview --dataset Okta --last 1h
```

How many events match, and the first 25 (the browser app shows 50). It is a sample, not the export.

### 4. Validate

```
python3 abstract_search.py validate --dataset Okta --where event.outcome=failure --last 24h
```

The same checklist as Validate in the browser app. The last line says whether the search is ready to export. Each row is explained in VALIDATION.md.

### 5. Export

```
python3 abstract_search.py export --dataset "Palo Alto Firewall" --where dest_port=3389 \
    --fields source_ipv4,dest_ipv4,dest_port,action --last 24h --format csv --merge
```

Progress goes to standard error; one JSON result line goes to standard output (see below). Every export runs its checks first, so a search that would fail Validate does not export.

Export options: `--format ndjson.gz` (default), `ndjson` or `csv`; `--merge` combines the slice files into one `export.<format>` when complete; `--out DIR` names a new, empty export folder; `--quiet` hides progress lines; `--expect-tenant ID` replaces `ABSTRACT_EXPECT_TENANT`.

### 6. List, resume or delete exports

```
python3 abstract_search.py exports                        # every export, with status, events and file
python3 abstract_search.py export --resume ~/AbstractExports/<folder> --merge
python3 abstract_search.py exports --delete <folder name>
python3 abstract_search.py searches                       # your saved searches on this computer
```

Resume continues from the first unfinished time slice, on the same tenant; the search comes from the export itself, so leave out the search options. `exports --delete` never deletes a running export.

## How to write a `<search>`

`preview`, `validate` and `export` take the same search.

**A dataset and filters**

```
--dataset "Okta" --where event.outcome=failure
--dataset "Okta" --where "user_name=alice@example.com,bob@example.com" --where event.outcome=failure --match any
--dataset "CRM - audit events" --where "user_name !contains example.com"
--dataset "Palo Alto Firewall" --where "dest_port >= 1024" --where "source_ipv4 != 10.1.2.3"
--dataset "Okta" --where "user_name startswith svc- or user_name has admin"
```

`--where` takes either form:

- `field=v1,v2` means the field is one of these values. Values are typed by the field's real type, so numbers (`dest_port=22,3389`), IP addresses and true/false fields (`field=true`) work as written. A value cannot contain a comma.
- A condition, written as in Abstract's search: `contains`, `!contains`, `has`, `!has`, `startswith`, `endswith` (and their `!` forms), `matches regex`, `==`, `!=`, `in`, `!in`, `>`, `>=`, `<`, `<=`, `between`, `isempty(f)`, `isnotempty(f)`, `and`, `or`, brackets and `not(…)`. SEARCH-REFERENCE.md, Use a search from Abstract, lists each one and what it becomes. Values need no quotes; quote a value that contains a space. Text operators ignore case except `==`, `in` and the `_cs` forms.

Put the whole condition in double quotes, the same way on macOS, Linux and Windows (Command Prompt and PowerShell). For a value with a space, use single quotes inside: `--where "user_name !contains 'example corp'"`. In the macOS or Linux terminal, if you type `!` inside double quotes and see "event not found", use single quotes around the condition instead: `--where 'user_name !contains example.com'`.

- Repeat `--where` for more filters.
- `--match all` (default): every `--where` must match. `--match any`: at least one must match. The dataset always applies.
- `--no-long-check` (advanced): do not check for text values longer than 256 characters. Abstract's search treats those as empty, so a filter on such a field would miss them (see SEARCH-REFERENCE.md, Filters).
- A dataset or field your tenant does not have is refused, so a typo cannot pass as "nothing matched".

**A search you already have**

```
--from-view "Firewall RDP and SSH"      one of your saved views in Abstract (name or id)
--from-file search.kql                  a search copied from Abstract's search box, saved in a text file
--from-saved "my search"                one of your saved searches (searches lists them)
```

A view or copied search is converted into a dataset and filters when it can be. If it cannot be converted and your tenant supports KQL, it runs as KQL, as written. The converted search is printed before anything runs. These options hold the whole search, so do not add `--dataset` or `--where`. Use a file rather than pasting a search onto the command line: PowerShell drops quotes inside arguments.

**A time range** (UTC)

```
--last 24h                                        15m, 1h, 4h, 24h, 7d, 30d …; ends 10 minutes ago
--start 2026-09-28T00:00:00Z --end 2026-09-29T00:00:00Z
```

`export` needs a time range; `preview` and `validate` use the last 24 hours when you give none. If a saved view, file or saved search sets its own range, that range is used unless you give one.

**Where to search** (optional)

```
--storage auto       Automatic (recommended): recent data, plus the archive for anything older (archive jobs)
--storage recent     Recent data only: fastest; older events kept in the archive are not included
--storage archive    the whole range from the archive, with archive jobs (minutes to an hour)
```

Archive search needs a dataset and filters; a search that runs as KQL cannot use it.

**Fields** (optional)

```
--fields "user_name,source_ipv4,action"      a field list; @timestamp and id are always added first
--every-field                                 complete events (not with CSV)
```

Without either, an export holds `@timestamp`, `id`, `data_stream.dataset` and `message`. `fields --dataset NAME` prints a ready list. In PowerShell, quote a `--fields` list that starts with `@`.

## Every API call, live, and the logs

Every command shows each call it makes to Abstract's API as it happens: the method, the path, the request it sent, the status and the time. The key is always masked. For example:

```
python3 abstract_search.py validate --dataset "Okta" --where "user_name !contains example.com" --last 1h
```

```
  API headers on every call: Authorization: Bearer ******** (masked) · X-AS-Vendor-Account-ID: … · User-Agent: … · Content-Type: application/json
  API GET /v2/auth/ -> 200 in 212 ms — 95 bytes
  API POST /v1/streamviewer/search -> 200 in 380 ms — 123 bytes
      {"vendor_account_id":"…","page_size":1,"start_time":"…","end_time":"…","condition":{…},"selected_fields":["id"]}
```

- The calls go to standard error, so the `--json` output and the JSON result line of `export` stay exactly as they are for scripts.
- `--hide-api` turns the calls off for one command. `--quiet` also turns them off, so a scheduled job stays quiet; add `--show-api` to a `--quiet` command to keep them. `ABX_SHOW_API=0` in the environment turns them off everywhere.
- Each line ends with the size of the answer. A retried call shows its attempt number. A call that failed shows the platform's message.
- Some calls ask for an error on purpose: a field that cannot exist makes the platform list every field, asking for a field as text makes it name the field's real type, and What this tenant supports tries features that may be off. Those show `expected: …` and are not logged as failures.
- A request longer than 2,000 characters is shortened on screen. `run.log` in the export folder and the browser app's API calls tab keep it whole.
- The terminal (`python3 abstract_search.py`, `--menu`) shows the calls the same way. With `--web`, they appear in the terminal running the app as well as in the browser.
- The key is never printed: the headers line shows it as `********`. Responses are not printed: they hold your events.

To run one of the calls yourself, the browser app's API calls tab has **Copy curl** for macOS / Linux, Windows PowerShell and Windows Command Prompt.

Logs, written whether or not the calls are shown:

| File | What it holds |
|---|---|
| `logs/abstract-search-YYYY-MM-DD.log` in your exports folder | Every error the tool showed, every call to the API that failed (with its request), and the details of any fault in the tool itself. Deleted after 30 days. |
| `run.log` in each export folder | Every step of that export and every API call it made, with its request. A resume adds to it. |

Every error message ends with "kept in the log: …", naming the file. The logs never contain your API key. They do contain your searches.

## The result line and exit codes

Every `export` ends with one JSON line on standard output, for example:

```
{"verdict": "COMPLETE", "rows_written": 1136, "rows_expected": 1136, "stored_duplicates": 0, "id_conflicts": 0, "slices": "4/4", "run_dir": "/home/me/AbstractExports/<folder>", "merged_file": "export.csv", "exit_code": 0, "coverage_warning": null, "coverage_unproven": false, "error": null, "zero_result": false, "zero_confirmed_by": null, "bad_timestamps": 0, "filter_unproven": [], "warnings": []}
```

The keys that matter most: `verdict` (COMPLETE, PARTIAL or INCOMPLETE), `rows_written`, `run_dir` (the export folder), `exit_code`, and `warnings`, one plain sentence for each limit that applies, such as recent data only or an unproven filter. Warnings are also printed to standard error, starting with `!`, even with `--quiet`. The same values are kept in the export's manifest; EXPORTS.md, Reading the manifest, explains them.

When the export stops before it starts (wrong tenant, unknown dataset or field, a filter that failed its proof), the line is `{"error": "…", "exit_code": 2, "log_file": "…"}`. `log_file` is the log the error was written to. Once the export is planned, every result line carries `run_dir`, also after Ctrl+C.

| Exit code | `export` |
|---|---|
| 0 | COMPLETE. Also when nothing matched (`zero_result` is true), and when you resume an export that is already complete. |
| 1 | INCOMPLETE. Resume it with `export --resume <folder> --merge`. |
| 2 | Stopped by a check or an error, such as a wrong tenant, an unknown dataset or field, a filter that failed its proof, or a full disk. |
| 3 | PARTIAL: recent data only. Older data was not searched. The `verdict` is `PARTIAL` and `coverage_warning` names the date range that was not searched (for example, the archive could not be searched with this key). Running the same export again stays PARTIAL with exit code 3; once the archive can be read, a new export of the same range is COMPLETE with exit code 0. A scheduler may treat it as success with a warning, but the file does not hold the whole range. |
| 130 | Cancelled with Ctrl+C. Resumable once the plan was written. |

`validate` ends with "Ready to export: N events. Estimated time: about M minutes". `export` prints `estimated time: …` with its plan and "… left" on each finished slice.

`validate` exits 0 when the search is ready to export, 1 when it is not ready or nothing matched, and 2 on an error. `status`, `preview`, `capabilities`, `datasets`, `views`, `searches` and `exports` exit 0, or 2 on an error. `fields` exits 1 when the dataset had no events in the last 30 days. These commands print text, or JSON with `--json`, and no result line.

## Scheduling

1. Build and validate the search in the browser app, then choose **Copy as command line**.
2. Use fixed `--start` and `--end` day boundaries, so consecutive runs never overlap or leave a gap.
3. Give each run its own dated `--out` folder. If a run is repeated for the same day, resume that folder instead of starting again.
4. Treat exit code 3 (PARTIAL) as success with a warning, and alert on it: the file does not hold the whole range. If `capabilities` shows the archive as not available, add `--storage recent`: the export then searches recent data only and says so in its warning.

cron on Linux. Save this as `/opt/abstract-search-export/daily-export.sh`, make it executable, and run it daily, for example `30 1 * * * /opt/abstract-search-export/daily-export.sh`:

```
#!/bin/sh
# Exports the previous UTC day. Exit 0 or 3 is success; anything else sends a mail.
set -a; . /etc/abstract/export.env; set +a
cd /opt/abstract-search-export || exit 2
day=$(date -u -d yesterday +%Y-%m-%d)
out="/srv/abstract-exports/firewall-$day"
if [ -f "$out/manifest.json" ]; then
    python3 abstract_search.py export --resume "$out" --merge --quiet >> /var/log/abstract-export.json
else
    python3 abstract_search.py export --dataset "<your firewall dataset>" \
        --fields "source_ipv4,dest_ipv4,action" \
        --start "${day}T00:00:00Z" --end "$(date -u +%Y-%m-%d)T00:00:00Z" \
        --out "$out" --merge --quiet >> /var/log/abstract-export.json
fi
rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ]; then
    echo "Abstract export for $day ended with exit code $rc" | mail -s "Abstract export" soc@example.com
fi
exit "$rc"
```

`/etc/abstract/export.env` holds one `NAME=value` line for each of `ABSTRACT_VENDOR_ACCOUNT_ID`, the key (or `ABSTRACT_API_KEY_COMMAND`) and `ABSTRACT_EXPECT_TENANT`, which pins the job to one tenant, and is readable only by the service account. On macOS, `date` takes `-v-1d` instead of `-d yesterday`.

Windows Task Scheduler, with the credentials set as environment variables from your secret store:

```
$day = (Get-Date).ToUniversalTime().Date
$start = $day.AddDays(-1).ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
$end = $day.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
$out = "D:\AbstractExports\firewall-" + $day.AddDays(-1).ToString("yyyy-MM-dd")
if (Test-Path "$out\manifest.json") {
    py C:\abstract-search-export\abstract_search.py export --resume $out --merge --quiet
} else {
    py C:\abstract-search-export\abstract_search.py export --dataset "<your firewall dataset>" --fields "source_ipv4,dest_ipv4,action" --start $start --end $end --out $out --merge --quiet
}
$rc = $LASTEXITCODE
if ($rc -ne 0 -and $rc -ne 3) { Write-Error "Abstract export exit code $rc"; exit $rc }
```

Run a long export you start by hand inside `tmux` or with `nohup`, so it survives closing the terminal.
