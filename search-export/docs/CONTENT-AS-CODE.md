# Saved views and fieldsets as code

The tool can write your tenant's saved views and fieldsets to files, compare those files with a tenant, and make a tenant match them. The files are called a bundle.

Use it to:

- **Keep a history.** Put the bundle in git. Each export changes only the files of views and fieldsets that changed, so `git diff` shows exactly what someone changed in Abstract, and when.
- **Restore.** If a view or fieldset is deleted or changed by mistake, apply yesterday's bundle to put it back.
- **Copy between tenants.** Export from one tenant and apply to another: a test tenant to production, one business unit to the next, or a managed service provider's standard pack to each customer tenant.
- **Ship an onboarding pack.** Keep a standard set of views and fieldsets for a data source in one bundle and apply it to every new tenant on its first day.

## What a bundle holds

```
bundle.json              what is in the bundle, when and where it came from, the SHA-256 of every file
fieldsets/<name>.json    one fieldset: its name, fields in order, description, tags, private or shared
views/<name>.json        one saved view: its search, time range, columns and the fieldsets it uses
```

- **No ids.** A view names the fieldsets it uses by their names, so the same bundle applies to any tenant.
- **Searches travel exactly.** A view's search is kept exactly as Abstract stores it: the filter-builder search, the KQL, or both.
- **Stable files.** Keys are sorted and the layout is fixed, so exporting the same tenant twice gives the same files.
- **Settings the tool does not know are kept, not sent.** If a view holds a setting this tool does not know, it is kept in the file under `unsupported`, with a note, and is never sent to a tenant.
- **Some things cannot travel, and the file says so in its `notes`:**
  - A view that also searched other sources, such as federated search, keeps only Abstract's own store.
  - A replay view's search lives in the replay job it ran, so it applies as columns and a time range only.

You can edit the files by hand: add a field to a fieldset, rename a view, or write a new view file. The tool notes any file that was changed or added after the export, and any file that is missing.

## Export

```
python3 abstract_search.py content export ./abstract-content
python3 abstract_search.py content export ./okta-pack --views "Okta failed sign-ins,Okta admin changes"
```

- `--kind views` exports every saved view, plus the fieldsets they use.
- `--kind fieldsets` exports every fieldset and no views.
- `--views` exports only those views, plus the fieldsets they use.
- `--fieldsets` exports only those fieldsets.
- Without a folder, the bundle goes to a new folder under `content/` in your exports folder.

`bundle.json` records which kinds the bundle holds in full (`scope`). This matters for prune.

## Compare

```
python3 abstract_search.py content diff ./abstract-content
```

Each view and fieldset is one of:

- **same:** identical on the tenant.
- **changed:** the tenant's copy differs; the line names what differs.
- **new:** not on the tenant.
- **only_in_tenant:** on the tenant but not in the bundle.
- **ambiguous:** the tenant has more than one item with that name.

Comparing changes nothing.

## Apply

```
python3 abstract_search.py content apply ./abstract-content
python3 abstract_search.py content apply ./abstract-content --yes
```

Applying is one review for the whole bundle, then one confirm. Export a backup bundle of the tenant first: it is your only undo.

1. **The review.** The tool reads the tenant and lists every change it will make:
   - fieldsets to create or update first, because a view may use a fieldset the bundle creates;
   - then views to create or update;
   - anything it will skip, with the reason. For example, a fieldset field this tenant does not have, or a view that uses a fieldset neither the bundle nor the tenant has.
2. **The confirm.** Without `--yes` the command stops after the review (exit code 3). With `--yes`, or when you confirm in the browser app or the terminal, it makes the changes in order.
3. **Each change.** The tool re-reads each item just before it changes it. If someone changed that item after your review, the tool stops there instead of overwriting it.
4. **If one fails.** The tool stops. It says what it already did, what failed and what it did not try. Fix the problem and apply again: everything already done compares as the same and is skipped.

Every change is written to `logs/changes-YYYY-MM-DD.log`.

To copy between tenants, export while connected to the first tenant, then connect to the second (its own `ABSTRACT_VENDOR_ACCOUNT_ID`, key and `ABSTRACT_EXPECT_TENANT`) and apply the same folder.

## Prune: delete what is not in the bundle

```
python3 abstract_search.py content apply ./abstract-content --prune
```

**With `--prune`, views and fieldsets that are on the tenant but not in the bundle are deleted, but only for a kind the bundle holds in full.**

- **A views-only bundle** (`--kind views`) never deletes fieldsets, even though it carries the fieldsets its views use.
- **A bundle of chosen views** (`--views`) deletes nothing.
- **A hand-made bundle without `scope`** counts a kind as held in full when the bundle has at least one item of it.

The review says which kinds prune leaves alone. The review starts with a line in capitals giving how many will be deleted, and lists each one. Use prune only when the bundle is meant to be the whole truth for that tenant, and read the list before you confirm. A file listed in `bundle.json` but missing from the folder counts as not in the bundle, so with prune its view or fieldset would be deleted. The tool notes every missing file.

## In the browser app, the terminal and an AI assistant

- **Browser app:** export a bundle, upload it to compare, review and apply.
- **Terminal:** main menu, **Views and fieldsets as code**.
- **AI assistant over MCP:** the tools are `content_export`, `content_diff` and `content_plan`. Applying needs the server to be started with `--allow-writes`; the assistant is told to show you the plan and wait for your yes before it calls `apply_change`, but the tool cannot check that you said it (see MCP.md).

Your API key needs Abstract's permission to change views and fieldsets to apply a bundle. Exporting and comparing only read.
