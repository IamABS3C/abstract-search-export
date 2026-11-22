"""
abx_core — the one session layer the guided terminal, the main menu and the browser app all drive.

Nothing here talks to the API except through abx_engine, so every front end produces the same numbers.
The API key lives only in this process's memory: it is never written to settings, saved searches,
profiles, manifests or exports, and it is removed from the environment once read.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import copy
import datetime as dt
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import uuid

import abx_engine as E
import abx_ops as O
import abx_kql as K
import abx_platform as P

APP_NAME = "Abstract Search & Export"
VERSION = E.VERSION
HERE = os.path.dirname(os.path.abspath(__file__))
PROD_URL = "https://api.abstractsecurity.app"


def home_dir():
    return E.DEFAULT_OUT_ROOT


def _p(*parts):
    return os.path.join(home_dir(), *parts)


def ensure_home():
    E.secure_umask()
    os.makedirs(home_dir(), exist_ok=True)
    try:
        os.chmod(home_dir(), 0o700)                         # exports hold sensitive event data
    except OSError:
        pass


# ───────────────────────────── operators & field types ─────────────────────────────

# Every operator per field type, each measured against the true answer on a production tenant (2026-10-31): the value
# kind is True (a list of values), False (one value), "range" (from, to) or None (no value). Text operators ignore
# case unless "match case" is set; "not" operators match events that HAVE a value (abx_ops).
OPERATORS = {
    "String": [("IS_IN_LIST", "is one of", True), ("IS_NOT_IN_LIST", "is not one of", True),
               ("CONTAINS", "contains", False), ("DOES_NOT_CONTAIN", "does not contain", False),
               ("STARTS_WITH", "starts with", False), ("DOES_NOT_START_WITH", "does not start with", False),
               ("ENDS_WITH", "ends with", False), ("DOES_NOT_END_WITH", "does not end with", False),
               ("CONTAINS_WORD", "has the word", False), ("DOES_NOT_CONTAIN_WORD", "does not have the word", False),
               ("MATCHES_REGEX", "matches the pattern (regex)", False),
               ("DOES_NOT_MATCH_REGEX", "does not match the pattern (regex)", False),
               ("IS_NOT_EMPTY", "has a value", None), ("IS_EMPTY", "is empty", None)],
    "List(String)": [("CONTAINS_ANY", "has any of these items", True), ("CONTAINS_ALL", "has all of these items", True),
                     ("CONTAINS", "has an item containing", False), ("DOES_NOT_CONTAIN", "has no item containing", False),
                     ("IS_NOT_EMPTY", "has a value", None), ("IS_EMPTY", "is empty", None)],
    "Ipv4": [("IS_IN_LIST", "is one of", True), ("IS_NOT_IN_LIST", "is not one of", True),
             ("IS_IN_SUBNET", "is in subnet (CIDR)", False), ("IS_NOT_IN_SUBNET", "is not in subnet", False),
             ("IS_RFC1918", "is private", None), ("IS_NOT_RFC1918", "is public", None),
             ("IS_NOT_EMPTY", "has a value", None), ("IS_EMPTY", "is empty", None)],
    "Float64": [("IS_IN_LIST", "is one of", True), ("IS_NOT_IN_LIST", "is not one of", True),
                ("IS_GREATER_THAN", "greater than", False), ("IS_GREATER_THAN_OR_EQUAL_TO", "at least", False),
                ("IS_LESS_THAN", "less than", False), ("IS_LESS_THAN_OR_EQUAL_TO", "at most", False),
                ("IS_IN_RANGE", "between (inclusive)", "range"), ("IS_OUTSIDE_RANGE", "not between", "range"),
                ("IS_NOT_EMPTY", "has a value", None), ("IS_EMPTY", "is empty", None)],
    "Boolean": [("IS_TRUE", "is true", None), ("IS_FALSE", "is false", None)],
}
CASE_OPS = {"IS_IN_LIST", "IS_NOT_IN_LIST", "CONTAINS", "DOES_NOT_CONTAIN", "STARTS_WITH", "DOES_NOT_START_WITH",
            "ENDS_WITH", "DOES_NOT_END_WITH", "CONTAINS_WORD", "DOES_NOT_CONTAIN_WORD", "MATCHES_REGEX",
            "DOES_NOT_MATCH_REGEX", "CONTAINS_ANY", "CONTAINS_ALL"}
NOT_OPS = set(O.NEGATIVE)
# KQL is NOT offered by this kit (removed 2026-10-01). Measured on two production tenants: a KQL query sent through
# the search API is silently ignored and returns every event (the backend flag that applies it is off there, while
# the product still shows a KQL box). Every search is built from a dataset and filters, which work on every tenant.
KQL_REMOVED_MSG = ("KQL is not available on this tenant, so this search cannot run as KQL. Build the search with a "
                   "dataset and filters, or use 'Use a search from Abstract' to convert it into filters.")
# True only after this session's capability check proved the connected tenant applies KQL (see Session.capabilities).
# A search copied from Abstract that cannot be converted into filters may then run as KQL; every export of it is still
# proven first. Reset whenever the session connects, disconnects or the check says otherwise.
KQL_ON = False
# Where to search: the one storage choice every front end offers (the engine picks the search method from it).
STORAGE_LABELS = {"auto": "Automatic (recommended)", "recent": "Recent data only", "archive": "Archive search"}
STORAGE_HINTS = {"auto": "recent data, plus the archive for anything older (minutes to an hour for that part)",
                 "recent": "fastest; older events kept in the archive are not included",
                 "archive": "the whole range from the archive, with archive jobs (minutes to an hour)"}
TYPE_LABELS = {"String": "text", "List(String)": "list of text", "Ipv4": "IP address", "Float64": "number",
               "Boolean": "true/false",
               "Date": "date", "StringifyJSON": "JSON text"}
TIME_PRESETS = ["15m", "1h", "4h", "24h", "7d", "30d"]
DEFAULT_FIELDS = ["@timestamp", "id", "data_stream.dataset", "message"]


def type_label(t):
    return TYPE_LABELS.get(t or "String", str(t).lower())


def ops_for(ftype):
    return OPERATORS.get(ftype, OPERATORS["String"])


def op_label(ftype, op):
    for o, label, _ in ops_for(ftype):
        if o == op:
            return label
    return str(op).lower().replace("_", " ")


def op_takes_list(ftype, op):
    for o, _, lst in ops_for(ftype):
        if o == op:
            return lst
    return False


def _split_values(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


_CIDR = re.compile(r"^(\d{1,3}\.){3}\d{1,3}/(\d|[12]\d|3[0-2])$")
_IPV4 = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")


def filter_problem(f):
    """A human-readable problem with one filter, or None."""
    ftype, op = f.get("type") or "String", f.get("op") or "IS_IN_LIST"
    if not f.get("field"):
        return "a filter has no field"
    lst = op_takes_list(ftype, op)
    vals = _split_values(f.get("value"))
    label = f"'{f['field']} {op_label(ftype, op)}'"
    if lst is True and not vals:
        return f"{label} needs at least one value"
    if lst is False and not vals:
        return f"{label} needs a value"
    if lst == "range":
        if len(vals) != 2:
            return f"{label} needs two numbers: from, to"
        try:
            lo, hi = float(vals[0]), float(vals[1])
        except ValueError:
            return f"{label} needs two numbers: from, to"
        if lo > hi:
            return f"{label}: the first number must not be larger than the second"
    if op in ("MATCHES_REGEX", "DOES_NOT_MATCH_REGEX"):
        try:
            re.compile(vals[0] if vals else "")
        except re.error as ex:
            return f"{label}: the pattern is not a valid regex ({ex})"
    if f.get("or_empty") and op not in NOT_OPS:
        return f"{label}: 'or has no value' only applies to a 'not' filter"
    if op in ("IS_IN_SUBNET", "IS_NOT_IN_SUBNET") and not all(_CIDR.match(v) for v in vals):
        return f"{label} needs a CIDR range like 10.0.0.0/8"
    if ftype == "Ipv4" and op == "IS_IN_LIST" and not all(_IPV4.match(v) for v in vals):
        return f"{label} needs IPv4 addresses like 10.1.2.3"
    if ftype == "Float64" and lst in (True, False):
        try:
            [float(v) for v in vals]
        except ValueError:
            return f"{label} needs numbers"
    return None


# ───────────────────────────── search spec ─────────────────────────────

def empty_spec():
    return {"name": "", "mode": "simple", "dataset": "", "filters": [], "kql": "", "advanced": "",
            "advanced_kind": "condition", "time": {"last": "24h"}, "fields": list(DEFAULT_FIELDS),
            "full_documents": False, "format": "ndjson.gz", "storage": "auto", "match": "all", "any_groups": [],
            "source_view": ""}


def normalize_spec(spec):
    base = empty_spec()
    base.update({k: v for k, v in (spec or {}).items() if k in base})
    if base["mode"] not in ("simple", "kql", "advanced"):
        base["mode"] = "simple"
    base["filters"] = [dict(f) for f in base.get("filters") or [] if isinstance(f, dict) and f.get("field")]
    base["any_groups"] = [[dict(f) for f in g if isinstance(f, dict) and f.get("field")]
                          for g in base.get("any_groups") or [] if isinstance(g, list)]
    base["any_groups"] = [g for g in base["any_groups"] if g]
    base["fields"] = [f for f in base.get("fields") or [] if isinstance(f, str) and f.strip()] or list(DEFAULT_FIELDS)
    if not isinstance(base.get("time"), dict):
        base["time"] = {"last": "24h"}
    if base.get("storage") not in STORAGE_LABELS:
        base["storage"] = "auto"
    if base.get("match") not in ("all", "any"):
        base["match"] = "all"
    return base


def spec_is_portable(spec):
    """Dataset + 'is one of' text filters: expressible identically by every search method."""
    spec = normalize_spec(spec)
    if spec["match"] == "any" and len(spec["filters"]) > 1:
        return False
    if spec.get("any_groups"):
        return False
    return spec["mode"] == "simple" and all(f.get("op", "IS_IN_LIST") == "IS_IN_LIST" and
                                            (f.get("type") or "String") == "String" and not f.get("case_sensitive")
                                            for f in spec["filters"])


MAX_GROUPS = 64


def either_of(pieces):
    """'At least one of these searches' as one search: each piece is (filters, any_groups), and the result is the
    (filters, any_groups) that matches exactly the events at least one piece matches. (a and b) or c becomes
    (a or c) and (b or c)."""
    out = [[]]
    for filters, groups in pieces:
        clauses = [[f] for f in filters] + [list(g) for g in groups]
        if not clauses:
            return [], []                                   # an empty piece matches everything
        out = [a + b for a in out for b in clauses]
        if len(out) > MAX_GROUPS:
            raise E.UserError("these alternatives expand into too many groups to run exactly; simplify them, or "
                              "split them into several exports")
    return [c[0] for c in out if len(c) == 1], [c for c in out if len(c) > 1]


def build_condition(spec):
    """The dataset AND the filters; with match "any", the dataset AND (at least one of the filters). 'At least one
    of' groups always apply as well."""
    conds = []
    if spec.get("dataset"):
        conds.append({"field": "data_stream.dataset", "operation": "IS_IN_LIST", "field_type": "String",
                      "case_sensitive": False, "values": [spec["dataset"]]})
    if spec.get("match") == "any" and len(spec["filters"]) > 1:
        inner = build_condition(dict(spec, dataset="", match="all", any_groups=[]))
        any_of = inner["conditions"] + [leaf for g in inner["groups"] for leaf in g["conditions"]]   # OR of ORs
        rest = build_condition(dict(spec, dataset="", match="all", filters=[]))["groups"]
        return {"operator": "and", "conditions": conds,
                "groups": [{"operator": "or", "conditions": any_of, "groups": []}] + rest}
    groups = []
    for f in spec["filters"]:
        c = filter_leaf(f)
        if f.get("or_empty") and c["operation"] in NOT_OPS:
            # "does not contain X, or has no value": KQL's own !contains rule, chosen per filter
            groups.append({"operator": "or", "groups": [], "conditions": [
                c, {"field": c["field"], "operation": "IS_EMPTY", "field_type": c["field_type"], "value": ""}]})
        else:
            conds.append(c)
    for g in spec.get("any_groups") or []:
        # "at least one of": an OR group; a "not … or has no value" member is two members of the same OR
        members = []
        for f in g:
            c = filter_leaf(f)
            members.append(c)
            if f.get("or_empty") and c["operation"] in NOT_OPS:
                members.append({"field": c["field"], "operation": "IS_EMPTY", "field_type": c["field_type"], "value": ""})
        groups.append({"operator": "or", "conditions": members, "groups": []})
    return {"operator": "and", "conditions": conds, "groups": groups}


def filter_leaf(f):
    """One filter row -> one API condition leaf."""
    ftype, op = f.get("type") or "String", f.get("op") or "IS_IN_LIST"
    c = {"field": f["field"], "operation": op, "field_type": ftype,
         "case_sensitive": bool(f.get("case_sensitive")) and op in CASE_OPS}
    lst = op_takes_list(ftype, op)
    vals = _split_values(f.get("value"))
    if lst is True:
        c["values"] = vals
    elif lst == "range":
        c.update({"from": vals[0] if vals else "", "to": vals[1] if len(vals) > 1 else "", "value": ""})
    elif lst is False:
        c["value"] = str(f.get("value", "")).strip()
    else:
        c["value"] = ""
    return c


def resolve_window(spec, at=None):
    """(start, end) as whole-second UTC datetimes. Relative ranges end 10 minutes ago. Raises UserError."""
    t = spec.get("time") or {}
    if t.get("start") or t.get("end") or t.get("custom"):
        if not (t.get("start") and t.get("end")):
            raise E.UserError("enter both a start and an end time in UTC, e.g. 2026-09-28T00:00:00Z")
        s, e = E.parse_user_ts(t["start"], "start time"), E.parse_user_ts(t["end"], "end time")
        s, e = E.floor_s(s), E.floor_s(e)
        if e <= s:
            raise E.UserError("the end time must be after the start time")
        if e > (at or E.now()):
            raise E.UserError("the end time is in the future — choose an end time that is now or earlier")
        return s, e
    end = E.floor_s(at or E.now()) - E.RELATIVE_END_LAG
    return end - E.parse_duration(t.get("last") or "24h"), end


def spec_problems(spec):
    """Everything wrong with a search before any API call (empty list = runnable)."""
    spec = normalize_spec(spec)
    out = []
    if spec["mode"] == "simple":
        if not spec["dataset"] and not spec["filters"] and not spec["any_groups"]:
            out.append("Add a dataset or at least one filter.")
        out += [p for p in (filter_problem(f) for f in spec["filters"] + [x for g in spec["any_groups"] for x in g]) if p]
    elif spec["mode"] == "kql":
        if not KQL_ON:
            out.append("This saved search uses KQL. " + KQL_REMOVED_MSG)
        elif not (spec.get("kql") or "").strip():
            out.append("This search has no KQL text.")
    else:
        if not (spec.get("advanced") or "").strip():
            out.append("This saved search has an empty advanced condition.")
        else:
            try:
                json.loads(spec["advanced"])
            except ValueError as ex:
                out.append(f"The advanced condition is not valid JSON ({ex}).")
    if spec["storage"] == "archive" and spec["mode"] != "simple":
        out.append("Archive search runs a dataset and filters. Choose Automatic or Recent data only for this search.")
    try:
        resolve_window(spec)
    except E.UserError as ex:
        out.append(str(ex)[0].upper() + str(ex)[1:] + ".")
    return out


def query_args(spec):
    spec = normalize_spec(spec)
    a = []
    if spec["mode"] == "simple":
        if spec_is_portable(spec):
            if spec["dataset"]:
                a += ["--dataset", spec["dataset"]]
            for f in spec["filters"]:
                a += ["--where", f"{f['field']}={','.join(_split_values(f.get('value')))}"]
        else:
            filters_only = dict(spec, dataset="")
            a += ["--condition-json", json.dumps(build_condition(filters_only))]
            if spec["dataset"]:
                a += ["--dataset", spec["dataset"]]           # ANDed by the engine, and guards every row
    elif spec["mode"] == "kql":
        if not KQL_ON:
            raise E.UserError(KQL_REMOVED_MSG)
        a += ["--kql", spec["kql"]]
    else:
        obj = json.loads(spec["advanced"])
        a += ["--dsl-json" if spec.get("advanced_kind") == "dsl" else "--condition-json", json.dumps(obj)]
    if spec["storage"] != "auto":
        a += ["--storage", spec["storage"]]
    return a


def _search_key(spec):
    spec = normalize_spec(spec)
    return json.dumps({k: spec[k] for k in ("mode", "dataset", "filters", "kql", "advanced", "time", "fields",
                                           "full_documents", "storage", "match")}, sort_keys=True)


def window_args(spec, at=None):
    s, e = resolve_window(spec, at)
    return ["--start", E.iso(s), "--end", E.iso(e)]


def fields_args(spec):
    spec = normalize_spec(spec)
    if spec["full_documents"]:
        return ["--every-field"]
    return ["--fields", ",".join(spec["fields"])]


def command_line(spec, tenant_id=None):
    """The command line that runs exactly this search as an export (for scheduling), POSIX shell quoting. Searches the
    readable options cannot express (other operators, exact case, KQL) run from their saved copy (--from-saved)."""
    import shlex

    def q(a):                                          # quote anything a shell or PowerShell could reinterpret
        return "'" + a.replace("'", "'\"'\"'") + "'" if re.search(r"[^\w./:=,-]", a) or a.startswith("@") else a
    spec = normalize_spec(spec)
    probs = [p for p in spec_problems(spec) if "time" not in p.lower()]
    if probs:
        raise E.UserError(probs[0])
    t = spec["time"]
    window = ["--last", t.get("last") or "24h"] if not (t.get("start") or t.get("end")) else \
        ["--start", t.get("start") or "", "--end", t.get("end") or ""]
    plain = spec["mode"] == "simple" and all((f.get("op") or "IS_IN_LIST") == "IS_IN_LIST" and not f.get("case_sensitive")
                                             and (f.get("type") or "String") in ("String", "Float64", "Ipv4")
                                             for f in spec["filters"])
    note = ("Set ABSTRACT_API_KEY and ABSTRACT_VENDOR_ACCOUNT_ID in the environment first (see the command line "
            "guide). On Windows PowerShell use py instead of python3.")
    if plain:                                          # the readable form: --dataset / --where / --match / --storage
        argv = ["export"] + (["--dataset", spec["dataset"]] if spec["dataset"] else [])
        for f in spec["filters"]:
            argv += ["--where", f"{f['field']}={','.join(_split_values(f.get('value')))}"]
        if spec["match"] == "any" and len(spec["filters"]) > 1:
            argv += ["--match", "any"]
        if spec["storage"] != "auto":
            argv += ["--storage", spec["storage"]]
        argv += window + fields_args(spec)
    else:
        name = (spec.get("name") or "").strip()
        argv = ["export", "--from-saved", name or "<the name you save it under>"]
        if not name or not os.path.isfile(_saved_file(name)):
            note = ("Save this search first (Saved searches), then run this command: its filters are kept in the "
                    "saved search. " + note)
    argv += ["--format", spec["format"], "--merge"]
    env = "export ABSTRACT_EXPECT_TENANT=" + shlex.quote(tenant_id) if tenant_id else ""
    return {"command": "python3 abstract_search.py " + " ".join(q(a) for a in argv), "env": env, "note": note}


def estimate_words(tm, archive):
    """The time estimate in words: total, then each part. A guide, from measured rates; the archive's event count is
    a guess until its job runs."""
    out = E.fmt_duration(tm.get("total_s"))
    if not archive:
        return out + " (recent data, at the measured rate of this tool; busy tenants can be slower)."
    parts = [f"recent data {E.fmt_duration(tm.get('recent_s'))}" if tm.get("recent_s") else None,
             f"archive {E.fmt_duration(tm.get('archive_s'))}"]
    text = out + " in all: " + ", ".join(p for p in parts if p) + "."
    if tm.get("archive_events_guess"):
        text += (f" The archive part assumes about {tm['archive_events_guess']:,} older events (as many per day as "
                 f"recent data, including reading them); the real number is known when its job finishes.")
    else:
        text += " The archive part is the job time; reading its events adds to it, and is known when the job finishes."
    return text


def describe_filter(f):
    ftype, op = f.get("type") or "String", f.get("op") or "IS_IN_LIST"
    out = f'{f["field"]} {op_label(ftype, op)} {f.get("value", "")}'.strip()
    if f.get("case_sensitive") and op in CASE_OPS:
        out += " (exact case)"
    if f.get("or_empty") and op in NOT_OPS:
        out += f" (or {f['field']} has no value)"
    return out


def describe_spec(spec):
    spec = normalize_spec(spec)
    if spec["mode"] == "simple":
        parts = [f'dataset = "{spec["dataset"]}"'] if spec["dataset"] else []
        for f in spec["filters"]:
            parts.append(describe_filter(f))
        if spec["match"] == "any" and len(spec["filters"]) > 1:
            ds = parts[:1] if spec["dataset"] else []
            parts = ds + ["(" + " OR ".join(parts[len(ds):]) + ")"]
        for g in spec["any_groups"]:
            parts.append("(" + " OR ".join(describe_filter(f) for f in g) + ")")
        q = " AND ".join(parts) or "(add a dataset or a filter)"
    elif spec["mode"] == "kql":
        q = ("KQL: " + (spec.get("kql") or "").strip()) if KQL_ON else "KQL search (not available on this tenant)"
    else:
        q = "advanced condition (saved search)"
    t = spec["time"]
    if t.get("start") or t.get("end"):
        w = f"{t.get('start') or '(start)'} → {t.get('end') or '(end)'}"
    else:
        w = f"last {t['last']}" if t.get("last") else "(choose a time range)"
    return {"query": q, "window": w, "storage": STORAGE_LABELS[spec["storage"]],
            "fields": "every field of every event" if spec["full_documents"] else ", ".join(spec["fields"])}


# ───────────────────────────── session ─────────────────────────────

class Busy(E.UserError):
    """An export is running; searches are paused so they do not compete with it (the platform limits
    concurrent searches per user)."""


class SavedExists(E.UserError):
    """A saved search with that name already exists."""


class Headless(E.UserError):
    """No file manager on this machine: the message (and .path) say where the export is."""

    def __init__(self, path):
        super().__init__(f"The export is in: {path}")
        self.path = path


def _kql_off():
    global KQL_ON
    KQL_ON = False


def _sentence_case(text: str) -> str:
    """A detail that starts with an ordinary lower-case word gets a capital. Field names (with "_" or "."), "@" names
    and numbers are left exactly as they are."""
    if not isinstance(text, str) or not text:
        return text
    first = text.split(" ", 1)[0].rstrip(",:;")
    if first in _SENTENCE_STARTS:
        return text[:1].upper() + text[1:]
    return text


# Words a detail sentence starts with. A field name (url, action, message) is never one of them, so it keeps its case.
_SENTENCE_STARTS = {"a", "an", "the", "this", "these", "that", "every", "everything", "no", "none", "nothing", "up",
                    "most", "some", "each", "it", "its", "removing", "values", "under", "about", "older", "recent",
                    "only", "ends", "could", "not", "can", "cannot", "one", "two", "all", "both", "your", "there"}


class Session:
    NOISY = {"event.original", "labels", "tags", "related_events", "matched_rules", "aggregation_count",
             "aggregation_start", "aggregation_end", "data_stream.namespace", "data_stream.type", "ingested_time",
             "insertion_time", "correlated_count", "risk_score"}
    PREFERRED = ["data_stream.dataset", "action", "event.outcome", "user_name", "user_id", "source_ipv4",
                 "source_address", "source_port", "dest_ipv4", "dest_address", "dest_port", "host_name",
                 "destination.domain", "url.original", "http.request.method", "http.response.status_code",
                 "event.category", "product", "vendor", "message", "cloud.account_id", "cloud.region",
                 "user_agent", "email.from", "email.to", "file.name", "process.name", "severity"]

    def __init__(self):
        # Empty until connect(); every method that uses them calls require() first.
        self.tenant: Any = None
        self.client: Any = None
        self._creds: Any = None
        self._fields = None
        self._types = {}
        self._datasets = None
        self.job: Any = None
        self._job_lock = threading.Lock()
        self._tickets = {}
        self._profiles = {}                        # dataset (casefold) -> learned field profile, this session only
        self._views = None                         # the tenant's saved views, fetched live, never written to disk
        self.views_without_search = 0
        self._caps = None                          # what this tenant supports, checked live (memory only)
        self._ticket_lock = threading.Lock()
        self._validated = None                     # (search key, start, end, when): exports reuse that window
        self._validate_cancels = set()             # the cancel Events of the validates running now
        self._vc_lock = threading.Lock()
        self._plans = P.Plans()                    # reviewed tenant changes waiting for a confirm (single use)
        self._kql_probe = None                     # True / False once this session checked whether KQL applies
        ensure_home()

    # -- connection --
    def connect(self, base_url, vendor_account_id, api_key):
        if self.busy():
            raise Busy("an export is running — wait for it to finish (or stop it) before switching tenant")
        base_url = (base_url or PROD_URL).strip().rstrip("/")
        P.forget_seen()
        self._plans.clear()
        self._kql_probe = None
        vendor_account_id, api_key = (vendor_account_id or "").strip(), (api_key or "").strip()
        if not vendor_account_id or not api_key:
            raise E.UserError("both the vendor account ID and the API key are required")
        client = E.Client(base_url, api_key, vendor_account_id, timeout=20, retries=1, quiet=True)
        try:
            code, j, _ = client.get("/v2/auth/")
        except E.Retryable as ex:
            raise E.UserError(f"could not reach {base_url} from this computer. Check your network, VPN or proxy, "
                              f"then try again. ({ex})")
        except E.Fatal as ex:
            raise E.UserError(str(ex))
        if code in (401, 403) and api_key == E.NETWORK_KEY:
            raise E.UserError(f"the request arrived without a valid API key (HTTP {code}). "
                              f"ABSTRACT_API_KEY_FROM_NETWORK=1 expects the network to add it: check that the secret "
                              f"names this API's host, the Authorization header and the Bearer prefix.")
        if code in (401, 403):
            raise E.UserError(f"the API key was rejected (HTTP {code}). Check the key and that it belongs to this "
                              f"vendor account.")
        if code != 200 or not isinstance(j, dict):
            raise E.UserError(f"could not verify the key (HTTP {code}).")
        org = j.get("current_organization") or {}
        vid = org.get("current_vendor_account_id")
        if not vid:
            raise E.UserError("the platform did not say which vendor account this key belongs to — try again")
        if vid != vendor_account_id:
            raise E.UserError(f"this key belongs to vendor account {vid}, not {vendor_account_id}. Use {vid}, or a "
                              f"key for the account you meant.")
        client.timeout, client.retries = 120, 4
        self.client = client
        self.tenant = {"name": org.get("name") or "(unnamed)", "vendor_account_id": vid, "base_url": client.base,
                       "production": client.production}
        self._creds = {"base_url": client.base, "vendor_account_id": vid, "api_key": api_key, "expect_tenant": vid}
        E.CREDS = dict(self._creds)
        self._fields, self._types, self._datasets = None, {}, None
        self._profiles, self._views, self._caps = {}, None, None
        _kql_off()
        return self.tenant

    def connect_from_env(self):
        v = os.environ.get("ABSTRACT_VENDOR_ACCOUNT_ID")
        try:
            k = E.key_from_environment() if v else None
        except E.UserError:
            os.environ.pop("ABSTRACT_API_KEY", None)
            raise
        if not (k and v):
            os.environ.pop("ABSTRACT_API_KEY", None)         # read once, never left for child processes
            return None
        try:
            t = self.connect(os.environ.get("ABSTRACT_BASE_URL") or PROD_URL, v, k)
        finally:
            os.environ.pop("ABSTRACT_API_KEY", None)         # keep the key out of child processes, even on failure
        exp = os.environ.get("ABSTRACT_EXPECT_TENANT")
        if exp and exp != t["vendor_account_id"]:
            self.disconnect()
            raise E.UserError(f"ABSTRACT_EXPECT_TENANT is {exp} but the key belongs to {t['vendor_account_id']}")
        return t

    def disconnect(self):
        if self.busy():
            raise Busy("an export is running — wait for it to finish (or stop it) before disconnecting")
        self.client, self.tenant, self._creds = None, None, None
        self._profiles, self._views, self._fields, self._types, self._datasets = {}, None, None, {}, None
        self._caps = None
        self._kql_probe = None
        self._plans.clear()
        P.forget_seen()
        _kql_off()
        E.CREDS = None

    def capabilities(self, refresh=False):
        """What this tenant supports (KQL, archive, complete-event search), checked by behaviour, read only. Kept in
        memory for this session; refresh=True checks again. Never stored on disk."""
        self.require()
        if self._caps is not None and not refresh:
            return self._caps
        if self.busy():
            raise Busy("An export is running. Checking the tenant waits until it finishes, so it does not slow the "
                       "export down.")
        global KQL_ON
        self._caps = E.tenant_capabilities(self.client)
        KQL_ON = bool(self._caps.get("kql"))
        return self._caps

    def kql_available(self):
        """True only when this session's capability check proved KQL is applied on this tenant."""
        return bool(self._caps and self._caps.get("kql")) or self._kql_probe is True

    def kql_on(self):
        """Whether this tenant applies KQL, checked once per session by behaviour (a value that cannot exist must
        match nothing, and a search with no condition must match the unfiltered total). Read only."""
        global KQL_ON
        self.require()
        if self._caps is not None and self._caps.get("kql") is not None:
            KQL_ON = bool(self._caps["kql"])
            return KQL_ON
        if self._kql_probe is None:
            if self.busy():
                raise Busy("An export is running. Checking whether this tenant applies KQL waits until it finishes.")
            r = E.kql_applies(self.client)
            if r is None:
                r = E.kql_applies(self.client)           # once more: a busy window can leave it undecided
            if r is None:
                raise E.UserError("Could not check whether this tenant applies KQL right now (the check searches the "
                                  "last hour, then the last day). Try again in a minute.")
            self._kql_probe = bool(r)
        KQL_ON = self._kql_probe
        return KQL_ON

    # -- KQL, plain English, field values, fieldsets and saved views (abx_platform) --
    def kql_check(self, text):
        return P.kql_check(self, text)

    def nl_to_kql(self, text):
        return P.nl_to_kql(self, text)

    def kql_preview(self, text, time_spec=None, n=50, fields=None):
        self._not_busy()
        return P.kql_preview(self, text, time_spec, n, fields)

    def kql_result_export(self, text, time_spec, fmt="csv", name=""):
        self._not_busy()
        return P.kql_result_export(self, text, time_spec, fmt, name)

    def field_values(self, field, prefix="", dataset="", time_spec=None, limit=20):
        return P.field_values(self, field, prefix, dataset, time_spec, limit)

    def field_catalog(self, dataset=""):
        """Every field a search or fieldset can use: the tenant's catalogue with types, plus the ext.* fields seen
        when this dataset was profiled this session."""
        self.require()
        out = [{"name": f, "type": self._types.get(f), "label": type_label(self._types.get(f)) if
                self._types.get(f) else "", "ext": False} for f in self.fields()]
        prof = self._profiles.get((dataset or "").casefold()) if dataset else None
        cov = {f["name"]: f["coverage"] for f in (prof or {}).get("fields", [])}
        for f in out:
            if f["name"] in cov:
                f["coverage"] = cov[f["name"]]
        for n, c in cov.items():
            if n.startswith("ext."):
                out.append({"name": n, "type": None, "label": "", "ext": True, "coverage": c})
        return {"dataset": dataset or "", "profiled": bool(prof), "fields": out}

    def fieldsets(self):
        return P.list_fieldsets(self)

    def fieldset(self, id_):
        return P.get_fieldset(self, id_)

    def fieldset_plan(self, op, data=None, id_=None):
        return P.plan_fieldset(self, op, data, id_)

    def view_plan(self, op, spec=None, id_=None, name=None, fieldset_ids=None):
        return P.plan_view(self, op, spec, id_, name, fieldset_ids)

    def apply_plan(self, token):
        r = P.apply_plan(self, token)
        if r["kind"] in ("view", "bundle"):
            self._views = None                           # the saved-view list changed
        return r

    def endpoints(self):
        return P.endpoints()

    # -- views and fieldsets as code (abx_content), automation recipes (abx_automate) --
    def content_document(self, views=None, fieldsets=None, kind=None):
        import abx_content
        return abx_content.document(self, views, fieldsets, kind)

    def content_export(self, out_dir=None, views=None, fieldsets=None, kind=None):
        import abx_content
        return abx_content.export(self, out_dir or abx_content.default_dir(self), views, fieldsets, kind)

    def content_diff(self, doc):
        import abx_content
        return abx_content.diff(self, doc)

    def content_plan(self, doc, prune=False):
        import abx_content
        return abx_content.plan(self, doc, prune)

    def require(self):
        if not self.client:
            raise E.UserError("not connected — choose Connect first")

    def busy(self):
        return bool(self.job and self.job["running"])

    def _not_busy(self):
        if self.busy():
            raise Busy("An export is running. Preview, Validate and field learning are paused until it finishes, "
                       "so they do not slow it down.")

    # -- catalogue (failures are never cached) --
    def fields(self, refresh=False):
        self.require()
        if (self._fields is None or refresh) and self.busy():
            if self._fields is not None:
                return self._fields
            raise Busy("An export is running. The field list loads after it finishes, so it does not slow the export.")
        if self._fields is None or refresh:
            try:
                names = E.field_enum(self.client)
            except (E.Retryable, E.Fatal) as ex:
                raise E.UserError(f"could not load the field list ({humanize(ex)}) — try again")
            if not names:
                raise E.UserError("could not load the field list — try again")
            self._fields = names
        return self._fields

    def top_fields(self):
        return E.top_level_fields(self.fields())

    def field_type(self, field):
        """The API names a field's type when asked with the wrong one ('Expected <type>'). A type already learned
        is returned at any time; asking the platform is a search, so it raises Busy while an export runs."""
        self.require()
        if field in self._types:
            return self._types[field]
        if self.busy():
            raise Busy("An export is running. Looking up a field's type is paused until it finishes, so it does not "
                       "slow the export down.")
        end = E.floor_s(E.now()) - dt.timedelta(minutes=1)
        body = {"vendor_account_id": self.client.va, "page_size": 1, "start_time": E.iso(end - dt.timedelta(minutes=1)),
                "end_time": E.iso(end), "selected_fields": ["id"],
                "condition": {"operator": "and", "conditions": [
                    {"field": field, "operation": "IS_NOT_EMPTY", "field_type": "String", "value": ""}], "groups": []}}
        try:
            code, j, _ = self.client.post("/v1/streamviewer/search", body,
                                          expect="asking as text makes the platform name the field's real type")
        except (E.Retryable, E.Fatal):
            return "String"                                   # not cached: ask again next time
        m = re.search(r"Expected '([A-Za-z0-9()]+)'", json.dumps(j) if not isinstance(j, str) else j)
        if code == 422 and m:
            t = m.group(1)                                    # the platform named the type
        elif code == 200 and isinstance(j, dict) and isinstance(j.get("metadata"), dict) and "events" in j:
            t = "String"                                      # a real answer to a text-typed filter
        else:
            return "String"                                   # an error of any other kind: not cached (CQ-27)
        self._types[field] = t
        return t

    def datasets(self, refresh=False):
        """Configuration names (proper-case dataset names) with 24h volume where the tenant can report it
        (events_24h None = unknown, not zero)."""
        self.require()
        if self._datasets is not None and not refresh:
            return self._datasets
        if self.busy():
            raise Busy("An export is running. Refreshing the dataset list is paused until it finishes, so it does not "
                       "slow the export down.")
        out, page, seen = {}, 1, set()
        while page <= 50:
            try:
                code, j, _ = self.client.get(f"/v1/configurations/?page_number={page}&page_size=100")
            except (E.Retryable, E.Fatal) as ex:
                code, j, why = None, None, humanize(ex)
            else:
                why = f"HTTP {code}"
            if code != 200 or not isinstance(j, dict):
                # the dataset list is unavailable now: fall back to the datasets that hold recent events
                recent = E.recent_dataset_names(self.client, days=7)
                if recent:
                    self._datasets = sorted(({"name": n, "active": None, "events_24h": None} for n in recent.values()),
                                            key=lambda d: d["name"].casefold())
                    return self._datasets
                raise E.UserError(f"could not load the dataset list ({why}) — try again, or type the dataset name")
            rows = j.get("data") or j.get("items") or j.get("configurations") or []
            new = [r for r in rows if (r.get("id") or r.get("name")) not in seen]
            if not new:
                break
            seen.update(r.get("id") or r.get("name") for r in new)
            for r in new:
                if r.get("type") and r.get("type") != "Event Source":
                    continue
                if r.get("name"):
                    out[r["name"].casefold()] = {"name": r["name"], "active": r.get("active"), "events_24h": None}
            # Measured: this endpoint ignores paging and returns everything each time, so page 2 adds nothing and
            # ends the loop. A server that does page may cap a page below 100 rows, so a short page is NOT the end:
            # only a page that adds nothing new is, with a hard cap of 50 pages (CQ-27).
            page += 1
        try:
            end = E.floor_s(E.now()) - dt.timedelta(minutes=2)
            code, j, _ = self.client.post("/v2/streamviewer/raw-search", {
                "start_time": E.iso(end - dt.timedelta(hours=24)), "end_time": E.iso(end), "size": 0,
                "aggs": {"d": {"terms": {"field": "data_stream.dataset", "size": 1000}}}})
            if code == 200 and isinstance(j, dict) and j.get("aggregations"):
                known = {}
                for b in (j["aggregations"].get("d") or {}).get("buckets") or []:
                    known[str(b.get("key", "")).casefold()] = b.get("doc_count")
                for k, d in out.items():
                    d["events_24h"] = known.get(k, 0)
        except (E.Retryable, E.Fatal):
            pass
        if not out:
            raise E.UserError("no datasets were found on this tenant")
        self._datasets = sorted(out.values(), key=lambda d: (-(d["events_24h"] or 0), d["name"].casefold()))
        return self._datasets

    # -- searches copied from Abstract (search box text or a saved view) --
    def views(self, refresh=False):
        """Every saved view on the tenant, with the search each holds (KQL, filter builder, or none), fetched LIVE
        (never written to disk)."""
        self.require()
        if self._views is not None and not refresh:
            return self._views
        try:
            counts = {}
            self._views = E.list_views(self.client, counts)
            self.views_without_search = counts.get("without_search", 0)
            self.views_from_filters = counts.get("filters", 0)
        except (E.Retryable, E.Fatal) as ex:
            raise E.UserError(f"could not load the saved views ({humanize(ex)}) — try again")
        return self._views

    def import_search(self, text: Optional[str] = None, view_id=None):
        """Convert a search copied from Abstract into this tool's own search (dataset, filters, time, fields).
        KQL is never sent to the API: each clause becomes a filter row the API applies and the tool proves.
        Returns {"spec", "readable", "notes", "source"}; raises UserError naming anything it cannot convert."""
        self.require()
        name, note_extra, view = "", [], None
        if view_id:
            v = next((x for x in self.views() if x["id"] == view_id), None)
            if v is None:
                raise E.UserError("that saved view no longer exists on this tenant — refresh the list")
            if v.get("problem"):
                raise E.UserError(v["problem"])
            view, text, name = v, v["kql"], v["name"]
            if len(v.get("source_ids") or []) > 1:
                note_extra.append("this saved view also searched other sources (such as federated search); only "
                                  "events stored in Abstract are exported")
            if v.get("origin") == "filters":
                note_extra.append("this view was built with Abstract's filter builder; its filters are shown above "
                                  "as this tool runs them")
            if v.get("origin") == "replay":
                note_extra.append("this is a replay view: its search is the one its replay ran, read from the replay "
                                  "job")
            if not text:                                 # columns and a time range only: load them, ask for a filter
                sp = empty_spec()
                sp["name"] = name
                self._view_time_fields(sp, v, None, None)
                return {"spec": normalize_spec(sp), "readable": ["no filter: this view holds only columns and a "
                                                                 "time range"],
                        "notes": ["Add a dataset or a filter before you preview or export: without one the search "
                                  "would match every event."] + note_extra, "source": name, "columns_only": True}
        if view is not None and view.get("alt_kql"):
            # the view holds both KQL and filter-builder conditions: when its KQL cannot be used, the conditions are
            try:
                return self._from_text(text, name, note_extra, view)
            except E.UserError as ex:
                r = self._from_text(view["alt_kql"], name, list(note_extra), view)
                r["notes"].insert(0, f"the view's KQL could not be used ({ex}); its filter-builder conditions are "
                                     "used instead")
                return r
        return self._from_text(text, name, note_extra, view)

    def _from_text(self, text, name, note_extra, view):
        if not (text or "").strip():
            raise E.UserError("paste the search from Abstract's search box, or pick a saved view")
        try:
            E.kql_gate(self.client, text)                    # the platform's own KQL validator, first
        except (E.Fatal, E.Retryable) as ex:
            raise E.UserError("This search cannot be converted exactly: " + str(ex))
        try:
            conv = K.convert(text, self.field_type, set(self.fields()) | set(re.findall(r"ext\.[\w.@-]+", text or "")))
        except K.ConvertError as ex:
            stop = K.kql_run_problem(text)
            if self.kql_available() and stop:
                raise E.UserError("This search cannot be converted exactly: " + str(ex) + ". It cannot run as KQL "
                                  "either: " + stop + ".")
            if self.kql_available():                   # this tenant applies KQL: run the search as written
                text = text or ""
                sp = empty_spec()
                sp.update(name=name, mode="kql", kql=" ".join(text.split()))
                return {"spec": normalize_spec(sp), "readable": ["runs as KQL: " + " ".join(text.split())],
                        "notes": ["It cannot be converted into filters (" + str(ex) + "), and KQL is available on "
                                  "this tenant, so it runs as KQL. Validate checks it before any export."]
                        + note_extra, "source": name or "pasted search"}
            why = " KQL is not available on this tenant, so it cannot run as written either." \
                if self._caps and self._caps.get("kql") is False else ""
            raise E.UserError("This search cannot be converted exactly: " + str(ex) + "." + why)
        for f in conv["filters"] + [x for g in conv.get("any_groups") or [] for x in g]:
            prob = filter_problem(f)
            if prob:
                raise E.UserError("This search cannot be converted exactly: " + prob + ".")
        sp = empty_spec()
        sp["name"] = name
        sp["dataset"] = conv["dataset"] or ""
        sp["filters"] = conv["filters"]
        sp["any_groups"] = conv.get("any_groups") or []
        if conv["fields"]:
            sp["fields"] = list(dict.fromkeys(["@timestamp", "id"] + conv["fields"]))
        notes = conv["notes"] + note_extra
        if conv["time"]:
            end_default = E.iso(E.floor_s(E.now()) - E.RELATIVE_END_LAG)[:19] + "Z"
            st, en = conv["time"]["start"], conv["time"]["end"] or end_default
            if not st:
                st = E.iso(E.parse_ts(en) - dt.timedelta(hours=24))[:19] + "Z"
                notes.append("the search had no start time, so the range starts 24 hours before its end")
            if not conv["time"]["end"]:
                notes.append("the search had no end time, so the range ends 10 minutes ago")
            sp["time"] = {"start": st, "end": en}
        if view is not None:
            self._view_time_fields(sp, view, conv["time"], conv["fields"])
        if not sp["dataset"] and not sp["filters"] and not sp["any_groups"]:
            raise E.UserError("This search has only a time range. Add a dataset or a filter to it first.")
        return {"spec": normalize_spec(sp), "readable": conv["readable"], "notes": notes,
                "source": name or "pasted search"}

    @staticmethod
    def _view_time_fields(sp, view, conv_time, conv_fields):
        """A saved view's own time range and columns, where its search did not set them. A relative range that is
        not one of the presets becomes the matching start and end."""
        vt = view.get("time")
        if vt and not conv_time:
            if vt.get("last") in TIME_PRESETS:
                sp["time"] = {"last": vt["last"]}
            elif vt.get("last"):
                end = E.floor_s(E.now()) - E.RELATIVE_END_LAG
                sp["time"] = {"start": E.iso(end - E.parse_duration(vt["last"]))[:19] + "Z", "end": E.iso(end)[:19] + "Z"}
            else:
                sp["time"] = {"start": vt["start"], "end": vt["end"]}
        if view.get("fields") and not conv_fields:
            sp["fields"] = list(dict.fromkeys(["@timestamp", "id"] + list(view["fields"])))

    # -- per-dataset field profiles --
    def dataset_fields(self, dataset, refresh=False) -> Dict[str, Any]:
        """Fields this dataset populates and how often (names and coverage only — never values), learned LIVE from
        the connected tenant. Kept in memory for this session only: the kit ships no tenant data and writes no
        profiles to disk. Never samples on its own: refresh=True samples now (on demand)."""
        self.require()
        dataset = (dataset or "").strip()
        if not dataset:
            raise E.UserError("pick a dataset first")
        vid = self.tenant["vendor_account_id"]
        key = dataset.casefold()
        if not refresh:
            prof = self._profiles.get(key)
            if prof:
                return dict(prof, cached=True, source="learned this session")
            return {"dataset": dataset, "tenant": vid, "sampled": 0, "fields": [], "suggested": [], "none": True,
                    "source": None}
        self._not_busy()
        docs, method = self._sample_dataset(dataset)
        enum = set(self.fields())
        seen = {}
        for d in docs:
            here = set()
            for k, v in flatten(d).items():
                if v is None or v == "" or v == [] or v == {}:
                    continue
                if k.startswith("ext."):                 # not in the tenant catalog, but exportable by name
                    here.add(k)
                    continue
                name = k if k in enum else next((p for p in (k.rsplit(".", i)[0] for i in range(1, k.count(".") + 1))
                                                 if p in enum), None)
                if name:
                    here.add(name)
            for n in here:
                seen[n] = seen.get(n, 0) + 1
        total = len(docs)
        fields = sorted(({"name": n, "seen": c, "coverage": round(100.0 * c / total, 1)} for n, c in seen.items()),
                        key=lambda f: (-f["coverage"], f["name"]))
        pri = {n: i for i, n in enumerate(self.PREFERRED)}
        cand = [f for f in fields if f["name"] not in self.NOISY and f["name"] not in ("id", "@timestamp")
                and (f["coverage"] >= 90 or (f["name"] in pri and f["coverage"] >= 30))]
        cand.sort(key=lambda f: (pri.get(f["name"], len(pri)), -f["coverage"], f["name"]))
        prof = {"dataset": dataset, "tenant": vid, "profiled": E.iso(E.now()), "sampled": total, "method": method,
                "fields": fields, "suggested": ["@timestamp", "id"] + [f["name"] for f in cand][:16],
                "cached": False, "source": "learned"}
        if total:
            self._profiles[key] = prof                       # memory only, cleared on disconnect / tenant switch
        return prof

    def _sample_dataset(self, dataset, per_window=250):
        q = E.Query(E.argparse.Namespace(dataset=dataset))
        end0 = E.floor_s(E.now()) - dt.timedelta(minutes=10)
        windows = [(end0 - dt.timedelta(days=d, hours=1), end0 - dt.timedelta(days=d)) for d in (0, 1, 3, 7)]
        windows.append((end0 - dt.timedelta(days=30), end0))
        docs = {}
        v1 = E.V1Engine(self.client, q, self.top_fields(), min(per_window, 200), quiet=True)
        for i, (st, en) in enumerate(windows):
            if i == len(windows) - 1 and len(docs) >= 50:
                break
            try:
                got = v1._call(v1._body(st, en, v1.page_size, q.to_condition()))["events"]
            except (E.Fatal, E.Retryable):
                got = []
            for d in got:
                if str(E.dig(d, "data_stream.dataset") or "").casefold() == dataset.casefold():
                    docs[d.get("id") or id(d)] = d
        return list(docs.values()), "streamviewer"

    def profile_all(self, sink=print, refresh=False):
        out = []
        for d in self.datasets():
            try:
                p = self.dataset_fields(d["name"])
                if p.get("none") or refresh:
                    p = self.dataset_fields(d["name"], refresh=True)
                sink(f"  {len(p['fields']):>4} fields  {p['sampled']:>5} sampled  {d['name']}"
                     + ("   (no events in the last 30 days)" if not p["sampled"] else ""))
                out.append(p)
            except Busy:
                raise                                     # an export started: stop, do not fail every dataset
            except E.UserError as ex:
                sink(f"     -  {d['name']}: {ex}")
        return out

    # -- engine calls --
    def _run(self, argv, sink=None, cancel=None, progress=None):
        """Run one engine command on THIS thread (own sink, cancel, progress, credentials)."""
        self.require()
        lines = []
        keys = ("sink", "cancel", "progress", "creds")
        prev = {k: getattr(E._TL, k, None) for k in keys}
        E._TL.sink = sink or lines.append
        E._TL.cancel = cancel
        E._TL.result = {}
        E._TL.progress = progress
        E._TL.creds = dict(self._creds)
        try:
            code = E.main(argv)
        finally:
            for k in keys:
                setattr(E._TL, k, prev[k])                    # a caller's cancel Event stays in force afterwards
        return code, dict(E.thread_result()), lines

    def preview(self, spec, n=25):
        """How many events match, and the first n of them."""
        self.require()
        self._not_busy()
        spec = normalize_spec(spec)
        if spec["mode"] == "kql" and not self.kql_on():
            raise E.UserError(KQL_REMOVED_MSG)
        probs = spec_problems(spec)
        if probs:
            raise E.UserError(probs[0])
        start, end = resolve_window(spec)
        # The export's own choices (CQ-20): the same engine, the same field selection and the same dataset guard, so
        # the preview shows the events the export would write.
        ns = E.build_parser().parse_args(["export", "--format", spec["format"]] + query_args(spec) +
                                         ["--start", E.iso(start), "--end", E.iso(end)] + fields_args(spec))
        q = E.Query(ns)
        name = E.resolve_engine_name(ns, q)
        enum = self.fields() if name != "raw" else None
        fields = E._fields_for(ns, self.client, name, enum)
        if name != "raw" and not spec["full_documents"]:
            bad = E.validate_fields(self.client, fields, enum)
            if bad:
                raise E.UserError(f"These fields do not exist on your tenant: {', '.join(bad)}")
        guard = q.dataset.casefold() if q.dataset else None
        # CQ-14: an interactive preview must answer (or fail) in about a minute, not hold a browser handler for
        # tens of minutes behind export-grade retries: a copy of the client with one retry and a 30 s timeout.
        pc = copy.copy(self.client)
        pc.retries, pc.timeout = 1, 30
        eng = E.ENGINE_CLASSES[name](pc, q, fields, min(n, 200), quiet=True)
        t0 = time.time()
        try:
            total = E.with_retry(lambda: eng.count(start, end), "count", True, attempts=2)
        except (E.Retryable, E.Fatal) as ex:
            raise E.UserError(f"the platform could not count this search ({humanize(ex)}). Try a shorter time range.")
        rows = []
        if total:
            try:
                for page in eng.pages(start, end, total):
                    if guard is not None and any(str(E.dig(r, "data_stream.dataset") or "").casefold() != guard
                                                 for r in page):
                        raise E.UserError("events from another dataset came back, so the dataset filter could not be "
                                          "confirmed. An export would be stopped for the same reason.")
                    rows.extend(page)
                    if len(rows) >= n:
                        break
            except (E.Retryable, E.Fatal) as ex:
                raise E.UserError(f"the platform refused this window ({humanize(ex)}). Try a shorter time range.")
        cols = fields if (not spec["full_documents"] and fields != ["*"]) else \
            sorted({k for r in rows[:n] for k in flatten(r)}, key=lambda k: (k not in ("@timestamp", "id"), k))[:40]
        return {"window": [E.iso(start), E.iso(end)], "total": total, "rows": rows[:n], "columns": cols,
                "seconds": round(time.time() - t0, 1),
                "zero_confirmed": bool(total == 0 and eng.zero_source and eng.zero_source != name)}

    def validate(self, spec, sink=None, cancel=None):
        """Every check an export runs, without exporting. Returns {ok, ready, checks, plan, window, zero_confirmed_by}.
        `cancel` (a threading.Event) or cancel_validate() stops it: it then raises E.Cancelled."""
        self.require()
        self._not_busy()
        if normalize_spec(spec)["mode"] == "kql":
            self.kql_on()                                    # sets KQL_ON for the checks that follow
        ev = cancel if cancel is not None else threading.Event()
        with self._vc_lock:
            self._validate_cancels.add(ev)
        prev = getattr(E._TL, "cancel", None)
        E._TL.cancel = ev                                    # every request on this thread now honours the Event
        try:
            out = self._validate(spec, sink, ev)
        except E.Cancelled:
            raise E.Cancelled("Validation stopped")
        finally:
            E._TL.cancel = prev
            with self._vc_lock:
                self._validate_cancels.discard(ev)
        if ev.is_set():
            raise E.Cancelled("Validation stopped")
        return out

    def cancel_validate(self):
        """Stop the validates running now (safe at any time; False when none was running)."""
        with self._vc_lock:
            evs = list(self._validate_cancels)
        for ev in evs:
            ev.set()
        return bool(evs)

    def _validate(self, spec, sink, ev):
        spec = normalize_spec(spec)
        checks = [{"name": "Connected tenant", "status": "PASS",
                   "detail": f"{self.tenant['name']} ({self.tenant['vendor_account_id']})"}]
        probs = spec_problems(spec)
        if probs:
            checks.append({"name": "Search", "status": "FAIL", "detail": " ".join(probs)})
            return {"ok": False, "ready": False, "checks": checks, "plan": None, "zero_confirmed_by": None}
        at = E.now()
        start, end = resolve_window(spec, at)
        self._validated = (_search_key(spec), start, end, time.time())
        wd = f"{E.iso(start)[:19]}Z → {E.iso(end)[:19]}Z"
        if at - end < E.SETTLED:
            checks.append({"name": "Time window", "status": "WARN",
                           "detail": wd + " — ends under 10 minutes ago; late events can still change the totals"})
        else:
            checks.append({"name": "Time window", "status": "PASS", "detail": wd})
        E.check_cancel()
        argv = ["export", "--estimate", "--quiet"] + query_args(spec) + ["--start", E.iso(start), "--end", E.iso(end)] \
            + fields_args(spec)
        code, res, lines = self._run(argv, sink=sink, cancel=ev)     # the engine checks the Event between counts
        if code == 130 or ev.is_set():
            raise E.Cancelled("Validation stopped")
        if code != 0 or "estimate" not in res:
            checks.append({"name": "Search", "status": "FAIL", "detail": humanize(res.get("error") or "the check failed")})
            return {"ok": False, "ready": False, "checks": checks, "plan": None, "zero_confirmed_by": None}
        cov = res.get("coverage")
        if cov:
            checks.append({"name": "Retention", "status": cov.get("status", "INFO"), "detail": cov["detail"]})
        checks.append({"name": "Fields", "status": "PASS",
                       "detail": "every field of every event" if spec["full_documents"] else ", ".join(spec["fields"])})
        pre = res.get("preflight") or {}
        for c in pre.get("controls") or []:
            label = "dataset" if c.get("field") == "data_stream.dataset" else c.get("field")
            r = c.get("result")
            if r == "PASS":
                d = (c["note"] if c.get("note") else
                     "a value that cannot exist matched 0 events, so the filter is applied"
                     if "impossible_value_total" in c else
                     f"removing it changes the count ({c.get('with_leaf'):,} vs {c.get('without_leaf'):,}), so the filter is applied")
                checks.append({"name": f"Filter proof: {label}", "status": "PASS", "detail": d})
            elif r == "UNPROVEN":
                checks.append({"name": f"Filter proof: {label}", "status": "WARN", "detail":
                               "this filter made no difference in the sample window, so the tool cannot confirm it narrows "
                               "the results. Check the preview before exporting."})
            elif r == "INCONCLUSIVE":
                checks.append({"name": f"Filter proof: {label}", "status": "WARN", "detail":
                               "the platform returned an error instead of a count for this check; the overall "
                               "filtered-vs-unfiltered comparison decides"})
            elif r == "INFO":
                checks.append({"name": "Filter proof", "status": "INFO", "detail": c.get("note", "")})
            elif r == "FAIL":
                checks.append({"name": f"Filter proof: {label}", "status": "FAIL", "detail": c.get("note") or
                               "a value that cannot exist still matched events — this filter could not be confirmed"})
        for cmpd in pre.get("comparisons") or []:
            f, u = cmpd.get("filtered"), cmpd.get("unfiltered")
            if f is not None and u:
                checks.append({"name": "Filtered vs unfiltered", "status": "PASS" if f < u else "WARN",
                               "detail": f"{f:,} of {u:,} events in {cmpd['window'][0][:16]} → {cmpd['window'][1][:16]}"})
        if pre.get("note"):
            checks.append({"name": "Filter note", "status": "INFO", "detail": pre["note"]})
        est = res["estimate"]
        total = est["total"]
        arch = est.get("archive")
        local = (res.get("local_check") or {}).get("fields")
        if local:
            checks.append({"name": "Long text values", "status": "INFO", "detail":
                           f"{', '.join(sorted(local))} has values longer than {E.O.LONG_TEXT} characters, which "
                           "most of Abstract's search filters do not see. The tool fetches every event where this "
                           "field has a value and applies this filter itself, so they are matched correctly. The count "
                           "below is an upper bound until the export runs."})
        E.check_cancel()
        if local:
            cross, confirmed_by = {"name": "Cross-check", "status": "INFO", "detail": "the tool applies part of this "
                                   "filter itself, so it was counted one way"}, None
        else:
            cross, confirmed_by = self._cross_check(spec, start, end, total, est)
        checks.append(cross)
        tm = est.get("time") or {}
        if arch:
            mins = f", roughly {arch['minutes']:,} minutes" if arch.get("minutes") else ""
            checks.append({"name": "Export plan", "status": "PASS", "detail":
                           (f"{total:,} events from recent data, plus " if total else "") +
                           f"everything before {arch['end'][:10]} from the archive: {arch['jobs']} archive job(s){mins}. "
                           "The archive part is counted and checked during the export."})
        elif total:
            checks.append({"name": "Export plan", "status": "PASS",
                           "detail": ("up to " if local else "") + f"{total:,} events, about {est['slices']:,} time slice"
                           + ("" if est["slices"] == 1 else "s")})
        if tm and (total or arch):
            w = estimate_words(tm, bool(arch))
            checks.append({"name": "Estimated time", "status": "INFO", "detail": w[0].upper() + w[1:]})
        else:
            checks.append({"name": "Nothing matched", "status": "WARN", "detail":
                           "0 events matched. A zero is a claim: " +
                           (f"a second, independent search ({confirmed_by}) also counted 0." if confirmed_by else
                            "no second, independent search confirmed it. Try a wider window before reporting an "
                            "absence.")})
        ok = all(c["status"] != "FAIL" for c in checks)
        for c in checks:                                     # each detail reads as a sentence (brand review, item 18)
            c["detail"] = _sentence_case(c.get("detail") or "")
        return {"ok": ok, "ready": ok and (total > 0 or bool(arch)), "checks": checks,
                "plan": {"total": total, "slices": est["slices"], "segments": est.get("segments"), "archive": arch,
                         "upper_bound": bool(local), "time": tm, "time_words": estimate_words(tm, bool(arch)) if tm else None},
                "window": [E.iso(start), E.iso(end)], "zero_confirmed_by": confirmed_by if total == 0 and not arch else None}

    def _cross_check(self, spec, start, end, total, est):
        """Count the same search a second, independent way (only where it can be expressed identically).
        Returns (check, the engine that independently confirmed a zero or None). Never raw: raw runs dataset /
        'is one of' as match_phrase, other semantics (DI-14). Never an engine that produced the zero (DI-16)."""
        name = "Cross-check"
        segs = est.get("segments") or []
        if len(segs) > 1:
            return {"name": name, "status": "INFO", "detail": "the range spans recent data and the archive, so each "
                    "part was counted one way"}, None
        if spec.get("mode") == "kql":
            return {"name": name, "status": "INFO", "detail": "a KQL search is counted one way (the KQL proof above "
                    "shows this tenant applies it)"}, None
        if est.get("engine") in ("cold", "coldquery") or est.get("archive"):
            return {"name": name, "status": "INFO", "detail": "an archive search is counted one way"
                    if spec.get("storage") == "archive" else "the whole range is older than recent data, so it was "
                    "counted one way"}, None
        ns = E.build_parser().parse_args(["export"] + query_args(spec) + ["--start", E.iso(start), "--end", E.iso(end)])
        q = E.Query(ns)
        claimed = set(est.get("zero_source") or []) | {est.get("engine")}
        order = [x for x in ("dataview",) if x not in claimed] if q.dsl is None else []
        for second in order:
            E.check_cancel()
            try:
                eng = E.ENGINE_CLASSES[second](self.client, q, ["id"], 1, quiet=True)
                n = E.with_retry(lambda: eng.count(start, end), f"{second} count", True, attempts=2)
            except (E.Retryable, E.Fatal, E.UserError):
                continue
            if n == total:
                independent = n != 0 or eng.zero_source == second
                return {"name": name, "status": "PASS", "detail": f"a second, independent search also counted {n:,} "
                        "events"}, (second if n == 0 and independent else None)
            return {"name": name, "status": "WARN", "detail": f"a second search counted {n:,} events, the plan {total:,}. "
                    "If the window ended recently, late events explain it — move the end time earlier and validate "
                    "again."}, None
        return {"name": name, "status": "INFO", "detail": "no second search method was available, so the count was "
                "checked one way only"}, None

    def compare(self, spec, engines=("v1", "dataview", "raw")):
        self.require()
        self._not_busy()
        spec = normalize_spec(spec)
        if not spec_is_portable(spec):
            raise E.UserError("comparing needs a dataset and/or 'is one of' filters on text fields")
        argv = ["count", "--quiet", "--engines", ",".join(engines)] + query_args(spec) + window_args(spec)
        code, res, _ = self._run(argv)
        if "error" in res:
            raise E.UserError(humanize(res["error"]))
        return res

    # -- export jobs --
    def start_export(self, spec, options=None):
        """Start an export on a background thread (one at a time)."""
        self.require()
        options = options or {}
        spec = normalize_spec(spec)
        if options.get("format"):
            spec["format"] = options["format"]
        if spec["mode"] == "kql" and not self.kql_on():
            raise E.UserError(KQL_REMOVED_MSG)
        probs = spec_problems(spec)
        if probs:
            raise E.UserError(probs[0])
        stamp = E.now().strftime("%Y%m%dT%H%M%SZ")
        label = E.safe_label(spec.get("name") or spec.get("dataset") or "export")
        while True:                                       # unique per run: never two exports in one folder
            run_dir = os.path.join(home_dir(), f"{stamp}_{label}-{uuid.uuid4().hex[:6]}")
            if not os.path.exists(run_dir):
                break
        argv = ["export", "--out", run_dir, "--format", spec["format"]] + query_args(spec) + self._export_window(spec) \
            + fields_args(spec)
        if options.get("merge", True):
            argv.append("--merge")
        if options.get("keep_parts"):
            argv.append("--keep-parts")
        os.makedirs(run_dir, exist_ok=True)
        with open(os.path.join(run_dir, "search.json"), "w", encoding="utf-8") as fh:
            json.dump(spec, fh, indent=2)
        return self._start_job("export", argv, run_dir)

    def _export_window(self, spec):
        """A relative range exported within 15 minutes of Validate uses the window Validate checked, so the
        export's numbers match what the user just saw."""
        v = self._validated
        if v and v[0] == _search_key(spec) and time.time() - v[3] < 900:
            return ["--start", E.iso(v[1]), "--end", E.iso(v[2])]
        return window_args(spec)

    def resume(self, run_dir):
        self.require()
        run_dir = _inside_home(run_dir, want_dir=True)
        man = E.load_manifest(run_dir)
        mt = man.get("tenant") or {}
        if mt.get("vendor_account_id") != self.tenant["vendor_account_id"]:
            raise E.UserError(f"this export belongs to {mt.get('name')} ({mt.get('vendor_account_id')}); connect to "
                              f"that tenant to resume it")
        argv = ["export", "--resume", run_dir]
        if "merge" not in (man.get("args") or {}):
            argv.append("--merge")          # a manifest from before 1.1 did not record it; every UI export merged
        return self._start_job("resume", argv, run_dir)

    def _start_job(self, kind, argv, run_dir):
        with self._job_lock:
            if self.busy():
                raise Busy("an export is already running — wait for it or stop it first")
            job = {"id": uuid.uuid4().hex[:8], "kind": kind, "run_dir": run_dir, "running": True, "lines": [],
                   "seq": 0, "result": None, "started": time.time(), "cancel": threading.Event(),
                   "progress": {"stage": "starting"}}
            self.job = job

        def sink(line):
            job["lines"].append(sanitize(line))
            job["seq"] += 1
            if len(job["lines"]) > 2000:
                del job["lines"][:1000]

        def run():
            try:
                code, res, _ = self._run(argv, sink=sink, cancel=job["cancel"], progress=job["progress"])
                res = dict(res, exit_code=code)
                for k, v in (("zero_result", False), ("zero_confirmed_by", None), ("bad_timestamps", 0),
                             ("filter_unproven", []), ("warnings", [])):
                    res.setdefault(k, v)                     # also on a run that stopped before it finished
                if res.get("error"):
                    res["error"] = humanize(res["error"])
                job["result"] = res
            except Exception as ex:                          # never lose the error in a thread
                import traceback
                for line in traceback.format_exc().splitlines()[-6:]:
                    sink("  " + line)                        # into the job log, never over the terminal UI
                job["result"] = {"error": f"Something went wrong in the tool itself: {ex}", "exit_code": 1,
                                 "zero_result": False, "zero_confirmed_by": None, "bad_timestamps": 0,
                                 "filter_unproven": [], "warnings": []}
            finally:
                job["running"] = False
                if not os.path.isfile(os.path.join(run_dir, "manifest.json")) and kind == "export":
                    shutil.rmtree(run_dir, ignore_errors=True)       # stopped before any plan existed
                    res = job["result"] = job["result"] or {"exit_code": 1}
                    res["run_dir"], res["nothing_to_resume"] = None, True
                    job["run_dir"] = None
                    if res.get("exit_code") == 130:
                        res["error"] = ("Stopped before any events were written — there is nothing to resume. "
                                        "Start the export again when you are ready.")

        threading.Thread(target=run, daemon=True, name=f"abx-{kind}").start()
        return self.job_status()

    def cancel(self):
        if self.busy():
            self.job["cancel"].set()
            return True
        return False

    def job_status(self):
        j = self.job
        if not j:
            return None
        p = dict(j["progress"])
        exp, wr = p.get("rows_expected") or 0, (p.get("rows_written") or 0) + (p.get("current_slice_rows") or 0)
        p["pct"] = round(100.0 * min(wr, exp) / exp, 1) if exp else None
        if j["running"]:
            left = E.eta_seconds(p, time.time() - j["started"])
            p["eta_s"] = round(left) if left is not None else None
            p["eta_words"] = E.fmt_duration(left) + " left" if left is not None else None
        return {"id": j["id"], "kind": j["kind"], "run_dir": j["run_dir"], "running": j["running"],
                "lines": j["lines"][-60:], "seq": j["seq"], "result": j["result"], "progress": p,
                "elapsed": round(time.time() - j["started"])}

    def lines_since(self, seq):
        j = self.job
        if not j:
            return [], seq
        new = j["seq"] - seq
        return (j["lines"][-new:] if new > 0 else []), j["seq"]

    def wait(self, poll=0.5, on_line=None):
        seq = 0
        while self.job and self.job["running"]:
            if on_line:
                lines, seq = self.lines_since(seq)
                for l in lines:
                    on_line(l)
            time.sleep(poll)
        if on_line and self.job:
            lines, seq = self.lines_since(seq)
            for l in lines:
                on_line(l)
        return self.job_status()

    # -- downloads (browser app) --
    def download_ticket(self, path):
        path = _inside_home(path, want_file=True)
        t = secrets.token_urlsafe(18)
        with self._ticket_lock:
            self._tickets = {k: v for k, v in self._tickets.items() if v[1] > time.time()}
            self._tickets[t] = (path, time.time() + 300)
        return t

    def redeem_ticket(self, ticket):
        with self._ticket_lock:
            v = self._tickets.pop(ticket or "", None)
        if not v or v[1] < time.time():
            raise E.UserError("this download link has expired — choose Download again")
        return v[0]


# ───────────────────────────── exports on disk ─────────────────────────────

def _inside_home(path, want_dir=False, want_file=False):
    """Resolve a path and require it to be inside the exports folder (symlinks resolved)."""
    root = os.path.realpath(home_dir())
    p = os.path.realpath(os.path.expanduser(str(path or "")))
    try:
        inside = os.path.commonpath([root, p]) == root
    except ValueError:                                       # different drives on Windows
        inside = False
    if want_file and p == root:
        inside = False
    if not inside:
        raise E.UserError("that is not inside your exports folder")
    if want_dir and not os.path.isdir(p):
        raise E.UserError("that export folder no longer exists")
    if want_file and not os.path.isfile(p):
        raise E.UserError("that file no longer exists")
    return p


def _lock_alive(run_dir):
    try:
        with open(os.path.join(run_dir, ".lock")) as fh:
            pid = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return False
    return E.pid_alive(pid)                              # never os.kill on Windows: that terminates the process


def list_exports(session=None):
    out = []
    root = home_dir()
    if not os.path.isdir(root):
        return out
    cur_tenant = (session.tenant or {}).get("vendor_account_id") if session else None
    running_dir = session.job["run_dir"] if session and session.busy() else None
    for name in sorted(os.listdir(root), reverse=True):
        d = os.path.join(root, name)
        mf = os.path.join(d, "manifest.json")
        if not os.path.isfile(mf):
            continue
        try:
            with open(mf, encoding="utf-8") as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            continue
        s = m.get("summary") or {}
        sl = [x for x in m.get("slices", []) if x.get("status") != "SPLIT"]
        if d == running_dir or _lock_alive(d):
            status = "RUNNING"
        elif s.get("verdict") == "PARTIAL":
            status = "PARTIAL"                             # finished, recent data only (exit code 3)
        elif s.get("verdict") == "COMPLETE":
            status = "COMPLETE"
        elif not s and all(x.get("status") == "PENDING" for x in sl):
            status = "PLANNED"
        elif any(x.get("status") == "FAILED" for x in sl):
            status = "FAILED"
        else:
            status = "INCOMPLETE"
        merged = s.get("merged_file")
        mpath = os.path.join(d, merged) if merged else None
        tid = (m.get("tenant") or {}).get("vendor_account_id")
        cov = m.get("coverage") or {}                      # the run's coverage record (the summary holds its words)
        partial = status == "PARTIAL" or (status == "COMPLETE" and bool(cov.get("unproven")))   # older manifests
        out.append({"run_dir": d, "name": name, "created": m.get("created"), "window": m.get("window"),
                    "partial": partial, "partial_note": E.partial_sentence(cov) if partial else None,
                    "tenant": (m.get("tenant") or {}).get("name"), "tenant_id": tid, "status": status,
                    "verdict": status,
                    "rows_written": sum(x.get("written", 0) for x in sl if x.get("status") == "COMPLETE"),
                    "rows_expected": s.get("rows_expected", sum(x.get("server_total") or 0 for x in sl)),
                    "stored_duplicates": s.get("stored_duplicates", 0), "coverage": s.get("coverage"),
                    "merged_file": merged if mpath and os.path.isfile(mpath) else None,
                    "merged_bytes": os.path.getsize(mpath) if mpath and os.path.isfile(mpath) else None,
                    "resumable": status in ("INCOMPLETE", "FAILED", "PLANNED") and (cur_tenant is None or cur_tenant == tid),
                    "query": m.get("query")})
    return out


def open_folder(path):
    """Show an export folder in the file manager; on a machine without one, say where it is."""
    p = _inside_home(path)
    if not os.path.isdir(p):
        p = os.path.dirname(p)
    headless = sys.platform.startswith("linux") and (os.environ.get("SSH_CONNECTION") or
                                                     not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")))
    env = {k: v for k, v in os.environ.items() if not k.startswith("ABSTRACT_")}
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", p], env=env)
        elif os.name == "nt":
            os.startfile(p)                                  # noqa: S606 (Windows only)
        elif headless or not shutil.which("xdg-open"):
            raise Headless(p)
        else:
            subprocess.Popen(["xdg-open", p], env=env)
    except OSError:
        raise Headless(p)
    return p


# ───────────────────────────── saved searches & settings ─────────────────────────────

def _saved_dir():
    d = _p("saved-searches")
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
        ex = os.path.join(HERE, "examples")
        for fn in sorted(os.listdir(ex)) if os.path.isdir(ex) else []:
            if fn.endswith(".json"):
                shutil.copyfile(os.path.join(ex, fn), os.path.join(d, "example-" + fn))
    return d


def _old_slug(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-")[:60]


def _saved_file(name_or_file):
    fn = os.path.basename(str(name_or_file or ""))
    if fn.endswith(".json"):
        return os.path.join(_saved_dir(), fn)
    if not str(name_or_file or "").strip():
        raise E.UserError("give the search a name")
    return os.path.join(_saved_dir(), E.safe_label(name_or_file) + ".json")


def saved_list():
    out = []
    for fn in sorted(os.listdir(_saved_dir())):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(_saved_dir(), fn), encoding="utf-8-sig") as fh:
                    sp = json.load(fh)
                out.append({"file": fn, "name": sp.get("name") or fn[:-5], "summary": describe_spec(sp),
                            "mode": normalize_spec(sp)["mode"]})
            except (OSError, ValueError):
                continue
    return out


def saved_save(spec, overwrite=False):
    spec = normalize_spec(spec)
    if not spec.get("name", "").strip():
        raise E.UserError("give the search a name")
    path = _saved_file(spec["name"])
    if os.path.exists(path) and not overwrite:
        raise SavedExists(f"a saved search called '{spec['name']}' already exists — save again to replace it")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, indent=2)
    return os.path.basename(path)


def sync_views(session, views=None):
    """Save every saved view that loads as a saved search on this computer (the conditions only, never events), so
    they appear under Saved searches and run with export --from-saved. A view saved before is updated in place; a
    saved search of your own with the same name is never replaced: the view is saved as "<name> (view)".
    Returns [{"view", "name", "file"}]."""
    views = views if views is not None else session.views(refresh=True)
    existing = {}
    for x in saved_list():
        try:
            existing[x["file"]] = saved_load(x["file"]).get("source_view") or ""
        except E.UserError:
            existing[x["file"]] = None
    out = []
    for v in views:
        if v.get("loads") is False or v.get("problem"):
            continue
        try:
            r = session.import_search(view_id=v["id"])
        except E.UserError:
            continue
        sp = dict(r["spec"], source_view=v["id"])
        name = v["name"]
        fn = os.path.basename(_saved_file(name))
        if fn in existing and existing[fn] != v["id"]:
            name = f"{v['name']} (view)"
            fn = os.path.basename(_saved_file(name))
            if fn in existing and existing[fn] != v["id"]:
                continue                                  # both names are taken by searches of your own
        sp["name"] = name
        out.append({"view": v["id"], "name": name, "file": saved_save(sp, overwrite=True)})
        existing[out[-1]["file"]] = v["id"]
    return out


def saved_load(name_or_file):
    path = _saved_file(name_or_file)
    if not os.path.isfile(path):
        # the name shown in lists is the one stored inside the file (the copied examples' file names differ)
        want = str(name_or_file or "").strip().casefold()
        hit = [x["file"] for x in saved_list() if str(x["name"]).casefold() == want]
        if len(hit) == 1:
            path = os.path.join(_saved_dir(), hit[0])
    if not os.path.isfile(path):
        raise E.UserError(f"no saved search called {name_or_file}")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return normalize_spec(json.load(fh))
    except ValueError as ex:
        raise E.UserError(f"that saved search is not valid JSON ({ex})")


def saved_delete(name_or_file):
    path = _saved_file(name_or_file)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def default_base_url():
    """The API the tool connects to unless told otherwise: production, or ABSTRACT_BASE_URL when set (for testing
    on another environment). Nothing is remembered between runs: the tool writes no settings file."""
    return (os.environ.get("ABSTRACT_BASE_URL") or PROD_URL).strip().rstrip("/")


def load_settings():
    """No settings are stored (2026-10-01): the API URL always starts at the default, and no tenant id or key is
    ever written. Kept as a function so the front ends have one place to ask."""
    return {"base_url": default_base_url()}


def save_settings(d):
    """Deliberately stores nothing (see load_settings)."""
    return None


# ───────────────────────────── help & messages ─────────────────────────────

HELP_TOPICS = [
    ("docs/QUICK-START.md", "Quick start"),
    ("docs/USER-GUIDE.md", "User guide"),
    ("docs/SEARCH-REFERENCE.md", "Search reference — operators, field types, using a search from Abstract"),
    ("docs/FIELDSETS-AND-VIEWS.md", "Fieldsets and saved views in Abstract — build, change, review and confirm"),
    ("docs/CONTENT-AS-CODE.md", "Saved views and fieldsets as code — back up, compare, restore, copy between tenants"),
    ("docs/AUTOMATION.md", "Run a search on a schedule — cron, launchd, systemd, Windows, GitHub Actions, containers"),
    ("docs/DOCKER.md", "Run it in Docker — the browser app, the command line and MCP in a container"),
    ("docs/MCP.md", "Use the tool from an AI assistant (MCP)"),
    ("docs/VALIDATION.md", "What Validate checks"),
    ("docs/EXPORTS.md", "Exports — formats, large exports, resuming"),
    ("docs/TROUBLESHOOTING.md", "Troubleshooting by message"),
    ("docs/SECURITY.md", "Security — what is called, stored and sent"),
    ("docs/CLI.md", "Command line and automation"),
]


def help_text(rel):
    if rel not in {p for p, _ in HELP_TOPICS}:
        raise E.UserError("no such help topic")
    with open(os.path.join(HERE, rel), encoding="utf-8") as fh:
        return fh.read()


# BY DESIGN (CQ-20): humanize() only REWORDS a message for display. Nothing decides control flow from these
# patterns: retry, split, stop and zero-settling use the exception class and Retryable.kind in abx_engine.
_HUMAN = [
    (r"(?i)/v1/coldstorage/v2/\S*: (?:HTTP 4\d\d|the platform rejected the request \(HTTP 4\d\d\))",
     "Archive search is not available on this tenant right now. Choose Automatic or Recent data only for this "
     "search."),
    (r"These fields do not exist on your tenant: (.*)", r"These fields do not exist on your tenant: \1"),
    (r"(The KQL is not valid: .*)", r"\1"),
    (r"KQL could not be confirmed", KQL_REMOVED_MSG),
    (r"(the filter on .* could not be confirmed.*)", r"\1"),
    (r"Invalid field type for field '([^']+)'\. Expected '([^']+)'",
     lambda m: f"{m.group(1)} is a {type_label(m.group(2))} field — remove the filter and add it again so the right "
               f"operators are offered."),
    (r"Invalid IPV4", "An IP filter needs IPv4 addresses like 10.1.2.3."),
    (r"the API key was rejected \(HTTP 401\)", "Your API key was rejected (HTTP 401): it is wrong, expired or "
     "revoked. Create a new key and connect again."),
    (r"permission denied \(HTTP 403\)", "Your API key does not have permission for this (HTTP 403). Ask your "
     "administrator for a key with event search permission."),
    (r"not enabled on this tenant", "This kind of search is not enabled on your tenant."),

    (r"could not reach (https://\S+)", r"Could not reach \1 from this computer. Check your network, VPN or proxy, then "
     r"try again."),
    (r"limiting how many searches run at once", "The platform is limiting how many searches run at once. The tool "
     "waits and retries; avoid running other searches with the same key meanwhile."),
    (r"(?:the )?(TLS certificate of the API could not be verified.*)", r"The \1"),
    (r"a (proxy requires authentication.*)", r"A \1"),
]


def humanize(msg):
    msg = str(msg)
    for pat, rep in _HUMAN:
        m = re.search(pat, msg, flags=re.S)
        if m:
            if rep is None:
                return sanitize(msg[m.start():][:600])
            return sanitize(rep(m) if callable(rep) else m.expand(rep))
    # strip transport prefixes such as "/v1/…: " that mean nothing to a customer
    msg = re.sub(r"^(POST|GET)?\s*/v\d/[\w/{}\-.]+:\s*", "", msg)
    return sanitize(msg[:600])


def sanitize(s):
    """Make untrusted text (event values, server messages) safe to print on a terminal: control and
    formatting characters — including escape sequences — become visible \\xNN / \\uNNNN forms."""
    s = str(s)
    out = []
    for ch in s:
        o = ord(ch)
        if ch in "\n\t":
            out.append(" ")
        elif o < 32 or o == 127 or 0x80 <= o <= 0x9F:
            out.append(f"\\x{o:02x}")
        elif unicodedata.category(ch) in ("Cf", "Cs", "Co", "Zl", "Zp"):
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    return "".join(out)


def flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}."))
    else:
        out[prefix[:-1]] = obj
    return out


def cell(v, width=None):
    """One table cell for the terminal: sanitised first, then truncated."""
    if v is None:
        s = ""
    elif isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = str(v)
    s = sanitize(s)
    if width and len(s) > width:
        s = s[: max(1, width - 1)] + "…"
    return s
