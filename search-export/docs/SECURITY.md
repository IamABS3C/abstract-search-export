# Security

This page lists what the tool calls, what it stores and where, what it never stores, and how the browser app is protected.

## Your API key

- Where a server mounts secrets as files, `ABSTRACT_API_KEY_FILE` names a file that holds the key on one line. The tool reads it when it connects and never copies it anywhere. When the file cannot be read, the message does not show its content.
- With `ABSTRACT_API_KEY_FROM_NETWORK=1` the tool holds no key at all and sends no `Authorization` header. A proxy you control adds the header to requests for the API after they leave the computer. The API call log then shows the header as added by the network.
- You type or paste the key at Connect, the tool reads it from the `ABSTRACT_API_KEY` environment variable, or it runs `ABSTRACT_API_KEY_COMMAND` once to read it from your secret manager, key vault or keychain (for example `op read …`, `az keyvault secret show …`, `vault kv get …`, `security find-generic-password … -w`). The command runs without a shell; its output is never logged, shown or written, and when it fails the message gives only its exit code. With either environment option the browser app connects on its own, so the key never passes through the browser.
- The key is never kept in browser storage. The browser app's page holds only a session token for the local app, in that tab's sessionStorage, and when you type the key in the Connect dialog it passes through the page once.
- It is held in memory only, for as long as the program runs. It is never written to disk: not to settings, saved searches, manifests, exports or logs.
- It is sent only to the API URL you connect to, over HTTPS, in the `Authorization` header.
- The tool never follows an HTTP redirect, so the key cannot be forwarded to another host.
- When the browser app, the guided terminal or the `capabilities`, `datasets`, `views`, `fields`, `preview` and `validate` commands read the key from the environment, they remove it from the environment as soon as they have read it, whether or not the connection works, so programs the tool starts later, such as your browser, do not receive it. Open folder starts the file manager without the API key (on macOS and Linux, without any `ABSTRACT_` variables; on Windows the vendor account ID and expected tenant variables are still passed on).
- Disconnect in the browser app discards the key from the session.
- Use a key with the least access that works. The tool reads events, the list of configurations, saved views, fieldsets and the tenant's settings. It changes only fieldsets and saved views, and only after you review and confirm each change (see below). A key without permission to change them still does everything else.

For `export`, the key stays in the environment of that one process, which starts no other programs. Protect the environment file or secret store that sets it, or use `ABSTRACT_API_KEY_COMMAND` so it is never in an environment variable at all.

In a dev container or GitHub Codespaces, use Codespaces secrets or `ABSTRACT_API_KEY_COMMAND`; never put a key in `devcontainer.json`, a `.env` file or the repository. Exports made in a codespace are stored in GitHub's cloud until you delete them or the codespace.

## Tenant guard

On the command line, every command that connects (`status`, `capabilities`, `datasets`, `views`, `fields`, `preview`, `validate`, `export`, `kql`, `nl`, `fieldsets` and `mcp`) first checks that the key belongs to the vendor account named by `ABSTRACT_VENDOR_ACCOUNT_ID`, and stops if it does not; Abstract also refuses a key for another account. `ABSTRACT_EXPECT_TENANT` (or `--expect-tenant`) is optional: it pins one tenant, for scheduled jobs, and when it is set every command refuses to run with `TENANT MISMATCH` unless it names that same vendor account. The check is the first call the tool makes (`GET /v2/auth/`). With `--from-view` and `--from-file` it also runs first, before any saved view is read or any search is validated, so a mixed-up key cannot read another tenant's views. `searches` and `exports` read only local files and need no tenant. Once connected, each command prints `Connected to <tenant name> (<vendor account id>) · production` on standard error (`--quiet` hides it), so you can see which tenant it is working on.

## What the tool calls

Almost everything below is a read. The only changes the tool can make are to **fieldsets and saved views**, and each one is shown to you first and made only after you confirm it (see Changes to your tenant). The tool never changes your configuration, detections, pipelines or data. Search APIs take their query in a `POST` body, which is why most calls are `POST`. `python3 abstract_search.py endpoints` (and the browser app's **Platform** panel) lists every endpoint with what this run of the tool saw.

| API | Used for |
|---|---|
| `GET /v2/auth/` | Verify the key and find which tenant it belongs to. |
| `GET /v1/configurations/` | List dataset names, for the suggestions, `datasets`, and the check that a dataset name exists. |
| `POST /v1/streamviewer/search` | Dataset and filter searches, Every field exports, the field list, field types and field sampling. |
| `POST /v1/dataview/search` | Searches that run as KQL, the KQL check in What this tenant supports, the cross-check, the check of an export that matched nothing, and the retention check. |
| `GET /v1/streamviewer/views` | Your saved views, when you choose Load views, Saved views from Abstract, `views` or `--from-view`. Read live, never stored. |
| `POST /v1/kql/validate` | Checks a KQL query, a search you paste or a saved view against your tenant's fields before it is converted or run as KQL, where the tenant offers it. |
| `POST /v1/kql/to-conditions`, `POST /v1/kql/from-conditions` | Abstract's own translation between KQL and filter-builder conditions: the reason a query cannot be converted, and the KQL form of a search saved as a view. Nothing runs. |
| `POST /v1/ase/kql` | Plain English to KQL, only when you ask for it. Sends the text you typed to your tenant's KQL assistant; it writes a query and runs nothing. |
| `POST /v1/streamviewer/field-suggestions` | The most common values of a field, for suggestions while you type. |
| `GET /v1/streamviewer/field-sets`, `GET /v1/streamviewer/field-set/{id}` | Reads your fieldsets. |
| `POST`, `PATCH`, `DELETE /v1/streamviewer/field-set…` | Creates, changes or deletes a fieldset, only after you confirm it. |
| `GET /v1/streamviewer/view/{id}` | Reads one saved view's search, time range and columns. |
| `POST`, `PATCH`, `DELETE /v1/streamviewer/view…` | Creates, changes or deletes a saved view, only after you confirm it. |
| `POST /v2/streamviewer/raw-search` | Dataset volumes for the last 24 hours, the datasets that hold recent events (for the dataset name check, and the dataset list when it cannot be loaded), and the complete-event check in What this tenant supports. |
| `POST /v1/coldstorage/query/submit` | Starts an archive job for the part of a range older than recent data (one per week of the range). The job is a search: it reads your archive and changes nothing in it. It appears in your tenant's archive job list under the key's user. |
| `GET /v1/coldstorage/query/{id}`, `POST …/{id}/events`, `POST …/{id}/cancel` | Follows an archive job, reads its results, and cancels it when you stop an export. |
| `GET /v1/coldstorage/query` | Checks, without starting a job, that your key may run archive jobs (What this tenant supports, and before an export that needs the archive). |
| `GET /v1/vendor-accounts/{your tenant}` | Reads how many days recent data and the archive keep, to place the edge between them. Nothing is changed. |
| `GET` the tenant's settings | Read once as part of What this tenant supports. Nothing is changed. |

Every request carries `Authorization`, `X-AS-Vendor-Account-ID` and a `User-Agent` of `abstract-search-export/<version>`.

Every one of these calls is shown as it happens: in the browser app's **Live API calls** strip and **API calls** tab, and on the command line and in the terminal (standard error; `--hide-api` turns it off). They show the method, path, headers, request body, status and time. The `Authorization` header is always shown as `Bearer ******** (masked)`; the key itself is never displayed, printed or written. Responses are never shown: they hold your events. A request field whose name looks like a key, token, secret or password would be shown as `***`; the tool sends none. **Copy curl** writes the key as a variable (`$ABSTRACT_API_KEY`, `$env:ABSTRACT_API_KEY` or `%ABSTRACT_API_KEY%`), never its value.

## What is stored, and where

Everything is under `~/AbstractExports`, or the folder set by `ABX_OUT_ROOT`.

| Item | Contains |
|---|---|
| `<export folder>/export.*` and `part-*` | The exported events. Treat them like the source data. |
| `<export folder>/manifest.json` | The tenant name and ID, the search (the dataset name and the filter values you chose), the fields, counts, filter proofs and slice results. No events, no key. |
| `<export folder>/search.json` | The search you ran, including its name, which is the saved view's name for an imported view (browser app and terminal only). No key. |
| `<export folder>` name | The UTC time, and the dataset or saved search name. |
| `saved-searches/*.json` | Your saved searches: dataset name, filter values, fields and time range. No key. |
| `<export folder>/run.log` | The export's technical log and every API call it made, with the request bodies (the search, field names, time windows). No key, no events. |
| `logs/changes-YYYY-MM-DD.log` | One line for every fieldset or saved view the tool created, changed or deleted: the time, the tenant ID, what was done, its ID, name and fields. Mode 0600. No key, no events. |
| `<export folder>/result.csv` or `result.json`, `query.kql` | A KQL result table you saved, and the query that made it. |
| `logs/abstract-search-YYYY-MM-DD.log` | Errors the tool showed, failed API calls with their requests and the platform's error message, and details of any fault in the tool. No key, no events. Created with mode 0600 and deleted after 30 days. |

No settings file is written: the API URL always starts at `https://api.abstractsecurity.app` (or `ABSTRACT_BASE_URL` when set), and the vendor account ID and key are typed or taken from the environment each time.

The kit itself ships with no data from any tenant: no field profiles, no saved views, no dataset names. Your tenant's dataset list, saved views, fields and field profiles are read live from the tenant you connect to and kept in memory for the session only.

On your own computer, the files above do hold tenant details you chose: each export's `manifest.json`, `search.json` and `run.log`, the logs, and your saved searches hold the dataset name and filter values, and `search.json` and a saved search can hold an imported view's name. A platform error message in a log can quote a filter value back.

Exports can contain user names, IP addresses and anything else in your logs. Store and share them under your data-handling policy. The filter values in `manifest.json` and saved searches can also be sensitive, for example a user name you searched for.

## What is never stored

- The API key.
- Event values in settings, saved searches or logs.
- The tenant's dataset list, field list, field profiles or saved views as a whole: these stay in memory and are gone when the app stops. Only the dataset name, filter values and view name of a search you export or save are written, in the files listed above.
- Anything outside the exports folder, except an export you send elsewhere with `--out` and files you download.

The tool sends nothing anywhere except the Abstract API you connect to. Plain English descriptions go to your tenant's own KQL assistant, through that API, only when you ask for one.

## Changes to your tenant

The tool can create, change and delete fieldsets and saved views. Each change works the same way:

1. The tool reads what is there now and shows you exactly what will change, and the request it will send.
2. Nothing happens until you confirm: in the review in the browser app or the terminal, with `--yes` on the command line, or, over MCP, when the assistant calls `apply_change` after you have said yes. A review is good for 10 minutes and for one change only.
3. Just before sending, the tool reads the item again. If it changed after your review, the tool stops instead of overwriting it.

Every change is written to `logs/changes-YYYY-MM-DD.log`. The key needs Abstract's permission to change views and fieldsets; without it the platform refuses the change and nothing happens.

## AI assistants (MCP)

`python3 abstract_search.py mcp` lets an AI assistant on your computer, such as a desktop AI assistant or GitHub Copilot in VS Code, use the tool (see MCP.md).

- **Read-only unless you turn changes on.** The assistant can search, preview, export and plan changes. It cannot create, change or delete saved views or fieldsets unless you start the tool with `--allow-writes` or set `ABSTRACT_MCP_ALLOW_WRITES=1`. The tool cannot check a "yes" typed in the chat, so this switch is the only thing that allows changes, and only the person who starts the tool can set it.
- **Use a read-only API key for an assistant** unless it needs to make changes. Abstract then refuses any change, whatever the tool is started with.
- **The key** stays in the tool's environment. It is never a tool argument or part of an answer.
- **Your events go to the assistant's service.** Every row the assistant asks to see is sent there, so use it under your organisation's policy for AI tools. `ABSTRACT_MCP_MAX_ROWS` limits how many rows a preview returns (50 by default, at most 500). Exports are written to your exports folder; the assistant gets their counts and file names, not their events.
- **The local HTTP mode** listens on `127.0.0.1` only and needs a token printed once in its terminal.

## File permissions

On macOS and Linux the tool sets its file-creation mask to 077 and makes the exports folder and each export folder private to your user (mode 0700). Other users on the same computer cannot read them.

On Windows those modes do not apply. The exports folder is inside your user profile and inherits its permissions.

The tool never follows a symbolic link inside an export folder. A manifest or part file that is a link is refused when you resume, and the files it writes (part files, the manifest, the combined file and their temporary copies) are opened so that a link planted in their place is never written through. An export folder someone hands you therefore cannot redirect the tool to read or overwrite another file.

## CSV and terminal safety

- A CSV text cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return is prefixed with `'`, so a spreadsheet does not run it as a formula.
- The browser app, the guided terminal, the main menu and the `fields`, `datasets`, `views` and `searches` commands show control characters and escape sequences as visible codes such as `\x1b`. Event values and server messages therefore cannot move the cursor, change colours or rewrite your terminal.
- The command line's JSON result line is escaped JSON, and its progress and error lines show control characters as visible codes too.

## The browser app

The browser app is a small web server on your own computer.

- It listens on `127.0.0.1` only, the loopback interface. Nothing on your network can connect to it.
- In a container it listens on the container's own interface (`--bind 0.0.0.0`), and the container's port is published to `127.0.0.1` on your computer, which has the same effect. Never publish that port on the network. See DOCKER.md.
- Each launch prints a link carrying a one-time launch code. The page removes the code from the address bar and exchanges it once for a session token. The code then stops working. Press Enter in the terminal to print a fresh link.
- Where the app has no terminal (a container or a service), a page that is not signed in asks for a new link (`POST /api/relink`). The app writes the link to its own log, at most once every 5 seconds, and the answer to the page never contains it. Only someone who can read the app's log can sign in.
- `GET /healthz` answers `{"ok": true}` with the version, without signing in, so a container platform can check the app is up. It says nothing else.
- The page keeps the session token in the tab's session storage and sends it with every request in the `X-ABX-Token` header. There are no cookies, and no long-lived secret appears in a URL.
- Requests whose `Host` header does not name `127.0.0.1` or `localhost` (any port) are refused. This blocks DNS rebinding. `ABX_WEB_URL` adds one more address, the one you open the app at, and nothing wider; it must be `https://` unless it is this computer.
- A request other than a page load is refused if it comes from a page that is not on `127.0.0.1` or `localhost`.
- It sends no CORS headers, so no other website can read its answers. Answers carry a strict Content-Security-Policy with no inline script or style, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `Cache-Control: no-store`.
- The page loads nothing from the internet. Scripts, images and the brand fonts come from the app itself.
- The API key is never sent back to the page.
- Downloads use tickets that expire after 5 minutes. The page asks for a ticket when you choose Download (`POST /api/ticket`), then fetches the file with it (`GET /api/download?ticket=...`). A ticket serves one whole-file download of one file. If that download is interrupted, the same link answers resume (Range) requests for the rest of the file until the last byte has been delivered or 5 minutes pass; after a completed download the link is finished. Tickets work only for files inside your exports folder, re-checked on every request.
- At most 16 requests are handled at once; beyond that the app answers "busy" (HTTP 503) at once instead of queueing.
- `POST /api/validate-cancel` stops a running validation. `POST /api/export-delete` deletes one export folder: only a direct child of the exports folder that holds a `manifest.json`, never one that is running, and only with the session token from the same page. It never removes a `.lock` file.
- Request bodies larger than 1 MB, and bodies that are not JSON, are refused. A client that stalls is cut off after 30 seconds; a download is allowed 300 seconds per read or write.

Stop the app with Ctrl+C in its terminal, or by stopping its container. The session tokens end with it.

## Threat notes

- The browser app protects your session from other websites and from other users of the computer. It does not protect against software already running under your own user account, which can read your files anyway.
- Anyone who can read your exports folder can read your exports. Keep it on an encrypted disk, and delete exports you no longer need.
- Over SSH, forward the app's port to your own computer (`ssh -L`). Do not expose it any other way.
- Keep the API key out of shell history, scripts and scheduler definitions. CLI.md shows how.
- A corporate proxy that inspects TLS sees the traffic it decrypts, including the key. That is a property of your network, not of the tool.
