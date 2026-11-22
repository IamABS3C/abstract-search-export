# Validation

Validate runs every check an export runs, and exports nothing. Run it before any large export, and before you report a number to anyone. It is the same checklist in the browser app (Validate), the guided terminal (the Review step, or the main menu's Validate the search) and the command line (`validate`). In the browser app, Export is enabled only after the current search passes Validate.

## What it confirms

Validate confirms three things before you rely on a result:

- each filter is tested on your tenant; a filter that cannot be confirmed to narrow the results stops the export or is flagged as a warning;
- a zero is a real zero, not a refused or failed search;
- the export can be planned in time slices that are each reconciled, so an export that stops early is reported INCOMPLETE, never finished.

## What the statuses mean

| Status | Meaning |
|---|---|
| PASS | Proven. |
| WARN | The export can run, but read the note first. |
| FAIL | The export will not run until this is fixed. |
| INFO | For your information. Nothing to fix. |

Validate is ready to export when nothing is FAIL and at least one event matched.

## The checks, in order

Rows appear only when they apply. The Retention row, for example, appears when the range starts more than a day ago (which includes Last 24 hours, because relative ranges end 10 minutes ago) or when Where to search is Recent data only.

### Connected tenant

Always PASS. Shows the tenant name and vendor account ID the search will run against. If it is the wrong tenant, disconnect and connect with the right vendor account ID.

### Search

FAIL, with the reason, when the search cannot run. Common reasons:

- Something is missing: no dataset or filter, a filter with no value, no time range.
- The dataset name is not one your tenant knows: "no dataset called '...' on this tenant — a partial name matches nothing", with a "Did you mean ...?" when a close name exists.
- A field does not exist on your tenant: "These fields do not exist on your tenant: ...". Fields under `ext.*` and `event.parsed.*` are not checked.
- A filter could not be confirmed: "the filter on ... could not be confirmed: a value that cannot exist still matched ...".
- The platform refused to count the range: shorten it and validate again.
- `--storage archive` with a search that runs as KQL: "Archive search runs a dataset and filters. Choose Automatic or Recent data only for this search."

Every message is listed with its fix in TROUBLESHOOTING.md. There is no Search row when the search is fine.

### Time window

PASS shows the exact window in UTC. The platform works in whole seconds, and the window covers the start second up to, but not including, the end second.

WARN when the window ends less than 10 minutes ago. Late events can still arrive and change the totals. Relative ranges such as Last 24 hours end 10 minutes ago for this reason. For a final export, choose a window that ended earlier.

### Retention

Where your range is kept, and whether all of it can be searched. The edge between recent data and the archive comes from your tenant's own retention setting, placed a few days inside recent data.

| Status | Detail starts with | Meaning |
|---|---|---|
| PASS | "the whole range is in recent data" | Nothing to do. |
| INFO | "events before DATE are older than recent data and are read from the archive, with N archive job(s)" | Automatic search reads the older part with archive jobs, during the export. Validate does not run them, so the plan below counts recent data only. |
| INFO | "this export searched recent data only" | You chose Recent data only. Older events kept in the archive are not included. The export result carries the same line as a warning, starting "Recent data only:". |
| WARN | "… The archive keeps N days, so nothing before DATE exists to export" | The range starts before the archive's retention. Abstract no longer holds that part, so it is not missing from the export; copies made earlier may still exist. |
| WARN | "the archive cannot be searched with this key" or "could not check how far back recent data goes" | The export covers recent data only. It finishes as "Partial: recent data only. Older data was not searched.", naming the range not searched; exit code 3 on the command line. |
| FAIL | "events before DATE are older than recent data and only in the archive, which a KQL search cannot read" | Shorten the range to recent data, or use a dataset and filters. |

The Export plan row then reads "N events from recent data, plus everything before DATE from the archive: N archive job(s), roughly M minutes." After an export, a retention limit is also shown under the result in the browser app, listed in `warnings`, and printed on the command line as a `! Retention: …` or `! Recent data only: …` line, even with `--quiet`.

### Fields

Always PASS when present. Lists the fields that will be exported, or "every field of every event". A field that does not exist fails the Search row instead.

### Filter proof: field

One row per filter, named after its field (the dataset filter shows as "dataset"). Each filter is proven on the first hour of the range (or of its part in recent data), called the sample hour. If that hour holds nothing to test the filter on, the proofs run again on the busiest window the tool counted, which can be several hours long. During an export, the log then says "the first hour had nothing to test the filter on — proving it on ...".

For "is one of" and "starts with" on text fields, and for the dataset, the proof is an impossible value. The tool runs the same search with the value replaced by one that cannot exist (`zzz-abx-no-such-value-qqq`). It must match 0 events.

- PASS: "A value that cannot exist matched 0 events, so the filter is applied".
- If it matched events, the filter could not be confirmed. The search stops, and the Search row shows "the filter on ... could not be confirmed". Change the operator to "is one of".

For a "not" operator (is not one of, does not contain, does not start with, and the others), the proof is its opposite (for example *contains* for *does not contain*). In the sample hour, no event may match both, and together they must cover every event where the field has a value.

- PASS: "'does not contain' (N) and its opposite (M) never overlap and together cover every event with a value (T) — this filter is exact". N + M can be a little more than T: a value made only of punctuation, such as `/`, counts as empty for *has a value* but is still seen by both.
- WARN, UNPROVEN: nothing in the sample hour matched the opposite, so an applied filter and an ignored one would look the same. Check the preview.
- Otherwise the platform did not apply the filter as written. The search stops with "the filter on … could not be confirmed: it and its opposite should never overlap and should cover every event with a value, but …".
- When the field has values longer than 256 characters, this proof is replaced by the tool applying the filter itself (see Long text values).

For every other operator, the proof is leave-one-out. The tool counts the search with and without this filter.

- PASS: "Removing it changes the count (N vs M), so the filter is applied", where N is the count with the filter and M the count without it.
- WARN, UNPROVEN: "this filter made no difference in the sample window, so the tool cannot confirm it narrows the results. Check the preview before exporting." This is normal when every event in that window already matches, for example "has a value" on a field every event has. Check the preview before exporting.

Either kind can also be:

- WARN, inconclusive: the platform returned an error instead of a count. The Filtered vs unfiltered row decides.

### Long text values

INFO, when a filter uses a text field that has values longer than 256 characters in your range: "url.original has values longer than 256 characters, which most of Abstract's search filters do not see. The tool fetches every event where this field has a value and applies this filter itself, so they are matched correctly. The count below is an upper bound until the export runs." The filter's own proof row then reads "values longer than 256 characters are present, so the tool applies this filter to every event itself". The Export plan then says "up to N events", and the Cross-check row says the search was counted one way. The export itself is exact: each event fetched for this reason is checked against your filter, and the ones that do not match are left out and counted in the manifest as `checked_out`. Every export with a text filter runs this check on every event, long values or not, so an event Abstract's search returns for a value it reads differently (for example one made only of punctuation) is left out the same way.

### Filter proof: KQL

PASS, for a search that runs as KQL: "KQL is applied on this tenant (a value that cannot exist matched 0 events)". The tool runs a KQL search for a value that cannot exist, which must match nothing. Every export of a KQL search runs this check again first, and stops if the tenant no longer applies KQL.

A KQL query that converts into filters (Exact export) is validated like any other search, with a proof for each filter. A KQL result table (`summarize`, `extend` and the like) is not an export of events, so it has no Validate: Abstract's KQL check runs before it, and Preview and Save result table show and save the table (see EXPORTS.md, KQL result tables).

### Filter proof

INFO, when At least one filter must match is chosen: "filters where any one may match cannot be proven one at a time; the filtered vs unfiltered comparison covers them together". Removing one of them proves nothing on its own. The Filtered vs unfiltered row checks them together, and the dataset filter is still proven on its own.

### Filtered vs unfiltered

Your search against the whole tenant, in the sample window: "N of M events". There is one row per window tested, so a re-run proof adds a second row.

- PASS: your search matched fewer events than the tenant holds.
- WARN: it matched every event. This passes only when each filter was proven on its own, and a Filter note row says so. If the parts could not be proven, the search stops: "the filter could not be confirmed: it matched every event in the sample and not every part of it could be proven".

### Filter note

INFO. Extra detail from the filter proofs, for example that the filter matched every event in the sample hour but each part was still proven.

### Cross-check

The same search counted a second, independent way.

- PASS: "a second, independent search also counted N events".
- WARN: the counts differ. Usually the window ended recently and late events arrived between the two counts. Move the end time earlier and validate again.
- INFO: the search can be counted only one way, so there is no second count. The detail says why:
  - "the range spans recent data and the archive, so each part was counted one way";
  - "the whole range is older than recent data, so it was counted one way";
  - "an archive search is counted one way" (Where to search is Archive search);
  - "a KQL search is counted one way (the KQL proof above shows this tenant applies it)";
  - "no second search method was available, so the count was checked one way only".

When nothing matched, the second count always uses a different search from the one that found the zero.

### Export plan

PASS: how many events will be exported, in about how many time slices. It appears only when something matched.

### Estimated time

INFO: how long the export should take, in all and by part, for example "About 12 minutes in all: recent data about 3 minutes, archive about 9 minutes." It is a guide, not a check, from rates measured with this tool (see EXPORTS.md, Typical sizes and speeds). Before an archive job runs, the number of older events is unknown, so the archive part assumes as many events per day as recent data, and the row says so. During the export, the browser app shows **time left (estimate)**, the terminal adds it to the progress line, and the command line adds it to each finished slice.

### Nothing matched

WARN, in place of the Export plan row, when the search matched 0 events. The row says whether a second, independent search also counted 0. The browser app and the guided terminal then offer to export anyway, which writes an empty file. See below.

## A zero is a claim

"No events" is the most expensive answer to get wrong. Before you report an absence:

1. Read the Nothing matched row. It says whether a second, independent method also counted 0.
2. Widen the time range. Some sources deliver in batches and look empty near the present.
3. Check the dataset name. Pick it from the suggestions rather than typing it. A name the tenant does not know is refused, but a real dataset that received nothing in your window is not.
4. Remove filters one at a time to see which one removes everything.

## During an export

The same protections keep running while an export runs:

- Each time slice is counted and reconciled against what was written; a slice that does not reconcile is retried or split, and the export is reported INCOMPLETE rather than finishing short.
- An error is never treated as the end of the data. A failed request is retried. A slice the platform keeps refusing is split into smaller slices.
- When you filter by dataset, every exported event's dataset is checked. An event from another dataset stops the export, because the dataset filter could not be confirmed.

EXPORTS.md explains the reconciliation in detail.
