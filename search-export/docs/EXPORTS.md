# Exports

This page covers export formats, where to search, the export folder, the manifest, how to check an export yourself, resuming and deleting, and large exports.

## Formats

| Format | File | Use it for |
|---|---|---|
| NDJSON.gz (default) | `export.ndjson.gz` | Almost everything. One JSON event per line, gzip-compressed. |
| NDJSON | `export.ndjson` | The same, uncompressed. Typically about 10 to 15 times larger. |
| CSV | `export.csv` | Spreadsheets. One column per field. Needs a field list. |

NDJSON keeps each event's nested structure, for example `{"data_stream": {"dataset": "Okta"}}`. It is written as UTF-8. With a field list, NDJSON keeps the platform's `null` values, so a field that is absent from an event reads differently from one that is present but empty or null.

The platform's answers are read as strict UTF-8. An answer with a byte that is not valid UTF-8 is never repaired, because that would silently change event data. It is retried, and a time slice that keeps getting one is split.

CSV details:

- The first row holds the field names, and the columns are exactly the fields you chose, in the order you chose them, with `@timestamp` and `id` first and each field once. The tool also reads `data_stream.dataset` to check every row belongs to your dataset; it becomes a column only if you chose it, or if you gave no field list on the command line (the default list is `@timestamp`, `id`, `data_stream.dataset`, `message`). With a field list, an NDJSON export always carries `data_stream.dataset` for the same reason.
- The file starts with a UTF-8 byte order mark so Excel reads accents and non-Latin text correctly.
- Nested values are written as JSON text in the cell.
- True and false are written as `true` and `false`, as in the JSON.
- A missing value, a null and an empty string are all an empty cell. CSV cannot tell them apart. Use NDJSON if the difference matters.
- A text cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading `'`. This stops a spreadsheet from running the cell as a formula. The value `-5` stored as text therefore appears as `'-5`.
- With Every field, the CSV has one column for every field found in any exported event: `@timestamp` and `id` first, then the ACS fields, then the `ext` fields, each in alphabetical order. The columns are worked out when the slices are combined, so a CSV with Every field needs Combine (`--merge`).
- Excel opens at most 1,048,576 rows, including the header. A larger CSV is cut off when Excel opens it. Compare `rows_written` with that limit, and use NDJSON.gz or several shorter time ranges for bigger exports.

The combined `.gz` file is several gzip streams joined end to end, one per time slice. That is valid gzip, and `gzip`, `zcat`, 7-Zip and Python read all of it. If a tool reports far fewer events than `rows_written`, it is reading only the first stream. Decompress the file with `gzip -dc` first.

## KQL result tables

A KQL query that reshapes the rows (`summarize`, `top`, `distinct`, `extend` or `project` with computed columns) gives a table, not a list of events, so it is not exported slice by slice. Run it in the KQL tab and choose **Save result table**, or use `kql save "QUERY" --format csv|json` on the command line. It is written to a new folder in your exports folder:

| File | Contents |
|---|---|
| `result.csv` or `result.json` | The table, at most 10,000 rows. |
| `query.kql` | The query that made it. |
| `result.json.meta` | The query, the time window, the number of rows saved and whether the query had more than were saved. |

A result table is not reconciled or proven like an event export: it is what Abstract's KQL engine returned for that window. It reads recent data only. Grouped text values can be shown in lower case, with case variants added together, on tenants that do not yet have the platform's fix.

## Every field

Every field exports complete events. The tool asks the platform for every top-level field your tenant has, which returns the whole event.

Every field is about 2 to 3 times slower than a field list, and the files are much larger. It cannot be written as CSV.

## Where to search

Every search has one Where to search choice. It is the same in the browser app (Where to search, under Time range), the guided terminal (the Time step, or Build or edit the search) and the command line (`--storage`). Saved searches keep it.

| Choice | What the export covers |
|---|---|
| Automatic (recommended), `--storage auto` | Recent data, plus the archive for anything older than recent data. The older part runs as archive jobs, one per week of the range. |
| Recent data only, `--storage recent` | Recent data only. Fastest. Older events kept in the archive are not included. The export still finishes COMPLETE (exit code 0), with the warning "Recent data only: this export searched recent data only; older events kept in the archive are not included." |
| `--storage archive` (command line only) | The whole range from the archive, with archive jobs. Needs a search with a dataset and filters. |

Recent data keeps your tenant's most recent days (31 on many tenants); the archive keeps older events (365 days on many tenants). Searching the archive runs an **archive job** on your tenant: it takes minutes for a day and roughly 45 minutes for a month of data on a busy tenant. The browser app and the terminal show the job's progress. Archive jobs appear in your tenant's archive job list under the key's user, like the ones you run in Abstract, and your key needs permission to run them.

How the archive part is checked:

- Each archive job counts its own matches; a slice is accepted only when the events written equal that count, exactly as for recent data.
- A job is accepted only when every one of its files was processed. A job that finishes with failed files, pauses, is cancelled, or whose results have expired is run again.
- Every event that comes back from the archive is checked against your search. If one does not match, the export stops rather than export events you did not ask for.
- The job's id is kept in `manifest.json`, so a resumed export reads the same job instead of running it again.
- The boundary between recent data and the archive is placed a few days inside recent data, because the oldest days of recent data are removed gradually. If you resume an export days later and part of it has aged out of recent data, that part is read from the archive instead.

With Automatic, if the archive cannot be searched with your key, the export is partial: verdict PARTIAL and exit code 3 on the command line, "PARTIAL: RECENT DATA ONLY" in the terminal, and in every front end the line "Partial: recent data only. Older data was not searched. Not searched: <start> to <end> UTC." Everything in recent data is in the export; the range it names was not searched, so the file is not a complete export of the range you asked for. Running it again stays PARTIAL; once the archive can be searched with your key, a new export of the same range is COMPLETE.

## The export folder

Each export is its own folder under `~/AbstractExports`. On Windows that is `C:\Users\<you>\AbstractExports`. Set `ABX_OUT_ROOT` to use another location. The folder name is the UTC time the export was created, a label (the dataset, or the saved search's name) and a random suffix, for example `20260930T101500Z_Okta-0004ce-3f2a1c`. Every run gets its own folder, so two exports started in the same second never share one. On the command line, `--out DIR` chooses the folder instead; it must not already hold an export.

| File | What it is |
|---|---|
| `export.ndjson.gz` (or `.ndjson`, `.csv`) | All events, combined. The time slices are joined oldest slice first; inside each slice, events stay in the platform's order, newest first. Written only when the export is COMPLETE and combining is on. |
| `part-00000.ndjson.gz` and so on | One file per time slice. While an export runs, these are the export. |
| `manifest.json` | The record of the export: tenant, search, fields, filter proofs, and every slice with its counts. |
| `run.log` | Every step of the export, as shown in the technical log, and every call to Abstract's API it made, with the request it sent. A resume adds to it. Send it with `manifest.json` when you ask for help. No key, no events. |
| `search.json` | The search you ran. Written by the browser app and the guided terminal, not by the command line. |
| `.lock` | Present only while the export runs. It stops two windows from writing the same export. On Windows it can remain after a forced quit: see TROUBLESHOOTING.md. |

Before combining, the tool counts the events in every part file and compares each count with the slice's `written` in the manifest. A part file that was cut short or changed puts its slice back to INCOMPLETE and the export stops with exit code 1. Resume it and that slice is fetched again.

After combining, the part files are deleted.

The browser app and the guided terminal combine the files by default. On the command line, combining is off unless you pass `--merge`.

## Why every export is reconciled slice by slice

An export is cut into time slices. Each time slice is counted and reconciled against what was written; a slice that does not reconcile is retried or split, and the export is reported INCOMPLETE rather than finishing short.

Each slice is small enough to come back in one answer from the platform, and that answer carries both the slice's events and its count. Nothing can change between the two.

One answer holds up to 5,000 events for a field list, 2,000 for Every field and 1,000 from the archive. Slices are planned at about 80% of that, so events that arrive late usually still fit. A slice that has grown too big is split and fetched again.

There is one exception: a single second that holds more events than one normal answer. A slice cannot be cut smaller than one second, so:

- With a field list, that second is fetched in one larger answer of up to 10,000 events.
- With Every field, the tool first fetches the list of ids in that second in one answer, then fetches the complete events in pages and checks them against that list.
- More than 10,000 events in one second cannot be proven this way. The tool fetches that second in pages under the strictest check, and if it cannot be proven the slice ends INCOMPLETE rather than risk an unproven result. See TROUBLESHOOTING.md.

Slice boundaries are whole seconds. The platform answers every search for the window from the start second up to, but not including, the end second. Each event belongs to exactly one slice by its own `@timestamp`. Slices therefore never overlap and never leave a gap.

## Duplicate copies and id conflicts

If the same event is returned more than once, under the same `id`, it is written once; the count of removed duplicates is reported (`stored_duplicates`). Some duplicate copies differ only in their ingestion time fields, `ingested_time` and `insertion_time`.

A slice counts every copy, so the export credits the removed copies and the slice still reconciles. The log shows this per slice as `(N duplicate copies removed)`. The totals are in the result and the manifest:

- `stored_duplicates`: duplicate copies of events that were written once.
- `reingested_copies`: duplicate copies that differed only in ingestion time fields. The first copy returned is the one written.

Two events can share an `id` but have different content. Both are always written, because they are different events, wherever they fall in the range. `id_conflicts` counts the ones found within the same time slice. The tool does not compare ids across slices, because a list of every id would grow with the export.

## Reading the manifest

`manifest.json` is one JSON object. The main parts:

| Key | What it holds |
|---|---|
| `tenant` | Tenant name, vendor account ID and API URL. Never the key. |
| `window` | The exact start and end, in UTC. |
| `query`, `fields`, `args` | The search, the exported fields and the options used. |
| `preflight` | The filter proofs that ran before exporting. |
| `coverage` | The retention check. It is empty when the range started less than a day ago. |
| `slices` | One entry per time slice. |
| `summary` | The verdict, written when the export finishes. |

Each slice entry has `start`, `end`, `status`, `server_total` (the platform's count), `written`, `stored_duplicates`, `id_conflicts`, `bad_timestamps`, `attempts` and any `error`. The status is one of PENDING, COMPLETE, INCOMPLETE, FAILED or SPLIT. A SPLIT slice was cut into smaller slices, which carry a `parent` pointing back to it.

Once all the parts of a SPLIT slice have been exported, the SPLIT slice also gets `children_total`, the parts' counts added up, and `split_drift`, that sum minus its own count from before the split. The window is live, so events that arrive late make `split_drift` non-zero. It is information, not an error: each part was reconciled on its own. A non-zero drift is also written to the log.

`bad_timestamps` counts events whose `@timestamp` the tool could not read as a time. The tool reads ISO-8601 text and epoch seconds or milliseconds. Such an event cannot be checked against the slice boundaries, so it is written in the slice that returned it, and the result carries a warning.

The `summary` block:

```json
"summary": {
  "slices": 12, "complete": 12,
  "rows_written": 50000, "rows_expected": 50000,
  "stored_duplicates": 2000, "reingested_copies": 500, "id_conflicts": 0,
  "bad_timestamps": 0, "filter_unproven": [], "zero_result": false, "zero_confirmed_by": null,
  "warnings": [], "verdict": "COMPLETE", "merged_file": "export.ndjson.gz"
}
```

These values are illustrative.

- `rows_expected` is the number of events the export must contain (the platform's count for the range, less duplicates and events owned by a neighbouring slice). COMPLETE means `rows_written` equals `rows_expected` and every slice is COMPLETE.
- `coverage` appears in the summary only when it limits the result. It either reads "RECENT DATA ONLY — events older than recent data were not checked", or says where searchable history starts.
- INCOMPLETE means at least one slice is missing or did not reconcile. Resume the export.
- `filter_unproven` lists filters whose proof removed nothing in the sample window, or whose check returned an error. The export still runs, because a correct filter can also remove nothing, for example on a feed where every sampled event matches. Check the exported events before you rely on such a filter.
- `zero_result` is true when the export matched nothing. It is still COMPLETE with exit code 0, so that a scheduled export of a quiet dataset does not fail. The tool then counts the whole window once more with a second, independent search method; `zero_confirmed_by` names it, or is null when no second search could count it the same way or it disagreed.
- `warnings` holds one plain sentence for each of these that applies: nothing matched, unreadable timestamps, unproven filters, a retention limit, and recent data only.
- `verdict` is COMPLETE, PARTIAL (recent data only, exit code 3) or INCOMPLETE (resume it).
- `coverage_warning` appears when the export is partial (exit code 3): "Partial: recent data only. Older data was not searched. Not searched: <start> to <end> UTC."

## Check an export yourself

You do not have to trust the verdict. Two checks take a minute.

1. Count the events. The number of lines must equal `rows_written`.
2. Count the distinct ids. The number of distinct ids is normally `rows_written` minus `id_conflicts`. It can be lower when two different events in different time slices share an id. Both are in the file, but `id_conflicts` counts only those inside one slice.

On macOS or Linux:

```
gzip -dc export.ndjson.gz | wc -l
```

On any system, including Windows (use `py` in place of `python3`):

```
python3 -c "import gzip,json,sys; ids=[json.loads(l)['id'] for l in gzip.open(sys.argv[1],'rt',encoding='utf-8')]; print(len(ids),'events',len(set(ids)),'distinct ids')" export.ndjson.gz
```

For an uncompressed `.ndjson` file, replace `gzip.open(sys.argv[1],'rt',encoding='utf-8')` with `open(sys.argv[1],encoding='utf-8')`.

For CSV, do not count lines. A cell can contain a line break. Use a CSV reader, which also skips the header row:

```
python3 -c "import csv,sys; r=list(csv.DictReader(open(sys.argv[1],encoding='utf-8-sig',newline=''))); print(len(r),'events',len({x['id'] for x in r}),'distinct ids')" export.csv
```

To check one slice, compare its `written` plus `stored_duplicates` plus `outside` with its `server_total`. `outside` counts events the platform returned whose own time belongs to a neighbouring slice; that slice writes them.

## Resuming

An export that did not finish can be resumed. That includes an export you stopped, a network drop, a closed window or a platform error.

- Browser app: Export history tab, then Resume.
- Guided terminal: Resume now on the result screen, or the main menu (press m), then Exports.
- Command line: `python3 abstract_search.py export --resume ~/AbstractExports/<folder> --merge`. The folder is printed after the plan line (`export folder: …`) and is in `run_dir` of every result line written after planning, including after Ctrl+C.

Resume skips every COMPLETE slice and starts at the first unfinished one. The search, fields, time range and slices come from the manifest, so a resumed export is the same export.

- You must be connected to the same tenant and API URL as the original export.
- Resume remembers whether the original export combined the files. Passing `--merge` when you resume also turns it on.
- If an export stopped before its plan was written, there is nothing to resume. Start it again.
- On the command line, options that would change the export (`--dataset`, `--where`, `--fields`, `--every-field`, `--format` and the time options) are refused with `--resume`.
- Resuming an export that is already complete fetches nothing. The command line says "this export is already complete — nothing to resume" and exits 0. If it was never combined, `--merge` combines it.
- An export of a search that runs as KQL resumes only while your tenant still supports KQL. The tool checks that first.

## Deleting an export

Delete removes one export folder and every file in it. It never deletes a running export: stop it first.

- Browser app: Export history tab, then Delete, and confirm.
- Guided terminal: main menu, Export history: list, resume, open folder, pick the export, then Delete this export, and confirm.
- Command line: `python3 abstract_search.py exports --delete <folder name>`. `exports` lists the names.

## Large exports

All three ways of running the tool work the same way, on your computer. Events go from the platform's API straight to this computer's disk, into `~/AbstractExports/<folder>`. The browser page never holds the export. It shows progress and a preview.

### Limits by interface

| | Browser app | Guided terminal | Command line |
|---|---|---|---|
| Where data goes | Your disk, in the export folder | Your disk, in the export folder | Your disk, in the export folder, or `--out` |
| Holds the export in memory | No. One slice at a time | No. One slice at a time | No. One slice at a time |
| Download | Download streams the file from disk; Open folder shows it | Open folder, or the path shown | The path is in the JSON result line |
| Survives closing the window | No. Resume continues | No. Resume continues | Yes, under `nohup`, `tmux`, `screen` or Task Scheduler |
| Survives the computer sleeping | No. Resume continues | No. Resume continues | No. Run it on a machine that does not sleep, then Resume if needed |
| Unattended or scheduled | No | No | Yes |
| Preview | Up to 50 events | Up to 25 events | Up to 25 events (`preview`) |
| Best for | Up to about 1M events or a few hundred MB | The same, over SSH or without a browser | Multi-million events, multi-GB files, schedules |

In the browser app, closing the browser tab does not stop an export. It runs in the terminal window that started the app. Closing that terminal window stops it.

The preview is a sample to check you have the right data. It is not the export.

### Typical sizes and speeds

Typical examples, not guarantees. Your speed depends on the platform's response time, your field list and the tenant's load.

| Export | Typical result |
|---|---|
| 6 fields, one full day, about 1 million events | About 16 minutes, about 1,000 events per second. 18 MB as `ndjson.gz`, about 20 bytes per event; about 240 bytes per event uncompressed. |
| Every field, one hour, about 40,000 events | About 2 minutes, about 375 events per second. About 200 bytes per event gzipped, about 3 KB per event uncompressed. |
| Memory | Around 44–79 MB whatever the export size: the lower figure for a field list, the higher for Every field. |

At those rates, 1 million Every field events take about 45 minutes, about 200 MB gzipped and about 3 GB uncompressed.

The estimate Validate shows uses these rates, timed over whole exports (connecting, proofs and combining included):

| Part | Rate used |
|---|---|
| Recent data, field list | about 700 events a second (85,347 events in 120 seconds) |
| Recent data, every field | about 230 events a second (39,444 events in 171 seconds) |
| Archive job | about 1.5 minutes per day of range, plus about 4 minutes per job |
| Reading archive results, field list | about 2,000 events a second (60,000 in 27 seconds) |
| Reading archive results, every field | about 700 events a second (assumed, not measured) |
| Getting started | about 30 seconds (connecting, the field list, the filter proofs) |

While an export runs, the time left is worked out from what has happened so far: the archive job's files, then the events written since the job finished, at the rate seen so far. The command line prints the estimate in its plan (`estimated time: …`) and the time left on each finished slice; `manifest.json` keeps the estimate under `estimate`.

Download in the browser app streams the file from disk in 1 MB pieces, so memory stays low even for multi-GB files, and the downloaded file is identical to the original. Downloads are named after the export folder (`<run folder>.<ext>`) and support resuming (HTTP Range), so a browser can continue an interrupted download. A download is still a second copy of a file that is already on your disk. For multi-GB exports, use Open folder instead.

### What limits an export

There is no row cap. The limits are:

- Time. Speed is set by how fast the platform answers, not by this computer, so a faster laptop does not help. Fewer fields do help.
- Disk space. Plan for about twice the final file size. While combining, the slice files and the combined file both exist.
- The platform's rate limits. When the platform answers "too many searches", the tool waits and tries again. Do not run other large searches with the same API key during an export.

Three things are deliberate:

- Memory. Each answer from the platform is read whole, but a time slice is at most one answer of 10,000 events, so memory stays bounded, typically around 44 MB for a field list and 79 MB for Every field.
- One connection per request. Speed is set by the platform's answer time, so reusing connections would make little difference.
- One time slice at a time. The platform limits how many searches one user runs at once. Slices in parallel would compete with each other and with your own searches, and get "too many searches" answers, so the tool stays sequential on purpose.

Before combining, the tool checks there is enough free space. If there is not, it stops with a message. The slice files are kept, so you can free space and resume.

### Recommendations

- Under about 1M events or a few hundred MB: use any interface.
- Multi-million events or multi-GB files: use the command line with `--merge`, inside `tmux` or `nohup`. If you want checkpoints, run one export per week of data. Keep `ndjson.gz`, and avoid CSV for Every field.
- Scheduled exports: use the command line with `ABSTRACT_EXPECT_TENANT`, a dated `--out` folder, and fixed `--start`/`--end` day boundaries if consecutive runs must not overlap or leave gaps. See CLI.md, Scheduling.
- Export only the fields you need. A field list is about 2 to 3 times faster than Every field and far smaller.
