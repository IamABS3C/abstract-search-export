"""
abx_wizard — the guided, step-by-step search & export for the terminal.

Connect → Dataset → Filters → Time → Fields → Review → Export. Every step shows where you are, what it is
for and what happens next. ← / Esc / b goes back a step, m opens the main menu, q quits. Everything runs
through the same abx_core session as the menu and the browser app.

The module also holds the pieces the main menu shares (error display, pickers, the progress watcher and the
result screen), so both front ends validate, word and recover the same way.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import json
import os
import textwrap
import threading
import datetime as dt
import time

import abx_core as C
import abx_engine as E
import abx_ui as U

STEPS = ["Connect", "Dataset", "Filters", "Time", "Fields", "Review", "Export"]

# Every action catches these and shows C.humanize(ex). (C.Busy and C.SavedExists are UserErrors.)
ERRORS = (E.UserError, E.Fatal, E.Retryable, E.Cancelled, OSError, ValueError)

S = C.sanitize

ADVANCED_MSG = ("This saved search was made for the command line. Run it there with "
                "python3 abstract_search.py export --from-saved \"<its name>\", or start a new search here.")

KQL_OFF_MSG = C.KQL_REMOVED_MSG


# ───────────────────────────── shared: messages ─────────────────────────────

def problem(ex):
    """Show any caught error in customer words (and keep it in today's log)."""
    if not isinstance(ex, (C.Busy, E.Cancelled)):
        E.log_error("ERROR", f"terminal: {C.humanize(ex)}", ex)
    if isinstance(ex, C.Busy):
        U.err(C.humanize(ex))
        U.say("Watch it or stop it from the main menu → Export.", indent=4, style=U.dim)
    elif isinstance(ex, (E.UserError, E.Fatal, E.Retryable, E.Cancelled)):
        U.err(C.humanize(ex))
    elif isinstance(ex, OSError) and not isinstance(ex, (ConnectionError, TimeoutError)):
        U.err("This computer could not read or write a file: " + C.humanize(ex))
    elif isinstance(ex, OSError):
        U.err("A network problem stopped this: " + C.humanize(ex) + ". Check your connection and try again.")
    else:
        U.err(C.humanize(ex))


def spec_lines(spec):
    d = C.describe_spec(spec)
    spec = C.normalize_spec(spec)
    mode = {"kql": "KQL" if C.KQL_ON else "KQL (not available on this tenant)",
            "advanced": "advanced (command line only)"}.get(spec["mode"], "dataset + filters")
    return [("Search", U.dim(mode)), ("Query", S(d["query"])), ("Time", S(d["window"])),
            ("Where", S(d["storage"])), ("Fields", S(d["fields"]))]


def print_spec(spec):
    for k, v in spec_lines(spec):
        print(U.clip(f"  {U.dim(k.ljust(7))} {v}"))


def save_search(spec):
    """Save the search; on a name clash ask before replacing. True when saved."""
    name = U.ask("name for this search (< back)", spec.get("name") or "")
    if not name.strip():
        U.note("Not saved: a saved search needs a name.")
        return False
    spec["name"] = name.strip()
    try:
        fn = C.saved_save(spec)
    except C.SavedExists:
        if not U.confirm(f"A saved search called '{S(spec['name'])}' already exists. Replace it?", False):
            U.note("Not saved. The existing search is unchanged.")
            return False
        fn = C.saved_save(spec, overwrite=True)
    U.ok("saved as " + S(fn) + U.dim("  in " + S(os.path.join(C.home_dir(), "saved-searches"))))
    return True


def unsaved_changes(spec):
    """UX-43: True when the search differs from a new one and is not what its saved file holds. Compared with
    the file on disk, so a save from the guided flow, the menu or the browser app all count."""
    key = _search_key(spec)
    if key == _search_key(C.empty_spec()):
        return False
    try:
        files = [x["file"] for x in C.saved_list()]
    except ERRORS:
        return True
    for f in files:                                              # a saved file's name need not match its "name"
        try:
            if _search_key(C.saved_load(f)) == key:
                return False
        except ERRORS:
            continue
    return True


def open_folder(path):
    """Open an export folder; on a headless machine the tool says where it is (information, not an error)."""
    try:
        p = C.open_folder(path)
        U.ok("opened " + S(p))
    except E.UserError as ex:                                    # C.Headless: no file manager here
        where = getattr(ex, "path", None)
        U.info(("The export is in: " + S(where)) if where else S(str(ex)))


# ───────────────────────────── shared: pickers ─────────────────────────────

def check_tenant(s, show=True):
    """Check what the connected tenant supports (live, read only, a few seconds) and print one line per item."""
    if show:
        print("  " + U.dim("checking what this tenant supports…"))
    try:
        caps = s.capabilities(refresh=True)
    except ERRORS as ex:
        if show:
            U.note("Could not check the tenant now (" + C.humanize(ex) + "). Searches are converted into filters.")
        return None
    if show:
        for it in caps["items"]:
            mark = U.teal("●") if it["status"] == "available" else (U.dim("○") if it["status"] == "not available"
                                                                    else U.amber("?"))
            print(f"    {mark} {it['label']}: {S(it['detail'])}")
    return caps


def capabilities_screen(s):
    """Main menu → Tenant: what this tenant supports, with a fresh check."""
    s.require()
    print()
    print("  " + U.bold("What this tenant supports") + U.dim("  (checked live, read only; nothing is stored)"))
    check_tenant(s)
    U.pause()


def import_from_abstract(s, spec):
    """Use a search from Abstract: one of the tenant's saved views (read live, never stored) or text pasted from
    Abstract's search box. It is converted to dataset + filters (never sent as KQL) and shown before it is used.
    Returns True when the search was replaced."""
    src = U.select("Use a search from Abstract", [("view", "One of this tenant's saved views"),
                                                  ("paste", "Paste a search copied from Abstract's search box")])
    try:
        if src == "view":
            print("  " + U.dim("loading this tenant's saved views…"))
            views = s.views(refresh=True)
            if not views:
                U.note("This tenant has no saved views.")
                U.pause()
                return False
            kinds = {"kql": "KQL", "filters": "filter builder", "replay": "replay", "none": "columns only",
                     "unreadable": "could not be read"}
            vid = U.select(f"Saved views ({len(views)})",
                           [(v["id"], S(v["name"]) + U.dim(f"  ({kinds.get(v.get('origin', 'kql'), 'KQL')})")) for v in views])
            r = s.import_search(view_id=vid)
        else:
            U.say("Paste the search on one line, for example:", style=U.dim)
            U.say('  events | where [\'data_stream.dataset\'] == "Okta" and action == "user.session.start"', style=U.dim)
            r = s.import_search(text=U.ask("search (< back)"))
    except C.Busy as ex:
        problem(ex)
        U.pause()
        return False
    except E.UserError as ex:
        problem(ex)
        U.pause()
        return False
    print()
    print("  " + U.bold("This will run as") + U.dim(f"  ({S(r['source'])})"))
    for line in r["readable"]:
        print("    " + U.teal("•") + " " + S(line))
    for n in r["notes"]:
        U.note(S(n))
    if not U.confirm("Use this search?", True):
        return False
    spec.clear()
    spec.update(r["spec"])
    U.ok("Loaded the view's columns and time range. Pick a dataset or add a filter in the next steps."
         if r.get("columns_only") else
         "Search converted. The next steps show it; Review previews and validates it before anything is exported.")
    if U.INTERACTIVE:
        time.sleep(0.8)
    return True


def choose_dataset(s, current: Optional[str] = ""):
    print("  " + U.dim("loading datasets…"))
    try:
        ds = s.datasets()
    except C.Busy as ex:                                         # no cached list yet and an export is running
        problem(ex)
        U.pause()
        raise U.Home()                                           # the main menu, where the export can be watched
    while True:
        q = U.ask("part of a dataset name (Enter for all, < back)", "")
        rows = [d for d in ds if q.casefold() in str(d.get("name", "")).casefold()]
        if rows:
            break
        U.note(f"no dataset matches '{S(q)}'")
    nw = max(12, min(44, max(U.vlen(S(d["name"])) for d in rows), U.cols() - 30))
    items = []
    for d in rows[:500]:
        n = d.get("events_24h")
        vol = "volume unknown" if n is None else ("no events in 24h" if n == 0 else f"{n:,} events/24h")
        if d.get("active") is False:
            vol += " · inactive"
        items.append((d["name"], U.pad(S(d["name"]), nw) + "  " + U.dim(vol)))
    items.append(("", U.dim("(no dataset: filters alone decide)")))
    cur = next((n for n, (k, _) in enumerate(items) if k and k == current), 0)
    hint = f"showing the first 500 of {len(rows)}; type more of the name to narrow" if len(rows) > 500 else None
    return U.select(f"{len(rows)} dataset(s)", items, default=cur, hint=hint)


def load_profile(s, dataset, say=True):
    """The dataset's field profile learned earlier in this session (never samples, never read from disk), or None."""
    if not dataset:
        return None
    p = s.dataset_fields(dataset)
    if p.get("sampled"):
        if say:
            src = "learned this session" if p.get("cached") else "learned now"
            when = str(p.get("profiled") or "")[:10]
            U.ok(f"{len(p.get('fields') or [])} fields known for {S(dataset)} ({src}{', ' + when if when else ''})")
        return p
    if say and p.get("none"):
        U.note(f"No field profile for {S(dataset)} yet. The Fields step can learn one now (10–30 seconds).")
    return None


def apply_dataset(s, spec, k):
    if k != spec.get("dataset") and (spec.get("filters") or spec.get("any_groups")):
        U.note(f"The {len(spec.get('filters') or []) + len(spec.get('any_groups') or [])} filter(s) built for the "
               "previous dataset were removed.")
        spec["filters"] = []
        spec["any_groups"] = []
    spec["dataset"], spec["mode"] = k, "simple"
    return load_profile(s, k)


def learn_profile(s, dataset):
    print("  " + U.dim(f"sampling recent {S(dataset)} events to learn its fields — this takes 10–30 seconds…"))
    p = s.dataset_fields(dataset, refresh=True)
    if not p.get("sampled"):
        U.note(f"No {S(dataset)} events in the last 30 days, so there is nothing to learn fields from.")
        return None
    U.ok(f"learned {len(p.get('fields') or [])} fields from {p['sampled']:,} sampled {S(dataset)} events")
    return p


def pick_field(s, prof, title):
    q = U.ask("field name or part of it (Enter lists them all)", "")
    cov = {f["name"]: f.get("coverage") for f in (prof or {}).get("fields") or []}
    names = [f["name"] for f in (prof or {}).get("fields") or []]
    hits = [n for n in names if q.lower() in n.lower()]
    if not hits:
        if prof:
            U.note("no field in this dataset's profile matches; searching every field on the tenant")
        hits = [n for n in s.fields() if q.lower() in n.lower()]
    if not hits:
        U.note(f"no field matches '{S(q)}'")
        return None
    hint = None
    if len(hits) > 200:
        hint = f"{len(hits)} fields match; showing the first 200. Type more of the name to narrow."
        hits = hits[:200]
    nw = max(12, min(40, max(U.vlen(S(n)) for n in hits), U.cols() - 20))
    items = [(n, U.pad(S(n), nw) + ("  " + U.dim(f"{cov[n]:5.1f}%") if cov.get(n) is not None else "")) for n in hits]
    return U.select(f"{title} ({len(hits)} match{'es' if len(hits) != 1 else ''})", items, hint=hint)


def _value_prompt(ftype, op, takes):
    if takes == "range":
        return "from, to (both ends included), e.g. 10, 100"
    if takes is True:
        what = {"Ipv4": "IPv4 addresses like 10.1.2.3", "Float64": "numbers"}.get(ftype, "values")
        return f"{what}, comma-separated"
    if op in ("IS_IN_SUBNET", "IS_NOT_IN_SUBNET"):
        return "a CIDR range, e.g. 10.0.0.0/8"
    if ftype == "Float64":
        return "a number"
    if op in ("STARTS_WITH", "DOES_NOT_START_WITH"):
        return "the text it starts with"
    if op in ("ENDS_WITH", "DOES_NOT_END_WITH"):
        return "the text it ends with"
    if op in ("MATCHES_REGEX", "DOES_NOT_MATCH_REGEX"):
        return "a regular expression, e.g. ^admin-"
    return "value"


def add_filter(s, spec, prof):
    """Field → operator (from the field's type) → a value checked with C.filter_problem."""
    field = pick_field(s, prof, "Filter on which field?")
    if not field:
        U.pause()
        return False
    print("  " + U.dim("checking the field type…"))
    ftype = s.field_type(field)
    tl = C.type_label(ftype)
    op = U.select(f"{S(field)} is {'an' if tl[:1].lower() in 'aeiou' else 'a'} {S(tl)} field — how should it match?",
                  [(o, lbl) for o, lbl, _ in C.ops_for(ftype)])
    takes = C.op_takes_list(ftype, op)
    while True:
        value = "" if takes is None else U.ask(_value_prompt(ftype, op, takes))
        f = {"field": field, "op": op, "type": ftype, "value": value}
        prob = C.filter_problem(f)
        if not prob:
            break
        U.err(S(prob) + " — try again (< to go back)")
    if op in C.CASE_OPS:
        f["case_sensitive"] = U.confirm("match upper and lower case exactly?", False)
    if op in C.NOT_OPS:                       # Abstract's search skips events where the field has no value
        f["or_empty"] = U.confirm(f"also keep events where {field} has no value?", False)
    spec["filters"].append(f)
    spec["mode"] = "simple"
    U.ok("added: " + S(C.describe_filter(f)))
    return True


def filter_label(f):
    ftype = f.get("type") or "String"
    op = f.get("op") or "IS_IN_LIST"
    return (f"{S(f.get('field'))} {U.bold(C.op_label(ftype, op))} {S(f.get('value', ''))}"
            + (U.dim("  (exact case)") if f.get("case_sensitive") and op in C.CASE_OPS else "")
            + (U.dim("  (or no value)") if f.get("or_empty") and op in C.NOT_OPS else ""))


def group_items(spec):
    """'At least one of' groups (from a search copied from Abstract) as removable menu items keyed ("g", index)."""
    return [(("g", x), "at least one of: " + "  OR  ".join(filter_label(f) for f in g) + U.dim("   (select to remove)"))
            for x, g in enumerate(spec.get("any_groups") or [])]


def remove_item(spec, k):
    if isinstance(k, tuple):
        if U.confirm("remove this group?", False):
            del spec["any_groups"][k[1]]
    elif U.confirm("remove this filter?", False):                 # UX-47: No, as in the menu and every delete
        del spec["filters"][k]


def show_window(spec):
    try:
        st, en = C.resolve_window(spec)
        U.info(f"window {E.iso(st)[:19]}Z → {E.iso(en)[:19]}Z (UTC)")
    except E.UserError:
        pass


def match_label(spec):
    return "at least one filter must match" if spec.get("match") == "any" else "every filter must match"


def choose_match(spec):
    """With more than one filter: every one must match, or at least one (the dataset always applies)."""
    items = [("all", "Every filter must match"), ("any", "At least one filter must match")]
    spec["match"] = U.select("With more than one filter", items, default=1 if spec.get("match") == "any" else 0)


def choose_storage(spec):
    """Where to search: the same two choices as the browser app (the command line also takes --storage archive)."""
    cur = spec.get("storage") if spec.get("storage") == "recent" else "auto"
    items = [(k, U.pad(C.STORAGE_LABELS[k], 26) + U.dim(C.STORAGE_HINTS[k])) for k in ("auto", "recent")]
    spec["storage"] = U.select("Where to search", items, default=[k for k, _ in items].index(cur)
                               if cur in C.STORAGE_LABELS else 0)


def _day_text(d):
    return d.strftime("%Y-%m-%dT00:00:00Z")


def _as_utc(text):
    """A date alone means the start of that day, UTC."""
    t = (text or "").strip()
    if len(t) == 10 and t[4] == "-" and t[7] == "-":
        return t + "T00:00:00Z"
    return t


def pick_day(title, default=None):
    """A calendar picker: a month, then a day in it (UTC). Returns a date."""
    today = E.now().date()
    months = []
    y, m = today.year, today.month
    for _ in range(14):
        months.append((f"{y:04d}-{m:02d}", dt.date(y, m, 1).strftime("%B %Y")))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    want = (default or today).strftime("%Y-%m")
    k = U.select(f"{title}: month", months, default=next((i for i, (v, _) in enumerate(months) if v == want), 0))
    yy, mm = int(k[:4]), int(k[5:])
    last = (dt.date(yy + (mm == 12), mm % 12 + 1, 1) - dt.timedelta(days=1)).day
    days = [(dt.date(yy, mm, d), dt.date(yy, mm, d).strftime("%a %d %b %Y") + (U.dim("  today") if dt.date(yy, mm, d) == today else ""))
            for d in range(1, last + 1) if dt.date(yy, mm, d) <= today]
    dd = (default or today).day
    return U.select(f"{title}: day (UTC)", days, default=min(len(days) - 1, max(0, dd - 1)))


def choose_time(spec):
    cur = spec.get("time") or {}
    items = [(p, f"Last {p}") for p in C.TIME_PRESETS] + [
        ("today", "Today (UTC)"), ("yesterday", "Yesterday (UTC)"), ("days", "Pick days from a calendar (UTC)"),
        ("custom", "Type an exact start and end (UTC)")]
    default = next((n for n, (k, _) in enumerate(items) if k == cur.get("last")),
                   len(items) - 1 if cur.get("start") else 3)
    k = U.select("Time range", items, default=default,
                 hint="Relative ranges end 10 minutes ago, so late-arriving events have settled.")
    today = E.now().date()
    if k in ("today", "yesterday", "days"):
        if k == "today":
            st, en = _day_text(today), E.iso(E.floor_s(E.now()) - E.RELATIVE_END_LAG)[:19] + "Z"
        elif k == "yesterday":
            st, en = _day_text(today - dt.timedelta(days=1)), _day_text(today)
        else:
            d1 = pick_day("First day")
            d2 = pick_day("Last day", default=d1)
            if d2 < d1:
                d1, d2 = d2, d1
            st = _day_text(d1)
            en = _day_text(d2 + dt.timedelta(days=1)) if d2 < today else \
                E.iso(E.floor_s(E.now()) - E.RELATIVE_END_LAG)[:19] + "Z"
        spec["time"] = {"start": st, "end": en}
        show_window(spec)
        return
    if k == "custom":
        while True:
            st = _as_utc(U.ask("start (UTC, e.g. 2026-09-28T00:00:00Z, or a date such as 2026-09-28)", cur.get("start") or ""))
            en = _as_utc(U.ask("end   (UTC)", cur.get("end") or ""))
            cand = dict(spec, time={"start": st, "end": en})
            try:
                C.resolve_window(cand)
                break
            except E.UserError as ex:
                U.err(C.humanize(ex) + " — try again (< to go back)")
        spec["time"] = {"start": st, "end": en}
    else:
        spec["time"] = {"last": k}
    show_window(spec)


def show_selected_fields(spec, prof):
    if spec.get("full_documents"):
        U.say("Exporting " + U.bold("every field of every event") + " (complete events; about 2-3x slower than a "
              "field list).")
        return
    cov = {f["name"]: f.get("coverage") for f in (prof or {}).get("fields") or []}
    fields = spec.get("fields") or []
    nw = max(12, min(40, max(U.vlen(S(f)) for f in fields), U.cols() - 22)) if fields else 12
    print("  " + U.dim(f"{len(fields)} field(s) selected" + ("  (coverage = share of sampled events that carry it)"
                                                          if cov else "")))
    for f in fields[:10]:
        c = cov.get(f)
        print(U.clip("    " + U.teal("✓") + " " + U.pad(S(f), nw) + ("  " + U.dim(f"{c:5.1f}%") if c is not None else "")))
    if len(fields) > 10:
        print("    " + U.dim(f"+{len(fields) - 10} more"))
    print()


def fields_menu(s, spec, prof, redraw=None, done_label="Continue →"):
    """Choose the export's columns. Returns the (possibly newly learned) profile."""
    while True:
        if redraw:
            redraw()
        show_selected_fields(spec, prof)
        items = [("keep", U.teal(done_label))]
        if prof and prof.get("suggested"):
            sug = prof["suggested"]
            items.append(("suggest", f"Use suggested fields ({len(sug)})  " + U.dim(S(", ".join(sug[2:6])) + "…")))
        items += [("add", U.pink("+ Add a field")), ("remove", "Remove a field"),
                  ("full", (U.teal("✓ ") if spec.get("full_documents") else "")
                   + "Every field (complete events, slower)")]
        if spec.get("dataset"):
            items.append(("learn", ("Learn fields for this dataset again" if prof else
                                    "Learn fields for this dataset now") + U.dim("  (samples recent events, 10–30 seconds)")))
        items.append(("default", "Reset to the defaults"))
        k = U.select("Fields", items, default=1 if (prof and spec.get("fields") == C.DEFAULT_FIELDS) else 0)
        if k == "keep":
            return prof
        if k == "suggest":
            spec["fields"], spec["full_documents"] = list(prof["suggested"]), False
        elif k == "add":
            try:
                f = pick_field(s, prof, "Add which field?")
            except U.Back:
                f = None
            if f and f not in spec["fields"]:
                spec["fields"].append(f)
                spec["full_documents"] = False
        elif k == "remove":
            removable = [(x, S(x)) for x in spec["fields"] if x not in ("@timestamp", "id")]
            if not removable:
                U.note("@timestamp and id are always exported; there is nothing else to remove.")
                U.pause()
                continue
            try:
                spec["fields"].remove(U.select("Remove which field?", removable))
            except U.Back:
                pass
        elif k == "full":
            spec["full_documents"] = not spec.get("full_documents")
        elif k == "learn":
            prof = learn_profile(s, spec["dataset"]) or prof
            U.pause()
        else:
            spec["fields"], spec["full_documents"] = list(C.DEFAULT_FIELDS), False


# ───────────────────────────── shared: preview, checks ─────────────────────────────

def show_preview(pv, max_rows=None, max_cols=8):
    tot = pv.get("total")
    tots = "unknown" if tot is None else f"{tot:,}"
    rows = pv.get("rows") or []
    print(U.clip(f"\n  {U.bold(tots)} events match  " + U.dim(f"· first {len(rows)} shown · {pv.get('seconds', 0)}s")))
    if tot == 0:
        if pv.get("zero_confirmed"):
            U.info("Zero confirmed by a second, independent search.")
        else:
            U.note("Zero events. A zero is a claim: try a wider time range before concluding nothing happened.")
    print()
    cols = pv.get("columns") or []
    U.table(rows, cols[:max_cols], C.flatten, C.cell, max_rows=max_rows)
    if len(cols) > max_cols:
        print("  " + U.dim(f"(+{len(cols) - max_cols} more columns in the export)"))


def show_checks(v):
    for c in v.get("checks") or []:
        st = str(c.get("status") or "INFO")
        col = U.STATUS.get(st, U.dim)
        print(U.clip(f"  {col(U.pad(S(st), 4))}  {U.bold(S(c.get('name')))}"))
        detail = S(c.get("detail") or "")
        if detail:
            for ln in textwrap.wrap(U.t(detail), max(20, U.width() - 10), break_on_hyphens=False):
                print("        " + U.dim(ln))


# ───────────────────────────── shared: validate (cancellable, cached) ─────────────────────────────

def _search_key(spec):
    """What makes two searches the same search: everything but the name and the file format."""
    sk = getattr(C, "_search_key", None)
    if sk:
        try:
            return sk(spec)
        except (KeyError, TypeError, ValueError):
            pass
    spec = C.normalize_spec(spec)
    return json.dumps({k: v for k, v in spec.items() if k not in ("name", "format")}, sort_keys=True, default=str)


# UX-43: Review (and menu Export) re-show the last preview and validation of an unchanged search instead of
# running them again. The TTL stays under core's 15-minute rule (Session._export_window): inside it an export
# reuses the window Validate checked, so the cached "N events" is exactly what gets exported. Only the most
# recent validation is reused, because core remembers only the most recent validated window.
CACHE_TTL = 600
_CACHE = {"preview": {}, "validate": None}


def _cache_key(s, spec):
    tn = s.tenant or {}
    return (tn.get("base_url"), tn.get("vendor_account_id"), _search_key(spec))


def cached(s, spec, kind):
    """(result, age in seconds) for an unchanged search checked in the last CACHE_TTL seconds, else (None, None)."""
    key = _cache_key(s, spec)
    hit = _CACHE["validate"] if kind == "validate" else _CACHE["preview"].get(key)
    if not hit or hit[0] != key:
        return None, None
    age = time.time() - hit[1]
    return (hit[2], age) if 0 <= age < CACHE_TTL else (None, None)


def remember(s, spec, kind, result, started=None):
    """started: when the call began. Core stamps its validated window at the START of validate, so the age is
    measured from there too; a slow validate can then never outlive core's 15-minute window."""
    entry = (_cache_key(s, spec), time.time() if started is None else started, result)
    if kind == "validate":
        _CACHE["validate"] = entry
    else:
        _CACHE["preview"] = {entry[0]: entry}                    # one search's preview is enough


def forget():
    _CACHE["preview"], _CACHE["validate"] = {}, None


def fmt_age(sec):
    sec = int(sec or 0)
    return "just now" if sec < 60 else f"{sec // 60} min ago"


def run_preview(s, spec, n=25, use_cache=True):
    """The preview, from the cache when this search was previewed in the last few minutes. (result, age|None)."""
    if use_cache:
        pv, age = cached(s, spec, "preview")
        if pv is not None:
            return pv, age
    pv = s.preview(spec, n)
    remember(s, spec, "preview", pv)
    return pv, None


def run_validate(s, spec, use_cache=True):
    """Validate on a worker thread so Ctrl+C lands at once (also on Windows). Ctrl+C asks the session to stop the
    validation (Session.cancel_validate) and shows "Validation stopped". Returns (result, age|None), or
    (None, None) when stopped. A session without cancel_validate keeps the old behaviour: Ctrl+C interrupts."""
    if use_cache:
        v, age = cached(s, spec, "validate")
        if v is not None:
            return v, age
    _CACHE["validate"] = None                                    # a stopped or failed run still moves core's window
    t0 = time.time()
    if not hasattr(s, "cancel_validate"):
        try:
            v = s.validate(spec)
        except (KeyboardInterrupt, E.Cancelled):
            print()
            U.note("Validation stopped. Nothing was exported.")
            return None, None
        remember(s, spec, "validate", v, t0)
        return v, None
    box = {}

    def work():
        try:
            box["v"] = s.validate(spec)
        except BaseException as ex:                              # handed to the main thread below
            box["ex"] = ex
    th = threading.Thread(target=work, name="abx-validate", daemon=True)
    th.start()
    stopping = False
    while th.is_alive():
        try:
            th.join(0.25)
        except KeyboardInterrupt:
            if not stopping:
                stopping = True
                try:
                    s.cancel_validate()
                except ERRORS:
                    pass
                print()
                U.note("Stopping the validation… the current request finishes first.")
            else:
                U.note("Still stopping. The current request finishes first.")
    ex = box.get("ex")
    if stopping or isinstance(ex, E.Cancelled):
        U.note("Validation stopped. Nothing was exported.")
        return None, None
    if ex is not None:
        raise ex
    remember(s, spec, "validate", box["v"], t0)
    return box["v"], None


def readiness(v):
    """One wording for the guided flow and the menu (and the browser app): 'ready' | 'zero' | 'fix'."""
    plan = v.get("plan") or {}
    total = plan.get("total")
    if v.get("ready"):
        a = plan.get("archive")
        up = "up to " if plan.get("upper_bound") else ""
        if a:
            mins = f", roughly {a['minutes']:,} minutes" if a.get("minutes") else ""
            print("  " + U.teal(f"Ready to export: {up}{total or 0:,} events from recent data, plus the part before "
                                f"{str(a['end'])[:10]} from the archive (counted during the export{mins})."))
        else:
            print("  " + U.teal(f"Ready to export: {up}{total or 0:,} events in about {plan.get('slices') or 1:,} " + ("slice." if (plan.get('slices') or 1) == 1 else "slices.")))
        if plan.get("time_words"):
            print("  " + U.dim("Estimated time: " + plan["time_words"]))
        return "ready"
    if v.get("ok") and total == 0:
        by = v.get("zero_confirmed_by")
        print("  " + U.amber("Nothing matched: 0 events."))
        U.say(("Zero confirmed by a second, independent search (" + S(by) + ")." if by else
               "The zero was not independently confirmed.") + " A zero is a claim: try a wider time range before "
              "concluding nothing happened.", indent=4, style=U.dim)
        return "zero"
    print("  " + U.red("Something needs fixing before this can be exported (see FAIL above)."))
    return "fix"


EMPTY_EXPORT_LABEL = "Export anyway (empty file)"


def confirm_empty_export():
    return U.confirm("Export anyway? Nothing matched, so the file will be empty.", False)


def cli_only(spec):
    """True (after explaining) when the spec is an advanced one that only the command line runs."""
    if C.normalize_spec(spec)["mode"] == "advanced":
        U.note(ADVANCED_MSG)
        return True
    return False


# ───────────────────────────── shared: export progress and result ─────────────────────────────

def progress_text(st):
    p = st.get("progress") or {}
    parts = [S(p.get("stage") or "starting")]
    exp = p.get("rows_expected")
    wr = (p.get("rows_written") or 0) + (p.get("current_slice_rows") or 0)
    if exp:
        parts.append(f"{wr:,}/{exp:,} events")
    elif wr:
        parts.append(f"{wr:,} events")
    if p.get("slices"):
        parts.append(f"slices {p.get('complete') or 0}/{p['slices']}")
    parts.append(U.fmt_elapsed(st.get("elapsed")))
    if p.get("eta_words"):
        parts.append(p["eta_words"])
    return " · ".join(parts)


def _drain(s, seq, prog):
    lines, seq = s.lines_since(seq)
    for ln in lines:
        prog.line("  " + U.dim("│ ") + S(ln))
    return seq


def stop_and_wait(s, prog, seq):
    s.cancel()
    prog.finish()
    print("  " + U.amber("Stopping… finishing the current request"), flush=True)
    while s.busy():
        try:
            seq = _drain(s, seq, prog)
            time.sleep(0.3)
        except KeyboardInterrupt:
            U.note("Still stopping. The current request finishes first, so the saved progress stays consistent.")
    return _drain(s, seq, prog)


def watch_export(s, seq=None, allow_leave=True):
    """Follow the running export: one progress line, new log lines streamed above it. Ctrl+C asks whether to
    stop. Returns the final job status, or None when the user left it running in the background."""
    st = s.job_status()
    if not st:
        return None
    if seq is None:
        seq = max(0, (st.get("seq") or 0) - 8)
    prog = U.Progress()
    U.say("Ctrl+C to stop the export" + (" or leave it running in the background" if allow_leave else ""), style=U.dim)
    while True:
        try:
            while s.busy():
                seq = _drain(s, seq, prog)
                st = s.job_status()
                p = st.get("progress") or {}
                prog.update(p.get("pct"), progress_text(st), p.get("stage"))
                time.sleep(0.4)
            break
        except KeyboardInterrupt:
            prog.finish()
            items = [("stop", "Stop the export"), ("keep", "Keep exporting")]
            if allow_leave:
                items.append(("leave", "Leave it running and go to the main menu"))
            try:
                choice = U.select("Stop the export?", items, back=False, home=False,
                                  hint="Stopping keeps everything written so far.")
            except KeyboardInterrupt:
                choice = "stop"
            if choice == "stop":
                seq = stop_and_wait(s, prog, seq)
                break
            if choice == "leave":
                return None
    _drain(s, seq, prog)
    st = s.job_status() or {}
    p = st.get("progress") or {}
    if (st.get("result") or {}).get("exit_code") in (0, 3):
        prog.update(100.0, progress_text(st), p.get("stage"))     # the last redraw shows the finished numbers
    prog.finish()
    return st


def result_path(r):
    run_dir, merged = r.get("run_dir"), r.get("merged_file")
    if merged:
        path = merged if os.path.isabs(merged) else os.path.join(run_dir or "", merged)
        if os.path.isfile(path):
            return path
    return run_dir


def resumable(r):
    return r.get("exit_code") in (1, 130) and not r.get("nothing_to_resume") and bool(r.get("run_dir")) \
        and os.path.isfile(os.path.join(r["run_dir"], "manifest.json"))


def show_result(r):
    r = r or {}
    code = r.get("exit_code")
    wr, exp = r.get("rows_written") or 0, r.get("rows_expected") or 0
    sl = f" in {S(r['slices'])} slices" if r.get("slices") else ""
    print()
    if code == 0:
        print("  " + U.gradient("EXPORT COMPLETE"))
        if r.get("zero_result"):
            U.ok(f"Nothing matched: 0 events written{sl}. The file is empty.")
        else:
            U.ok(f"{wr:,} events written{sl}: the complete result.")
    elif code == 3:
        print("  " + U.amber(U.bold("PARTIAL: RECENT DATA ONLY")))
        U.note(S(r.get("coverage_warning") or E.PARTIAL))
        U.note(f"{wr:,} events written{sl} from recent data.")
    elif code == 1:
        print("  " + U.amber(U.bold("INCOMPLETE")))
        U.note(f"{wr:,} of {exp:,} events written{sl}. Resume continues from the first unfinished slice.")
        if r.get("error"):
            U.err(S(r["error"]))
    elif code == 130:
        print("  " + U.amber(U.bold("STOPPED")))
        if r.get("nothing_to_resume"):
            U.note(S(r.get("error") or "Stopped before any events were written, so there is nothing to resume."))
        else:
            U.note(f"{wr:,} of {exp:,} events written so far. The export can be resumed: choose Resume now, or "
                   "later from the main menu → Export history.")
    else:
        print("  " + U.red(U.bold("STOPPED BY A CHECK")))
        U.err(S(r.get("error") or "the export stopped before it finished"))
    if r.get("stored_duplicates"):
        U.info(f"{r['stored_duplicates']:,} duplicate copies were removed; every event is exported once")
    if r.get("id_conflicts"):
        U.info(f"{r['id_conflicts']:,} different events share an id with another event; each was written")
    shown = {str(r.get("coverage_warning") or ""), str(r.get("error") or "")}
    for w in r.get("warnings") or []:                            # the engine's notes (zero proof, timestamps, filters)
        if w and str(w) not in shown:
            U.note(S(w))
            shown.add(str(w))
    path = result_path(r)
    if path and os.path.exists(path):
        size = ""
        if os.path.isfile(path):
            try:
                size = "  " + U.dim(U.fmt_bytes(os.path.getsize(path)))
            except OSError:
                size = ""
        label = "File  " if os.path.isfile(path) else "Folder"
        print()
        print(f"  {U.dim(label)} {U.bold(S(path))}{size}")                # never clipped: it is the answer


def after_export(s, r, extra):
    """The actions after a result: Resume now (when resumable), Open folder, then the caller's own items.
    Returns the caller's chosen key, or 'left' when a resumed export was left running."""
    while True:
        items = []
        if resumable(r):
            items.append(("resume", U.teal("Resume now")))
        if r.get("run_dir") and os.path.isdir(r["run_dir"]):
            items.append(("open", "Open folder"))
        items += extra
        print()
        k = U.select("What next?", items, back=False)
        try:
            if k == "resume":
                s.resume(r["run_dir"])
                st = watch_export(s, seq=0)
                if st is None:
                    return "left"
                r = st.get("result") or {}
                show_result(r)
                continue
            if k == "open":
                open_folder(result_path(r) or r["run_dir"])
                continue
        except ERRORS as ex:
            problem(ex)
            continue
        return k


def short_path(p):
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if home and home != "~" and p.startswith(home + os.sep) else p


def large_export_tip():
    print("  " + U.bold("Large exports"))
    for line in (f"The file is written directly to {S(short_path(os.path.join(C.home_dir(), '<run>')))}.",
                 "Keep this window open. If the computer sleeps, Resume continues where it stopped.",
                 "For unattended multi-GB exports, use the command line: python3 abstract_search.py export … "
                 "--merge under nohup, tmux or Task Scheduler (Help → Command line)."):
        U.say(U.t("· ") + line, indent=4)
    print()


def choose_format(spec):
    fmts = [("ndjson.gz", "NDJSON.gz  " + U.dim("compressed, one JSON event per line (recommended)")),
            ("ndjson", "NDJSON     " + U.dim("uncompressed")),
            ("csv", "CSV        " + U.dim("one column per field, for spreadsheets"))]
    cur = next((n for n, (k, _) in enumerate(fmts) if k == spec.get("format")), 0)
    return U.select("File format", fmts, default=cur)


# ───────────────────────────── the wizard ─────────────────────────────

class Wizard:
    def __init__(self, session, spec=None):
        self.s = session
        self.spec = C.normalize_spec(spec) if spec else C.empty_spec()
        self.prof = None
        self.cleared = None                                      # the spec that passed Review, as JSON
        self._fresh = False                                      # Review re-runs preview and validate once
        self.shown_job = None                                    # the job whose result this wizard showed

    def frame(self, i, title, blurb):
        U.header(f"v{C.VERSION}", self.s.tenant, sanitize=S)
        U.steps_bar(STEPS, i)
        print("  " + U.gradient(title.upper()))
        U.say(blurb, style=U.dim)
        print()

    def _snapshot(self):
        return json.dumps(C.normalize_spec(self.spec), sort_keys=True)

    def _skipped(self, i):
        return i == 2 and self.spec.get("mode") == "kql"      # a KQL search holds its own filters

    def run(self):
        """Returns 'menu', 'quit' or 'done'."""
        if self.spec.get("mode") == "advanced":
            U.header(f"v{C.VERSION}", self.s.tenant, sanitize=S)
            U.note(ADVANCED_MSG)
            try:
                k = U.select("What now?", [("new", "Start a new guided search"), ("menu", "Main menu")], back=False)
            except U.Home:
                return "menu"
            if k != "new":
                return "menu"
            self.spec = C.empty_spec()
        steps = [self.connect, self.dataset, self.filters, self.when, self.fields, self.review, self.export]
        i = 0 if not self.s.tenant else 1
        while True:
            if i > 0 and not self.s.tenant:
                i = 0
            direction = 1
            try:
                nxt = steps[i](i)
                if nxt in ("done", "menu", "quit"):
                    return nxt
                i = min(len(STEPS) - 1, i + 1) if nxt is None else nxt
            except U.Back:
                i, direction = max(0, i - 1), -1
            except U.Home:
                return "menu"
            except KeyboardInterrupt:
                raise
            except ERRORS as ex:
                problem(ex)
                i = self.recover(i)
                if i is None:
                    return "menu"
            while self._skipped(i):
                i += direction

    def recover(self, i):
        items = [("retry", "Try again")] + ([("back", "Go back a step")] if i > 0 else []) + [("menu", "Main menu")]
        try:
            k = U.select("What now?", items, back=False)
        except U.Home:
            return None
        if k == "retry":
            return i
        if k == "back":
            return max(0, i - 1)
        return None

    # -- 1 connect --
    def connect(self, i):
        self.frame(i, "Connect to your tenant",
                   "Your API key stays in this computer's memory for this session only. It is never saved.")
        if self.s.tenant:
            tn = self.s.tenant
            k = U.select("Tenant", [("keep", f"Continue with {U.bold(S(tn['name']))} ({S(tn['vendor_account_id'])})"),
                                    ("switch", "Connect to a different tenant")], back=False)
            if k == "keep":
                return None
        U.require_secret_terminal()                              # PORT-23: before the URL, not after it
        st = C.load_settings()
        base = U.ask("API URL", st.get("base_url") or C.PROD_URL)
        va = U.ask("Vendor account ID", st.get("vendor_account_id") or "")
        key = U.ask("API key (hidden as you type)", secret=True)
        print("  " + U.dim("checking the key…"))
        tn = self.s.connect(base, va, key)
        self.prof = None
        U.ok(f"connected to {U.bold(S(tn['name']))} ({S(tn['vendor_account_id'])})"
             + ("  " + U.pink("production") if tn.get("production") else ""))
        check_tenant(self.s)
        if U.INTERACTIVE:
            time.sleep(0.6)
        return None

    # -- 2 dataset --
    def dataset(self, i):
        self.frame(i, "Choose a dataset", "A dataset is one source of events: the name of an integration such as your "
                   "firewall, identity or cloud audit feed.")
        if self.spec.get("mode") == "kql":
            if self.s.kql_on():
                print("  " + U.bold("This search runs as KQL") + U.dim("  (KQL is available on this tenant)"))
                print("    " + S(self.spec.get("kql") or ""))
                k = U.select("Keep it?", [("keep", "Yes, continue"), ("new", "Start a new search instead")])
                if k == "keep":
                    return 3
                self.spec.clear()
                self.spec.update(C.empty_spec())
                return 1
            return self._kql_gate()
        how = U.select("How do you want to start?", [
            ("build", "Pick a dataset" + (U.dim(f"  (now: {S(self.spec['dataset'])})") if self.spec.get("dataset") else "")),
            ("english", "Describe it in plain English  " + U.dim("(Abstract's assistant writes the KQL; you check it)")),
            ("kql", "Write a KQL query  " + U.dim("(checked live before anything runs)")),
            ("import", "Use a search from Abstract  " + U.dim("(a saved view, or a search copied from the search box)"))])
        if how in ("english", "kql"):
            import abx_tui_platform as TP
            c = TP.check_for_wizard(self.s, how)
            sp = TP.spec_from_check(c, self.spec) if c else None
            if sp is None:
                return 1
            self.spec.clear()
            self.spec.update(sp)
            self.prof = load_profile(self.s, self.spec["dataset"], say=False) if self.spec.get("dataset") else None
            return 3 if self.spec.get("mode") == "kql" else 2
        if how == "import":
            if import_from_abstract(self.s, self.spec):
                self.prof = load_profile(self.s, self.spec["dataset"], say=False) if self.spec.get("dataset") else None
                return 3 if self.spec.get("mode") == "kql" else 2   # the Filters step shows what was converted
            return 1
        k = choose_dataset(self.s, self.spec.get("dataset"))
        if k == self.spec.get("dataset") and self.prof:
            return None
        self.prof = apply_dataset(self.s, self.spec, k)
        if U.INTERACTIVE:
            time.sleep(0.8)
        return None

    def _kql_gate(self):
        """A KQL search on a tenant where KQL is not available: say so and rebuild it as dataset + filters."""
        U.note("This saved search uses KQL. " + KQL_OFF_MSG)
        U.pause()
        self.spec["mode"], self.spec["kql"] = "simple", ""
        return 1

    # -- 3 filters --
    def filters(self, i):
        while True:
            self.frame(i, "Narrow it down (optional)", "Filters keep only the events you need. With more than one, choose "
                       "whether every filter or at least one must match. Text ignores upper and lower case unless you choose "
                       "otherwise.")
            n = len(self.spec["filters"]) + len(self.spec.get("any_groups") or [])
            items = [("go", U.teal("Continue →") + U.dim(f"  ({n} filter{'s' if n != 1 else ''})"))]
            items += [(x, filter_label(f) + U.dim("   (select to remove)")) for x, f in enumerate(self.spec["filters"])]
            items += group_items(self.spec)
            items.append(("add", U.pink("+ Add a filter")))
            if n > 1:
                items.append(("match", "Match: " + match_label(self.spec)))
            k = U.select("Filters", items, default=0 if self.spec["filters"] else len(items) - 1)
            if k == "go":
                return None
            if k == "match":
                choose_match(self.spec)
                continue
            if k == "add":
                try:
                    add_filter(self.s, self.spec, self.prof)
                except U.Back:
                    pass
            else:
                remove_item(self.spec, k)

    # -- 4 time --
    def when(self, i):
        self.frame(i, "Pick a time range", "Custom times are UTC. Automatic searches recent data, plus the archive "
                   "when the range reaches past it.")
        choose_time(self.spec)
        if self.spec.get("mode") == "simple":
            choose_storage(self.spec)
        if U.INTERACTIVE:
            time.sleep(0.6)
        return None

    # -- 5 fields --
    def fields(self, i):
        if self.prof is None and self.spec.get("dataset"):
            self.prof = load_profile(self.s, self.spec["dataset"], say=False)
        self.prof = fields_menu(self.s, self.spec, self.prof, redraw=lambda: self.frame(
            i, "Choose the columns", "@timestamp and id are always included. Suggested fields are the who / what / "
            "where fields this dataset actually carries."))
        return None

    # -- 6 review --
    def review(self, i):
        self.cleared = None
        self.frame(i, "Review and validate", "The tool previews the first events, then runs every check an export "
                   "runs: fields, proof that each filter is applied, a second independent count and retention.")
        print_spec(self.spec)
        if cli_only(self.spec):
            U.select("Next", [("menu", "Main menu")], back=True)
            return "menu"
        probs = C.spec_problems(self.spec)
        if probs:
            print()
            for p in probs:
                U.err(S(p))
            print()
            return U.select("Change what?", [(1, "Dataset"), (2, "Filters"), (3, "Time"), (4, "Fields")], default=0)
        fresh = self._fresh
        self._fresh = False
        if cached(self.s, self.spec, "preview")[0] is None:     # "Validate again" re-runs the checks, not this
            print("\n  " + U.dim("previewing the first 25 events…"))
        pv, _ = run_preview(self.s, self.spec, 25)
        fit = max(4, min(25, U.rows() - 38))
        show_preview(pv, max_rows=fit, max_cols=6)
        if fresh or cached(self.s, self.spec, "validate")[0] is None:
            print("\n  " + U.dim("validating — the same checks an export runs (Ctrl+C stops it)…"))
        v, age = run_validate(self.s, self.spec, use_cache=not fresh)
        items = []
        if v is None:                                            # stopped with Ctrl+C
            items.append(("again", U.teal("Validate again")))
            state = "stopped"
        else:
            if age is not None:
                U.info(f"Unchanged search: these are the checks from {fmt_age(age)}. Choose Validate again to re-run "
                       "them.")
            print()
            show_checks(v)
            print()
            state = readiness(v)
            if state == "ready":
                items.append(("export", U.teal("Export these events →")))
            elif state == "zero":
                items.append(("anyway", EMPTY_EXPORT_LABEL))
            if age is not None:
                items.append(("again", "Validate again"))
        items += [("change", "Change something"), ("save", "Save this search")]
        if len(pv.get("rows") or []) > fit:
            items.append(("rows", f"Show all {len(pv['rows'])} preview events"))
        while True:
            k = U.select("Next", items, default=0)
            if k == "export":
                self.cleared = self._snapshot()
                return None
            if k == "anyway":
                if confirm_empty_export():
                    self.cleared = self._snapshot()
                    return None
            elif k == "again":
                self._fresh = True
                return i
            elif k == "change":
                return U.select("Change what?", [(1, "Dataset"), (2, "Filters"), (3, "Time"), (4, "Fields")])
            elif k == "save":
                try:
                    save_search(self.spec)
                except U.Back:                                   # '<' at the name prompt: stay on Review
                    pass
                except ERRORS as ex:
                    problem(ex)
            elif k == "rows":
                show_preview(pv, max_cols=8)

    # -- 7 export --
    def export(self, i):
        if self.cleared != self._snapshot():
            return 5                                             # only a validated search is exported
        self.frame(i, "Export", "The export can be stopped and resumed. The result is checked against the platform's "
                   "own count, slice by slice.")
        large_export_tip()
        fmt = choose_format(self.spec)
        self.spec["format"] = fmt
        self.cleared = self._snapshot()
        probs = C.spec_problems(self.spec)
        if probs:
            for p in probs:
                U.err(S(p))
            U.pause()
            return i
        if not U.confirm("Start the export now?", True):
            raise U.Back()
        self.s.start_export(self.spec, {"format": fmt, "merge": True})
        print()
        st = watch_export(self.s, seq=0)
        if st is None:
            return "menu"
        return self.done(st.get("result") or {})

    def done(self, r):
        self.shown_job = (self.s.job_status() or {}).get("id")
        show_result(r)
        k = after_export(self.s, r, [("new", "Start a new search"), ("again", "Change this search and export again"),
                                     ("menu", "Main menu"), ("quit", "Quit")])
        job = self.s.job_status() or {}
        self.shown_job = job.get("id") if not job.get("running") else self.shown_job
        self.cleared = None
        if k == "new":
            self.spec, self.prof = C.empty_spec(), None
            return 1
        if k == "again":
            return 1
        if k in ("left", "menu"):
            return "menu"
        return "quit"
