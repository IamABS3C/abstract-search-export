# Example saved searches

The first time the tool needs your saved-searches folder (for example when the browser app starts, or when you first list or open saved searches), these examples are copied into `~/AbstractExports/saved-searches/` with an `example-` prefix. They are copied only once, when that folder is created, so a new version of the kit does not replace them. The examples never contain credentials.

Their dataset names come from the demo tenant ("Palo Alto Firewall" and "Okta"). Before you run one, change the dataset to the exact name of one of yours: `python3 abstract_search.py datasets` lists them, and a name your tenant does not know is refused. Load the example in the browser app (Saved searches) or the guided terminal (main menu, Saved searches), change the dataset name, then save it under your own name.

| File | Shows |
|---|---|
| `firewall-rdp-from-internet.json` | A number filter plus an IP filter: RDP (port 3389) from public addresses |
| `dataset-full-documents.json` | Every field of every event for one dataset |
| `cloud-audit-errors-kql.json` | A KQL search: cloud audit events whose raw record holds an error code and whose outcome is failure. It converts into filters, so it exports exactly; on a tenant without cloud audit data it matches nothing |
| `okta-failures-not-service-accounts.json` | A "not" filter with **or has no value** (user names that do not start with `svc-`, or no user name) and an **At least one of** group (the action contains `authentication` or `mfa`) |

To run one from the command line without loading it, use its file name: `python3 abstract_search.py export --from-saved example-dataset-full-documents.json`. Its dataset must exist on your tenant.
