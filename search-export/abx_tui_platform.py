"""
abx_tui_platform — the guided terminal's screens for KQL, plain English, fieldsets and changes to saved
views. Every screen calls the same abx_core session the browser app and the command line use. A change to the
tenant is always shown first, exactly, and made only after a yes.
"""
from __future__ import annotations

import json
import os
from typing import Any, List, Optional, Tuple

import abx_core as C
import abx_ui as U
import abx_wizard as W

S = C.sanitize
ERRORS = W.ERRORS
RUNS = {"filters": ("Exact export", U.teal), "kql": ("Runs as KQL", U.blue),
        "kql_result": ("KQL result table", U.amber), "refused": ("Cannot run", U.pink)}


def choose(title, items, back=True, hint=None, default=0):
    try:
        return U.select(title, items, back=back, hint=hint, default=default)
    except (U.Back, U.Home):
        return None


def show_check(c):
    print("\n  " + U.bold("KQL") + "  " + S(c["kql"]))
    if c.get("valid") is False:
        for v in c.get("violations") or []:
            U.err(S(v.get("reason") or "") + (f"  ({S(v['suggestion'])})" if v.get("suggestion") else ""))
    for w in c.get("warnings") or []:
        U.note(S(w))
    label, colour = RUNS.get(c["runs_as"], (c["runs_as"], str))
    reason = c["reason"] or ""
    if reason.lower().startswith(label.lower()):
        reason = reason[len(label):].lstrip(" :;,")
    print("  " + colour(label) + "  " + U.dim(S(reason)))
    for line in c.get("readable") or []:
        print("    " + U.teal("•") + " " + S(line))


def spec_from_check(c, base=None):
    """The current search, from a checked KQL query (None when it cannot become one)."""
    sp = dict(base or C.empty_spec())
    if c["runs_as"] == "filters":
        sp.update(mode="simple", dataset=c["dataset"], filters=c["filters"], any_groups=c["any_groups"], kql="")
    elif c["runs_as"] == "kql":
        sp.update(mode="kql", kql=c["kql"], dataset="", filters=[], any_groups=[])
    else:
        return None
    return C.normalize_spec(sp)


def result_table(s, kql, spec):
    print("  " + U.dim("running it on Abstract…"))
    r = s.kql_preview(kql, (spec or {}).get("time") or {"last": "1h"}, 25)
    print()
    U.table(r["rows"], r["columns"][:8], C.flatten, C.cell, max_rows=25)
    n = r.get("total")
    U.info(f"{'' if n is None else f'{n:,} '}rows, {r['window'][0][:19]} → {r['window'][1][:19]} UTC")
    if U.confirm("Save this result table to a file?", False):
        fmt = choose("Format", [("csv", "CSV"), ("json", "JSON")]) or "csv"
        out = s.kql_result_export(kql, (spec or {}).get("time") or {"last": "1h"}, fmt, "kql-result")
        U.ok(f"saved {out['rows']:,} rows to {S(out['file'])}"
             + ("  (only the first 10,000)" if out["truncated"] else ""))


def after_check(menu, c):
    """What to do with a checked query: use it as the search, see its result table, or leave it."""
    acts: List[Tuple[Any, str]] = []
    if c["runs_as"] in ("filters", "kql"):
        acts.append(("use", "Use it as the current search"))
    if c["runs_as"] != "refused":
        acts.append(("table", "Run it and show the rows" if c["runs_as"] != "kql_result" else "Show its result table"))
    acts.append(("edit", "Edit the KQL"))
    k = choose("What now?", acts)
    if k == "use":
        sp = spec_from_check(c, menu.spec)
        if sp is not None:
            menu.spec = sp
            U.ok("Loaded. Next: Preview, Validate, then Export.")
            U.pause()
        return None
    if k == "table":
        result_table(menu.s, c["kql"], menu.spec)
        U.pause()
        return None
    if k == "edit":
        return c["kql"]
    return None


def do_kql(menu, start=""):
    menu.s.require()
    text = start
    while True:
        print("\n  " + U.bold("Write a KQL query") + U.dim("  (one line; starts with events. Type < to go back.)"))
        print("  " + U.dim('e.g. events | where data_stream.dataset =~ "aws waf" and action == "BLOCK"'))
        text = U.ask("KQL", text or None)
        if not (text or "").strip():
            return
        c = menu.s.kql_check(text)
        show_check(c)
        nxt = after_check(menu, c)
        if nxt is None:
            return
        text = nxt


def do_english(menu):
    menu.s.require()
    print("\n  " + U.bold("Describe what you want") + U.dim("  Abstract's assistant writes the KQL; you read it "
                                                           "before anything runs. Type < to go back."))
    print("  " + U.dim("e.g. failed logins from outside the US in the last day"))
    text = U.ask("In plain English")
    if not (text or "").strip():
        return
    print("  " + U.dim("asking Abstract's assistant…"))
    r = menu.s.nl_to_kql(text)
    if not r["kql"]:
        U.note("The assistant did not write a query: " + S(r["explanation"]))
        U.pause()
        return
    print("\n  " + U.dim("Written by Abstract's assistant. Check it says what you meant."))
    print("  " + S(r["explanation"]))
    show_check(r["check"])
    nxt = after_check(menu, r["check"])
    if nxt is not None:
        do_kql(menu, nxt)


def confirm_plan(menu, plan) -> Optional[dict]:
    """Show exactly what will change in Abstract, then make the change only after a yes."""
    print("\n  " + U.bold(f"{plan['op'].capitalize()} {plan['kind']}") + "  " + S(plan.get("name") or "") +
          U.dim(f"   {plan['request']}"))
    if plan.get("visibility"):
        print("  " + U.amber(S(plan["visibility"])))
    for c in plan["changes"]:
        if "added" in c:
            bits = (["adds " + ", ".join(c["added"])] if c["added"] else []) + \
                   (["removes " + ", ".join(c["removed"])] if c["removed"] else []) + \
                   (["changes the order"] if c.get("reordered") else [])
            print("    " + U.teal(c["key"]) + ": " + S("; ".join(bits)))
        else:
            print("    " + U.teal(c["key"]) + ": " + U.dim(S(json.dumps(c.get("before"), default=str))[:100]) +
                  " → " + S(json.dumps(c.get("after"), default=str))[:160])
    for w in plan.get("warnings") or []:
        U.note(S(w))
    if not U.confirm(f"Make this change in Abstract ({S((menu.s.tenant or {}).get('name') or '')})?", False):
        U.info("Nothing was changed.")
        return None
    r = menu.s.apply_plan(plan["token"])
    U.ok(f"Done: {r['op']}d {r['kind']} {S(str(r.get('id') or ''))}")
    return r


def _pick_fields(menu, start):
    """Build a field list: from a dataset's most-used fields, from the catalogue, or typed (ext.* allowed)."""
    fields = list(start)
    while True:
        items: List[Tuple[Any, str]] = [(("rm", i), f"{i + 1}. {S(f)}" + U.dim("   (select to remove)"))
                                        for i, f in enumerate(fields)]
        items += [("ds", U.teal("+ Add the fields a dataset fills most often")),
                  ("type", U.teal("+ Add fields by name (ext.* too)")),
                  ("up", "Move a field up"), ("done", U.pink("Done"))]
        k = choose(f"Fields ({len(fields)})", items, hint="the order here is the column order in Abstract")
        if k is None:
            return None
        if k == "done":
            return fields
        if k == "ds":
            ds = W.choose_dataset(menu.s, menu.spec.get("dataset"))
            if ds:
                prof = menu.s.dataset_fields(ds, refresh=True)
                for f in prof.get("fields") or []:
                    print(U.clip(f"    {S(f['name']).ljust(40)} {f['coverage']:5.1f}%"))
                fields += [f for f in prof.get("suggested") or [] if f != "id" and f not in fields]
        elif k == "type":
            for f in (U.ask("Field names, comma-separated") or "").split(","):
                f = f.strip()
                if f and f not in fields:
                    fields.append(f)
        elif k == "up":
            i = choose("Which field moves up?", [(i, S(f)) for i, f in enumerate(fields) if i > 0])
            if i:
                fields[i - 1], fields[i] = fields[i], fields[i - 1]
        elif isinstance(k, tuple):
            fields.pop(k[1])


def do_fieldsets(menu):
    menu.s.require()
    while True:
        print("  " + U.dim("loading fieldsets…"))
        fs = menu.s.fieldsets()
        items: List[Tuple[Any, str]] = [("new", U.teal("+ Build a new fieldset"))]
        items += [(f["id"], S(f["name"]) + U.dim(f"  ({len(f['fields'])} fields, {f.get('share_level')})"))
                  for f in fs]
        k = choose(f"Fieldsets in Abstract ({len(fs)})", items, hint="a fieldset is a named list of columns that "
                                                                      "saved views use")
        if k is None:
            return
        try:
            if k == "new":
                name = U.ask("Name")
                fields = _pick_fields(menu, ["@timestamp"])
                if not fields:
                    continue
                confirm_plan(menu, menu.s.fieldset_plan("create", {"name": name, "fields": fields}))
            else:
                f = menu.s.fieldset(k)
                print("\n  " + U.bold(S(f["name"])) + U.dim(f"  made by {S(f.get('created_by') or 'unknown')}"))
                print("  " + S(", ".join(f.get("fields") or [])))
                a = choose("What now?", [("fields", "Change its fields"), ("rename", "Rename it"),
                                         ("use", "Use these fields for the current search"),
                                         ("delete", U.red("Delete it"))])
                if a == "fields":
                    new = _pick_fields(menu, list(f.get("fields") or []))
                    if new is not None:
                        confirm_plan(menu, menu.s.fieldset_plan("update", {"fields": new}, k))
                elif a == "rename":
                    confirm_plan(menu, menu.s.fieldset_plan("update", {"name": U.ask("New name", f["name"])}, k))
                elif a == "use":
                    menu.spec["fields"] = list(dict.fromkeys(["@timestamp", "id"] + list(f.get("fields") or [])))
                    menu.spec["full_documents"] = False
                    U.ok("The current search now exports these fields.")
                elif a == "delete":
                    confirm_plan(menu, menu.s.fieldset_plan("delete", None, k))
        except ERRORS as ex:
            W.problem(ex)
        U.pause()


def view_actions(menu, views):
    """Save the current search as a view, or change or delete one of the tenant's views."""
    k = choose("Change saved views in Abstract", [("save", "Save the current search as a new view"),
                                                  ("update", "Replace a view's search with the current one"),
                                                  ("rename", "Rename a view"), ("delete", U.red("Delete a view"))])
    if k is None:
        return
    try:
        if k == "save":
            probs = C.spec_problems(menu.spec)
            if probs:
                U.err(S(probs[0]))
                return
            name = U.ask("Name of the view", menu.spec.get("name") or None)
            confirm_plan(menu, menu.s.view_plan("create", menu.spec, name=name))
            return
        v = choose("Which view?", [(x["id"], S(x["name"])) for x in views])
        if v is None:
            return
        if k == "update":
            confirm_plan(menu, menu.s.view_plan("update", menu.spec, v))
        elif k == "rename":
            cur = next((x for x in views if x["id"] == v), {})
            confirm_plan(menu, menu.s.view_plan("update", None, v, name=U.ask("New name", cur.get("name"))))
        else:
            confirm_plan(menu, menu.s.view_plan("delete", id_=v))
    except ERRORS as ex:
        W.problem(ex)


def check_for_wizard(s, how):
    """Guided search, step 2: a plain English description or a KQL query, checked, shown and agreed. Returns the
    check of a query that can run as an export (filters or KQL), or None to go back."""
    text = ""
    while True:
        if how == "english":
            print("  " + U.dim("Abstract's assistant writes the KQL; you read it before anything runs. e.g. failed "
                              "logins from outside the US"))
            words = U.ask("Describe what you want")
            if not (words or "").strip():
                return None
            print("  " + U.dim("asking Abstract's assistant…"))
            r = s.nl_to_kql(words)
            if not r["kql"]:
                U.note("The assistant did not write a query: " + S(r["explanation"]))
                continue
            print("  " + S(r["explanation"]))
            c = r["check"]
        else:
            print("  " + U.dim('One line, starting with events. e.g. events | where data_stream.dataset =~ "okta" '
                              'and event.outcome == "failure"'))
            text = U.ask("KQL", text or None)
            if not (text or "").strip():
                return None
            c = s.kql_check(text)
        show_check(c)
        if c["runs_as"] in ("filters", "kql"):
            if U.confirm("Use this search?", True):
                return c
        elif c["runs_as"] == "kql_result":
            U.note("This query reshapes the rows (a summary or computed columns), so it is not an export of events. "
                   "Use main menu → Write or paste a KQL query to see and save its result table.")
        how, text = "kql", c["kql"]


# ── views and fieldsets as code ──────────────────────────────────────────────────────────────────────────────────
def do_content(menu):
    """Export, compare and apply bundles of saved views and fieldsets."""
    import abx_content as CT
    menu.s.require()
    k = choose("Views and fieldsets as code", [
        ("export", "Export this tenant's saved views and fieldsets to a folder"),
        ("diff", "Compare a bundle folder with this tenant"),
        ("apply", "Apply a bundle folder to this tenant (one review, then you confirm)"),
        ("prune", U.red("Apply a bundle and DELETE what is not in it"))],
        hint="a bundle is one file per view and fieldset, with no ids: keep it in git, restore it, or apply it to "
             "another tenant")
    if k is None:
        return
    try:
        if k == "export":
            d = U.ask("Folder", CT.default_dir(menu.s))
            print("  " + U.dim("reading every saved view and fieldset…"))
            r = menu.s.content_export(d)
            U.ok(f"wrote {r['counts']['views']} saved views and {r['counts']['fieldsets']} fieldsets to {S(r['dir'])}")
            for p in r.get("problems") or []:
                U.note(S(p))
            return
        doc = CT.read_folder(U.ask("Bundle folder (or a bundle .json file)"))
        for n in doc.get("notes") or []:
            U.note(S(n))
        if k == "diff":
            rows = menu.s.content_diff(doc)
            colour = {"same": U.dim, "changed": U.amber, "new": U.teal, "only_in_tenant": U.blue,
                      "ambiguous": U.pink}
            lines = [colour.get(r["status"], str)(r["status"].ljust(15)) + f" {r['kind'].ljust(8)} {S(r['name'])}"
                     + (U.dim("   " + ", ".join(r["changes"])) if r.get("changes") else "") for r in rows]
            same = sum(1 for r in rows if r["status"] == "same")
            U.page("\n".join(["  " + x for x in lines] + ["", f"  {same} of {len(rows)} match."]))
            return
        plan = menu.s.content_plan(doc, prune=(k == "prune"))
        confirm_bundle(menu, plan)
    except ERRORS as ex:
        W.problem(ex)


def confirm_bundle(menu, plan):
    c = plan["counts"]
    print("\n  " + U.bold(f"Apply bundle: {c['create']} create, {c['update']} update, {c['delete']} delete")
          + (U.red("   prune is on") if plan.get("prune") else ""))
    for w in plan.get("warnings") or []:
        (U.err if w.startswith("PRUNE") else U.note)(S(w))
    for it in plan["items"]:
        col = {"create": U.teal, "update": U.amber, "delete": U.pink}[it["op"]]
        keys = ", ".join(x["key"] for x in it.get("changes") or []) if it["op"] == "update" else ""
        print(U.clip("    " + col(it["op"].ljust(6)) + f" {it['kind'].ljust(8)} {S(it['name'])}"
                     + (U.dim("   " + keys) if keys else "")
                     + (U.dim("   · " + S(it["visibility"])) if it.get("visibility") else "")))
    for sk in plan.get("skipped") or []:
        print(U.clip("    " + U.dim("skip  ") + f" {sk['kind'].ljust(8)} {S(sk['name'])}  " + U.dim(S(sk["reason"]))))
    if not plan["items"]:
        U.ok("Nothing to change: the tenant already matches the bundle.")
        return None
    if not U.confirm(f"Make these {len(plan['items'])} change(s) in "
                     f"{S((menu.s.tenant or {}).get('name') or '')}?", False):
        U.info("Nothing was changed.")
        return None
    r = menu.s.apply_plan(plan["token"])
    U.ok(f"{len(r['done'])} change(s) made.")
    if r.get("failed"):
        f = r["failed"]
        U.err(f"Stopped at {f['op']} {f['kind']} {S(f['name'])}: {S(f['error'])}")
        U.note(f"{len(r['not_attempted'])} not attempted. Fix the problem and apply again: what is done shows as the "
               "same and is skipped.")
    return r


# ── automation recipes ───────────────────────────────────────────────────────────────────────────────────────────
def do_automate(menu, spec=None):
    """Write the files that run the current search (or a saved one) on a schedule."""
    import abx_automate as AU
    spec = spec or menu.spec
    probs = C.spec_problems(dict(spec, time={"last": "1h"}))
    if probs:
        U.err(S(probs[0]))
        return
    t = choose("Where should it run?", [
        ("cron", "Linux or Unix with cron"), ("launchd", "macOS (launchd)"), ("systemd", "Linux with systemd timers"),
        ("windows-task", "Windows Task Scheduler"), ("github-actions", "GitHub Actions"),
        ("container-cron", "A container: Docker or a Kubernetes CronJob")],
        hint="every recipe takes the key from a keychain or secret store, never from a file")
    if t is None:
        return
    every = choose("How often?", [("15m", "Every 15 minutes"), ("1h", "Every hour"), ("6h", "Every 6 hours"),
                                  ("1d", "Once a day")], default=3) or "1d"
    at = U.ask("Start time for a daily run (UTC, HH:MM)", "00:20") if every == "1d" else "00:20"
    vid = (menu.s.tenant or {}).get("vendor_account_id") or U.ask("Vendor account ID the job runs against")
    try:
        r = AU.recipe(spec, t, vid, (menu.s.tenant or {}).get("base_url"), every, at)
        d = U.ask("Folder for the files", os.path.join(C.home_dir(), "automation", f"{r['job']}-{t}"))
        r = AU.recipe(spec, t, vid, (menu.s.tenant or {}).get("base_url"), every, at, folder=os.path.abspath(d))
        paths = AU.write(r, d)
    except ERRORS as ex:
        W.problem(ex)
        return
    U.ok(f"wrote {len(paths)} files to {S(os.path.abspath(d))}")
    lines = ["  " + U.bold("Next")] + [f"    {i}. {S(s)}" for i, s in enumerate(r["steps"], 1)] + [""] + \
        ["  " + U.dim("note: " + S(n)) for n in r["notes"]]
    U.page("\n".join(lines))
