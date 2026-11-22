# Troubleshooting

Find the message you see, then the fix. Messages are shown as the browser app and the guided terminal word them. The command line shows the same problem prefixed with `ERROR:`, `STOPPED:`, `FAILED:` or `FATAL:`, sometimes with more technical detail. Words in capitals, such as ID, NAME or N, stand for your own values.

## Tenant checks

### TENANT MISMATCH: connected as NAME (ID), expected OTHER-ID

Command line. The key belongs to a different tenant than `ABSTRACT_EXPECT_TENANT` or `--expect-tenant` names. The command was stopped before anything else was read. Use the key for the tenant you meant, or correct the expected ID.

### ABSTRACT_EXPECT_TENANT is X but the key belongs to Y

The browser app or the terminal connected from the environment, and the key belongs to another tenant than `ABSTRACT_EXPECT_TENANT` names. It disconnected again. Use the right key, or correct the variable.

### connected as NAME (ID). Set ABSTRACT_EXPECT_TENANT=ID (or pass --expect-tenant ID) so …

Command line. `ABSTRACT_EXPECT_TENANT` is required for every command that connects: `capabilities`, `datasets`, `views`, `fields`, `preview`, `validate` and `export`. Set it to the vendor account ID shown, after checking the tenant name is the one you mean.

### this key belongs to vendor account X, not Y. Use X, or a key for the account you meant.

The vendor account ID you entered does not match the key. Use the ID shown, or a key for the account you meant. On the command line this reads `this API key belongs to vendor account X, not Y`.

### not connected — set ABSTRACT_API_KEY and ABSTRACT_VENDOR_ACCOUNT_ID, or start abstract_search.py and choose Connect

Command line, `export`. The key or the vendor account ID is missing from the environment. See CLI.md.

### set ABSTRACT_VENDOR_ACCOUNT_ID and ABSTRACT_API_KEY (or ABSTRACT_API_KEY_COMMAND) first (see Start-Here-Guide.html, Part 3)

Command line, `capabilities`, `datasets`, `views`, `fields`, `preview` and `validate`. The same cause: set both variables in the environment.

### The environment credentials did not work: …

The browser app found `ABSTRACT_API_KEY` and `ABSTRACT_VENDOR_ACCOUNT_ID` in the environment, but could not connect with them. The terminal says `environment credentials did not work: …`. The rest of the message gives the reason, for example a rejected key. Fix the variables, or choose Connect and enter the details by hand.

### both the vendor account ID and the API key are required

Connect needs both. Fill in the empty one.

### No terminal to read the API key from safely: set ABSTRACT_API_KEY, or run ssh with -t

The guided terminal does not read a key from piped input or from a session without a terminal. Set `ABSTRACT_API_KEY` in the environment, or connect with `ssh -t`. In Git Bash (mintty) on Windows, run `winpty python abstract_search.py`.

## The key

### Your API key was rejected (HTTP 401): it is wrong, expired or revoked. Create a new key and connect again.

The key is wrong, expired or revoked, or has a typo. Create a new key and connect again. On the command line this reads `the API key was rejected (HTTP 401) — it is wrong, expired or revoked`.

### the API key was rejected (HTTP 403). Check the key and that it belongs to this vendor account.

At Connect. The platform refused the key for this vendor account. Check that the vendor account ID is the one the key was created for, and that the key is still active.

### Your API key does not have permission for this (HTTP 403). Ask your administrator for a key with event search permission.

The key works but lacks a permission for one of the calls. On the command line this reads `permission denied (HTTP 403) — …` with the platform's detail. Ask your administrator for a key with read access to events, configurations (the dataset list) and saved views. If the detail mentions the vendor account, check that `ABSTRACT_VENDOR_ACCOUNT_ID` is the account the key belongs to.

### could not verify the key (HTTP N).

The platform gave an unexpected answer while checking the key. Try again in a moment. If it repeats, send the code to your Abstract contact.

### the platform did not say which vendor account this key belongs to — try again

The platform's answer was incomplete. Try again.

### the API key contains spaces, line breaks or other characters an API key cannot have — paste it again

The paste picked up extra characters. Paste the key again. A similar message, `the API key or account ID contains characters that cannot be sent — paste them again`, means the same.

### the vendor account ID should be 4–64 letters, digits, '-' or '_'

Check the vendor account ID. It is not your organization name.

## Network, TLS and proxy

### Could not reach URL from this computer. Check your network, VPN or proxy, then try again.

The tool could not open a connection to the API. It retried first. Check:

1. You can reach the API URL from this computer, for example in a browser.
2. You are on the VPN if your company requires one.
3. If your company uses a web proxy, set `HTTPS_PROXY`, for example `HTTPS_PROXY=http://proxy.example.com:8080`. Python honours `HTTPS_PROXY` and `NO_PROXY`.
4. A proxy that needs a password: `HTTPS_PROXY=http://user:password@proxy.example.com:8080`. Only basic proxy authentication works. NTLM and Kerberos proxies are not supported.

On macOS or Linux: `export HTTPS_PROXY=http://proxy.example.com:8080`. In PowerShell: `$env:HTTPS_PROXY = "http://proxy.example.com:8080"`.

### A proxy requires authentication (HTTP 407): set HTTPS_PROXY=http://user:password@host:port (NTLM/Kerberos proxies are not supported)

Your proxy wants a user name and password. Put them in `HTTPS_PROXY` as shown in step 4 above. If your proxy accepts only NTLM or Kerberos, ask IT for a proxy or an exception that accepts basic authentication, or run the tool from a machine that does not need the proxy.

### your proxy refused the connection to https://api.abstractsecurity.app (…). Ask your IT team to allow api.abstractsecurity.app through the proxy.

The proxy is reachable but blocks this host (the detail often reads `Tunnel connection failed: 403 Forbidden`). Ask your IT team to allow `api.abstractsecurity.app`, or the API host Abstract gave you, through the proxy.

### the TLS certificate of the API could not be verified. On macOS with python.org Python, run 'Install Certificates.command'; behind a corporate proxy, set SSL_CERT_FILE to your company's CA bundle.

Python does not trust the certificate it was shown. Two usual causes:

- On macOS with Python from python.org, the certificates were never installed. Run `Install Certificates.command` from the Python folder in Applications.
- Your company's network inspects TLS and shows its own certificate. Ask IT for the company's root CA certificate as a PEM file, then point Python at a bundle that contains it.

On macOS or Linux: `export SSL_CERT_FILE=/path/to/company-ca-bundle.pem`. In PowerShell: `$env:SSL_CERT_FILE = "C:\path\to\company-ca-bundle.pem"`.

The bundle replaces Python's default list, so it must also contain the public root certificates if any of your traffic is not inspected. IT can usually provide a combined bundle. On Windows, Python also trusts the certificates in the Windows certificate store, so this often works without any change.

### the API answered with a redirect (HTTP CODE) ... Check the API URL.

The tool never follows redirects, so your key cannot be sent elsewhere. Use the exact API URL, for example `https://api.abstractsecurity.app`, with no path.

### the API URL must be https://host

Use the API URL Abstract gave you, starting with `https://` and with nothing after the host name.

## Rate limits

### The platform is limiting how many searches run at once. The tool waits and retries; avoid running other searches with the same key meanwhile.

In the log this appears as `busy (HTTP 429) — retrying` or `the platform is busy (too many searches at once) — waiting 60s`. The tool waits and continues by itself. If it still cannot continue, that slice is left unfinished and the export ends INCOMPLETE. Resume it later. Do not run other large searches or a second export with the same key at the same time.

## Platform errors

### the platform rejected the request (HTTP 400 or 422) — DETAIL

The platform refused a search the tool sent. The detail says why, for example a value of the wrong type. Check the filter values, validate again, and if it repeats send the message to your Abstract contact.

### not found (HTTP 404) — DETAIL

The platform does not offer a call the tool needed, on this tenant or at this API URL. Check the API URL. If it is right, send the message to your Abstract contact.

### the platform could not count this search (…). Try a shorter time range.

Preview could not count the search. Choose a shorter time range and try again.

### the platform refused this window (…). Try a shorter time range.

Preview could not fetch events for this window. Choose a shorter time range.

### the platform refused to count START→END — try a shorter time range

Validate (the Search row) could not count part of the range, even after retrying. Choose a shorter time range and validate again.

## Loading lists

### could not load the dataset list (…) — try again, or type the name

The dataset list did not load. Try again, or type the full dataset name.

### no datasets were found on this tenant

The tenant has no integration configurations the key can see. Check that you are connected to the right tenant, and that the key can read configurations.

### could not load the field list (…) — try again

The tenant's field list did not load. Try again in a moment.

### could not load the saved views (…) — try again

The saved views did not load. Try again. The key needs read access to saved views.

### that saved view no longer exists on this tenant — refresh the list

Someone deleted or changed the view after the list loaded. Load views again (browser) or reopen Saved views from Abstract (terminal).

### An export is running, so the dataset list loads when it finishes. You can type the name meanwhile.

Browser app. The dataset list waits while an export runs, so it does not compete with the export for the platform's search limit. Type the full name, or wait for the export.

## Building a search

### no dataset called 'NAME' on this tenant — a partial name matches nothing. Did you mean 'FULL NAME'? Run 'datasets' to list them all …

The dataset name is not one your tenant knows. Use the full name; upper and lower case do not matter. Run `python3 abstract_search.py datasets` to list every name, or pick it from the suggestions.

### These fields do not exist on your tenant: FIELDS (in --where). A misspelled field matches nothing. Check the name with 'fields --dataset NAME'.

Command line. A `--where` field is misspelled or not on your tenant. Run `fields --dataset NAME` to see the names the dataset uses. Names are exact.

### These fields do not exist on your tenant: FIELDS

A field name is misspelled or not on your tenant. Remove it, or pick fields from the field list or the Fields tab. Names are exact.

### 'FIELD' is not a field on your tenant

The browser app could not find the field you typed for a filter or the export. Pick it from the suggestions.

### FIELD is a TYPE field — remove the filter and add it again so the right operators are offered.

The filter uses an operator for the wrong field type, for example a text operator on a number field. Remove the filter and add it again. The tool then offers the operators that fit.

### An IP filter needs IPv4 addresses like 10.1.2.3.

Enter IPv4 addresses, separated by commas. For a range, use "is in subnet" with a CIDR such as `10.0.0.0/8`.

### 'FIELD OPERATOR' needs a value (or needs numbers, or needs a CIDR range like 10.0.0.0/8)

Fill in the filter's value in the form shown.

### Add a dataset or at least one filter. …

A search needs a dataset or a filter.

### This search has only a time range. Add a dataset or a filter to it first.

The search you converted from Abstract set only a time range. Add a dataset or a filter.

### "*" is not a field — choose fields, or export every field

Command line. Use `--every-field` for complete events, or list the fields.

### could not read this tenant's field list, which every-field exports need — try again

An every-field export needs the tenant's field list, and it did not load. Try again.

### the end time is in the future — choose an end time that is now or earlier

Use an end time that is now or earlier, in UTC. For a final export, use one that ended at least 10 minutes ago.

### the start time 'X' is not a valid UTC time — use a form like 2026-09-28T00:00:00Z

Write times as `YYYY-MM-DDTHH:MM:SSZ`, in UTC.

### the end time must be after the start time

Swap or correct the two times.

### enter both a start and an end time in UTC, e.g. 2026-09-28T00:00:00Z

A custom range needs both times.

## Command-line options

### give a time range: --last 24h, or --start and --end

`preview`, `validate` and `export` need a time range, unless you resume or the search you use holds one.

### 'X' is not a duration — use a form like 30m, 1h, 24h, 7d

`--last` takes a number and a unit: `m`, `h` or `d`.

### FIELD is a number field; 'V' is not a number

A `--where` value on a number field must be a number, for example `--where dest_port=22,3389`.

### FIELD is an IP address field; 'V' is not an IPv4 address

A `--where` value on an IP field must be an IPv4 address such as `10.1.2.3`. For a subnet, write a condition: `--where "ipv4_is_in_range(source_ipv4, 10.0.0.0/8)"`.

### FIELD is a true/false field: use --where FIELD=true or FIELD=false

A true/false field takes exactly one value, `true` or `false`.

### --where cannot filter on FIELD (a TYPE field). Build this filter in the browser app or the guided terminal, save the search, and run it with --from-saved

The `field=value` form handles text, number, IP and true/false fields. For a list field such as `tags`, write a condition instead, for example `--where "tags == identity:employee"` or `--where "tags contains identity"`.

### --where expects field=value[,value…], got 'X'

Write each filter as `--where field=value`, `--where field=value1,value2`, or a condition such as `--where "user_name !contains example.com"` (see CLI.md).

### --where '…': …

Command line. The condition could not be converted; the message after the colon names the part, as for a search from Abstract (see below). Two that are specific to the command line:

- `use --dataset for the dataset and --last / --start / --end for the time` — put those in their own options, not in `--where`.
- `event not found` (printed by the macOS or Linux terminal before the tool runs) — the terminal read `!` inside double quotes. Put the condition in single quotes: `--where 'user_name !contains example.com'`.

### put the address 10.1.2.3 in quotes

In a search pasted from Abstract, an IP address must be in quotes, as Abstract's search requires: `source_ipv4 == "10.1.2.3"`. On the command line, `--where` accepts it without quotes.

### DIR already holds an export. Choose a new, empty folder for --out, or continue that export with --resume.

`--out` points at a folder with a `manifest.json`. Use a new folder, or continue that export with `export --resume DIR --merge`. This stops one export from overwriting another.

### no saved search called NAME (see: searches)

`--from-saved` did not find that name. Run `python3 abstract_search.py searches` to list your saved searches. For the copied examples, use the file name, for example `example-firewall-rdp-from-internet.json`.

### --from-saved already holds the whole search; do not add …

Leave out the options the message lists. You can still change the time range, format, fields, `--out` and `--merge`.

### this KQL search cannot be exported: '| take' changes which or how many events come back; remove it so the export holds every matching event

A search that runs as KQL may only filter (`where`), sort and choose fields (`project`). A stage that limits, aggregates, reshapes or joins rows (`| take`, `| limit`, `| summarize`, `| extend`, `| join` …) would export something other than every matching event, so it is refused. Remove that stage and use the search again. The same reason is added to "cannot be converted exactly" when such a search is pasted or picked from a saved view.

### unknown option '--X'; see --help

The option is not known, or it is not the first argument. `--web` and `--menu` must come first, and `--port` and `--no-browser` go after `--web`. Options of a command go after it; a mistyped one prints `error: unrecognized arguments` with the usage text, and exit code 2.

### unknown command 'X'; see --help

The commands are `capabilities`, `datasets`, `views`, `searches`, `exports`, `fields`, `preview`, `validate` and `export`. See `--help`.

### --port needs a number, e.g. --port 8765 / --port must be between 0 and 65535

Use a number from 0 to 65535 (0 picks any free port), or leave `--port` out.

## Using a search from Abstract

### This search cannot be converted exactly: …

You used a search from Abstract that contains something this tool cannot convert into filters, and it could not run as KQL instead (where your tenant supports KQL, such a search runs as KQL). The message names the part and says what to do instead, for example:

- `the function strlen() is not supported` — only `isempty`, `isnotempty`, `isnull`, `isnotnull` and `ipv4_is_in_range` convert.
- `| take is not supported` — remove the row limit.
- `FIELD is a list field; use contains, !contains or == (an exact item)` — `has` does not apply to a list field.
- `comparing FIELD with an empty value is not supported; use isempty(FIELD) or isnotempty(FIELD) instead`.
- `a value for FIELD starts or ends with a space, …` — remove the spaces, or build the filter here.
- `a value for FIELD contains a comma, …` — a value inside `in (…)` cannot contain a comma; use `==` or `contains` for that value.
- `this combination of 'and' and 'or' expands into too many groups to run exactly` — the search has many alternatives that each hold several conditions (seven or more `(a and b) or (c and d) …`). Split it into several exports.

`and`, `or`, brackets, `not(…)`, `contains`, `has`, `endswith`, `matches regex`, `between`, `>=` and `<=` all convert; SEARCH-REFERENCE.md lists every form.

On the command line the same refusal reads `the saved view 'NAME' cannot be converted exactly: …` or `the file PATH cannot be converted exactly: …`. The same checks the browser app applies to each filter (a subnet must be a CIDR, IP addresses must be IPv4, numbers must be numbers) apply on the command line too. Rewrite the search without the part named in Abstract, or build it here with a dataset and filters. Nothing is converted halfway: the search is converted exactly, runs as KQL, or is refused.

### Abstract's search rejects this query - …

Abstract's own KQL validator rejected the search for your tenant, for example `!=` on a free-text field such as `message`, or an unknown field. The tool refuses it too, so it never exports something Abstract's search would not have run.

### no saved view called 'NAME' on this tenant. Did you mean …? Run 'views' to list them all.

Command line, `--from-view`. Use the view's exact name (any case) or its id. `python3 abstract_search.py views` lists every saved view, with whether it is ready to export.

### the saved view 'NAME' holds no filter, only columns and a time range, so it would export every event …

The view was saved with columns and a time range but no filter. Load it in the browser app or the terminal (it fills in the columns and the time range), add a dataset or a filter, save it, and run it with `export --from-saved "<name>"`.

### the saved view 'NAME': the condition on FIELD is a model condition (such as a model lookup), which this tool cannot run

A view built with Abstract's filter builder can use a condition that looks values up in a model. The tool cannot run that condition, so the view is listed as "cannot convert". Rebuild the filter here without it.

### N saved views are called 'NAME'; pass the view id instead

Two or more views share that name. Run `views` and pass the id shown in brackets.

### --from-view / --from-file / --from-saved already hold the whole search; do not add …

The search you named is the whole search. Leave out `--dataset` and `--where`; you can still give a time range, where to search and fields.

### --from-view / --from-file start a new export; they cannot be used with --resume

To continue an export, use `--resume DIR` on its own: the search comes from its manifest.

### this view's filters cannot be converted: the operation X on FIELD is not supported

A view built with Abstract's filter builder uses an operation this tool does not know. Build the same filter here, or tell your Abstract contact which operation it was.

### note: this saved view also searched other sources (such as federated search); only events stored in Abstract are exported

Information. Events from the other sources are not in the export.

### … KQL is not available on this tenant, so it cannot run as written either.

The search from Abstract cannot be converted into filters, and your tenant does not support KQL searches, so it cannot run at all. The start of the message names the part that cannot be converted. Rewrite the search without it, or build it with a dataset and filters. What this tenant supports shows whether KQL is available.

### KQL is not available on this tenant now, so this search cannot run as KQL. Build the search with a dataset and filters instead.

Every export of a search that runs as KQL checks first that your tenant applies KQL. This time it did not, so nothing was exported. Choose What this tenant supports to check again, or build the search with a dataset and filters.

### This saved search uses KQL. KQL is not available on this tenant, so this search cannot run as KQL. …

The terminal shows this, and the browser app shows "This saved search uses KQL, which is not available on this tenant", when you load a saved search that runs as KQL on a tenant without KQL. Rebuild it with a dataset and filters: choose Start a new search, or use a search from Abstract to convert it.

### Archive search runs a dataset and filters. Choose Automatic or Recent data only for this search.

A search that runs as KQL cannot use Archive search. Choose Automatic or Recent data only. On the command line this reads `archive search runs a dataset and filters, not KQL`.

## What this tenant supports

### Could not check the tenant now (…). Searches are converted into filters.

The terminal could not finish the check after connecting. Everything still works; a search from Abstract is converted into filters and does not run as KQL. Choose What this tenant supports in the main menu to check again. In the browser app the Tenant tab says "Could not check the tenant: …"; choose Check again.

### An export is running. Checking the tenant waits until it finishes, so it does not slow the export down.

Check again when the export has finished.

### unknown

An item that reads unknown could not be checked now, for example because the tenant had no events in the last day to test with. Check again later. Until KQL reads available, searches from Abstract are converted into filters.

## Filter proofs

### the filter on FIELD OPERATOR could not be confirmed: a value that cannot exist still matched N events. Use 'is one of' instead.

The tool could not confirm that this filter narrows the results. The export could contain the wrong events, so it was refused. Change the operator to "is one of".

### the filter on FIELD 'does not contain' could not be confirmed: it and its opposite should never overlap and should cover every event with a value, but …

A "not" filter is proven against its opposite: no event may match both, and every event with a value must match one of them. That did not hold, so the platform did not apply the filter as written and the export was stopped. Validate again; if it repeats, send the Validate result to your Abstract contact. Meanwhile, "is one of" with the values you want is a filter the tool can prove.

### the filter could not be confirmed: it matched every event in the sample and not every part of it could be proven. Use 'is one of' filters.

Your search matched every event in the tenant for the sample hour, and at least one filter could not be proven. Change the filters to "is one of", or add a dataset.

### this filter made no difference in the sample window, so the tool cannot confirm it narrows the results. Check the preview before exporting.

A warning, labelled UNPROVEN in the manifest. The export can still run. After an export, the same warning ends "…so the tool cannot confirm it narrows the results. Check the exported events before relying on that filter." The filter excluded nothing in the window it was tested on, so the proof had nothing to measure. This is normal when every event there already matches, for example "has a value" on a field every event has. Check the preview before exporting.

### the tool could not confirm that your filters are applied: the platform returned errors instead of counts. Shorten the time range and try again, or use 'is one of' filters.

The platform refused the proof searches, so nothing was exported. Shorten the time range and validate again, or change the filters to "is one of".

### N events from another dataset came back, so the dataset filter could not be confirmed. The export was stopped to keep the file correct.

Every exported event's dataset is checked, and events from another dataset were found. Validate again. If it repeats, send the manifest to your Abstract contact. In the browser app's preview and the terminal's preview the message reads "events from another dataset came back, so the dataset filter could not be confirmed. An export would be stopped for the same reason."

### FIELD has values longer than 256 characters, which most of Abstract's search filters do not see …

Information, not a problem. Most of Abstract's search filters do not see text values longer than 256 characters, so a filter there would miss them. The tool fetches every event where that field has a value and applies your filter itself, so the export is exact. Validate's count reads "up to N" because it includes the events fetched for this reason; the export's own count is exact. The same search in Abstract can show fewer events than the export for this reason.

### A "not" filter returns fewer events than I expected

"Does not contain", "is not one of" and the other "not" filters leave out events where the field is empty, as Abstract's search does. Tick **or has no value** on the filter (on the command line, add `or isempty(FIELD)` to the condition) to keep them.

## Zero events

### Nothing matched. A zero is a claim: confirmed by … / not independently confirmed

Nothing matched. Validate shows this as the Nothing matched row. An export that matches nothing still ends COMPLETE with exit code 0, so that scheduled exports of quiet datasets do not fail, and its result carries this line in `warnings`, with `zero_result` true and `zero_confirmed_by` naming the second search method that also counted 0, or null. A misspelled dataset name or `--where` field is refused before this point, so a zero here comes from a real dataset and real fields. Before you report that something did not happen:

1. Check whether a second, independent method also counted 0 (`zero_confirmed_by`, or the Nothing matched row in Validate).
2. Widen the time range. Some sources deliver in batches and look empty near the present.
3. Check the filter values. A value is matched exactly (upper and lower case aside).
4. Remove filters one at a time to find the one that removes everything.

## Retention

### events before DATE are older than recent data and are read from the archive, with N archive job(s) (roughly M minutes); the rest from recent data.

Information, not a problem. With Where to search set to Automatic, the tool reads the older part from the archive by itself, with archive jobs. Keep the app open while they run; the progress shows how many of the job's files are done.

### The archive keeps N days, so nothing before DATE exists to export

Your range starts before the archive's own retention. Abstract no longer holds the part before DATE, so it is not missing from the export; copies made earlier, such as older exports, may still exist. On the command line the result's `warnings` carries it, and a `! Retention: …` line is printed even with `--quiet`. Ask your Abstract contact if you need longer retention.

### archive job … finished with N of M files not processed; it will be run again

The platform could not read part of the archive for that job. The tool runs the job again by itself (up to four times per slice). If it keeps happening, the slice ends INCOMPLETE: resume the export later.

### archive job … ended paused / cancelled / failed (…); it will be run again

The same: the job is run again. A job you cancel in Abstract is also run again when the export resumes.

### the results of archive job … are no longer available; it will be run again

The platform keeps a job's results for a limited time. When you resume an export much later, the tool runs that job again.

### the archive returned an event that does not match the search (id …), so this archive search cannot be trusted

Every event from the archive is checked against your search. One did not match, so the export stopped rather than include events you did not ask for. Narrow the search, or search recent data only, and tell your Abstract contact the event id.

### Recent data only: this export searched recent data only; older events kept in the archive are not included.

Information. You chose Recent data only, so events older than recent data are not in the export. The export is still COMPLETE (exit code 0). To include them, choose Automatic.

### Partial: recent data only. Older data was not searched. (exit code 3 on the command line)

Messages: "events before DATE are older than recent data, and the archive cannot be searched with this key; the export covers recent data only" or "could not check how far back recent data goes on this tenant (…); the export covers recent data only." The result reads "Partial: recent data only. Older data was not searched. Not searched: <start> to <end> UTC.", and the terminal shows PARTIAL: RECENT DATA ONLY. Everything in recent data is in the export, but the range it names was not searched, so the file is not a complete export of the range you asked for. Check the archive in What this tenant supports, and ask your Abstract administrator whether your key may run archive jobs. If your tenant has no archive, choose Recent data only so the result says exactly what was searched. The result's verdict is PARTIAL; running the export again stays PARTIAL, and once the archive can be read a new export of the range is COMPLETE.

### events before DATE are older than recent data and only in the archive, which a KQL search cannot read

A search that runs as KQL can read recent data only. Shorten the range so it starts inside recent data, or build the search with a dataset and filters, which read the archive automatically.

## Exporting

### INCOMPLETE — resume it later

At least one slice did not finish or did not reconcile. In the log you may see `accounted for <n> events but the platform counts <m>`. Resume the export: see EXPORTS.md. Resume starts at the first unfinished slice. If the same slice fails again on a window that ended long ago, send the manifest to your Abstract contact.

### resume it with: export --resume "FOLDER" --merge

Command line, after Ctrl+C or a failure once the export was planned. Run exactly that command when the cause is fixed. The folder is also in `run_dir` of the result line.

### Stopped before any events were written — there is nothing to resume.

You stopped the export before its plan existed. Start it again.

### N events fall inside one second (TIME) — more than can be retrieved for one second (CAP). Narrow the search with a filter, or export fewer fields.

More events share one second than the tool can fetch for that second. Narrow the search with a filter, or export fewer fields.

A second with more than 10,000 events can also leave its slice INCOMPLETE, because it cannot be proven complete. Resume once to retry it. If it stays INCOMPLETE, narrow the search with a filter so fewer events share that second.

### the platform keeps refusing to count even a one-second window

The platform returned errors even for the smallest window. Wait and try again. If it repeats, send the message and the time shown to your Abstract contact.

### This kind of search is not enabled on your tenant.

The search the tool needed is not available for your tenant, for example complete-event search for an Every field export. What this tenant supports shows which searches are available. Use a dataset and filters with a field list.

### an export is already running — wait for it or stop it first

One export runs at a time. Wait for it, or stop it. A stopped export can be resumed.

### An export is running. Preview, Validate and field learning are paused until it finishes, so they do not slow it down.

The platform limits how many searches one user can run at once, so other searches wait for the export.

### this export is already running in another window

Another window or process is writing this export. Use that window, or close it first.

On macOS and Linux the tool clears a lock left by a process that has ended. On Windows it cannot check that safely, so if you are sure nothing is writing the export, for example after the computer restarted or you pressed Ctrl+C twice, delete the `.lock` file in the export folder and resume.

### this export belongs to NAME (ID) ... connect to that tenant to resume it

Resume needs the same tenant and API URL as the original export.

## Resuming and combining

### this export is already complete — nothing to resume

Information. The export finished earlier, and its combined file is there (or combining was not asked for). Nothing was searched; the exit code is 0 and the result line carries `already_complete: true`. To combine an export that was never combined, add `--merge`.

### --resume continues the same export, so its search, fields, format and time range come from its manifest. Leave out …, or start a new export.

Remove the options the message lists. To export something different, start a new export.

### Resuming an export of a KQL search

An export of a search that runs as KQL resumes only while your tenant still supports KQL. If it no longer does, the resume stops with "KQL is not available on this tenant now …". Start a new export with a dataset and filters.

### DIR has no manifest.json — it is not an export folder

The folder is not an export, or its manifest was removed. The browser app says "that folder is not an export (it has no manifest.json)". Check the path.

### the manifest in DIR is a symbolic link; refusing to resume it

The tool never follows links inside an export folder. The same applies to a part file: `the part file X in DIR is a symbolic link; refusing to resume it`. Replace the link with the real file, or start a new export.

### part file X no longer matches its time slice (…) — resume the export to fetch that slice again

A slice file was cut short or changed after it was written. Its slice was set back to INCOMPLETE. Resume the export, and that slice is fetched again.

### part file X is missing — the export cannot be combined; resume it

A slice file that the manifest records as complete was deleted. Resume the export: a resume fetches every slice whose file is gone again, then combines the files. If the combined file `export.*` is already in the folder, the export is complete and a resume says so without searching again.

## Disk space

### not enough free disk space to combine the files (needs X GB, Y GB free). The part files are complete; free space and resume, or use them as they are.

Every slice finished, but there is not room to write the combined file. The part files are the complete export. Free space and resume the export to combine them, or use the part files as they are. Plan for about twice the final size. On the command line this ends with exit code 2.

### The disk fills up while slices are being written

The export stops with "could not write the export to disk (No space left on device) — free some space, then resume" (exit code 2 on the command line). Free space, then resume. Finished slices are kept.

## Browser app

### The app is busy with other requests. Try again in a moment.

More than 16 requests arrived at once (usually many open tabs). Wait a second and repeat; close extra tabs.

### Validation stopped

You chose Stop validating (or Ctrl+C in the terminal). Nothing was exported. Validate again when ready.

### Validate this search first. Export is enabled once it passes.

Export runs only a search that passed Validate. Choose Validate.

### This validation is more than 15 minutes old. Validate again to enable Export.

A validation is honoured for 15 minutes. Validate again.

### Download needs the combined file

The Export history tab offers Download only for an export that is COMPLETE and was combined into one file. Keep Combine into one file when complete ticked (it is on by default), or use Open folder to reach the part files.

### no export called 'NAME' (see: exports)

Command line, `exports --delete`. Use the export's folder name exactly as `exports` lists it, or its full path.

### That export is still running. Stop it first, then delete it.

Stop the export (or wait for it to finish), then choose Delete again. On Windows, a folder left behind by a crash can look running: if no export is running, delete the `.lock` file in that folder and try again.

### Port N is in use, so the app is using port M instead.

Information. Open the link printed in the terminal, which has the right port.

### Ports N to M are all in use by other programs.

Close the program using those ports, start with another port (`--web --port 8765`), or leave `--port` out to let the app pick a free one.

### N is not a valid port. Use a number from 0 to 65535 (0 picks any free port), or leave --port out.

Use a port number in that range.

### Could not start: …

The browser page could not load its first data from the app. Reload the page; if it repeats, restart the app. In the terminal, "Could not start: … set ABX_OUT_ROOT to another folder" means the exports folder could not be created or written: check its permissions, or set `ABX_OUT_ROOT`.

### ERROR: could not start the browser app: …

The app could not start its local web server. The reason follows, for example a port that cannot be used. Try another `--port`, or leave it out.

### The browser was not opened because this is an SSH session (or: no desktop display was found)

The app runs, but it cannot open a browser here. Open the printed link in a browser on this computer. Over SSH, forward the port from your own computer first, as the terminal shows: `ssh -L <port>:127.0.0.1:<port> <this machine>`. Then open the link on your own computer. `--no-browser` starts the app without trying to open a browser.

### Could not open a browser automatically. Open the link above yourself.

Copy the link from the terminal into your browser.

### This link has already been used or is out of date. In the terminal running the app, press Enter to get a new link.

Each link works once. Press Enter in the terminal window running the app, and open the new link.

### This page is no longer signed in to the app. The app was probably restarted.

The app was restarted since this page signed in. Open the latest link from the terminal (press Enter there for a new one).

### This page needs the link printed in the terminal.

The page was opened without its one-time link, for example from a bookmark or a new tab. Open the link printed in the terminal.

### The app is not responding. Is its terminal window still open?

The terminal running the app was closed or stopped. Start it again with `python3 abstract_search.py --web`. An export that was running can be resumed.

### This download link has already been used. Choose Download again.

A download ticket serves one download. Choose Download again.

### this download link has expired — choose Download again

Download links expire after 5 minutes. Choose Download again. For large files, use Open folder.

### The export is in: PATH

This computer has no file manager to open, for example over SSH. The path shown is where the export is.

## Terminal

### Arrow keys do not work, or numbered prompts appear instead of menus (Windows)

The console cannot process terminal control codes, so the tool falls back to numbered prompts and opens the main menu first; choose 1 for the guided flow. They work the same way. For arrow keys and colour, use Windows Terminal, which is free from the Microsoft Store.

### The terminal shows odd characters or broken boxes

Your terminal cannot show the colours or symbols. Set `ABX_PLAIN=1` for plain numbered prompts with no colour, no art and no redrawing. This also suits screen readers. Set `NO_COLOR=1` to keep the menus but turn colour off.

On macOS or Linux: `ABX_PLAIN=1 python3 abstract_search.py`. In PowerShell: `$env:ABX_PLAIN = "1"`, then run the tool.

### python3 is not found (Windows)

Use the Python launcher: `py abstract_search.py`.

### Abstract Search & Export needs Python 3.8 or newer

Install a current Python from python.org, or your operating system's package manager.

### Something went wrong in the tool itself

An unexpected error in the tool, not in your search. The details are in the terminal window. Run it again with `ABX_DEBUG=1` (terminal) or `ABX_WEB_DEBUG=1` (browser app) to get the full technical detail, and send it as described below.

## Getting help

Send your Abstract contact:

1. The output of `python3 abstract_search.py --version`. It names the exact version and build.
2. The log the error message named ("kept in the log: …"): `logs/abstract-search-YYYY-MM-DD.log` in your exports folder. It holds the error, any API call that failed with its request, and the details of a fault in the tool itself.
3. The export folder's `manifest.json` and `run.log`, if the problem is with an export. `run.log` holds every step and API call of the export. It holds the search, the filter proofs and every slice's counts and errors. It never holds your API key. It does hold your dataset name and filter values, so check them before you share it.
4. The command you ran and its JSON result line, for the command line. Its standard error shows every API call it made (if you used `--quiet`, run it again with `--show-api`).
5. For a problem in the terminal or the browser app, the terminal output after running it again with `ABX_DEBUG=1` (guided terminal and main menu: full error details) or `ABX_WEB_DEBUG=1` (browser app: request log and error details). For example `ABX_DEBUG=1 python3 abstract_search.py`, or in PowerShell `$env:ABX_WEB_DEBUG = "1"`, then `py abstract_search.py --web`.

Never send your API key. Nothing above contains it, but check anything you paste from your shell. The logs and `run.log` contain your searches, so share them only with people allowed to see them.

### something went wrong in the tool itself (…). The details are in the log; please send it to your Abstract contact

A fault in the tool, not in your search or your tenant. The full details (a traceback) are in the log the message names. Send that file to your Abstract contact. In the browser app the message reads "Something went wrong in the tool itself (…). The details are in the log: …".

### (kept in the log: PATH)

Every error the command line and the terminal show ends with this line. It names today's log file, where the same error is written with the time.

## KQL and plain English

### this query cannot run: … (with the reason from Abstract's KQL check)

Abstract's own KQL check refused the query against your tenant's fields. The reason usually names the field and suggests the right one ("did you mean `user_name`?"). Fix the name or the operator and check it again. A mistyped `ext.*` name is not refused by Abstract and matches nothing, so compare those with `fields --dataset NAME`.

### this query changes which rows come back (… '| summarize' …). Save its result table with: kql save "<query>"

The query reshapes the rows, so it is not an export of events. Use `kql preview` to see the result table and `kql save` to write it to a CSV or JSON file, or remove the stage to export the matching events.

### it cannot be converted into filters (…), and KQL search is not available on this tenant

Your tenant does not apply KQL searches (What this tenant supports shows it). Use a query that converts into filters: plain `where` conditions on stored fields. SEARCH-REFERENCE.md lists what converts.

### Abstract's KQL assistant is not available on this tenant (HTTP N)

The plain English assistant is switched off for your tenant or your key. Write the KQL yourself.

### The assistant did not write a query: …

The assistant explains why, for example that grouping is not something it writes. Rephrase the request as a filter, or write the query yourself and add `| summarize …`.

## Fieldsets and saved views

### Nothing was changed. Run the same command with --yes to make this change.

The command showed what it would change and stopped, as it always does first. Read the change, then run the same command with `--yes`.

### this fieldset/saved view was changed by someone else after you reviewed it. Review it again.

Between your review and the change, someone else changed the item. The tool did not overwrite their change. Review it again; the new review shows the current state.

### this change was already applied, was cancelled, or is out of date. Review it again and confirm the new plan.

Each review is good for one change and for 10 minutes. Review the change again.

### This fieldset cannot be saved: X is not a stored field on this tenant (a KQL computed column cannot go in a fieldset)

A fieldset holds stored fields only: your tenant's fields, `ext.*` and `event.parsed.*`. Remove the computed column, or add the stored field it is computed from.

### your API key is not allowed to create the fieldset (HTTP 403). Ask an admin for the permission.

The key can read but not change fieldsets or views. Everything else still works. Use a key that may change them, or ask your administrator.

### a saved view needs columns: add fields or pick a fieldset

Abstract needs a view to have columns. Add fields to the search, or pick a fieldset for the view.

## Views and fieldsets as code

### PATH is not a bundle: there is no bundle.json in it (make one with: content export)

Point `content diff` or `content apply` at the folder `content export` wrote (the one holding `bundle.json`), or at a single bundle `.json` file.

### not a content bundle (schema abstract.content/v1 expected)

The file is not a bundle this tool wrote, or it was edited so its `schema` line changed. Export a fresh bundle.

### the bundle has two views called 'NAME' (or two fieldsets)

Names are matched without regard to case, so each must be unique in a bundle. Rename one of the files' `name`.

### SKIP … uses fieldset(s) neither the bundle nor the tenant has

Add that fieldset to the bundle (export it with the view, or with `--fieldsets`), or create it on the tenant first.

### SKIP … X is not a field on this tenant

This tenant does not have that field, so Abstract would refuse the fieldset or view. Remove the field from the file, or apply to a tenant that has it.

### PRUNE: N object(s) on this tenant are NOT in the bundle and will be DELETED

You used `--prune`. Read the list. If any of them should stay, add them to the bundle (export again) or apply without `--prune`.

### STOPPED at … Not attempted: N

One change failed, so the tool stopped there. The changes before it were made. Fix the cause shown, then apply the same bundle again: what is already done now compares as the same and is skipped.

## Scheduled exports

### set ABSTRACT_API_KEY_COMMAND (or ABSTRACT_API_KEY from a secret store) first

The scheduler starts the script with almost no environment. Set `ABSTRACT_API_KEY_COMMAND` where the recipe's README says (the crontab line, the launchd plist, the systemd service, your Windows user environment), or the secret in GitHub or Kubernetes.

### run it every 15m, 1h, 6h or 1d

Those are the intervals a recipe supports, so that windows follow each other exactly.

### --at takes a UTC time such as 00:20

A daily run's start time, in 24-hour UTC.

### say which tenant the job runs against (its vendor account ID)

With `--from-saved`, the tool does not connect, so it needs `--vendor-account ID` or `ABSTRACT_VENDOR_ACCOUNT_ID`.

