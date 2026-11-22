"""MCP mode: an AI assistant (desktop and coding assistants, VS Code Copilot, Cursor and other MCP clients) uses this
tool's searches, checks, exports, saved views and fieldsets through the Model Context Protocol.

    python3 abstract_search.py mcp               stdio (the client starts this process)
    python3 abstract_search.py mcp --http 8765   streamable HTTP on 127.0.0.1 only, with a bearer token

The tenant comes from the environment, exactly as for the command line (ABSTRACT_VENDOR_ACCOUNT_ID,
ABSTRACT_EXPECT_TENANT, and ABSTRACT_API_KEY or ABSTRACT_API_KEY_COMMAND). The key is never a tool argument and never
appears in a result. A change to the tenant needs a plan from this same process and then apply_change with its token;
the plan tells the assistant to show the user the changes and get a yes first.

In stdio mode standard output carries protocol messages only: every log line and API call trace goes to standard
error.
"""
import hmac
import http.server
import json
import os
import re
import secrets
import socketserver
import sys
import threading
import traceback
import urllib.parse
from typing import Any, Dict, List, Optional

import abx_core as C
import abx_engine as E

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "abstract-search-export", "title": "Abstract Search & Export", "version": C.VERSION}
ROWS_DEFAULT, ROWS_HARD_CAP = 50, 500


def _rows_cap():
    """ABSTRACT_MCP_MAX_ROWS (default 50, never above 500): the most rows a preview returns to the assistant."""
    try:
        n = int(os.environ.get("ABSTRACT_MCP_MAX_ROWS") or ROWS_DEFAULT)
    except ValueError:
        n = ROWS_DEFAULT
    return max(1, min(n, ROWS_HARD_CAP))


MAX_ROWS = _rows_cap()
MAX_BODY = 1024 * 1024
WRITES_OFF = ("Changes to your Abstract tenant are switched off for this MCP server, so nothing was changed. The tool "
              "cannot check a \"yes\" typed in the chat, so writes are turned on only where the server is started: "
              "add --allow-writes to the server's arguments (abstract_search.py mcp --allow-writes) or set "
              "ABSTRACT_MCP_ALLOW_WRITES=1 in its environment, then restart it. To make the change without the "
              "assistant, use the browser app, the terminal or the command line with the same plan.")


def _writes_from_env():
    return os.environ.get("ABSTRACT_MCP_ALLOW_WRITES", "").strip() == "1"
ALLOWED_HOSTS = ("127.0.0.1", "localhost")

INSTRUCTIONS = (
    "Abstract Search & Export, connected to one Abstract tenant. Rules for using it well: "
    "(1) Call status first; if it is not connected, tell the user which environment variables to set. "
    "(2) Check KQL with kql_check before running it, and show the user how it will run (runs_as). "
    "(3) KQL written by nl_to_kql is a draft: show it to the user and run it only after they agree. "
    "(4) A zero is a claim: when a search matches nothing, say so with the time window it searched, never as "
    "'nothing happened'. "
    "(5) Changes to the tenant (fieldsets, saved views) are two steps: a plan call, then apply_change with its token. "
    "Show the user the plan's changes and get an explicit yes before apply_change. The server is READ-ONLY unless it "
    "was started with --allow-writes or ABSTRACT_MCP_ALLOW_WRITES=1 (status says which); plans always work, so you "
    "can show what would change. "
    "(6) validate before export_start; exports run in the background, poll export_status.")

SPEC_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "A search, the same shape the browser app saves. Give either mode 'simple' with a dataset and/or filters, or "
        "mode 'kql' with kql text (only where kql_check says runs_as 'kql'; prefer the filters kql_check returns). "
        "time is {\"last\": \"1h\"|\"24h\"|\"7d\"…} or {\"start\": ISO-UTC, \"end\": ISO-UTC}. "
        "A filter is {\"field\", \"op\", \"value\", \"type\"}: op is an operator such as IS_IN_LIST, "
        "IS_NOT_IN_LIST, CONTAINS, DOES_NOT_CONTAIN, STARTS_WITH, ENDS_WITH, MATCHES_REGEX, IS_EMPTY, IS_NOT_EMPTY, "
        "GREATER_THAN, LESS_THAN, BETWEEN; value is text (comma-separated for lists); type is String, Float64, Ipv4, "
        "Boolean or List(String). any_groups is a list of filter lists where at least one filter in each group must "
        "match. Easiest: run kql_check on a KQL query and pass its dataset, filters and any_groups here."),
    "properties": {
        "name": {"type": "string"},
        "mode": {"type": "string", "enum": ["simple", "kql"], "default": "simple"},
        "dataset": {"type": "string", "description": "the dataset's full name (any case)"},
        "filters": {"type": "array", "items": {"type": "object"}},
        "any_groups": {"type": "array", "items": {"type": "array", "items": {"type": "object"}}},
        "match": {"type": "string", "enum": ["all", "any"], "default": "all"},
        "kql": {"type": "string"},
        "time": {"type": "object"},
        "fields": {"type": "array", "items": {"type": "string"}},
        "full_documents": {"type": "boolean", "default": False},
        "storage": {"type": "string", "enum": ["auto", "recent", "archive"], "default": "auto"},
        "format": {"type": "string", "enum": ["ndjson", "ndjson.gz", "csv"], "default": "ndjson.gz"},
    },
}
TIME_SCHEMA: Dict[str, Any] = {"type": "object", "description": "{\"last\": \"1h\"} or {\"start\": ISO-UTC, \"end\": "
                               "ISO-UTC}; default the last hour"}


def _obj(props, required=None):
    out: Dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = list(required)
    return out


class ToolError(Exception):
    """A tool failed in a way the assistant should read (returned with isError: true)."""


class Server:
    def __init__(self, session=None, allow_writes=None):
        self.allow_writes = _writes_from_env() if allow_writes is None else bool(allow_writes)
        self.s = session or C.Session()
        self._lock = threading.RLock()
        self._tokens = set()                 # plan tokens this process issued
        self.initialized = False
        self.tools = self._tool_table()

    # ── connection ──
    def connected(self):
        return bool(self.s.client)

    def ensure(self):
        if self.s.client:
            return self.s
        expect = os.environ.get("ABSTRACT_EXPECT_TENANT")
        if not os.environ.get("ABSTRACT_VENDOR_ACCOUNT_ID") or not (
                os.environ.get("ABSTRACT_API_KEY") or os.environ.get("ABSTRACT_API_KEY_COMMAND")
                or os.environ.get("ABSTRACT_API_KEY_FILE") or os.environ.get("ABSTRACT_API_KEY_FROM_NETWORK") == "1"):
            raise ToolError("Not connected. Set ABSTRACT_VENDOR_ACCOUNT_ID, ABSTRACT_EXPECT_TENANT and "
                            "ABSTRACT_API_KEY_COMMAND (or ABSTRACT_API_KEY) in this MCP server's environment, then "
                            "restart it. See docs/MCP.md.")
        saved = os.environ.pop("ABSTRACT_EXPECT_TENANT", None)
        try:
            t = self.s.connect_from_env()
        finally:
            if saved is not None:
                os.environ["ABSTRACT_EXPECT_TENANT"] = saved
        if not t:
            raise ToolError("Not connected: the API key could not be read from the environment. Set "
                            "ABSTRACT_API_KEY_COMMAND (preferred) or ABSTRACT_API_KEY and restart the server.")
        if not expect:
            vid = t["vendor_account_id"]
            self.s.disconnect()
            raise ToolError(f"Connected as {C.sanitize(t['name'])} ({vid}) but ABSTRACT_EXPECT_TENANT is not set. Set "
                            f"ABSTRACT_EXPECT_TENANT={vid} so nothing can run against the wrong tenant, then restart.")
        if expect != t["vendor_account_id"]:
            vid = t["vendor_account_id"]
            self.s.disconnect()
            raise ToolError(f"Tenant mismatch: the key belongs to {vid}, ABSTRACT_EXPECT_TENANT is {expect}.")
        return self.s

    # ── tools ──
    def _tool_table(self) -> List[Dict[str, Any]]:
        def t(name, title, desc, schema, fn, read_only=True, destructive=False):
            return {"name": name, "title": title, "description": desc, "inputSchema": schema, "fn": fn,
                    "annotations": {"readOnlyHint": read_only, "destructiveHint": destructive,
                                    "openWorldHint": True}}
        no_args = _obj({})
        return [
            t("status", "Connection status",
              "Which Abstract tenant this server is connected to, or exactly what to set when it is not. Call first.",
              no_args, self.t_status),
            t("capabilities", "Tenant capabilities",
              "What this tenant supports, checked live by behaviour (read only): KQL search, the archive, complete "
              "events. refresh=true checks again.", _obj({"refresh": {"type": "boolean"}}), self.t_capabilities),
            t("datasets", "Datasets", "Every dataset (integration configuration) on the tenant with its 24-hour volume.",
              no_args, self.t_datasets),
            t("fields", "Field catalogue",
              "The fields a search or fieldset can use. With dataset and profile=true, samples that dataset live and "
              "adds how often each field is filled (coverage) and suggested columns.",
              _obj({"dataset": {"type": "string"}, "profile": {"type": "boolean"}}), self.t_fields),
            t("field_values", "Common values of a field",
              "The most common values of one field over a time window, optionally starting with a prefix or limited "
              "to a dataset. Use it to get exact spelling and case before writing ==.",
              _obj({"field": {"type": "string"}, "prefix": {"type": "string"}, "dataset": {"type": "string"},
                    "time": TIME_SCHEMA, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["field"]),
              self.t_field_values),
            t("kql_check", "Check a KQL query",
              "How a KQL query would run on this tenant, without running it: Abstract's own validator, then runs_as = "
              "'filters' (exact export, re-checked on every event), 'kql' (runs natively on Abstract), 'kql_result' "
              "(a result table such as summarize; save it with kql_save) or 'refused' (with the reason and "
              "suggestion). Always call this before running KQL.",
              _obj({"kql": {"type": "string", "description": "starts with events, e.g. events | where action == "
                                                            "\"Login\""}}, ["kql"]), self.t_kql_check),
            t("nl_to_kql", "Plain English to KQL",
              "Abstract's assistant writes KQL from a description. It only writes the query; nothing runs. Show the "
              "KQL and its check to the user and run it (kql_preview) only if they agree. The assistant writes "
              "filters only; it declines grouping and explains why.",
              _obj({"text": {"type": "string"}}, ["text"]), self.t_nl_to_kql),
            t("kql_preview", "Run KQL (preview)",
              f"Runs a KQL query natively for a preview and returns up to {MAX_ROWS} rows (or the result table of a "
              "summarize). Report the window with the count; a zero is only 'no matches in this window'.",
              _obj({"kql": {"type": "string"}, "time": TIME_SCHEMA,
                    "rows": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS}}, ["kql"]), self.t_kql_preview),
            t("kql_save", "Save a KQL result table",
              "Saves a KQL query's result table (summarize, project, extend …) as CSV or JSON in a new folder in the "
              "exports folder on this computer. At most 10,000 rows; says when there were more.",
              _obj({"kql": {"type": "string"}, "time": TIME_SCHEMA, "format": {"type": "string", "enum": ["csv", "json"]},
                    "name": {"type": "string"}}, ["kql"]), self.t_kql_save, read_only=False),
            t("preview", "Preview a search",
              f"How many events a search matches and the first rows (up to {MAX_ROWS}), with the window searched.",
              _obj({"spec": SPEC_SCHEMA, "rows": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS}}, ["spec"]),
              self.t_preview),
            t("validate", "Validate a search",
              "Every check an export runs, without exporting: fields exist, the filter is proven, the count, the plan "
              "and an estimated time. Run before export_start.", _obj({"spec": SPEC_SCHEMA}, ["spec"]), self.t_validate),
            t("export_start", "Start an export",
              "Starts exporting every matching event to this computer in the background (one export at a time). "
              "Validate first. Poll export_status.",
              _obj({"spec": SPEC_SCHEMA, "format": {"type": "string", "enum": ["ndjson", "ndjson.gz", "csv"]},
                    "merge": {"type": "boolean", "default": True}}, ["spec"]), self.t_export_start, read_only=False),
            t("export_status", "Export progress", "The running or last export: progress, time left, result.",
              no_args, self.t_export_status),
            t("export_cancel", "Stop the export", "Stops the running export. It stays resumable.", no_args,
              self.t_export_cancel, read_only=False),
            t("exports_list", "Export history on this computer", "The exports in the exports folder, newest first.", no_args,
              self.t_exports_list),
            t("views_list", "Saved views", "Every saved view in Abstract, with how it was built and its columns.",
              no_args, self.t_views_list),
            t("view_import", "Load a saved view",
              "A saved view as a search spec this tool runs (dataset and filters, or KQL), with notes.",
              _obj({"view_id": {"type": "string"}}, ["view_id"]), self.t_view_import),
            t("view_plan", "Plan a saved-view change",
              "Plans creating, updating or deleting a saved view in Abstract. Changes nothing: returns the exact "
              "changes and a token. Show the changes to the user and call apply_change only after an explicit yes. "
              "Works when the server is read-only; applying needs --allow-writes.",
              _obj({"op": {"type": "string", "enum": ["create", "update", "delete"]}, "spec": SPEC_SCHEMA,
                    "id": {"type": "string"}, "name": {"type": "string"},
                    "fieldset_ids": {"type": "array", "items": {"type": "string"}}}, ["op"]), self.t_view_plan),
            t("fieldsets_list", "Fieldsets", "Every fieldset (named column list) on the tenant.", no_args,
              self.t_fieldsets_list),
            t("fieldset_get", "Read a fieldset", "One fieldset in full.", _obj({"id": {"type": "string"}}, ["id"]),
              self.t_fieldset_get),
            t("fieldset_plan", "Plan a fieldset change",
              "Plans creating, updating or deleting a fieldset. Fields must be stored fields (the catalogue, ext.*, "
              "event.parsed.*), never KQL computed columns. Changes nothing: returns the changes and a token. Show "
              "them to the user and call apply_change only after an explicit yes. Works when the server is read-only; "
              "applying needs --allow-writes.",
              _obj({"op": {"type": "string", "enum": ["create", "update", "delete"]}, "id": {"type": "string"},
                    "data": _obj({"name": {"type": "string"}, "fields": {"type": "array", "items": {"type": "string"}},
                                  "description": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
                                  "share_level": {"type": "string", "enum": ["private", "public"]}})}, ["op"]),
              self.t_fieldset_plan),
            t("content_export", "Export views and fieldsets as code",
              "Saved views and fieldsets as a bundle (abstract.content/v1): one file per object, no ids, fieldsets "
              "referred to by name, so it applies to any tenant. Writes a folder under the exports root and returns "
              "its path and counts (not the whole bundle). views/fieldsets: names or ids; leave out for all.",
              _obj({"views": {"type": "array", "items": {"type": "string"}},
                    "fieldsets": {"type": "array", "items": {"type": "string"}},
                    "kind": {"type": "string", "enum": ["views", "fieldsets"],
                             "description": "only saved views (plus the fieldsets they use) or only fieldsets"},
                    "dir": {"type": "string", "description": "folder to write (default: a new one under the "
                                                              "exports root)"}}), self.t_content_export),
            t("content_diff", "Compare a bundle with the tenant",
              "Each view and fieldset in a bundle folder against the connected tenant: same, changed, new, "
              "only_in_tenant or ambiguous. Read only.",
              _obj({"dir": {"type": "string", "description": "bundle folder or bundle .json file"}}, ["dir"]),
              self.t_content_diff),
            t("content_plan", "Plan applying a bundle",
              "Plans making the tenant match a bundle (create and update; with prune=true also DELETE objects not in "
              "the bundle). Changes nothing: returns every change, what is skipped and why, and ONE token for the "
              "whole set. Show the user the list (call out deletes loudly) and call apply_change only after an "
              "explicit yes.",
              _obj({"dir": {"type": "string"}, "prune": {"type": "boolean"}}, ["dir"]), self.t_content_plan),
            t("automation_recipe", "Automation recipe for a search",
              "The files that run a search as a scheduled export: cron, launchd, systemd, windows-task, "
              "github-actions or container-cron. Never holds the key (it comes from ABSTRACT_API_KEY_COMMAND or the "
              "platform's secret store); sets the tenant guard; back-to-back windows; alerts to ABX_ALERT_WEBHOOK on "
              "failure. Give a saved search name or a spec. With dir, writes the files there.",
              _obj({"target": {"type": "string", "enum": ["cron", "launchd", "systemd", "windows-task",
                                                          "github-actions", "container-cron"]},
                    "saved": {"type": "string", "description": "a saved search's name"}, "spec": SPEC_SCHEMA,
                    "every": {"type": "string", "enum": ["15m", "1h", "6h", "1d"]},
                    "at": {"type": "string", "description": "UTC HH:MM for a daily run"},
                    "job": {"type": "string"}, "dir": {"type": "string"}}, ["target"]), self.t_automation_recipe),
            t("apply_change", "Apply a planned change",
              "Makes the planned change(s) to the tenant. Refused unless this server was started with "
              "--allow-writes (or ABSTRACT_MCP_ALLOW_WRITES=1): the tool cannot check a yes typed in the chat, so "
              "the person running the server decides. Only a token from view_plan, fieldset_plan or content_plan in "
              "this same server works, once, within 10 minutes. Call only after the user said yes to that plan.",
              _obj({"token": {"type": "string"}}, ["token"]), self.t_apply_change, read_only=False, destructive=True),
            t("endpoints", "Platform endpoints",
              "The Abstract API endpoints this tool uses, which are optional, and what this session saw from each.",
              no_args, self.t_endpoints),
        ]

    def tool(self, name):
        return next((x for x in self.tools if x["name"] == name), None)

    # handlers return (summary text, structured data)
    def t_status(self, a):
        if self.s.client:
            t = self.s.tenant
            mode = "changes to the tenant are ON" if self.allow_writes else "read-only (changes to the tenant are OFF)"
            return (f"Connected to {C.sanitize(t['name'])} ({t['vendor_account_id']}) at {t['base_url']}; {mode}; "
                    f"previews return at most {MAX_ROWS} rows.",
                    {"connected": True, "tenant": t["name"], "vendor_account_id": t["vendor_account_id"],
                     "base_url": t["base_url"], "version": C.VERSION, "writes": self.allow_writes,
                     "max_rows": MAX_ROWS})
        try:
            self.ensure()
            return self.t_status(a)
        except (ToolError, E.UserError, E.Fatal, E.Retryable) as ex:
            msg = C.humanize(ex)
            return (msg, {"connected": False, "reason": msg, "version": C.VERSION, "writes": self.allow_writes,
                          "max_rows": MAX_ROWS,
                          "set": ["ABSTRACT_VENDOR_ACCOUNT_ID", "ABSTRACT_EXPECT_TENANT",
                                  "ABSTRACT_API_KEY_COMMAND or ABSTRACT_API_KEY", "ABSTRACT_BASE_URL (optional)"]})

    def t_capabilities(self, a):
        caps = self.ensure().capabilities(refresh=bool(a.get("refresh")))
        return ("; ".join(f"{i['label']}: {i['status']}" for i in caps.get("items") or []), caps)

    def t_datasets(self, a):
        ds = self.ensure().datasets()
        return (f"{len(ds)} datasets.", {"datasets": ds})

    def t_fields(self, a):
        s = self.ensure()
        if a.get("dataset") and a.get("profile"):
            ds = E.check_dataset_name(s.client, a["dataset"])
            p = s.dataset_fields(ds, refresh=True)
            return (f"{ds}: {p.get('sampled', 0)} events sampled, {len(p.get('fields') or [])} fields filled.", p)
        cat = s.field_catalog(a.get("dataset") or "")
        return (f"{len(cat['fields'])} fields.", cat)

    def t_field_values(self, a):
        r = self.ensure().field_values(a["field"], a.get("prefix") or "", a.get("dataset") or "", a.get("time"),
                                       a.get("limit") or 20)
        if not r.get("available"):
            return (f"Field values are not available here ({r.get('reason') or 'unknown reason'}).", r)
        return (f"{len(r['values'])} values for {a['field']}.", r)

    def t_kql_check(self, a):
        c = self.ensure().kql_check(a["kql"])
        return (f"runs_as={c['runs_as']}: {c['reason']}", c)

    def t_nl_to_kql(self, a):
        r: Dict[str, Any] = self.ensure().nl_to_kql(a["text"])
        if not r["kql"]:
            return (f"No query written: {r['explanation']}", r)
        chk: Any = r.get("check") or {}
        r = dict(r, note="Draft only. Show this KQL to the user and run it only if they agree.")
        return (f"Draft KQL (not run): {r['kql']} — runs_as={chk.get('runs_as', '?')}", r)

    def t_kql_preview(self, a):
        n = max(1, min(int(a.get("rows") or 25), MAX_ROWS))
        r = self.ensure().kql_preview(a["kql"], a.get("time"), n)
        r = dict(r, rows=r["rows"][:n])
        total = r.get("total")
        w = f"{r['window'][0]} to {r['window'][1]} UTC"
        if total == 0:
            return (f"No matches in this window ({w}).", r)
        return (f"{'' if total is None else f'{total:,} '}rows in {w}; {len(r['rows'])} returned (capped at {n}).", r)

    def t_kql_save(self, a):
        r = self.ensure().kql_result_export(a["kql"], a.get("time"), a.get("format") or "csv", a.get("name") or "")
        return (f"Saved {r['rows']:,} rows to {r['file']}" + (" (truncated at 10,000)" if r["truncated"] else ""), r)

    def t_preview(self, a):
        n = max(1, min(int(a.get("rows") or 25), MAX_ROWS))
        r = self.ensure().preview(a["spec"], n)
        w = f"{r['window'][0]} to {r['window'][1]} UTC"
        if not r["total"]:
            return (f"No matching events in this window ({w}).", r)
        return (f"{r['total']:,} events match in {w}; first {len(r['rows'])} returned (capped at {n}).", r)

    def t_validate(self, a):
        r = self.ensure().validate(a["spec"])
        plan = r.get("plan") or {}
        if r.get("ready"):
            msg = f"Ready to export {plan.get('total', 0):,} events." + \
                  (f" Estimated time: {plan['time_words']}." if plan.get("time_words") else "")
        else:
            fails = [c["name"] for c in r.get("checks") or [] if c.get("status") == "FAIL"]
            msg = "Not ready: " + ("; ".join(fails) if fails else (r.get("message") or "see checks"))
        return (msg, r)

    def t_export_start(self, a):
        s = self.ensure()
        s.start_export(a["spec"], {"format": a.get("format"), "merge": a.get("merge", True), "keep_parts": False})
        j = s.job_status()
        return (f"Export started in {j['run_dir'] if j else 'the exports folder'}. Poll export_status.", {"job": j})

    def t_export_status(self, a):
        j = self.s.job_status() if self.s.client else None
        if not j:
            return ("No export has run in this session.", {"job": None})
        p = j.get("progress") or {}
        state = "running" if j["running"] else ((j.get("result") or {}).get("verdict") or "finished")
        return (f"Export {state}" + (f", {p['pct']}%" if p.get("pct") is not None else "")
                + (f", {p['eta_words']}" if p.get("eta_words") else "") + ".", {"job": j})

    def t_export_cancel(self, a):
        ok = self.s.cancel() if self.s.client else False
        return ("Stopping the export; it stays resumable." if ok else "No export is running.", {"cancelled": ok})

    def t_exports_list(self, a):
        rows = C.list_exports(self.s if self.s.client else None)
        return (f"{len(rows)} exports in {C.home_dir()}.", {"exports": rows, "folder": C.home_dir()})

    def t_views_list(self, a):
        vs = self.ensure().views(refresh=True)
        out = [{"id": v["id"], "name": v["name"], "origin": v.get("origin", "kql"), "kql": v.get("kql"),
                "problem": v.get("problem"), "time": v.get("time"), "columns": v.get("fields") or []} for v in vs]
        return (f"{len(out)} saved views.", {"views": out})

    def t_view_import(self, a):
        r = self.ensure().import_search(view_id=a["view_id"])
        return ("Loaded: " + "; ".join(r.get("readable") or []), r)

    def _writes_note(self):
        return "" if self.allow_writes else (" This server is read-only, so apply_change will be refused: tell the user "
                                             "to restart it with --allow-writes, or to make the change in the browser "
                                             "app, terminal or command line.")

    def _plan_result(self, plan: Dict[str, Any]):
        self._tokens.add(plan["token"])
        plan = dict(plan, instruction="Nothing has changed yet. Show the user these changes and who will see them "
                                      "(the visibility line) and ask for an explicit yes. Only then call apply_change "
                                      "with this token." + self._writes_note())
        bits = []
        changes: List[Dict[str, Any]] = plan["changes"]
        for c in changes:
            if "added" in c:
                bits.append(f"{c['key']}: +{len(c['added'])} -{len(c['removed'])}" +
                            (" (order)" if c.get("reordered") else ""))
            else:
                bits.append(c["key"])
        return (f"Plan ({plan['request']}): " + (", ".join(bits) or "no field changes") +
                ". Nothing changed yet; ask the user before apply_change.", plan)

    def t_view_plan(self, a):
        return self._plan_result(self.ensure().view_plan(a["op"], a.get("spec"), a.get("id"), a.get("name"),
                                                         a.get("fieldset_ids")))

    def t_fieldsets_list(self, a):
        # compact: a tenant can hold dozens of fieldsets with long field lists (measured: 45 came to 225 KB in full).
        # fieldset_get returns one in full.
        fs = self.ensure().fieldsets()
        rows = [{"id": f["id"], "name": f["name"], "share_level": f.get("share_level"),
                 "field_count": len(f["fields"]), "fields": f["fields"][:12],
                 "more_fields": max(0, len(f["fields"]) - 12)} for f in fs]
        return (f"{len(fs)} fieldsets (first 12 fields each; fieldset_get shows one in full).", {"fieldsets": rows})

    def t_fieldset_get(self, a):
        return ("Fieldset " + str(a["id"]), self.ensure().fieldset(a["id"]))

    def t_fieldset_plan(self, a):
        return self._plan_result(self.ensure().fieldset_plan(a["op"], a.get("data"), a.get("id")))

    def t_content_export(self, a):
        s = self.ensure()
        views = a.get("views") if isinstance(a.get("views"), list) else None
        fsets = a.get("fieldsets") if isinstance(a.get("fieldsets"), list) else None
        kind = a.get("kind") or None
        if views is not None and fsets is None and kind is None:
            fsets = []
        r = s.content_export(a.get("dir") or None, views, fsets, kind)
        return (f"Wrote {r['counts']['views']} views and {r['counts']['fieldsets']} fieldsets to {r['dir']}.", r)

    def t_content_diff(self, a):
        import abx_content as CT
        doc = CT.read_folder(str(a["dir"]))
        rows = self.ensure().content_diff(doc)
        counts: Dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return ("Compared: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + ".",
                {"diff": rows, "notes": doc.get("notes") or []})

    def t_content_plan(self, a):
        import abx_content as CT
        plan = self.ensure().content_plan(CT.read_folder(str(a["dir"])), bool(a.get("prune")))
        c = plan["counts"]
        if plan.get("token"):
            self._tokens.add(plan["token"])
        plan = dict(plan, instruction="Nothing has changed yet. Show the user every item (deletes first and loudly) "
                                      "and ask for an explicit yes. Only then call apply_change with this token."
                                      + self._writes_note())
        return (f"Plan: {c['create']} create, {c['update']} update, {c['delete']} delete, "
                f"{len(plan['skipped'])} skipped. Nothing changed yet; ask the user before apply_change.", plan)

    def t_automation_recipe(self, a):
        import abx_automate as AU
        if a.get("saved"):
            spec = C.saved_load(str(a["saved"]))
        elif isinstance(a.get("spec"), dict):
            spec = a["spec"]
        else:
            raise ToolError("give saved (a saved search's name) or spec")
        vid = os.environ.get("ABSTRACT_VENDOR_ACCOUNT_ID") or ((self.s.tenant or {}).get("vendor_account_id"))
        r = AU.recipe(spec, str(a["target"]), vid, os.environ.get("ABSTRACT_BASE_URL"), str(a.get("every") or "1d"),
                      str(a.get("at") or "00:20"), a.get("job") or None)
        if a.get("dir"):
            r["written"] = AU.write(r, str(a["dir"]))
        return (f"{len(r['files'])} files for a {r['target']} job '{r['job']}' every {r['every']}"
                + (f", written to {a['dir']}" if a.get("dir") else "") + ". Follow the steps; the key never goes in a "
                "file.", r)

    def t_apply_change(self, a):
        if not self.allow_writes:
            raise ToolError(WRITES_OFF)
        tok = str(a.get("token") or "")
        if tok not in self._tokens:
            raise ToolError("That token was not issued by a plan in this server (or was already used). Call "
                            "view_plan, fieldset_plan or content_plan, show the user the changes, and use the new "
                            "token.")
        self._tokens.discard(tok)
        r = self.ensure().apply_plan(tok)
        if r.get("kind") == "bundle":
            msg = f"Done: {len(r['done'])} change(s) made."
            if r.get("failed"):
                msg += (f" Stopped at {r['failed']['op']} {r['failed']['kind']} {r['failed']['name']}: "
                        f"{r['failed']['error']}. {len(r['not_attempted'])} not attempted.")
            return (msg, r)
        return (f"Done: {r['op']} {r['kind']} {r.get('id') or ''}".strip(), r)

    def t_endpoints(self, a):
        rows = self.s.endpoints()
        return (f"{len(rows)} endpoints; {sum(1 for r in rows if r['status'] == 'ok')} answered OK this session.",
                {"endpoints": rows})

    # ── JSON-RPC ──
    def handle(self, msg: Any) -> Optional[Dict[str, Any]]:
        """One JSON-RPC message in, the response out (None for a notification or a response from the client)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _err(None, -32600, "Invalid Request: expected one JSON-RPC 2.0 object")
        mid = msg.get("id")
        method = msg.get("method")
        if method is None:
            return None                                   # a response to something we never send
        is_note = "id" not in msg
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return None if is_note else _err(mid, -32602, "params must be an object")
        try:
            result = self._dispatch(method, params)
        except _RpcError as ex:
            return None if is_note else _err(mid, ex.code, ex.message)
        except Exception as ex:                           # never a traceback to the client; the details go to the log
            path = E.log_error("FAILED", f"MCP {method}: {type(ex).__name__}: {ex}", ex)
            return None if is_note else _err(mid, -32603, f"Internal error ({type(ex).__name__}); details in {path}")
        if is_note:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _dispatch(self, method, p):
        if method == "initialize":
            want = str(p.get("protocolVersion") or "")
            self.initialized = True
            return {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                    "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False},
                                     "prompts": {"listChanged": False}},
                    "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}
        if method in ("notifications/initialized", "notifications/cancelled"):
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [{k: v for k, v in x.items() if k != "fn"} for x in self.tools]}
        if method == "tools/call":
            return self.call_tool(str(p.get("name") or ""), p.get("arguments") or {})
        if method == "resources/list":
            res = []
            if self.s.client:
                res.append({"uri": "abstract://fields", "name": "fields", "title": "Field catalogue",
                            "description": "Fields on the connected tenant", "mimeType": "application/json"})
            return {"resources": res}
        if method == "resources/read":
            uri = str(p.get("uri") or "")
            if uri == "abstract://fields":
                if not self.s.client:
                    raise _RpcError(-32002, "not connected: call the status tool")
                with self._lock:
                    text = json.dumps(self.s.field_catalog(""))
            else:
                raise _RpcError(-32002, f"no resource {uri}")
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": text}]}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "prompts/list":
            return {"prompts": [{"name": n, "title": d["title"], "description": d["description"],
                                 "arguments": d["arguments"]} for n, d in PROMPTS.items()]}
        if method == "prompts/get":
            name = str(p.get("name") or "")
            if name not in PROMPTS:
                raise _RpcError(-32602, f"no prompt {name}")
            args = p.get("arguments") or {}
            return {"description": PROMPTS[name]["description"],
                    "messages": [{"role": "user", "content": {"type": "text",
                                                              "text": PROMPTS[name]["text"].format(**{
                                                                  k["name"]: args.get(k["name"], "")
                                                                  for k in PROMPTS[name]["arguments"]})}}]}
        raise _RpcError(-32601, f"Method not found: {method}")

    def call_tool(self, name, args):
        t = self.tool(name)
        if t is None:
            raise _RpcError(-32602, f"Unknown tool: {name}")
        if not isinstance(args, dict):
            raise _RpcError(-32602, "arguments must be an object")
        missing = [k for k in t["inputSchema"].get("required", []) if args.get(k) in (None, "")]
        if missing:
            return _tool_error(f"Missing argument(s): {', '.join(missing)}")
        try:
            with self._lock:
                text, data = t["fn"](args)
        except (ToolError, E.UserError, E.Fatal, E.Retryable, E.Cancelled, ValueError, OSError) as ex:
            return _tool_error(C.humanize(ex))
        data = _scrub(data)
        return {"content": [{"type": "text", "text": C.sanitize(text)}], "structuredContent": data
                if isinstance(data, dict) else {"result": data}, "isError": False}

    # ── transports ──
    def run_stdio(self, inp, out):
        """Read one JSON-RPC message per line from inp and write each response as one line to out. While it runs,
        anything else that prints to standard output goes to standard error instead."""
        real = sys.stdout
        sys.stdout = sys.stderr
        try:
            for line in inp:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    resp: Optional[Dict[str, Any]] = _err(None, -32700, "Parse error")
                else:
                    resp = self.handle(msg)
                if resp is not None:
                    out.write(json.dumps(resp, default=str) + "\n")
                    out.flush()
        finally:
            sys.stdout = real


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_error(msg):
    return {"content": [{"type": "text", "text": C.sanitize(msg)}], "isError": True}


_SECRET_KEYS = re.compile(r"(?i)^(api[_-]?key|authorization|token|password|secret)$")


def _scrub(obj):
    """Results never carry anything shaped like a credential (plan tokens are named token and are kept)."""
    if isinstance(obj, dict):
        return {k: ("***" if _SECRET_KEYS.match(str(k)) and k != "token" else _scrub(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


PROMPTS = {
    "exact-export": {
        "title": "Build an exact export",
        "description": "Turn a description into a validated export of every matching event",
        "arguments": [{"name": "goal", "description": "what to export, e.g. failed logins last week", "required": True}],
        "text": ("I want to export: {goal}. Use the Abstract Search & Export tools: check status, find the dataset "
                 "(datasets) and exact field values (field_values), write KQL and run kql_check, show me the "
                 "query and how it runs, then validate and tell me the count, window and estimated time. Start the "
                 "export only after I say yes."),
    },
    "question-to-kql": {
        "title": "Turn a question into KQL",
        "description": "Draft KQL for a question, check it, and preview it after you agree",
        "arguments": [{"name": "question", "description": "the question, in plain English", "required": True}],
        "text": ("Write Abstract KQL for: {question}. Use nl_to_kql, then kql_check. Show me the "
                 "query, Abstract's check and how it will run. Ask me before running kql_preview, and when the "
                 "answer is zero, tell me the window searched."),
    },
}


# ── HTTP transport ───────────────────────────────────────────────────────────────────────────────────────────────
def _hostname(host):
    h = (host or "").strip().lower()
    if h.startswith("["):
        return h[1:h.find("]")] if "]" in h else ""
    return h.rsplit(":", 1)[0] if ":" in h else h


class _HTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make_http(server: Server, port=0):
    """A streamable-HTTP MCP endpoint at http://127.0.0.1:PORT/mcp. Returns (httpd, token)."""
    token = secrets.token_urlsafe(24)
    sessions = set()

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "abstract-search-export-mcp"
        sys_version = ""

        def log_message(self, format, *args):        # noqa: A002 (base signature)
            pass

        def _send(self, code, obj=None, extra=None):
            data = b"" if obj is None else json.dumps(obj, default=str).encode("utf-8")
            self.send_response(code)
            if obj is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _guard(self):
            if _hostname(self.headers.get("Host")) not in ALLOWED_HOSTS:
                self._send(403, {"error": "bad host"})
                return False
            o = self.headers.get("Origin")
            if o is not None:
                u = urllib.parse.urlparse(o)
                if u.scheme not in ("http", "https") or (u.hostname or "") not in ALLOWED_HOSTS:
                    self._send(403, {"error": "cross-origin request refused"})
                    return False
            auth = self.headers.get("Authorization") or ""
            given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if not given or not hmac.compare_digest(given, token):
                self._send(401, {"error": "missing or wrong bearer token"}, {"WWW-Authenticate": "Bearer"})
                return False
            if urllib.parse.urlparse(self.path).path != "/mcp":
                self._send(404, {"error": "not found"})
                return False
            return True

        def do_GET(self):
            if self._guard():
                self._send(405, {"error": "this server does not open a stream; POST JSON-RPC to /mcp"},
                           {"Allow": "POST, DELETE"})

        def do_DELETE(self):
            if self._guard():
                sessions.discard(self.headers.get("Mcp-Session-Id"))
                self._send(200, {})

        def do_POST(self):
            if not self._guard():
                return
            try:
                n = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                self.close_connection = True
                return self._send(413, {"error": "request too large"})
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                self.close_connection = True
                return self._send(415, {"error": "requests must be JSON"})
            raw = self.rfile.read(n)
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._send(400, _err(None, -32700, "Parse error"))
            resp = server.handle(msg)
            extra = {}
            if isinstance(msg, dict) and msg.get("method") == "initialize":
                sid = secrets.token_hex(16)
                sessions.add(sid)
                extra["Mcp-Session-Id"] = sid
            if resp is None:
                return self._send(202, None, extra)
            return self._send(200, resp, extra)

    httpd = _HTTPServer(("127.0.0.1", int(port or 0)), H)
    return httpd, token


# ── entry point ──────────────────────────────────────────────────────────────────────────────────────────────────
def main(argv=None):
    argv = list(argv or [])
    if {"-h", "--help"} & set(argv):
        print(__doc__, file=sys.stderr)
        return 0
    # Logs and the live API trace go to standard error; standard output belongs to the protocol.
    E.LOG_SINK = lambda m: print(E._CTRL.sub(lambda x: f"\\x{ord(x.group()):02x}", str(m)), file=sys.stderr, flush=True)
    srv = Server(allow_writes=("--allow-writes" in argv) or _writes_from_env())
    print("abstract-search-export MCP: " + ("changes to the tenant are ON (--allow-writes)" if srv.allow_writes else
                                           "read-only; start with --allow-writes to allow changes")
          + f"; previews return at most {MAX_ROWS} rows", file=sys.stderr, flush=True)
    if "--http" in argv:
        try:
            port = int(argv[argv.index("--http") + 1])
        except (IndexError, ValueError):
            print("--http needs a port number, e.g. --http 8765 (0 picks a free one)", file=sys.stderr)
            return 2
        httpd, token = make_http(srv, port)
        print(f"Abstract Search & Export MCP server: http://127.0.0.1:{httpd.server_address[1]}/mcp\n"
              f"Bearer token (shown once; the client sends it as 'Authorization: Bearer <token>'):\n{token}\n"
              "Press Ctrl+C to stop.", file=sys.stderr, flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
        return 0
    try:
        srv.run_stdio(sys.stdin, sys.stdout)
    except KeyboardInterrupt:
        pass
    except Exception:                                     # the client sees the process end; the reason is in stderr
        traceback.print_exc(file=sys.stderr)
        return 2
    return 0


__all__ = ["Server", "main", "make_http", "PROTOCOLS"]
