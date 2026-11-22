# Fieldsets and saved views in Abstract

The tool reads, builds and changes two things in your Abstract tenant:

- **Fieldsets**: a named list of columns, in order. Saved views use them to show the same columns everywhere.
- **Saved views**: a named search with its time range and columns.

These are the only things the tool can change in your tenant. Every change follows the same three steps.

**Back up first.** There is no undo: the tool cannot bring back a deleted item, and Abstract has no undo for these changes. Export a bundle of the tenant before you change it (`content export`, or **Saved views → Content as code → Export bundle**); applying that bundle later is how you restore. Items are matched by name, so an item with the same name is updated whoever created it.

**Who sees the change.** The review always says:

| You are | The review says |
|---|---|
| Creating a private fieldset (the default) | "Only you will see this in Abstract. An API key with views permission on this tenant can still read it." |
| Creating a fieldset you chose to share | "Everyone on the tenant will see this." |
| Creating a saved view | "Everyone on the tenant who can open saved views will see this: saved views have no private setting." |
| Updating or deleting anything | "This changes it for everyone who can see it." |

## Review, confirm, done

1. **Review.** The tool reads what is there now and shows exactly what will change: fields added, removed or reordered, the name, the search, the time range. It shows the request it will send (for example `PATCH /v1/streamviewer/field-set/{id}`) and any warning, such as who made the item.
2. **Confirm.** Nothing is changed until you confirm. In the browser app and the terminal you confirm in the review; on the command line you run the same command again with `--yes` (without it, the command shows the review and exits with code 3).
3. **Done.** Just before sending the change, the tool reads the item again. If someone else changed it after your review, it stops and asks you to review again, so it never overwrites their change.

Every change is written to `logs/changes-YYYY-MM-DD.log` in your exports folder: the time, the tenant, what was created, updated or deleted, its name and fields. Never the key.

Your API key needs permission to change views and fieldsets. Without it, the change is refused with HTTP 403 and nothing happens.

## Build a fieldset

1. Pick a dataset. The tool learns which fields it fills and how often (coverage), from a sample of recent events, read live and kept in memory only.
2. Choose fields from that list, from your tenant's full field list, or by name. `ext.*` and `event.parsed.*` fields can be added by name.
3. Put them in order. The order is the column order in Abstract.
4. Name it, choose private or shared, and save. The review shows the whole list before anything is created.

A fieldset holds stored fields only. A column computed by a KQL query (`extend`, `parse`, `project x = …`) cannot go in a fieldset, and the tool refuses it with that reason. Abstract also refuses unknown standard fields.

You can also update a fieldset (rename it, add or remove fields, reorder them), delete it, or use its fields as the columns of the current search.

## Save a search as a saved view

Any search you can run here can be saved as a saved view in Abstract, with its time range and its columns (its own fields, or a fieldset you pick).

- A search built with filters is saved in the filter-builder form Abstract's own views use, so it opens in Abstract's filter builder. Where your tenant applies KQL, the view also carries the same search as KQL.
- A KQL query is saved as KQL.
- Loading the view back gives the same search: on a test tenant, a saved view and the search it came from matched the same number of events.

You can also replace a view's search with the current one, rename it, or delete it.

## Where to find it

| | Fieldsets | Saved views |
|---|---|---|
| Browser app | **Fieldsets** panel: list, the fieldset builder, update, delete | **Saved views**: Save current search as a view, Update view, Delete view |
| Guided terminal | Main menu: **Fieldsets in Abstract** | Main menu: **Saved views from Abstract → Save the current search as a view, or change or delete a view** |
| Command line | `fieldsets`, `fieldsets show`, `fieldsets create`, `fieldsets update`, `fieldsets delete` | `views save`, `views update`, `views delete` |
| AI assistant over MCP | `fieldsets_list`, `fieldset_get`, `fieldset_plan`; `apply_change` only when the server runs with `--allow-writes` | `views_list`, `view_plan`; `apply_change` only when the server runs with `--allow-writes` |

Command-line examples:

```
python3 abstract_search.py fieldsets
python3 abstract_search.py fieldsets create --name "Okta triage" --from-dataset "Okta" 
python3 abstract_search.py fieldsets create --name "Okta triage" --from-dataset "Okta" --yes
python3 abstract_search.py fieldsets update "Okta triage" --add ext.client_zone --remove message --yes
python3 abstract_search.py views save --name "Failed Okta logins" --kql 'events | where data_stream.dataset =~ "okta" and event.outcome == "failure"' --last 24h --fieldset "Okta triage" --yes
python3 abstract_search.py views delete "Failed Okta logins" --yes
```
