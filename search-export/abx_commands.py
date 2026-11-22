"""Command-line commands for KQL, plain English, fieldsets, saved-view changes and the endpoint list.

    kql check   "QUERY"                       how the query would run here, without running it
    kql preview "QUERY" [--last 1h] [--rows N] run it and show the first rows (or the result table)
    kql save    "QUERY" [--format csv|json] [--name N]   save the result table to a new export folder
    nl "WHAT YOU WANT" [--preview]            plain English to KQL with Abstract's assistant; never runs unless --preview
    fieldsets [list] | show ID|NAME
    fieldsets create --name N --fields a,b [--from-dataset NAME] [--description T] [--share private|public] [--yes]
    fieldsets update ID|NAME [--name N] [--fields a,b] [--add a,b] [--remove a,b] [--description T] [--yes]
    fieldsets delete ID|NAME [--yes]
    views save --name N <search> [--fieldset ID|NAME] [--yes]
    views update ID|NAME [<search>] [--name N] [--fieldset ID|NAME] [--yes]
    views delete ID|NAME [--yes]
    endpoints                                 the platform endpoints this tool uses and what this session saw
    content export [DIR] [--views a,b] [--fieldsets a,b] [--kind views|fieldsets]   views and fieldsets as files
    content diff DIR                          compare a bundle with the tenant
    content apply DIR [--prune] [--yes]       make the tenant match the bundle (one review for the whole bundle)
    automate TARGET (--from-saved NAME | <search>) [--every 1d] [--at 00:20] [--out DIR]
                                              the files that run a search on a schedule (cron, launchd, systemd,
                                              windows-task, github-actions, container-cron)

A change to the tenant prints exactly what it will do and stops (exit 3). Run it again with --yes to make it.
"""
import argparse
import json
import os
import sys

import abx_core as C
import abx_engine as E

S = C.sanitize
NEEDS_YES = 3


def _p(prog, desc):
    p = argparse.ArgumentParser(prog=f"abstract_search.py {prog}", description=desc)
    p.add_argument("--expect-tenant", help="the vendor account id you expect (or ABSTRACT_EXPECT_TENANT)")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    return p


def _time(p):
    p.add_argument("--last", help="15m, 1h, 4h, 24h, 7d … (default 1h)")
    p.add_argument("--start", help="UTC start, e.g. 2026-11-01T00:00:00Z")
    p.add_argument("--end", help="UTC end")


def _time_spec(a, default="1h"):
    if a.start or a.end:
        return {"start": a.start or "", "end": a.end or ""}
    return {"last": a.last or default}


def _print_check(c):
    print(f"\n  {S(c['kql'])}\n")
    if c.get("valid") is False:
        print("  Abstract's KQL check: refused")
        for v in c.get("violations") or []:
            print(f"    • {S(v.get('reason') or '')}" + (f"  ({S(v['suggestion'])})" if v.get("suggestion") else ""))
    elif c.get("valid"):
        print("  Abstract's KQL check: valid")
    for w in c.get("warnings") or []:
        print(f"    note: {S(w)}")
    words = {"filters": "Exact export", "kql": "Runs as KQL", "kql_result": "KQL result table", "refused": "Cannot run"}
    label = words.get(c["runs_as"], c["runs_as"])
    print(f"\n  {label}: {S(_without_label(c['reason'], label))}")
    for line in c.get("readable") or []:
        print(f"    • {S(line)}")
    srv = c.get("server") or {}
    if srv.get("refusal"):
        print(f"  Abstract's filter builder could not hold it: {S((srv['refusal'] or {}).get('message') or '')}")
    print()


def _without_label(reason, label):
    """The reason without the words the label already says ("exact export: …" under "Exact export")."""
    r = reason or ""
    if r.lower().startswith(label.lower()):
        r = r[len(label):].lstrip(" :;,")
    return r


def kql_cmd(connect, args):
    if not args or args[0] not in ("check", "preview", "save"):
        print(__doc__, file=sys.stderr)
        return 2
    sub, rest = args[0], args[1:]
    p = _p(f"kql {sub}", "KQL against the connected tenant")
    p.add_argument("query", help="the KQL query, in quotes (starting with events)")
    if sub != "check":
        _time(p)
    if sub == "preview":
        p.add_argument("--rows", type=int, default=25)
    if sub == "save":
        p.add_argument("--format", choices=("csv", "json"), default="csv")
        p.add_argument("--name", default="")
    a = p.parse_args(rest)
    try:
        s = connect(rest)
        if isinstance(s, int):
            return s
        if sub == "check":
            r = s.kql_check(a.query)
        elif sub == "preview":
            r = s.kql_preview(a.query, _time_spec(a), a.rows)
        else:
            r = s.kql_result_export(a.query, _time_spec(a), a.format, a.name)
    except (E.UserError, E.Fatal, E.Retryable, OSError, ValueError) as ex:
        return _err(ex)
    if a.json:
        print(json.dumps(r, indent=2, default=str))
        return 0 if r.get("runs_as") != "refused" else 1
    if sub == "check":
        _print_check(r)
        return 0 if r["runs_as"] != "refused" else 1
    if sub == "save":
        print(f"\n  saved {r['rows']:,} rows to {S(r['file'])}"
              + (f"  (the query had {r['total']:,}; only the first 10,000 are saved)" if r["truncated"] else "") + "\n")
        return 0
    _table(r)
    return 0


def _table(r):
    n = r.get("total")
    print(f"\n  {S(r['reason'])}")
    print(f"  {('' if n is None else f'{n:,} ')}rows  {r['window'][0][:19]} → {r['window'][1][:19]} UTC  "
          f"(first {min(len(r['rows']), 25)} shown)\n")
    cols = r["columns"][:6]
    flats = [C.flatten(x) for x in r["rows"][:25]]
    ws = [min(30, max([len(c)] + [len(S(C.cell(f.get(c)))) for f in flats])) for c in cols]
    print("  " + "  ".join(S(c)[:w].ljust(w) for c, w in zip(cols, ws)))
    for f in flats:
        print("  " + "  ".join(S(C.cell(f.get(c)))[:w].ljust(w) for c, w in zip(cols, ws)))
    print()


def nl_cmd(connect, args):
    p = _p("nl", "Plain English to KQL with Abstract's assistant. It writes the query; nothing runs unless you add "
                 "--preview.")
    p.add_argument("text", nargs="+", help="what you are looking for")
    p.add_argument("--preview", action="store_true", help="also run the query it wrote and show the first rows")
    _time(p)
    a = p.parse_args(args)
    try:
        s = connect(args)
        if isinstance(s, int):
            return s
        r = s.nl_to_kql(" ".join(a.text))
        prev = s.kql_preview(r["kql"], _time_spec(a), 25) if (a.preview and r["kql"] and r["check"] and
                                                               r["check"]["runs_as"] != "refused") else None
    except (E.UserError, E.Fatal, E.Retryable, OSError, ValueError) as ex:
        return _err(ex)
    if a.json:
        print(json.dumps(dict(r, preview=prev), indent=2, default=str))
        return 0 if r["kql"] else 1
    if not r["kql"]:
        print(f"\n  The assistant did not write a query: {S(r['explanation'])}\n")
        return 1
    print(f"\n  Written by Abstract's assistant. Read it before you run it.\n  {S(r['explanation'])}")
    _print_check(r["check"])
    if prev:
        _table(prev)
    elif r["check"]["runs_as"] != "refused":
        print("  run it with:  kql preview \"<the query above>\"   or export it:  export --kql \"<the query>\"\n")
    return 0


def _find(items, key):
    key = (key or "").strip()
    hit = [x for x in items if x.get("id") == key] or [x for x in items if (x.get("name") or "").casefold() == key.casefold()]
    if not hit:
        raise E.UserError(f"nothing called {key!r} (check the list)")
    if len(hit) > 1:
        raise E.UserError(f"{len(hit)} items are called {key!r}; use the id instead: " + ", ".join(x["id"] for x in hit))
    return hit[0]


def _csv(v):
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _show_plan(plan, yes, s):
    print(f"\n  {plan['op'].capitalize()} {plan['kind']}: {S(plan.get('name') or '')}   ({plan['request']})")
    if plan.get("visibility"):
        print(f"  {S(plan['visibility'])}")
    for c in plan["changes"]:
        if "added" in c:
            bits = []
            if c["added"]:
                bits.append("adds " + ", ".join(c["added"]))
            if c["removed"]:
                bits.append("removes " + ", ".join(c["removed"]))
            if c.get("reordered"):
                bits.append("changes the order")
            print(f"    {c['key']}: " + "; ".join(bits))
        else:
            print(f"    {c['key']}: {S(json.dumps(c.get('before'), default=str))[:120]} → "
                  f"{S(json.dumps(c.get('after'), default=str))[:200]}")
    for w in plan.get("warnings") or []:
        print(f"    note: {S(w)}")
    if not yes:
        print("\n  Nothing was changed. Run the same command with --yes to make this change.\n")
        return NEEDS_YES
    r = s.apply_plan(plan["token"])
    print(f"\n  Done: {r['op']}d {r['kind']} {S(str(r.get('id') or ''))}\n")
    return 0


def fieldsets_cmd(connect, args):
    sub = args[0] if args and not args[0].startswith("-") else "list"
    rest = args[1:] if args and not args[0].startswith("-") else args
    p = _p(f"fieldsets {sub}", "Abstract fieldsets: the named column lists saved views use")
    if sub in ("show", "update", "delete"):
        p.add_argument("target", help="the fieldset's id or name")
    if sub in ("create", "update"):
        p.add_argument("--name")
        p.add_argument("--fields", help="the whole field list, in order, comma-separated")
        p.add_argument("--description")
        p.add_argument("--share", choices=("private", "public"))
    if sub == "create":
        p.add_argument("--from-dataset", help="start from the fields this dataset fills most often (learned live)")
    if sub == "update":
        p.add_argument("--add", help="fields to add at the end")
        p.add_argument("--remove", help="fields to take out")
    if sub in ("create", "update", "delete"):
        p.add_argument("--yes", action="store_true", help="make the change (without it, only show what would change)")
    a = p.parse_args(rest)
    try:
        s = connect(rest)
        if isinstance(s, int):
            return s
        if sub == "list":
            fs = s.fieldsets()
            if a.json:
                print(json.dumps(fs, indent=2))
                return 0
            print(f"\n  {len(fs)} fieldsets on {S(s.tenant['name'])}\n")
            for f in fs:
                print(f"  {S(f['name'])}  ({S(f['id'])}, {f.get('share_level')}, {len(f['fields'])} fields)")
                print(f"      {S(', '.join(f['fields'][:12]))}" + (" …" if len(f["fields"]) > 12 else ""))
            print()
            return 0
        if sub == "show":
            f = s.fieldset(_find(s.fieldsets(), a.target)["id"])
            print(json.dumps(f, indent=2, default=str))
            return 0
        if sub == "create":
            fields = _csv(a.fields)
            if a.from_dataset and not fields:
                ds = E.check_dataset_name(s.client, a.from_dataset)
                fields = [f for f in s.dataset_fields(ds, refresh=True)["suggested"] if f != "id"]
            data = {"name": a.name, "fields": fields, "description": a.description, "share_level": a.share or "private"}
            plan = s.fieldset_plan("create", data)
        elif sub == "update":
            cur = s.fieldset(_find(s.fieldsets(), a.target)["id"])
            fields = _csv(a.fields) or list(cur.get("fields") or [])
            fields += [f for f in _csv(a.add) if f not in fields]
            fields = [f for f in fields if f not in set(_csv(a.remove))]
            data = {"name": a.name, "description": a.description, "share_level": a.share, "fields": fields}
            plan = s.fieldset_plan("update", data, cur["id"])
        else:
            plan = s.fieldset_plan("delete", None, _find(s.fieldsets(), a.target)["id"])
        if a.json and not a.yes:
            print(json.dumps(plan, indent=2, default=str))
            return NEEDS_YES
        return _show_plan(plan, a.yes, s)
    except (E.UserError, E.Fatal, E.Retryable, OSError, ValueError) as ex:
        return _err(ex)


def views_change_cmd(connect, spec_from_args, search_parser, args):
    """views save | update | delete (views with no subcommand lists them, in abstract_search.py)."""
    sub, rest = args[0], args[1:]
    p = search_parser(f"views {sub}")
    if sub in ("update", "delete"):
        p.add_argument("target", help="the saved view's id or name")
    if sub in ("save", "update"):
        p.add_argument("--name")
        p.add_argument("--fieldset", help="a fieldset (id or name) for the view's columns")
    p.add_argument("--yes", action="store_true", help="make the change (without it, only show what would change)")
    a = p.parse_args(rest)
    try:
        s = connect(rest)
        if isinstance(s, int):
            return s
        fsid = None
        if getattr(a, "fieldset", None):
            fsid = [_find(s.fieldsets(), a.fieldset)["id"]]
        if sub == "delete":
            plan = s.view_plan("delete", id_=_find(s.views(refresh=True), a.target)["id"])
        else:
            has_search = any(getattr(a, k, None) for k in ("dataset", "where", "from_view", "from_file", "from_saved",
                                                            "kql", "kql_file"))
            spec = spec_from_args(s, a) if has_search else None
            if sub == "save":
                if spec is None:
                    raise E.UserError("say what the view searches: --dataset/--where, --kql, --from-file or --from-saved")
                plan = s.view_plan("create", spec, name=a.name, fieldset_ids=fsid)
            else:
                plan = s.view_plan("update", spec, id_=_find(s.views(refresh=True), a.target)["id"], name=a.name,
                                   fieldset_ids=fsid)
        if a.json and not a.yes:
            print(json.dumps(plan, indent=2, default=str))
            return NEEDS_YES
        return _show_plan(plan, a.yes, s)
    except (E.UserError, E.Fatal, E.Retryable, OSError, ValueError) as ex:
        return _err(ex)


def endpoints_cmd(args):
    import abx_platform as P
    rows = P.endpoints()
    if "--json" in args:
        print(json.dumps(rows, indent=2))
        return 0
    print("\n  Platform endpoints this tool uses (status = what this run of the tool saw)\n")
    for r in rows:
        print(f"  {r['method'].ljust(6)} {r['path'].ljust(40)} {r['status'].ljust(8)} "
              f"{'optional' if r['optional'] else 'required'}  {r['use']}")
    print()
    return 0


def _csv_or_none(v):
    return _csv(v) if v is not None else None


def _show_bundle_plan(plan, yes, s):
    t = plan.get("tenant") or {}
    src = plan.get("source") or {}
    print(f"\n  Apply bundle to {S(t.get('name') or '')} ({S(t.get('id') or '')})"
          + (f", exported from {S(src.get('tenant_name') or '')} ({S(src.get('tenant_id') or '')})" if src else ""))
    print(f"  {plan['counts']['create']} to create, {plan['counts']['update']} to update, "
          f"{plan['counts']['delete']} to delete" + ("   (prune is on)" if plan.get("prune") else ""))
    for w in plan.get("warnings") or []:
        print(f"  {'!!' if w.startswith('PRUNE') else 'note:'} {S(w)}")
    for it in plan["items"]:
        keys = ", ".join(c["key"] for c in it.get("changes") or [])
        print(f"    {it['op'].upper().ljust(6)} {it['kind'].ljust(8)} {S(it['name'])}" + (f"   ({keys})" if
                                                                                         it["op"] == "update" else "")
              + (f"   · {S(it['visibility'])}" if it.get("visibility") else ""))
    for sk in plan.get("skipped") or []:
        print(f"    SKIP   {sk['kind'].ljust(8)} {S(sk['name'])}   {S(sk['reason'])}")
    if not plan["items"]:
        print("\n  Nothing to change: the tenant already matches the bundle.\n")
        return 0
    if not yes:
        print("\n  Nothing was changed. Run the same command with --yes to make these changes.\n")
        return NEEDS_YES
    r = s.apply_plan(plan["token"])
    print(f"\n  Done: {len(r['done'])} change(s) made.")
    if r.get("failed"):
        f = r["failed"]
        print(f"  STOPPED at {f['op']} {f['kind']} {S(f['name'])}: {S(f['error'])}")
        print(f"  Not attempted: {len(r['not_attempted'])}. Fix the problem and run the same command again: what "
              "is already done shows as the same and is skipped.\n")
        return 2
    print()
    return 0


def content_cmd(connect, args):
    import abx_content as CT
    sub = args[0] if args and not args[0].startswith("-") else ""
    rest = args[1:]
    if sub not in ("export", "diff", "apply"):
        print("usage: content export [DIR] [--views a,b] [--fieldsets a,b] | content diff DIR | "
              "content apply DIR [--prune] [--yes]", file=sys.stderr)
        return 2
    p = _p(f"content {sub}", "Saved views and fieldsets as code")
    p.add_argument("dir", nargs="?" if sub == "export" else None,
                   help="the bundle folder (or a single bundle .json file)")
    if sub == "export":
        p.add_argument("--views", help="only these saved views (names or ids, comma-separated); default every view")
        p.add_argument("--fieldsets", help="only these fieldsets; default every fieldset (or, with --views, the "
                                           "ones those views use)")
        p.add_argument("--kind", choices=("views", "fieldsets"), help="only saved views (plus the fieldsets they "
                                                                      "use) or only fieldsets")
    if sub == "apply":
        p.add_argument("--prune", action="store_true", help="also DELETE views and fieldsets that are not in the "
                                                             "bundle")
        p.add_argument("--yes", action="store_true", help="make the changes (without it, only show them)")
    a = p.parse_args(rest)
    try:
        s = connect(rest)
        if isinstance(s, int):
            return s
        if sub == "export":
            views = _csv_or_none(a.views)
            fsets = _csv_or_none(a.fieldsets)
            if views is not None and fsets is None and not a.kind:
                fsets = []
            r = s.content_export(a.dir, views, fsets, a.kind)
            if a.json:
                print(json.dumps(r, indent=2))
                return 0
            print(f"\n  wrote {r['counts']['views']} saved views and {r['counts']['fieldsets']} fieldsets to "
                  f"{S(r['dir'])}")
            for pr in r.get("problems") or []:
                print(f"  note: {S(pr)}")
            print("  compare later with:  content diff \"<folder>\"   apply with:  content apply \"<folder>\"\n")
            return 0
        doc = CT.read_folder(a.dir)
        if sub == "diff":
            rows = s.content_diff(doc)
            if a.json:
                print(json.dumps({"diff": rows, "notes": doc.get("notes") or []}, indent=2))
                return 0
            for n in doc.get("notes") or []:
                print(f"  note: {S(n)}")
            print()
            for r in rows:
                print(f"  {r['status'].ljust(15)} {r['kind'].ljust(8)} {S(r['name'])}"
                      + (f"   ({', '.join(r['changes'])})" if r.get("changes") else "")
                      + (f"   {S(r['reason'])}" if r.get("reason") else ""))
            same = sum(1 for r in rows if r["status"] == "same")
            compared = sum(1 for r in rows if r["status"] != "not_compared")
            print(f"\n  {same} of {compared} compared items match.\n")
            return 0
        plan = s.content_plan(doc, a.prune)
        if a.json and not a.yes:
            print(json.dumps(plan, indent=2, default=str))
            return NEEDS_YES if plan["items"] else 0
        return _show_bundle_plan(plan, a.yes, s)
    except (E.UserError, E.Fatal, E.Retryable, OSError, ValueError) as ex:
        return _err(ex)


def automate_cmd(connect, spec_from_args, search_parser, args):
    import abx_automate as AU
    import abx_engine as EN
    target = args[0] if args and not args[0].startswith("-") else ""
    rest = args[1:]
    if target not in AU.TARGETS:
        print("usage: automate TARGET (--from-saved NAME | <search>) [--every 15m|1h|6h|1d] [--at HH:MM] "
              "[--out DIR]\n  TARGET is one of: " + ", ".join(AU.TARGETS), file=sys.stderr)
        return 2
    p = search_parser(f"automate {target}")
    p.add_argument("--every", choices=sorted(AU.EVERY), default="1d", help="how often it runs (default 1d)")
    p.add_argument("--at", default="00:20", help="for a daily run, the UTC time it starts (default 00:20)")
    p.add_argument("--out", help="write the files into this folder (default: print them)")
    p.add_argument("--job", help="a short name for the job (default: the search's name)")
    p.add_argument("--vendor-account", help="the tenant the job runs against (default: the one you are connected "
                                            "to, or ABSTRACT_VENDOR_ACCOUNT_ID)")
    p.add_argument("--app", help="where abstract_search.py is on the computer that runs the job")
    a = p.parse_args(rest)
    try:
        vid = a.vendor_account or os.environ.get("ABSTRACT_VENDOR_ACCOUNT_ID")
        needs_tenant = any(getattr(a, k, None) for k in ("dataset", "where", "from_view", "kql", "kql_file",
                                                         "from_file"))
        if a.from_saved and not needs_tenant:
            spec = C.saved_load(a.from_saved)
        else:
            s = connect(rest)
            if isinstance(s, int):
                return s
            spec = spec_from_args(s, a)
            vid = vid or s.tenant["vendor_account_id"]
        r = AU.recipe(spec, target, vid, os.environ.get("ABSTRACT_BASE_URL"), a.every, a.at, a.job, a.app,
                      os.path.abspath(a.out) if a.out else None)
    except (E.UserError, EN.Fatal, EN.Retryable, OSError, ValueError) as ex:
        return _err(ex)
    if a.out:
        paths = AU.write(r, a.out)
        if a.json:
            print(json.dumps({"files": paths, "steps": r["steps"], "notes": r["notes"]}, indent=2))
            return 0
        print(f"\n  wrote {len(paths)} files to {S(os.path.abspath(a.out))}\n")
    elif a.json:
        print(json.dumps(r, indent=2))
        return 0
    else:
        for f in r["files"]:
            print(f"\n===== {f['path']} =====\n{f['text']}", end="")
    print("\n  Next:")
    for i, st in enumerate(r["steps"], 1):
        print(f"    {i}. {S(st)}")
    for n in r["notes"]:
        print(f"  note: {S(n)}")
    print()
    return 0


def _err(ex):
    msg = C.humanize(ex)
    path = E.log_error("ERROR", msg, None)
    print(f"ERROR: {msg}", file=sys.stderr)
    print(f"  (kept in the log: {path})", file=sys.stderr)
    return 2
