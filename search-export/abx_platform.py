"""Abstract platform features beyond searching and exporting: KQL checks and native KQL, plain English to KQL,
field values, fieldsets and saved views (read, create, update, delete), and the list of platform endpoints this
tool uses.

Every write to the tenant goes through a plan first: plan_*() reads what is there now, builds the exact request,
and returns the changes with a single-use token. apply(token) re-reads the object and refuses if it changed since
the plan, then sends the one request. Nothing here writes without a token from the same session.
"""
import argparse
import datetime as dt
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import abx_engine as E
import abx_kql as K

# ── the platform endpoints this tool uses ────────────────────────────────────────────────────────────────────────
# optional = the tool works without it (it falls back, or the feature says it is not available on this tenant).
REGISTRY = [
    ("GET", "/v2/auth/", "sign in: which tenant the key belongs to", False),
    ("GET", "/v1/configurations/", "dataset list", True),
    ("POST", "/v1/streamviewer/search", "events: count, preview and export (recent data)", False),
    ("POST", "/v1/dataview/search", "native KQL: search, preview, summarize and export", True),
    ("POST", "/v2/streamviewer/raw-search", "full documents and dataset counts", True),
    ("GET", "/v1/vendor-accounts/{id}", "storage retention (where recent data ends)", True),
    ("POST", "/v1/coldstorage/query/submit", "archive jobs: start", True),
    ("GET", "/v1/coldstorage/query/{id}", "archive jobs: progress", True),
    ("POST", "/v1/coldstorage/query/{id}/events", "archive jobs: results", True),
    ("POST", "/v1/coldstorage/query/{id}/cancel", "archive jobs: stop", True),
    ("GET", "/v1/coldstorage/query", "archive jobs: list (capability check, replay views)", True),
    ("POST", "/v1/kql/validate", "KQL check against the tenant's own field catalogue", True),
    ("POST", "/v1/kql/to-conditions", "KQL to filter-builder conditions, with the reason when it cannot", True),
    ("POST", "/v1/kql/from-conditions", "filter-builder conditions to KQL", True),
    ("GET", "/v1/kql/capabilities", "KQL functions and stages this tenant parses", True),
    ("POST", "/v1/ase/kql", "plain English to KQL (Abstract's assistant; generates, never runs)", True),
    ("GET", "/v1/streamviewer/views", "saved views: list", False),
    ("GET", "/v1/streamviewer/view/{id}", "saved views: details", False),
    ("POST", "/v1/streamviewer/view", "saved views: create", True),
    ("PATCH", "/v1/streamviewer/view/{id}", "saved views: update", True),
    ("DELETE", "/v1/streamviewer/view/{id}", "saved views: delete", True),
    ("GET", "/v1/streamviewer/field-sets", "fieldsets: list", True),
    ("GET", "/v1/streamviewer/field-set/{id}", "fieldsets: details", True),
    ("POST", "/v1/streamviewer/field-set", "fieldsets: create", True),
    ("PATCH", "/v1/streamviewer/field-set/{id}", "fieldsets: update", True),
    ("DELETE", "/v1/streamviewer/field-set/{id}", "fieldsets: delete", True),
    ("POST", "/v1/streamviewer/field-suggestions", "most common values of a field", True),
    ("GET", "/v1/feature-flags/flags", "tenant settings shown in the capability check", True),
    ("POST", "/v2/reports/{id}/export", "report exports", True),
]

# Seen this session: "METHOD path-template" -> (status, when). Filled by note().
_SEEN: Dict[str, Any] = {}
_SEEN_LOCK = threading.Lock()


def _template(path):
    path = path.split("?")[0]
    for m, p, _, _ in REGISTRY:
        rx = "^" + re.escape(p).replace(r"\{id\}", "[^/]+") + "$"
        if re.match(rx, path):
            return p
    return path


def note(method, path, status):
    """Record a platform call's outcome by endpoint (no bodies, no ids). Once an endpoint has answered with success
    it stays "ok": a later 404 for one missing object does not mean the endpoint is off."""
    k = f"{method} {_template(path)}"
    with _SEEN_LOCK:
        prev = _SEEN.get(k)
        if prev and prev[0] and 200 <= prev[0] < 300 and not (status and 200 <= status < 300):
            return
        _SEEN[k] = (status, time.time())


def forget_seen():
    """A new tenant connection starts a fresh endpoint record."""
    with _SEEN_LOCK:
        _SEEN.clear()


def endpoints():
    rows = []
    with _SEEN_LOCK:
        seen = dict(_SEEN)
    for m, p, use, optional in REGISTRY:
        st = seen.get(f"{m} {p}")
        if st is None:
            status, detail = "unknown", "not used yet this session"
        else:
            code = st[0]
            status = "ok" if code and 200 <= code < 300 else "off" if code in (403, 404, 405) else "error"
            detail = f"HTTP {code}" if code else "no answer"
        rows.append({"method": m, "path": p, "use": use, "optional": optional, "status": status, "detail": detail})
    return rows


E.API_HOOKS.append(note)


def _call(client, method, path, body=None, expect=None):
    code, j, _ = client.request(method, path, body, expect=expect)
    return code, j


# ── KQL ──────────────────────────────────────────────────────────────────────────────────────────────────────────
RUNS_AS_WORDS = {
    "filters": "exact export: converted into filters the tool re-checks on every event",
    "kql": "runs as KQL on Abstract (every matching event; the tool cannot re-check KQL row by row)",
    "kql_result": "runs as KQL on Abstract and exports the query's result table (not every event)",
    "refused": "cannot run",
}


def validate_kql(client, text):
    """Abstract's own KQL check, as data. None when the validator is unavailable."""
    try:
        code, j = _call(client, "POST", "/v1/kql/validate", {"kql_query": text}, expect="checks a KQL query")
    except E.Retryable:
        return None
    if code != 200 or not isinstance(j, dict):
        return None
    vio = [v for v in (j.get("violations") or []) if isinstance(v, dict)]
    return {"valid": bool(j.get("valid", not vio)) and not vio,
            "violations": [{"kind": v.get("kind") or v.get("code"), "reason": v.get("reason") or v.get("message")
                            or v.get("detail") or "", "suggestion": v.get("suggestion"), "field": v.get("field")}
                           for v in vio],
            "warnings": [w if isinstance(w, str) else (w.get("reason") or w.get("message") or json.dumps(w))
                         for w in (j.get("warnings") or [])]}


def server_to_conditions(client, text):
    """Abstract's translation of KQL into filter-builder conditions. None when unavailable."""
    try:
        code, j = _call(client, "POST", "/v1/kql/to-conditions", {"kql_query": text},
                        expect="translates KQL to conditions")
    except E.Retryable:
        return None
    if code != 200 or not isinstance(j, dict):
        return None
    return {"condition_ok": j.get("condition") is not None, "condition": j.get("condition"),
            "refusal": j.get("refusal")}


def server_from_conditions(client, condition):
    try:
        code, j = _call(client, "POST", "/v1/kql/from-conditions", {"condition": condition},
                        expect="translates conditions to KQL")
    except E.Retryable:
        return None
    if code != 200 or not isinstance(j, dict):
        return None
    return {"kql": j.get("kql_query") or j.get("kql"), "refusal": j.get("refusal")}


def kql_check(session, text):
    """How this KQL would run here, without running it. See RUNS_AS_WORDS."""
    session.require()
    text = " ".join((text or "").split())
    if not text:
        raise E.UserError("type or paste a KQL query, starting with events")
    if not re.match(r"^events\b", text):
        text = "events | where " + text if not text.startswith("|") else "events " + text
    out = {"kql": text, "valid": None, "violations": [], "warnings": [], "runs_as": "refused", "reason": "",
           "dataset": "", "filters": [], "any_groups": [], "readable": [], "server": None}
    v = validate_kql(session.client, text)
    if v is not None:
        out.update(v)
    out["server"] = server_to_conditions(session.client, text)
    if out["valid"] is False:
        first = out["violations"][0] if out["violations"] else {}
        out["reason"] = (first.get("reason") or "Abstract's KQL check refused this query") + \
            (f" ({first['suggestion']})" if first.get("suggestion") else "")
        return out
    try:
        conv = K.convert(text, session.field_type, set(session.fields()) | set(re.findall(r"ext\.[\w.@-]+", text)))
        import abx_core as C
        probs = [p for p in (C.filter_problem(f) for f in conv["filters"] +
                             [x for g in conv.get("any_groups") or [] for x in g]) if p]
        if probs:
            raise K.ConvertError(probs[0])
        out.update(runs_as="filters", dataset=conv["dataset"] or "", filters=conv["filters"],
                   any_groups=conv.get("any_groups") or [], readable=conv.get("readable") or [],
                   reason=RUNS_AS_WORDS["filters"])
        return out
    except K.ConvertError as ex:
        why = str(ex)
    if not session.kql_on():
        out["reason"] = f"it cannot be converted into filters ({why}), and KQL search is not available on this tenant"
        return out
    stop = K.kql_run_problem(text) or _computed_columns(text)
    if stop is None:
        out.update(runs_as="kql", reason=RUNS_AS_WORDS["kql"] + f"; not converted because {why}")
    else:
        out.update(runs_as="kql_result", reason=RUNS_AS_WORDS["kql_result"] + f" ({stop})")
    return out


def _computed_columns(text):
    """Why a query's `project` makes columns that are not stored fields (`n = strlen(x)`, `tolower(x)`), or None.
    Such a query returns a result table, not events: an event export asks for stored fields."""
    t = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', text or "")
    for m in re.finditer(r"\|\s*project\b(?!-)([^|]*)", t):
        body = m.group(1)
        if re.search(r"(?<![=!<>])=(?!=)", body) or "(" in body:
            return "'| project' computes new columns, so the result is a table, not the matching events"
    return None


def nl_to_kql(session, text):
    """Plain English to KQL with Abstract's assistant. Generates only: never runs the query."""
    session.require()
    text = (text or "").strip()
    if not text:
        raise E.UserError("describe what you are looking for, for example: failed logins from outside the US")
    if len(text) > 2000:
        raise E.UserError("keep the description under 2,000 characters")
    try:
        code, j = _call(session.client, "POST", "/v1/ase/kql", {"query": text}, expect="asks the KQL assistant")
    except E.Retryable as ex:
        raise E.UserError(f"Abstract's KQL assistant did not answer ({ex}). Try again, or write the KQL yourself.")
    if code in (403, 404, 405):
        raise E.UserError(f"Abstract's KQL assistant is not available on this tenant (HTTP {code}). Write the KQL "
                          "yourself.")
    if code != 200 or not isinstance(j, dict):
        raise E.UserError(f"Abstract's KQL assistant could not answer (HTTP {code}). Try rephrasing.")
    kql = (j.get("kql") or "").strip()
    res = {"text": text, "kql": kql, "explanation": j.get("explanation") or "", "source": "abstract", "check": None}
    if kql:
        res["check"] = kql_check(session, kql)
    return res


def _kql_engine(session, text, fields, limit):
    q = E.Query(argparse.Namespace(kql=text))
    return E.DataviewEngine(session.client, q, fields, limit, quiet=True)


def kql_preview(session, text, time_spec=None, n=50, fields=None):
    """Run KQL natively for a preview (or a result table). The window is the spec's time range."""
    import abx_core as C
    session.require()
    if not session.kql_on():
        raise E.UserError(C.KQL_REMOVED_MSG)
    chk = kql_check(session, text)
    if chk["runs_as"] == "refused":
        raise E.UserError("This query cannot run: " + chk["reason"])
    start, end = C.resolve_window({"time": time_spec or {"last": "1h"}})
    n = max(1, min(int(n or 50), 10000))
    eng = _kql_engine(session, chk["kql"], fields or ["@timestamp"], n)
    t0 = time.time()
    try:
        body = eng._body(start, end, n)
        rows, _, total = eng._stream(body)
    except (E.Fatal, E.Retryable) as ex:
        raise E.UserError(f"Abstract refused this KQL search ({C.humanize(ex)}).")
    cols: List[str] = []
    for r in rows[:200]:
        for k in C.flatten(r):
            if k not in cols:
                cols.append(k)
    return {"kql": chk["kql"], "runs_as": chk["runs_as"], "reason": chk["reason"], "rows": rows[:n],
            "columns": cols[:60], "total": total, "returned": len(rows), "window": [E.iso(start), E.iso(end)],
            "seconds": round(time.time() - t0, 1)}


def kql_result_export(session, text, time_spec, fmt="csv", name=""):
    """Save a KQL query's result table (summarize, project, extend …) to a new folder in the exports folder.
    At most 10,000 rows; the file says when the result had more."""
    import abx_core as C
    res = kql_preview(session, text, time_spec, 10000)
    if fmt not in ("csv", "json"):
        raise E.UserError("a KQL result table is saved as csv or json")
    stamp = E.now().strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name or "kql-result").strip("-")[:60] or "kql-result"
    folder = os.path.join(C.home_dir(), f"{slug}-{stamp}")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "result." + fmt)
    flat = [C.flatten(r) for r in res["rows"]]
    if fmt == "json":
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(res["rows"], fh, indent=1, default=str)
    else:
        import csv
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(res["columns"])
            for r in flat:
                w.writerow([("" if r.get(c) is None else json.dumps(r[c]) if isinstance(r.get(c), (list, dict))
                             else r[c]) for c in res["columns"]])
    with open(os.path.join(folder, "query.kql"), "w", encoding="utf-8") as fh:
        fh.write(res["kql"] + "\n")
    with open(os.path.join(folder, "result.json.meta"), "w", encoding="utf-8") as fh:
        json.dump({"kql": res["kql"], "window": res["window"], "rows": res["returned"], "total": res["total"],
                   "truncated": bool(res["total"] and res["total"] > res["returned"]), "format": fmt,
                   "saved": E.iso(E.now())}, fh, indent=1)
    return {"folder": folder, "file": path, "rows": res["returned"], "total": res["total"],
            "truncated": bool(res["total"] and res["total"] > res["returned"])}


# ── field values ─────────────────────────────────────────────────────────────────────────────────────────────────
def field_values(session, field, prefix="", dataset="", time_spec=None, limit=20):
    import abx_core as C
    session.require()
    start, end = C.resolve_window({"time": time_spec or {"last": "24h"}})
    body: Dict[str, Any] = {"field": field, "start_time": E.iso(start), "end_time": E.iso(end),
                            "limit": max(1, min(int(limit or 20), 100))}
    if prefix:
        body["prefix"] = prefix
    if dataset:
        body["condition"] = K.to_condition([], dataset)
    try:
        code, j = _call(session.client, "POST", "/v1/streamviewer/field-suggestions", body,
                        expect="reads common values of a field")
    except E.Retryable:
        return {"field": field, "values": [], "available": False, "reason": "the platform did not answer"}
    if code != 200 or not isinstance(j, dict):
        return {"field": field, "values": [], "available": False, "reason": f"HTTP {code}"}
    return {"field": field, "values": list(j.get("values") or []), "available": bool(j.get("available", True)),
            "reason": j.get("unavailable_reason") or j.get("message")}


# ── fieldsets and views: read, plan, apply ───────────────────────────────────────────────────────────────────────
STORED_RX = re.compile(r"^(ext\.|event\.parsed\.).+")
SHARE_LEVELS = ("private", "public")
PLAN_TTL = 600


class Plans:
    """Single-use, expiring plan tokens for this session."""

    def __init__(self):
        self._p: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def add(self, plan):
        tok = secrets.token_urlsafe(18)
        with self._lock:
            now = time.time()
            for k in [k for k, v in self._p.items() if v["expires"] < now]:
                del self._p[k]
            self._p[tok] = dict(plan, expires=now + PLAN_TTL)
        return tok

    def take(self, tok):
        with self._lock:
            p = self._p.pop(str(tok or ""), None)
        if p is None or p["expires"] < time.time():
            raise E.UserError("this change was already applied, was cancelled, or is out of date. Review it again "
                              "and confirm the new plan.")
        return p

    def clear(self):
        with self._lock:
            self._p.clear()


def _check(code, j, what):
    if code in (200, 201, 202, 204):
        return j
    detail = j.get("detail") if isinstance(j, dict) else j
    if isinstance(detail, list):
        detail = "; ".join(str(d.get("msg") or d) if isinstance(d, dict) else str(d) for d in detail[:3])
    if code == 403:
        raise E.UserError(f"your API key is not allowed to {what} (HTTP 403). Ask an admin for the permission.")
    if code == 404:
        raise E.UserError(f"could not {what}: it was not found (HTTP 404). Refresh and try again.")
    raise E.UserError(f"could not {what} (HTTP {code}): {str(detail)[:300]}")


def _q(id_):
    return urllib.parse.quote(str(id_), safe="")


def list_fieldsets(session):
    session.require()
    out, page = [], 1
    while page <= 50:
        code, j = _call(session.client, "GET", f"/v1/streamviewer/field-sets?page_number={page}&page_size=100")
        if code in (403, 404):
            raise E.UserError(f"fieldsets are not available to this API key (HTTP {code})")
        j = _check(code, j, "list fieldsets") or {}
        got = j.get("fieldsets") if isinstance(j, dict) else j
        got = got or []
        out.extend(got)
        if len(got) < 100:
            break
        page += 1
    seen, uniq = set(), []
    for f in out:
        if f.get("id") not in seen:
            seen.add(f.get("id"))
            uniq.append(f)
    return [{"id": f.get("id"), "name": f.get("name"), "description": f.get("description"),
             "fields": list(f.get("fields") or []), "tags": f.get("tags"), "share_level": f.get("share_level"),
             "created_by": f.get("created_by"), "updated_at": f.get("updated_at"),
             "created_at": f.get("created_at")} for f in uniq]


def get_fieldset(session, id_):
    session.require()
    code, j = _call(session.client, "GET", f"/v1/streamviewer/field-set/{_q(id_)}")
    return _check(code, j, "read that fieldset")


def fieldset_field_problems(session, fields):
    """Fieldsets hold stored fields only: the tenant's field catalogue, ext.* and event.parsed.*."""
    known = set(session.fields())
    probs = []
    if not fields:
        probs.append("add at least one field")
    seen = set()
    for f in fields:
        if f in seen:
            probs.append(f"{f} is listed twice")
        seen.add(f)
        if f in known or STORED_RX.match(f):
            continue
        if "." not in f and re.match(r"^[A-Za-z_]\w*$", f):
            probs.append(f"{f} is not a stored field on this tenant (a KQL computed column cannot go in a fieldset)")
        else:
            probs.append(f"{f} is not a field on this tenant")
    return probs


def _diff(before, after, keys):
    out = []
    for k in keys:
        b, a = (before or {}).get(k), (after or {}).get(k)
        if b != a:
            if isinstance(b, list) and isinstance(a, list):
                add = [x for x in a if x not in b]
                rem = [x for x in b if x not in a]
                order = not add and not rem
                out.append({"key": k, "before": b, "after": a, "added": add, "removed": rem, "reordered": order})
            else:
                out.append({"key": k, "before": b, "after": a})
    return out


FS_KEYS = ("name", "fields", "description", "tags", "share_level")


def views_using_fieldsets(session, fs_ids):
    """The saved views that use each of these fieldsets, read from every view's details (the list leaves fieldsets
    out). Slow on a tenant with many views, so only a delete asks. Returns ({fieldset id: [{id, name, orphan}]},
    views checked, views that could not be read). orphan: the view has no columns of its own and no other
    fieldset, so it would be left with none."""
    import abx_content as CT
    summary = CT._list_view_ids(session)
    out: Dict[str, List[dict]] = {f: [] for f in fs_ids}
    unread = 0
    for v, d, _ in CT._view_details(session, summary):
        if d is None:
            unread += 1
            continue
        ids = [x for x in (d.get("fieldset_ids") or []) if x]
        for f in ids:
            if f in out:
                others = [x for x in ids if x not in out]
                out[f].append({"id": v["id"], "name": v.get("name") or v["id"],
                               "orphan": not d.get("fields") and not others})
    return out, len(summary), unread


def affected_view_warnings(session, fs, skip_views=()):
    """Warnings for deleting fieldsets: which saved views use them, loudly when a view would be left with no
    columns. fs: [(id, name)]. skip_views: view ids the same review deletes anyway."""
    users, checked, unread = views_using_fieldsets(session, [f for f, _ in fs])
    out = []
    for fid, name in fs:
        hit = [u for u in users.get(fid, []) if u["id"] not in set(skip_views)]
        if not hit:
            continue
        orphans = [u["name"] for u in hit if u["orphan"]]
        out.append(f"VIEWS AFFECTED: deleting fieldset {name} changes {len(hit)} saved view(s) that use it: "
                   + ", ".join(u["name"] for u in hit[:20]) + (" …" if len(hit) > 20 else ""))
        if orphans:
            out.append(f"VIEWS LEFT WITH NO COLUMNS: {len(orphans)} of them have no other columns and will show none: "
                       + ", ".join(orphans[:20]) + (" …" if len(orphans) > 20 else ""))
    tail = f"Checked {checked} saved view(s) for this fieldset" + ("s" if len(fs) > 1 else "")
    if unread:
        tail += f"; {unread} could not be read, so a view that uses it may be missing from this list"
    if not out:
        tail = ("No saved view uses " + ("these fieldsets" if len(fs) > 1 else "this fieldset") + " (" +
                tail[0].lower() + tail[1:] + ")")
    return out + [tail + "."]


# Who sees a fieldset or saved view, in plain words. Measured on a test tenant (2026-11-20): saved views have no
# private setting (a view read back has no share level, and the list shows views from 28 different people), and an
# API key with views permission also reads other people's private fieldsets (38 of 45 seen were someone else's).
SEEN_PRIVATE = ("Only you will see this in Abstract. An API key with views permission on this tenant can still "
                "read it.")
SEEN_PUBLIC = "Everyone on the tenant will see this."
SEEN_VIEW = "Everyone on the tenant who can open saved views will see this: saved views have no private setting."
SEEN_CHANGE = "This changes it for everyone who can see it."


def visibility(kind, op, share_level=None):
    """The plain-words visibility line for a review."""
    if op in ("update", "delete"):
        return SEEN_CHANGE
    if kind == "view":
        return SEEN_VIEW
    return SEEN_PUBLIC if share_level == "public" else SEEN_PRIVATE


def plan_fieldset(session, op, data=None, id_=None):
    session.require()
    data = dict(data or {})
    if op not in ("create", "update", "delete"):
        raise E.UserError("choose create, update or delete")
    before = None
    if op in ("update", "delete"):
        if not id_:
            raise E.UserError("pick the fieldset to change")
        before = get_fieldset(session, id_)
    after: Dict[str, Any] = {}
    warnings: List[str] = []
    if op in ("create", "update"):
        after = {k: (before or {}).get(k) for k in FS_KEYS} if before else {"share_level": "private"}
        for k in FS_KEYS:
            if k in data and data[k] is not None:
                after[k] = data[k]
        after["name"] = str(after.get("name") or "").strip()
        if not after["name"]:
            raise E.UserError("give the fieldset a name")
        after["fields"] = [str(f).strip() for f in (after.get("fields") or []) if str(f).strip()]
        probs = fieldset_field_problems(session, after["fields"])
        if probs:
            raise E.UserError("This fieldset cannot be saved: " + "; ".join(probs[:5]) + ".")
        if after.get("share_level") not in SHARE_LEVELS:
            raise E.UserError("share level must be private or public")
        if op == "create":
            same = [f for f in list_fieldsets(session) if (f["name"] or "").casefold() == after["name"].casefold()]
            if same:
                warnings.append(f"a fieldset named {after['name']} already exists; this creates a second one")
    changes = _diff(before, after, FS_KEYS) if op != "delete" else \
        [{"key": "fieldset", "before": (before or {}).get("name"), "after": None}]
    if op == "update" and not changes:
        raise E.UserError("Nothing to change: the fieldset already looks like this.")
    if op == "delete":
        warnings += affected_view_warnings(session, [(id_, (before or {}).get("name") or id_)])
    if before and before.get("created_by"):
        warnings.append(f"Created by {before.get('created_by')}.")
    body: Optional[Dict[str, Any]] = None
    if op == "create":
        body = {k: after[k] for k in FS_KEYS if after.get(k) not in (None, "")}
    elif op == "update":
        body = {c["key"]: after.get(c["key"]) for c in changes}
    tok = session._plans.add({"kind": "fieldset", "op": op, "id": id_, "body": body,
                              "updated_at": (before or {}).get("updated_at")})
    seen = visibility("fieldset", op, (after or before or {}).get("share_level"))
    if op == "update" and before and after and before.get("share_level") != after.get("share_level"):
        seen += " " + (SEEN_PUBLIC if after.get("share_level") == "public" else SEEN_PRIVATE)
    return {"kind": "fieldset", "op": op, "id": id_, "name": (after or before or {}).get("name"), "before": before,
            "visibility": seen,
            "after": after or None, "changes": changes, "warnings": warnings, "request": _request_line("fieldset", op, id_),
            "body": body, "token": tok, "expires_in": PLAN_TTL}


def _request_line(kind, op, id_):
    base = {"fieldset": "/v1/streamviewer/field-set", "view": "/v1/streamviewer/view"}[kind]
    return {"create": f"POST {base}", "update": f"PATCH {base}/{id_}", "delete": f"DELETE {base}/{id_}"}[op]


def get_view(session, id_):
    session.require()
    code, j = _call(session.client, "GET", f"/v1/streamviewer/view/{_q(id_)}")
    return _check(code, j, "read that saved view")


def time_selection(spec_time):
    """This tool's time range as a saved view's time_selection, in the shape Abstract's own views store
    ("Relative" with a number and a unit, or "Range" with a start and an end)."""
    t = spec_time or {}
    blank = {"numeric_value": None, "option_selected": None, "start_date": None, "end_date": None,
             "end_numeric_value": None, "end_option_selected": None}
    if t.get("start") and t.get("end"):
        return dict(blank, type="Range", start_date=t["start"], end_date=t["end"])
    m = re.fullmatch(r"(\d+)([mhd])", str(t.get("last") or "24h"))
    if not m:
        return None
    return dict(blank, type="Relative", numeric_value=int(m.group(1)),
                option_selected={"m": "minutes", "h": "hours", "d": "days"}[m.group(2)])


VIEW_KEYS = ("name", "kql_query", "query", "time_selection", "fields", "fieldset_ids")


def builder_query(cond):
    """An API condition as the filter-builder `query` a saved view stores. Measured on a test tenant (2026-11-20): a view
    created with `condition` keeps nothing (condition and query read back null), while `query` in this shape is kept
    verbatim, and it is what Abstract's own filter-builder views hold."""
    n = [0]

    def nid(prefix):
        n[0] += 1
        return f"{prefix}{n[0]}"

    def leaf(c, depth, index, parent):
        vals = c.get("values")
        out = {"id": nid("n"), "depth": depth, "field": c["field"], "index": index,
               "value": ", ".join(str(v) for v in vals) if isinstance(vals, list) else str(c.get("value", "")),
               "parentId": parent, "fieldType": c.get("field_type") or "String",
               "field_operation": c["operation"], "subFieldOperation": "", "eventConditionType": "condition"}
        if c.get("case_sensitive"):
            out["caseSensitive"] = True
        if c.get("from") not in (None, "") or c.get("to") not in (None, ""):
            out["from"], out["to"] = c.get("from"), c.get("to")
        return out

    def group(g, depth, parent):
        gid = nid("g")
        items = [leaf(c, depth + 1, i, gid) for i, c in enumerate(g.get("conditions") or [])]
        items += [group(x, depth + 1, gid) for x in g.get("groups") or []]
        return {"id": gid, "operator": g.get("operator") or "and", "value": items}

    return [group(cond, 0, None)]


def plan_view(session, op, spec=None, id_=None, name=None, fieldset_ids=None):
    import abx_core as C
    session.require()
    if op not in ("create", "update", "delete"):
        raise E.UserError("choose create, update or delete")
    before = None
    if op in ("update", "delete"):
        if not id_:
            raise E.UserError("pick the saved view to change")
        before = get_view(session, id_)
    after: Dict[str, Any] = {}
    warnings: List[str] = []
    if op in ("create", "update"):
        after = {k: (before or {}).get(k) for k in VIEW_KEYS} if before else {}
        if spec is not None:
            sp = C.normalize_spec(spec)
            probs = C.spec_problems(sp)
            if probs:
                raise E.UserError(probs[0])
            if sp["mode"] == "kql":
                if not session.kql_on():
                    raise E.UserError(C.KQL_REMOVED_MSG)
                after["kql_query"], after["query"] = sp["kql"], None
            else:
                cond = C.build_condition(sp)
                after["query"] = builder_query(cond)
                after["kql_query"] = None
                if session.kql_on():                     # where KQL applies, the view also opens in Abstract's KQL box
                    fk = server_from_conditions(session.client, cond)
                    if fk and fk.get("kql"):
                        after["kql_query"] = fk["kql"]
            after["time_selection"] = time_selection(sp.get("time"))
            after["fields"] = [f for f in sp["fields"] if f != "id"] or None
            if not name and sp.get("name"):
                name = sp["name"]
        if name:
            after["name"] = str(name).strip()
        if fieldset_ids is not None:
            after["fieldset_ids"] = list(fieldset_ids) or None
        if not (after.get("name") or "").strip():
            raise E.UserError("give the saved view a name")
        if after.get("fields"):
            probs = fieldset_field_problems(session, after["fields"])
            if probs:
                raise E.UserError("This view cannot be saved: " + "; ".join(probs[:5]) + ".")
        if not after.get("fields") and not after.get("fieldset_ids"):
            raise E.UserError("a saved view needs columns: add fields or pick a fieldset")
    changes = _diff(before, after, VIEW_KEYS) if op != "delete" else [{"key": "view", "before": (before or {}).get("name"), "after": None}]
    if op == "update" and not changes:
        raise E.UserError("Nothing to change: the saved view already looks like this.")
    body: Optional[Dict[str, Any]] = None
    if op == "create":
        body = {k: after[k] for k in VIEW_KEYS if after.get(k) not in (None, "", [])}
        body["storage_type"] = "HOT"
    elif op == "update":
        body = {c["key"]: after.get(c["key"]) for c in changes}
    tok = session._plans.add({"kind": "view", "op": op, "id": id_, "body": body,
                              "updated_at": (before or {}).get("updated_at")})
    return {"kind": "view", "op": op, "id": id_, "name": (after or before or {}).get("name"), "before": before,
            "visibility": visibility("view", op),
            "after": after or None, "changes": changes, "warnings": warnings, "request": _request_line("view", op, id_),
            "body": body, "token": tok, "expires_in": PLAN_TTL}


def apply_plan(session, token):
    session.require()
    p = session._plans.take(token)
    if p["kind"] == "bundle":                            # a content bundle: one review, many changes
        import abx_content
        return abx_content.apply_bundle(session, p)
    kind, op, id_ = p["kind"], p["op"], p["id"]
    get = get_fieldset if kind == "fieldset" else get_view
    base = "/v1/streamviewer/field-set" if kind == "fieldset" else "/v1/streamviewer/view"
    label = "fieldset" if kind == "fieldset" else "saved view"
    if op in ("update", "delete"):
        now = get(session, id_)
        if p.get("updated_at") and now.get("updated_at") and now.get("updated_at") != p["updated_at"]:
            raise E.UserError(f"this {label} was changed by someone else after you reviewed it. Review it again.")
    if op == "create":
        code, j = _call(session.client, "POST", base, p["body"])
        res = _check(code, j, f"create the {label}")
        new_id = (res or {}).get("id") if isinstance(res, dict) else None
    elif op == "update":
        code, j = _call(session.client, "PATCH", f"{base}/{_q(id_)}", p["body"])
        res = _check(code, j, f"update the {label}")
        new_id = id_
    else:
        code, j = _call(session.client, "DELETE", f"{base}/{_q(id_)}")
        res = _check(code, j, f"delete the {label}")
        new_id = id_
    _audit(session, kind, op, new_id, p.get("body"))
    return {"kind": kind, "op": op, "id": new_id, "result": res if isinstance(res, dict) else None}


def _audit(session, kind, op, id_, body):
    """One line per change in the daily change log next to the error log (names and fields only, never keys)."""
    try:
        line = json.dumps({"at": E.iso(E.now()), "tenant": (session.tenant or {}).get("vendor_account_id"),
                           "kind": kind, "op": op, "id": id_,
                           "name": (body or {}).get("name"), "fields": (body or {}).get("fields")})
        d = E.log_dir()
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "changes-" + E.now().strftime("%Y-%m-%d") + ".log")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
