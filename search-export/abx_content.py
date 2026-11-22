"""Saved views and fieldsets as code: export them to files, compare files with a tenant, and apply files to a
tenant (the same one or another).

A bundle is a folder (or the same content as one JSON document):

    bundle.json              schema, when, the source tenant, counts, the SHA-256 of every file, the source ids
    fieldsets/<name>.json    {name, fields, description, tags, share_level}
    views/<name>.json        {name, query and/or kql_query, time_selection, fields, fieldsets (by NAME), …}

Object files hold no ids, timestamps or authors, keys are sorted and the JSON layout is fixed, so exporting the same
tenant twice gives byte-identical object files and a git diff shows only real changes. Views refer to fieldsets by
name, so a bundle applies to any tenant. A view's search is kept exactly as Abstract stores it (the filter-builder
`query` and/or `kql_query`); a key this tool does not know is kept, flagged and never sent.

Applying is one review for the whole bundle: fieldsets first (a view may need a fieldset the bundle creates), then
views; with prune, objects that exist only in the tenant are deleted. Nothing changes until that review is confirmed
with its single-use token, and each change re-reads its object first, exactly like a single change.
"""
import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional

import abx_engine as E
import abx_platform as P

SCHEMA = "abstract.content/v1"
MAX_OBJECTS = 2000
MAX_BUNDLE_BYTES = 8 * 1024 * 1024

FS_PORTABLE = ("name", "fields", "description", "tags", "share_level")
# What a view keeps when it travels. Everything else Abstract returns is either runtime state (DROP) or unknown
# (kept under "unsupported", flagged, never sent).
VIEW_PORTABLE = ("name", "query", "kql_query", "unstructured_query", "condition", "time_selection", "fields",
                 "storage_type", "column_properties", "order_by", "order_type", "group_by", "associations")
VIEW_DROP = {"id", "created_at", "updated_at", "created_by", "updated_by", "last_executed_query", "queries",
             "vendor_account_id", "organization_id", "fieldset_ids", "fieldsets", "source_ids", "is_favorite",
             "favorite", "owner", "share_level", "_job", "kql_verdict"}    # kql_verdict: Abstract's own check
# Values Abstract fills in when a view is created without them. Measured on a test tenant (2026-11-20): an update
# that sends order_by null is refused (HTTP 422), so a missing key means "the default", never null.
VIEW_DEFAULTS = {"order_by": "@timestamp", "order_type": "DESC", "storage_type": "HOT"}
VIEW_COMPARE = ("query", "kql_query", "unstructured_query", "condition", "time_selection", "fields", "storage_type",
                "column_properties", "order_by", "order_type", "group_by", "associations", "fieldsets")


def _slug(name, taken):
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:60] or "item"
    out, i = s, 2
    while out in taken:
        out, i = f"{s}-{i}", i + 1
    taken.add(out)
    return out


def _dump(obj):
    """The fixed JSON layout of every bundle file: sorted keys, 2 spaces, UTF-8, a final newline."""
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _clean(v: Any) -> Any:
    """Drop top-level None and empty values so a missing key and a null key compare equal. Values inside are kept
    exactly as Abstract stores them (a view's search must travel byte for byte)."""
    if isinstance(v, dict):
        return {k: x for k, x in v.items() if x not in (None, "", [], {})}
    return v


def fieldset_object(f) -> Dict[str, Any]:
    o = {k: f.get(k) for k in FS_PORTABLE}
    o["fields"] = list(o.get("fields") or [])
    o["share_level"] = o.get("share_level") or "private"
    return _clean(o)


def view_object(d, fs_names) -> Dict[str, Any]:
    """A saved view as a portable object. fs_names maps fieldset id -> name on the source tenant."""
    o: Dict[str, Any] = {k: d.get(k) for k in VIEW_PORTABLE if k in d}
    flags = []
    ids = [x for x in (d.get("fieldset_ids") or []) if x]
    names = []
    for i in ids:
        if i in fs_names:
            names.append(fs_names[i])
        else:
            flags.append(f"uses a fieldset ({i}) that could not be read; it is left out")
    if names:
        o["fieldsets"] = names
    if len(d.get("source_ids") or []) > 1:
        flags.append("also searched other sources (such as federated search); only Abstract's own store travels")
    if not (d.get("query") or d.get("kql_query") or d.get("unstructured_query")) and d.get("queries"):
        flags.append("a replay view: its search lives in the replay job it ran, which does not travel; it applies as "
                     "columns and a time range only")
    extra = {k: v for k, v in d.items() if k not in VIEW_PORTABLE and k not in VIEW_DROP and v not in (None, "", [], {})}
    if extra:
        o["unsupported"] = extra
        flags.append("holds settings this tool does not know (" + ", ".join(sorted(extra)) + "); they are kept in "
                     "the file and not sent")
    o = _clean(o)
    if isinstance(o.get("time_selection"), dict):        # Abstract fills the unused parts with null
        o["time_selection"] = {k: v for k, v in o["time_selection"].items() if v is not None}
    for k, dflt in VIEW_DEFAULTS.items():
        if o.get(k) == dflt:
            del o[k]
    if flags:
        o["notes"] = flags
    return o


# ── reading a tenant ─────────────────────────────────────────────────────────────────────────────────────────────
def _list_view_ids(session):
    out, page, seen = [], 1, set()
    while page <= 50:
        code, j = P._call(session.client, "GET", f"/v1/streamviewer/views?page_size=200&page_number={page}")
        j = P._check(code, j, "list saved views") or {}
        got = (j.get("views") if isinstance(j, dict) else None) or []
        new = [v for v in got if v.get("id") not in seen]
        if not new:
            break
        seen.update(v.get("id") for v in new)
        out += new
        if len(got) < 200:
            break
        page += 1
    return out


def _view_details(session, summary):
    from concurrent.futures import ThreadPoolExecutor
    ctx = {k: getattr(E._TL, k, None) for k in ("sink", "cancel", "creds")}

    def one(v):
        for k, val in ctx.items():
            setattr(E._TL, k, val)
        try:
            return v, P.get_view(session, v["id"]), None
        except (E.UserError, E.Fatal, E.Retryable) as ex:
            return v, None, str(ex)
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one, summary))


def _pick(items, wanted, label):
    if wanted is None:
        return list(items)
    out, missing = [], []
    for w in wanted:
        w = str(w).strip()
        hit = [x for x in items if x.get("id") == w] or \
              [x for x in items if str(x.get("name") or "").casefold() == w.casefold()]
        if hit:
            out += [h for h in hit if h not in out]
        else:
            missing.append(w)
    if missing:
        raise E.UserError(f"no {label} called " + ", ".join(repr(m) for m in missing) + " on this tenant")
    return out


def read_tenant(session, views=None, fieldsets=None):
    """The tenant's views and fieldsets as portable objects: {"views": [...], "fieldsets": [...], "source_ids",
    "problems"}. views/fieldsets: None = all, or a list of names or ids. Fieldsets that chosen views use are always
    included, so the bundle applies on its own."""
    session.require()
    all_fs = P.list_fieldsets(session)
    fs_names = {f["id"]: f["name"] for f in all_fs}
    problems: List[str] = []
    v_sum = _list_view_ids(session) if views is None or views else []
    chosen_v = _pick(v_sum, views, "saved view") if views is not None else v_sum
    detail = _view_details(session, chosen_v) if chosen_v else []
    vobjs, source_ids = [], {"views": {}, "fieldsets": {}}
    need_fs = set()
    for v, d, err in detail:
        if d is None:
            problems.append(f"saved view {v.get('name') or v.get('id')} could not be read ({err}); it is left out")
            continue
        o = view_object(d, fs_names)
        vobjs.append(o)
        source_ids["views"][o.get("name") or v["id"]] = v["id"]
        need_fs.update(o.get("fieldsets") or [])
    fs_chosen = _pick(all_fs, fieldsets, "fieldset") if fieldsets is not None else list(all_fs)
    have = {f["name"] for f in fs_chosen}
    fs_chosen += [f for f in all_fs if f["name"] in need_fs and f["name"] not in have]
    fobjs = []
    for f in fs_chosen:
        fobjs.append(fieldset_object(f))
        source_ids["fieldsets"][f["name"]] = f["id"]
    return {"views": vobjs, "fieldsets": fobjs, "source_ids": source_ids, "problems": problems}


# ── bundles: document <-> folder ─────────────────────────────────────────────────────────────────────────────────
def document(session, views=None, fieldsets=None, kind=None):
    """The bundle as one JSON document (what the browser app and MCP pass around). views/fieldsets: None = every one,
    [] = none, or names/ids. kind "views" or "fieldsets" exports only that kind (a views-only bundle still carries the
    fieldsets its views use, so it applies on its own). "scope" records which kinds the bundle holds in FULL: prune
    deletes only those."""
    if kind not in (None, "", "views", "fieldsets"):
        raise E.UserError("kind is views or fieldsets")
    if kind == "views":
        fieldsets = []
    elif kind == "fieldsets":
        views = []
    t = read_tenant(session, views, fieldsets)
    tn = session.tenant or {}
    scope = {"views": "all" if views is None else ("some" if t["views"] else "none"),
             "fieldsets": "all" if fieldsets is None else ("some" if t["fieldsets"] else "none")}
    return {"schema": SCHEMA, "exported_at": E.iso(E.now())[:19] + "Z", "scope": scope,
            "source": {"tenant_id": tn.get("vendor_account_id"), "tenant_name": tn.get("name")},
            "views": sorted(t["views"], key=lambda o: str(o.get("name") or "").casefold()),
            "fieldsets": sorted(t["fieldsets"], key=lambda o: str(o.get("name") or "").casefold()),
            "source_ids": t["source_ids"], "problems": t["problems"] + _dup_notes(t)}


def _dup_notes(t):
    d = duplicates(dict(t, schema=SCHEMA))
    return [f"{len([o for o in t[k] if str(o.get('name') or '').strip().casefold() == n])} {k} are called "
            f"{next(o['name'] for o in t[k] if str(o.get('name') or '').strip().casefold() == n)!r}; they are all "
            "exported, but a review cannot tell them apart, so applying skips them until they are renamed"
            for k in ("fieldsets", "views") for n in sorted(d[k])]


def write_folder(doc, out_dir):
    """Write a bundle document as a folder. Returns {"dir", "files", "counts"}."""
    validate(doc)
    out_dir = os.path.abspath(os.path.expanduser(out_dir))
    for sub in ("views", "fieldsets"):
        os.makedirs(os.path.join(out_dir, sub), exist_ok=True)
        for fn in os.listdir(os.path.join(out_dir, sub)):          # a re-export replaces the previous files
            if fn.endswith(".json"):
                os.remove(os.path.join(out_dir, sub, fn))
    hashes, files = {}, []
    for sub, key in (("fieldsets", "fieldsets"), ("views", "views")):
        taken: set = set()
        for o in doc[key]:
            rel = f"{sub}/{_slug(o.get('name'), taken)}.json"
            data = _dump(o)
            with open(os.path.join(out_dir, rel), "wb") as fh:
                fh.write(data)
            hashes[rel] = _sha(data)
            files.append(rel)
    meta = {"schema": SCHEMA, "exported_at": doc.get("exported_at"), "source": doc.get("source") or {},
            "scope": doc.get("scope") or prune_scope(doc),
            "counts": {"views": len(doc["views"]), "fieldsets": len(doc["fieldsets"])}, "files": hashes,
            "source_ids": doc.get("source_ids") or {}, "problems": doc.get("problems") or []}
    with open(os.path.join(out_dir, "bundle.json"), "wb") as fh:
        fh.write(_dump(meta))
    return {"dir": out_dir, "files": ["bundle.json"] + files, "counts": meta["counts"]}


def read_folder(path):
    """A bundle folder (or a single bundle .json document) as a document. Every file listed in bundle.json must be
    present with the recorded SHA-256; files added by hand (not listed) are read too, and said so."""
    path = os.path.abspath(os.path.expanduser(str(path or "")))
    if os.path.isfile(path):
        if os.path.getsize(path) > MAX_BUNDLE_BYTES:
            raise E.UserError("this bundle is larger than 8 MB")
        with open(path, encoding="utf-8-sig") as fh:
            try:
                doc = json.load(fh)
            except ValueError as ex:
                raise E.UserError(f"{path} is not valid JSON ({ex})")
        validate(doc)
        return doc
    meta_p = os.path.join(path, "bundle.json")
    if not os.path.isfile(meta_p):
        raise E.UserError(f"{path} is not a bundle: there is no bundle.json in it (make one with: content export)")
    with open(meta_p, encoding="utf-8-sig") as fh:
        meta = json.load(fh)
    if meta.get("schema") != SCHEMA:
        raise E.UserError(f"bundle.json says schema {meta.get('schema')!r}; this tool reads {SCHEMA}")
    doc: Dict[str, Any] = {"schema": SCHEMA, "exported_at": meta.get("exported_at"), "source": meta.get("source"),
                           "scope": meta.get("scope"),
                           "views": [], "fieldsets": [], "source_ids": meta.get("source_ids") or {}, "problems": [],
                           "notes": []}
    listed = dict(meta.get("files") or {})
    total = 0
    for sub in ("fieldsets", "views"):
        d = os.path.join(path, sub)
        for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if not fn.endswith(".json"):
                continue
            rel = f"{sub}/{fn}"
            with open(os.path.join(d, fn), "rb") as fh:
                data = fh.read()
            total += len(data)
            if total > MAX_BUNDLE_BYTES:
                raise E.UserError("this bundle is larger than 8 MB")
            if rel in listed and listed[rel] != _sha(data):
                doc["notes"].append(f"{rel} was edited after it was exported")
            elif rel not in listed:
                doc["notes"].append(f"{rel} was added after the export")
            try:
                doc[sub].append(json.loads(data.decode("utf-8-sig")))
            except ValueError as ex:
                raise E.UserError(f"{rel} is not valid JSON ({ex})")
            listed.pop(rel, None)
    for rel in sorted(listed):
        doc["notes"].append(f"{rel} is listed in bundle.json but missing: it is not applied, and with prune its "
                            "object would be deleted")
    validate(doc)
    return doc


def validate(doc):
    """Refuse anything that is not a bundle this tool wrote (or one shaped like it)."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise E.UserError(f"not a content bundle (schema {SCHEMA} expected)")
    for key in ("views", "fieldsets"):
        if not isinstance(doc.get(key), list):
            raise E.UserError(f"the bundle's {key} must be a list")
    if len(doc["views"]) + len(doc["fieldsets"]) > MAX_OBJECTS:
        raise E.UserError(f"a bundle holds at most {MAX_OBJECTS} views and fieldsets")
    for key in ("fieldsets", "views"):
        for o in doc[key]:
            if not isinstance(o, dict) or not str(o.get("name") or "").strip():
                raise E.UserError(f"every object in the bundle's {key} needs a name")
            if key == "fieldsets" and not isinstance(o.get("fields"), list):
                raise E.UserError(f"fieldset {o['name']!r} has no fields list")
    return True


def prune_scope(doc):
    """Which kinds a bundle holds in full. A bundle written by export says so in "scope"; a hand-made one without
    it counts a kind as held when it has at least one object of that kind. Prune deletes only kinds held in full."""
    sc = doc.get("scope") if isinstance(doc.get("scope"), dict) else None
    if sc:
        return {k: sc.get(k) if sc.get(k) in ("all", "some", "none") else "none" for k in ("views", "fieldsets")}
    return {k: "all" if doc.get(k) else "none" for k in ("views", "fieldsets")}


def duplicates(doc):
    """Names used by more than one view (or fieldset) in the bundle. A tenant can hold two items with the same name;
    they export side by side, but cannot be told apart when applied, so the review skips them."""
    out: Dict[str, set] = {"views": set(), "fieldsets": set()}
    for key in ("fieldsets", "views"):
        seen: set = set()
        for o in doc[key]:
            n = str(o.get("name") or "").strip().casefold()
            if n in seen:
                out[key].add(n)
            seen.add(n)
    return out


# ── compare and apply ────────────────────────────────────────────────────────────────────────────────────────────
def _index(objs):
    out: Dict[str, List[dict]] = {}
    for o in objs:
        out.setdefault(str(o.get("name") or "").strip().casefold(), []).append(o)
    return out


def _cmp_view(o):
    out = {k: o.get(k) for k in VIEW_COMPARE if o.get(k) not in (None, "", [], {})}
    if isinstance(out.get("time_selection"), dict):
        out["time_selection"] = {k: v for k, v in out["time_selection"].items() if v is not None}
    for k, dflt in VIEW_DEFAULTS.items():
        if out.get(k) == dflt:
            del out[k]
    return out


def _cmp_fs(o):
    return {k: o.get(k) for k in FS_PORTABLE if o.get(k) not in (None, "", [], {})}


def _tenant_views(session, need, fs_names):
    """The tenant's saved views by name. Only views whose names are in `need` are read in full (a tenant can hold
    a thousand views; a bundle usually names a few); the rest are known by name, id and last change, which is all
    a prune needs."""
    out: Dict[str, List[dict]] = {}
    v_sum = _list_view_ids(session)
    wanted = [v for v in v_sum if str(v.get("name") or "").strip().casefold() in need]
    detail = {v["id"]: d for v, d, _ in _view_details(session, wanted)} if wanted else {}
    for v in v_sum:
        n = str(v.get("name") or "").strip().casefold()
        d = detail.get(v["id"])
        if d is not None:
            o = dict(view_object(d, fs_names), _id=v["id"], _updated=d.get("updated_at"))
        else:
            o = {"name": v.get("name") or v["id"], "_id": v["id"], "_updated": v.get("updated_at"),
                 "_stub": n in need}                       # in need but unreadable: never compared or overwritten
        out.setdefault(n, []).append(o)
    return out


def diff(session, doc):
    """Each object of the bundle against the tenant: same, changed, new, or only_in_tenant."""
    validate(doc)
    fs_all = P.list_fieldsets(session)
    fs_names = {f["id"]: f["name"] for f in fs_all}
    need = {str(o["name"]).strip().casefold() for o in doc["views"]}
    t_views = _tenant_views(session, need, fs_names)
    out = []
    for kind, mine, t_idx, cmp_ in (("fieldset", doc["fieldsets"], _index([fieldset_object(f) for f in fs_all]),
                                     _cmp_fs), ("view", doc["views"], t_views, _cmp_view)):
        dups = duplicates(doc)[kind + "s"]
        for o in mine:
            hits = t_idx.get(str(o["name"]).strip().casefold(), [])
            if str(o["name"]).strip().casefold() in dups:
                out.append({"kind": kind, "name": o["name"], "status": "ambiguous",
                            "reason": f"the bundle has more than one {kind} with this name"})
            elif len(hits) > 1:
                out.append({"kind": kind, "name": o["name"], "status": "ambiguous",
                            "reason": f"the tenant has {len(hits)} {kind}s with this name"})
            elif not hits:
                out.append({"kind": kind, "name": o["name"], "status": "new"})
            elif hits[0].get("_stub"):
                out.append({"kind": kind, "name": o["name"], "status": "ambiguous",
                            "reason": "the tenant's copy could not be read; try again"})
            else:
                a, b = cmp_(o), cmp_(hits[0])
                ch = [c["key"] for c in P._diff(b, a, sorted(set(a) | set(b)))]
                out.append({"kind": kind, "name": o["name"], "status": "changed" if ch else "same", "changes": ch})
        mine_names = {str(o["name"]).strip().casefold() for o in mine}
        extra = [h for name, hits in t_idx.items() if name not in mine_names for h in hits]
        if prune_scope(doc).get(kind + "s"):
            out += [{"kind": kind, "name": h["name"], "status": "only_in_tenant"} for h in extra]
        elif extra:
            # the bundle holds only some of this kind: the rest of the tenant is not part of the comparison
            out.append({"kind": kind, "name": f"{len(extra)} other {kind}(s) on the tenant", "status": "not_compared",
                        "count": len(extra),
                        "reason": "this bundle holds only some of them, so the others are not compared"})
    return out


def plan(session, doc, prune=False):
    """One review for the whole bundle. Returns {"items": [...], "skipped": [...], "token", "counts", "prune",
    "warnings"}. Each item says what it will do (create, update or delete), the request, and the changes."""
    session.require()
    validate(doc)
    fs_all = P.list_fieldsets(session)
    fs_names = {f["id"]: f["name"] for f in fs_all}
    t_fs = _index([dict(fieldset_object(f), _id=f["id"], _updated=f.get("updated_at")) for f in fs_all])
    t_v = _tenant_views(session, {str(o["name"]).strip().casefold() for o in doc["views"]}, fs_names)
    items, skipped, warnings = [], [], list(doc.get("notes") or [])
    bundle_fs = {str(o["name"]).strip().casefold() for o in doc["fieldsets"]}
    dups = duplicates(doc)
    for o in doc["fieldsets"]:
        hits = t_fs.get(str(o["name"]).strip().casefold(), [])
        if str(o["name"]).strip().casefold() in dups["fieldsets"]:
            skipped.append({"kind": "fieldset", "name": o["name"], "reason": "the bundle has more than one fieldset "
                                                                             "with this name; rename them first"})
            continue
        if len(hits) > 1:
            skipped.append({"kind": "fieldset", "name": o["name"], "reason": f"{len(hits)} fieldsets have this name "
                                                                             "on the tenant; rename one first"})
            continue
        probs = P.fieldset_field_problems(session, list(o.get("fields") or []))
        if probs:
            skipped.append({"kind": "fieldset", "name": o["name"], "reason": "; ".join(probs[:3])})
            continue
        want = fieldset_object(o)
        if not hits:
            items.append({"kind": "fieldset", "op": "create", "name": o["name"], "id": None,
                          "body": {k: want[k] for k in FS_PORTABLE if k in want},
                          "changes": [{"key": k, "before": None, "after": want.get(k)} for k in FS_PORTABLE
                                      if k in want]})
            continue
        have = hits[0]
        ch = P._diff(_cmp_fs(have), _cmp_fs(want), sorted(set(_cmp_fs(have)) | set(_cmp_fs(want))))
        if ch:
            items.append({"kind": "fieldset", "op": "update", "name": o["name"], "id": have["_id"],
                          "updated_at": have.get("_updated"), "body": {c["key"]: want.get(c["key"]) for c in ch},
                          "changes": ch})
    for o in doc["views"]:
        hits = t_v.get(str(o["name"]).strip().casefold(), [])
        if str(o["name"]).strip().casefold() in dups["views"]:
            skipped.append({"kind": "view", "name": o["name"], "reason": "the bundle has more than one saved view "
                                                                         "with this name; rename them first"})
            continue
        if len(hits) > 1:
            skipped.append({"kind": "view", "name": o["name"], "reason": f"{len(hits)} saved views have this name "
                                                                         "on the tenant; rename one first"})
            continue
        amb = [n for n in o.get("fieldsets") or [] if n.strip().casefold() in dups["fieldsets"]
               or len(t_fs.get(n.strip().casefold(), [])) > 1]
        if amb:
            skipped.append({"kind": "view", "name": o["name"], "reason": "uses a fieldset name that more than one "
                                                                         "fieldset has: " + ", ".join(amb)})
            continue
        if hits and hits[0].get("_stub"):
            skipped.append({"kind": "view", "name": o["name"], "reason": "the tenant's copy could not be read; try "
                                                                         "again"})
            continue
        missing = [n for n in o.get("fieldsets") or [] if n.strip().casefold() not in bundle_fs
                   and not t_fs.get(n.strip().casefold())]
        if missing:
            skipped.append({"kind": "view", "name": o["name"], "reason": "uses fieldset(s) neither the bundle nor the "
                                                                         "tenant has: " + ", ".join(missing)})
            continue
        if o.get("fields"):
            probs = P.fieldset_field_problems(session, list(o["fields"]))
            if probs:
                skipped.append({"kind": "view", "name": o["name"], "reason": "; ".join(probs[:3])})
                continue
        if not (o.get("fields") or o.get("fieldsets")):
            skipped.append({"kind": "view", "name": o["name"], "reason": "a saved view needs columns (fields or a "
                                                                         "fieldset)"})
            continue
        for n in o.get("notes") or []:
            warnings.append(f"view {o['name']}: {n}")
        body = {k: o[k] for k in VIEW_PORTABLE if k in o and k != "condition"}
        if o.get("condition") and not (o.get("query") or o.get("kql_query")):
            warnings.append(f"view {o['name']}: holds only a condition, which Abstract does not keep on a view; it "
                            "applies as columns and a time range")
        if not hits:
            items.append({"kind": "view", "op": "create", "name": o["name"], "id": None,
                          "body": dict(body, storage_type=body.get("storage_type") or "HOT"),
                          "fieldset_names": list(o.get("fieldsets") or []),
                          "changes": [{"key": k, "before": None, "after": "…"} for k in sorted(_cmp_view(o))]})
            continue
        have = hits[0]
        ch = P._diff(_cmp_view(have), _cmp_view(o), sorted(set(_cmp_view(have)) | set(_cmp_view(o))))
        if ch:
            upd = {c["key"]: o.get(c["key"], VIEW_DEFAULTS.get(c["key"])) for c in ch if c["key"] != "fieldsets"}
            items.append({"kind": "view", "op": "update", "name": o["name"], "id": have["_id"],
                          "updated_at": have.get("_updated"), "body": upd,
                          "fieldset_names": list(o.get("fieldsets") or []) if any(c["key"] == "fieldsets" for c in ch)
                          else None, "changes": ch})
    if prune:
        sc = prune_scope(doc)
        for k, label in (("views", "saved views"), ("fieldsets", "fieldsets")):
            if sc[k] != "all":
                warnings.append(f"Prune leaves {label} alone: this bundle does not hold every one of them.")
        b_v = {str(o["name"]).strip().casefold() for o in doc["views"]}
        for name, hits in (t_v.items() if sc["views"] == "all" else []):
            if name not in b_v:
                items += [{"kind": "view", "op": "delete", "name": h["name"], "id": h["_id"],
                           "updated_at": h.get("_updated"), "body": None,
                           "changes": [{"key": "view", "before": h["name"], "after": None}]} for h in hits]
        for name, hits in (t_fs.items() if sc["fieldsets"] == "all" else []):
            if name not in bundle_fs:
                items += [{"kind": "fieldset", "op": "delete", "name": h["name"], "id": h["_id"],
                           "updated_at": h.get("_updated"), "body": None,
                           "changes": [{"key": "fieldset", "before": h["name"], "after": None}]} for h in hits]
        n_del = sum(1 for i in items if i["op"] == "delete")
        if n_del:
            warnings.insert(0, f"PRUNE: {n_del} object(s) on this tenant are NOT in the bundle and will be DELETED. "
                               "Read the list before you confirm.")
    fs_del = [(i["id"], i["name"]) for i in items if i["kind"] == "fieldset" and i["op"] == "delete"]
    if fs_del:                                        # say which views lose columns, before anything is confirmed
        v_del = [i["id"] for i in items if i["kind"] == "view" and i["op"] == "delete"]
        at = 1 if warnings and warnings[0].startswith("PRUNE") else 0
        warnings[at:at] = P.affected_view_warnings(session, fs_del, v_del)
    for i in items:
        i["request"] = P._request_line(i["kind"], i["op"], i["id"] or "")
    for i in items:
        i["visibility"] = P.visibility(i["kind"], i["op"], (i.get("body") or {}).get("share_level"))
    counts = {op: sum(1 for i in items if i["op"] == op) for op in ("create", "update", "delete")}
    tok = session._plans.add({"kind": "bundle", "op": "apply", "id": None, "body": None, "updated_at": None,
                              "items": items}) if items else None
    tn = session.tenant or {}
    return {"kind": "bundle", "tenant": {"id": tn.get("vendor_account_id"), "name": tn.get("name")},
            "source": doc.get("source"), "items": items, "skipped": skipped, "warnings": warnings, "counts": counts,
            "prune": bool(prune), "token": tok, "expires_in": P.PLAN_TTL if tok else None,
            "name": f"{counts['create']} create, {counts['update']} update, {counts['delete']} delete"}


def apply_bundle(session, p):
    """Make every change of a confirmed bundle review, in order (fieldsets, then views, then deletes). Each change
    re-reads its object first. Stops at the first failure and says what was done and what was not."""
    items = list(p.get("items") or [])
    order = {("fieldset", "create"): 0, ("fieldset", "update"): 1, ("view", "create"): 2, ("view", "update"): 3,
             ("view", "delete"): 4, ("fieldset", "delete"): 5}
    items.sort(key=lambda i: order.get((i["kind"], i["op"]), 9))
    done: List[dict] = []
    failed: Optional[dict] = None
    fs_ids: Dict[str, str] = {}

    def fs_id(name):
        k = name.strip().casefold()
        if k not in fs_ids:
            for f in P.list_fieldsets(session):
                fs_ids.setdefault(str(f["name"]).strip().casefold(), f["id"])
        return fs_ids.get(k)
    for n, it in enumerate(items):
        try:
            body = dict(it["body"]) if it.get("body") is not None else None
            if it["kind"] == "view" and it.get("fieldset_names") is not None and body is not None:
                ids = [fs_id(x) for x in it["fieldset_names"]]
                if any(i is None for i in ids):
                    raise E.UserError("a fieldset this view uses was not found on the tenant")
                body["fieldset_ids"] = ids or None
            tok = session._plans.add({"kind": it["kind"], "op": it["op"], "id": it["id"], "body": body,
                                      "updated_at": it.get("updated_at")})
            r = P.apply_plan(session, tok)
            if it["kind"] == "fieldset" and it["op"] == "create" and r.get("id"):
                fs_ids[str(it["name"]).strip().casefold()] = r["id"]
            done.append({"kind": it["kind"], "op": it["op"], "name": it["name"], "id": r.get("id")})
        except (E.UserError, E.Fatal, E.Retryable) as ex:
            failed = {"kind": it["kind"], "op": it["op"], "name": it["name"], "error": str(ex)}
            rest = [{"kind": x["kind"], "op": x["op"], "name": x["name"]} for x in items[n + 1:]]
            return {"kind": "bundle", "op": "apply", "id": None, "done": done, "failed": failed, "not_attempted": rest,
                    "result": None}
    return {"kind": "bundle", "op": "apply", "id": None, "done": done, "failed": None, "not_attempted": [],
            "result": None}


def export(session, out_dir, views=None, fieldsets=None, kind=None):
    doc = document(session, views, fieldsets, kind)
    r = write_folder(doc, out_dir)
    r["problems"] = doc["problems"]
    return r


def default_dir(session):
    """A new folder under the exports root for a content export."""
    import abx_core as C
    tn = session.tenant or {}
    label = E.safe_label(tn.get("name") or tn.get("vendor_account_id") or "tenant")
    return os.path.join(C.home_dir(), "content", f"{label}-{E.now().strftime('%Y%m%dT%H%M%SZ')}")
