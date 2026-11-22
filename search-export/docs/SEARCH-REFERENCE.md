# Search reference

## Dataset

The dataset is the name of an integration configuration, for example your firewall or identity feed. Use the full name: upper and lower case do not matter, but a partial name matches nothing. Pick it from the suggestions to avoid typos. On the command line, `datasets` lists every name.

Validate and every export check the name against your tenant's datasets. A name the tenant does not know is refused with "no dataset called '…' on this tenant", and a "Did you mean …?" when a close name exists, because a misspelled dataset would match nothing and a zero is easy to misread.

## Filters

The tool asks the platform for each field's type, then offers the operators for that type. They match the way Abstract's own search matches.

| Field type | Examples | Operators |
|---|---|---|
| Text | `user_name`, `action`, `url.original` | is one of, is not one of, contains, does not contain, starts with, does not start with, ends with, does not end with, has the word, does not have the word, matches the pattern (regex), does not match the pattern, has a value, is empty |
| List of text | `tags` | has any of these items, has all of these items, has an item containing, has no item containing, has a value, is empty |
| IP address | `source_ipv4`, `dest_ipv4` | is one of, is not one of, is in subnet (CIDR), is not in subnet, is private, is public, has a value, is empty |
| Number | `dest_port`, `event.duration` | is one of, is not one of, greater than, at least, less than, at most, between, not between, has a value, is empty |
| True/false | flag fields | is true, is false |

How they match:

- **Upper and lower case.** Text operators ignore case. Tick **Match case** on the filter to match exact case (on the command line, use the `_cs` operators, `==` or `in`).
- **has the word** matches a whole word: `has the word admin` matches `admin`, `svc-admin` and `admin@example.com`, but not `administrator`. A word ends at anything that is not a letter or a digit.
- **Empty.** *is empty*, *has a value* and the "not" operators look for a letter or a digit, as Abstract's search does, so a value made only of punctuation, such as `/` or `-`, counts as empty for them. The other operators still match it: *is one of* `/` and *contains* `/` find it.
- **"Not" operators need a value.** Every operator except *is empty* only matches events where the field has a value. So `user_name does not contain example.com` leaves out events with no `user_name`, exactly as Abstract's search does. To keep those too, tick **or has no value** on the filter.
- **between** and **not between** take two numbers, `from, to`, and include both ends.
- **Lists.** *has any of these items* and *has all of these items* compare whole items. *has an item containing* matches part of an item.
- **Several values.** *is one of*, *is not one of* and the list operators take several values separated by commas, for example `alice, bob, carol`. A value in these lists cannot contain a comma. The other operators take one value, which may contain commas.
- **Patterns** are regular expressions that match anywhere in the value, for example `^admin-` or `(alice|bob)@`. The tool checks the pattern before it runs. Abstract's search refuses some patterns, for example `^` or `$` inside brackets; the message then ends "failed to parse regex", so write the pattern another way.
- **Long text values.** Text values longer than 256 characters, such as long URLs or messages, still count as a value, but most of Abstract's search filters do not see them: *starts with*, *ends with*, patterns, *is one of*, and *contains* with anything other than a plain word. When a filter uses a field that has such values in your time range, the tool fetches every event where that field has a value and applies the filter itself, so the export is exact. It can take longer, because more events are read. Validate shows this as **Long text values** and the count as "up to". On the command line, `--no-long-check` turns this off.
- **The archive.** Archive jobs match patterns with exact case, and *contains* on a list field only matches whole items. For those filters the archive job reads every event where the field has a value, and the tool applies the filter itself, so the result is the same as on recent data.
- **Every event is checked.** Whenever a search has a text or list filter, the tool checks each event it receives against the search before writing it, and leaves out any that do not match. The manifest counts those as `checked_out`.

IP operators need an IP field such as `source_ipv4`. Address fields such as `source_address` are text, so use the text operators on them.

These rules apply in the browser app, the terminal and the command line alike. Every filter is still tested before an export runs (see VALIDATION.md).

## Every filter, or at least one

With more than one filter, you choose how they combine:

| Choice | Command line | Meaning |
|---|---|---|
| Every filter must match (default) | `--match all` | An event must pass every filter. |
| At least one filter must match | `--match any` | An event must pass at least one filter. |

The dataset always applies: with "at least one", the search is the dataset AND (filter 1 OR filter 2 …).

A search copied from Abstract that mixes `and` and `or` can also bring **at least one of** groups. Each group shows on its own line, for example *at least one of: user_name is one of "a"  OR  action is one of "c"*. Every group must hold, as well as the filters. You can remove a group, but you build new ones by pasting a search or with `--where` on the command line. In the browser app the choice is **With more than one filter**, under the filters; in the terminal it is **Match: …** in the Filters step. Validate cannot prove filters where any one may match one at a time; it proves them together by comparing the search with the whole tenant (see VALIDATION.md).

## Time range

- Relative ranges (last 15 minutes up to last 30 days) end 10 minutes ago, so late-arriving events have settled.
- A custom range takes a UTC start and end, for example `2026-09-28T00:00:00Z`. The end must not be in the future.
  - Browser app: **Custom range (UTC)** has a date and time picker for the start and the end, read as UTC, with the same moment in your own time zone shown under each, and quick ranges: Today, Yesterday, Last 7 days, Last 30 days, Last month (whole UTC days).
  - Terminal: the Time step offers **Today (UTC)**, **Yesterday (UTC)**, **Pick days from a calendar (UTC)** (a month, then a day, for the first and the last day) and **Type an exact start and end**, where a date alone such as `2026-09-28` means the start of that day.
  - Command line: `--start` and `--end`.
- The platform works in whole seconds. A window covers the start second up to, but not including, the end second. Fractions of a second are dropped.
- The range filters on `@timestamp`, the event's own time. `ingested_time` is when Abstract stored it.

## Where to search

| Choice | Command line | What it searches |
|---|---|---|
| Automatic (recommended) | `--storage auto` (default) | Recent data, plus the archive for anything older (archive jobs: minutes to an hour). |
| Recent data only | `--storage recent` | Recent data only. Fastest. Older events kept in the archive are not included, and the result says so. |
| (command line only) | `--storage archive` | The whole range from the archive, with archive jobs. Needs a dataset and filters. |

See EXPORTS.md, Where to search, for what each choice means for an export.

## Use a search from Abstract

You can start from a search you already have in Abstract: one of your tenant's saved views, or a search copied from Abstract's search box, for example

```
events | where ['data_stream.dataset'] == "crm - audit events" and user_name == "a.person@example.com" and action == "ContentTransfer" | sort by ['@timestamp'] desc
```

The tool shows you what it will run ("This will run as …") before anything runs, in this order:

1. **Converted first.** The search is converted into this tool's own dataset, filters, time range and field list whenever it can be. Each filter is then proven by Validate, like any other search.
2. **KQL where the tenant supports it.** If the search cannot be converted (for example it uses `or`, `contains` or brackets) and your tenant supports KQL searches, it runs as KQL, exactly as written. It shows as "runs as KQL: …". Validate then shows a "Filter proof: KQL" row, and every export of it checks again that the tenant applies KQL first. What this tenant supports tells you whether KQL is available.
3. **Refused otherwise.** If it cannot be converted and KQL is not available, it is refused with the reason, ending "KQL is not available on this tenant, so it cannot run as written either."

A search that runs as KQL reads recent data only. The archive needs a search with a dataset and filters.

You can also write KQL directly, or describe the search in plain English and let Abstract's KQL assistant write it: see "How a KQL or plain-English search is checked before it runs" below. A query that reshapes rows (`summarize`, `extend` …) is not an export of events; preview it and save its result table instead.

Before converting, the search is checked by Abstract's own KQL validator against your tenant's fields, where the tenant offers it, so anything Abstract's search would reject (an unknown field, `!=` on a free-text field such as `message`) is refused here too.

What converts, and how:

| In the search | Becomes |
|---|---|
| `field == "x"`, `field in ("x", "y")` | is one of, exact case (as in Abstract's search) |
| `field =~ "x"`, `field in~ (...)` | is one of, any case |
| `field != "x"`, `!in` / `field !~ "x"`, `!in~` | is not one of, exact case / any case |
| `['data_stream.dataset'] == "x"` | the dataset (dataset names match in any case) |
| `contains`, `!contains` | contains, does not contain |
| `has`, `!has` | has the word, does not have the word |
| `startswith`, `!startswith`, `endswith`, `!endswith` | starts with, does not start with, ends with, does not end with |
| `hasprefix`, `hassuffix` (and `!`) | a word starting or ending with the value: *at least one of* starts with (ends with) the value, or matches a pattern for it after (before) a character that is not a letter or digit |
| `matches regex "…"` | matches the pattern |
| `has_any ("a", "b")` / `has_all ("a", "b")` | at least one of the words / every word |
| `contains_cs`, `has_cs`, `startswith_cs` … | the same operator, exact case |
| `isnotempty(f)` / `isnotnull(f)`, `isempty(f)` / `isnull(f)` | has a value / is empty |
| `field > n`, `>=`, `<`, `<=` | greater than, at least, less than, at most (number fields) |
| `field between (a .. b)`, `!between` | between, not between (both ends included) |
| `field == n`, `!=`, `in`, `!in` on a number or IP field | is one of, is not one of |
| `field == true` / `false`, `!= true` / `false` | is true / is false, on true/false fields |
| `ipv4_is_in_range(f, "10.0.0.0/8")` | is in subnet; inside `not(…)`, is not in subnet |
| `list_field == "x"`, `list_field contains "x"` | has any of these items (exact item) / has an item containing |
| `or`, brackets, any mix of `and` and `or` | filters plus *at least one of* groups that match exactly the same events |
| `not(…)` | the opposite operator for each condition inside it, for example `not(a == "x" or b contains "y")` becomes `a is not one of "x"` and `b does not contain "y"` |
| `['@timestamp'] > datetime(...)`, `>=`, `<`, `<=` | the time range |
| `\| project a, b` | the fields to export |
| `\| sort by …`, `\| order by …` | ignored: the export holds every matching event either way |

KQL itself counts an empty field as "not equal" and "not containing". Abstract's search, and this tool, do not: `!=`, `!contains` and `not(…)` match events that have a value. Tick **or has no value** on the filter to include the empty ones.

Cannot be converted. Where your tenant supports KQL, a search that contains these runs as KQL instead; otherwise it is refused, with the reason:

| In the search | Why, and what to do |
|---|---|
| Other functions, such as `strlen()`, `tolower()` or `parse_json()` | Only `isempty`, `isnotempty`, `isnull`, `isnotnull` and `ipv4_is_in_range` can be converted. |
| `has` on a list field | Use `==` for an exact item or `contains` for part of an item. |
| `== ""`, `!= ""` (an empty value) | Use `isempty(f)` or `isnotempty(f)`. |
| A value with a leading or trailing space | A leading or trailing space would not be kept in the search. Remove it, or build the filter here. |
| A comma inside a value in `in (…)` | A value in a list cannot contain a comma. Use `==` or `contains` for that value. |
| A date field other than `@timestamp`, or `not(…)` around `@timestamp` | Only `@timestamp` sets the time range, and only with `>`, `>=`, `<`, `<=`. |
| So many alternatives that the search would need more than 64 groups | For example seven or more `(a and b) or (c and d) …` alternatives. Split the search into several exports. |
| `\| take`, `\| limit` | A row limit would change "export everything". Remove it before you use the search. |
| Any other stage, such as `\| summarize` | Only `where`, `sort` / `order` and `project` can be converted. |

To run it as filters instead, rewrite the search without them, or build it here with a dataset and filters. Unusual input always gives a readable reason, never a crash.

Saved views are read live from the tenant you are connected to each time, and never stored unless you save them. Every view is listed:

| Kind | What it holds | What the tool does |
|---|---|---|
| KQL | A search in Abstract's search box (KQL) | Converts it, as above. |
| Filter builder | Conditions built with Abstract's filter builder (they are read from the view itself, because the list of views leaves them out) | Converts every condition and its `and` / `or` groups, as above. |
| Replay | A view a detection replay made: its search is in the replay job it ran, which the tool reads | Converts that search and uses the replay's time range. |
| Columns only | Columns and a time range, no filter | Loads the columns and the time range. Add a dataset or a filter before you preview or export. |

A view's own time range and columns are used too: a relative range ("last 24 hours") or the exact start and end it was saved with, and its columns as the fields to export. `views` on the command line, Saved views from Abstract in the terminal menu, and Check every view in the browser app show which of your views are ready, how each one runs, and why the others cannot. **Save all as saved searches** (browser), **Save every view as a saved search** (terminal menu) or `views --save` (command line) copies every view that loads into your saved searches; doing it again updates them. If a saved view also searched other sources (such as federated search), only events stored in Abstract are exported, and the tool says so.

## How a KQL or plain-English search is checked before it runs

Every KQL query is checked first, without running it. The tool then shows one of four answers:

| Answer | What it means |
|---|---|
| **Exact export** | The query only filters events, so it is converted into the tool's own filters. Every exported event is re-checked against them, and Validate proves the filter (see What Validate checks). This is the most reliable way to export. |
| **Runs as KQL** | The query filters events but cannot be converted, for example because it uses a function. Where your tenant applies KQL, it runs on Abstract exactly as written. The tool cannot re-check KQL row by row, so the plan and the manifest say so. |
| **KQL result table** | The query reshapes the rows: `summarize`, `extend`, `project` with computed columns, `top`, `take`, `distinct` and similar. It is not an export of events. Preview shows the result table, and **Save result table** writes it to a CSV or JSON file (at most 10,000 rows; the file says when the query had more). |
| **Cannot run** | The KQL check refused it, or it needs KQL and your tenant does not apply KQL. The reason is shown, with what to change. |

Whether your tenant applies KQL is checked once per session by behaviour, without changing anything: a value that cannot exist must match nothing, and a search with no filter must match the full count. **What this tenant supports** shows the answer. Where KQL is not applied, every query that converts into filters still works.

When you describe the search in plain English, Abstract's KQL assistant writes the query:

- The query is shown to you first, with the same check as above. Nothing runs until you choose to run it.
- Read it before you run it. The assistant can choose a different field, value or time than you meant.
- Your text goes to your Abstract tenant's assistant, under your API key. Do not paste credentials into it.

Where to find it:

| | Plain English | KQL |
|---|---|---|
| Browser app | **Plain English** tab | **KQL** tab, with the live "how this runs" answer, field and value suggestions |
| Guided terminal | Step 2: **Describe it in plain English**; main menu: **Describe what you want in plain English** | Step 2: **Write a KQL query**; main menu: **Write or paste a KQL query** |
| Command line | `nl "…" [--preview]` | `kql check "…"`, `kql preview "…"`, `kql save "…"`, and `--kql "…"` on `preview`, `validate` and `export` |
| AI assistant over MCP | `nl_to_kql` | `kql_check`, `kql_preview`, `kql_save` |

```
python3 abstract_search.py kql check 'events | where data_stream.dataset =~ "okta" and event.outcome == "failure"'
python3 abstract_search.py nl "failed logins from outside the US" --preview --last 24h
```

In Windows PowerShell 5.1, double quotes inside an argument are removed. Put the query in a text file and use `--kql-file query.txt`.

## Fields

- `@timestamp` and `id` are always exported, first.
- The tool checks every field you export and every filter field against your tenant before it runs. A misspelled field stops the search with "These fields do not exist on your tenant". Fields under `ext.*` and `event.parsed.*` are not checked.
- Vendor fields with no standard name live under `ext.*`. On the command line you can export them by name with `--fields`, and filter on them by name with `--where`, for example `--where "ext.user_type contains Auto"`.
- Learn fields samples the dataset live and shows the fields it populates and how often. Suggested fields picks the most useful ones. Profiles are kept in memory for the session only.

Every field of every event exports complete events. The tool asks for every top-level field your tenant has, which returns the whole event. It is about 2 to 3 times slower than a field list and needs NDJSON, not CSV. See EXPORTS.md for sizes.

## Anything else

This tool is a companion to search in Abstract. Its filters cover every operator Abstract's search offers for these field types. For logic a filter cannot hold, such as a function, build the search in Abstract and use it here: it runs as KQL where your tenant supports it.
