"""
abx_menu — the terminal main menu for Abstract Search & Export.

Arrow-key menus on a terminal, plain numbered prompts everywhere else (pipes, TERM=dumb, ABX_PLAIN=1), over
SSH and on macOS, Linux and Windows, with nothing to install. Every action calls the same abx_core session the
guided flow and the browser app use, and every action recovers from errors back to this menu.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import os
import traceback

import abx_core as C
import abx_engine as E
import abx_ui as U
import abx_wizard as W
import abx_tui_platform as TP

S = C.sanitize
ERRORS = W.ERRORS


def choose(title, items, back=True, hint=None, default=0):
    """A menu where back and 'main menu' both mean: leave this screen (None)."""
    try:
        return U.select(title, items, back=back, hint=hint, default=default)
    except (U.Back, U.Home):
        return None


class Menu:
    def __init__(self, session=None):
        self.s = session or C.Session()
        self.spec = C.empty_spec()
        self.quit = False
        self.seen_job = None                                     # the job whose result was already shown
        try:
            tn = self.s.connect_from_env()
            if tn:
                U.ok(f"connected from environment variables to {S(tn['name'])} ({S(tn['vendor_account_id'])})")
                W.check_tenant(self.s)
        except ERRORS as ex:
            U.err("environment credentials did not work: " + C.humanize(ex))

    # -- screen --
    def show_search(self):
        print(U.clip("  " + U.bold("Current search") + (f"  {U.dim(S(self.spec['name']))}" if self.spec.get("name")
                                                         else "")))
        for k, v in W.spec_lines(self.spec):
            print(U.clip(f"    {U.dim(k.ljust(7))} {v}"))
        U.rule()

    def run(self):
        if U.launch_mode():                                      # ABX_LAUNCH=1: the launch, once, at start
            U.rocket_launch()
        while not self.quit:
            U.header(f"v{C.VERSION}", self.s.tenant, sanitize=S)
            self.show_search()
            job = self.s.job_status()
            unseen = self.unseen_result()
            if job and job["running"]:
                print(U.clip("  " + U.blue("export running: " + W.progress_text(job))))
            elif unseen:
                print(U.clip("  " + U.teal("the export finished — choose Export to see the result")))
            busy = self.s.busy()
            items = [("guided", U.pink(U.bold("Guided search & export")) + U.dim("  step by step, recommended")),
                     ("connect", "Connect / switch tenant"),
                     ("tenant", "What this tenant supports  " + U.dim("(KQL, archive, complete events)")),
                     ("build", "Build or edit the search"),
                     ("english", "Describe what you want in plain English  " + U.dim("(Abstract's assistant writes "
                                                                                    "the KQL; you check it first)")),
                     ("kql", "Write or paste a KQL query  " + U.dim("(checked live: exact export where it converts)")),
                     ("preview", "Preview results (first 25 events)"),
                     ("validate", "Validate the search (fields, filter proof, cross-check, export plan)"),
                     ("export", U.blue("Watch the running export") if busy else
                      (U.teal("See the finished export's result") if unseen else "Export all results")),
                     ("exports", "Export history: list, resume, open folder"),
                     ("views", "Saved views from Abstract  " + U.dim("(every view on this tenant, read live)")),
                     ("saved", "Saved searches  " + U.dim("(yours, on this computer)")),
                     ("fieldsets", "Fieldsets in Abstract  " + U.dim("(build, change and save column lists)")),
                     ("content", "Views and fieldsets as code  " + U.dim("(back up, compare, restore, copy to "
                                                                         "another tenant)")),
                     ("automate", "Run a search on a schedule  " + U.dim("(cron, launchd, systemd, Windows, GitHub "
                                                                         "Actions, containers)")),
                     ("fields", "Browse fields of a dataset or of the tenant"),
                     ("learn", "Learn the fields of every dataset  " + U.dim("(live, kept in memory only)")),
                     ("datasets", "List every dataset"),
                     ("web", "Open the browser app"),
                     ("help", "Help"),
                     ("quit", "Quit")]
            try:
                k = U.select("Main menu", items, back=False, home=False, words=("launch",))
            except U.EndOfInput:
                self.shutdown()
                return 0
            except KeyboardInterrupt:
                k = "quit"
            if k == "quit":
                if self.request_quit():
                    return 0
                continue
            if k == "word:launch":                               # hidden launch mode: how screens look, nothing else
                on = not U.launch_mode()
                U.launch_mode(on)
                if on and U.launch_possible():
                    U.rocket_launch()
                elif not U.launch_possible():
                    U.info("Launch mode needs a colour terminal.")
                continue
            self.action(getattr(self, "do_" + k))
        return 0

    def action(self, fn):
        """Run one menu action; whatever goes wrong, come back to the menu with a readable message."""
        try:
            fn()
        except U.EndOfInput:
            raise
        except U.Quit:
            if not self.request_quit():
                return
        except KeyboardInterrupt:
            print()
            return
        except (U.Back, U.Home):
            return
        except ERRORS as ex:
            W.problem(ex)
            U.pause()
        except Exception as ex:                                  # last resort: never a bare traceback
            path = E.log_error("FAILED", f"terminal: {type(ex).__name__}: {ex}", ex)
            U.err("Something went wrong in the tool itself: " + S(f"{type(ex).__name__}: {ex}")
                  + ". Your exports are safe; you can try again. The details are in " + S(path))
            if os.environ.get("ABX_DEBUG"):
                traceback.print_exc()
            U.pause()

    def unseen_result(self):
        job = self.s.job_status()
        return bool(job and not job["running"] and job.get("result") is not None and job["id"] != self.seen_job)

    def request_quit(self):
        """Quit, but never leave an export half-written by killing its thread: stop it cleanly first."""
        if self.s.busy():
            try:
                stop = U.confirm("An export is running. Stop it and quit? (it can be resumed later)", False)
            except KeyboardInterrupt:
                stop = True
            if not stop:
                return False
            self.shutdown()
        self.quit = True
        print()
        return True

    def shutdown(self):
        if self.s.busy():
            W.stop_and_wait(self.s, U.Progress(), (self.s.job_status() or {}).get("seq") or 0)
            r = (self.s.job_status() or {}).get("result") or {}
            if W.resumable(r):
                U.info("The export was stopped and can be resumed from the main menu → Export history.")

    # -- guided --
    def do_guided(self):
        w = W.Wizard(self.s, self.spec)
        try:
            res = w.run()
        except U.Quit:
            res = "quit"
        except U.EndOfInput:
            raise
        except KeyboardInterrupt:
            res = "menu"
        self.spec = w.spec
        if w.shown_job:
            self.seen_job = w.shown_job
        if res == "quit" and not self.request_quit():
            return

    # -- connect --
    def do_connect(self):
        U.require_secret_terminal()                              # PORT-23: refuse before the first prompt
        st = C.load_settings()
        print("\n  " + U.bold("Connect to your Abstract tenant"))
        print("  " + U.dim("The key is kept in memory only and is never saved. See Help → Security. Type < to go back."))
        base = U.ask("API URL", st.get("base_url") or C.PROD_URL)
        va = U.ask("Vendor account ID", st.get("vendor_account_id") or "")
        key = U.ask("API key (hidden)", secret=True)
        print("  " + U.dim("checking the key…"))
        tn = self.s.connect(base, va, key)
        U.ok(f"connected to {U.bold(S(tn['name']))} ({S(tn['vendor_account_id'])})"
             + ("  " + U.pink("production") if tn.get("production") else ""))
        W.check_tenant(self.s)
        U.pause()

    # -- build --
    def profile(self):
        ds = self.spec.get("dataset")
        if not ds or not self.s.tenant:
            return None
        try:
            return W.load_profile(self.s, ds, say=False)
        except ERRORS:
            return None

    def do_build(self):
        self.s.require()
        if W.cli_only(self.spec):
            if U.confirm("Start a new search instead?", True):
                self.spec = C.empty_spec()
            else:
                return
        if self.spec.get("mode") == "kql" and not self.s.kql_on():   # KQL not available here: rebuild as filters
            U.note("This saved search uses KQL. " + W.KQL_OFF_MSG)
            self.spec["mode"], self.spec["kql"] = "simple", ""
        kql = None
        while True:
            print()
            self.show_search()
            n = len(self.spec["filters"]) + len(self.spec.get("any_groups") or [])
            k = choose("Build the search", [
                ("dataset", "Dataset: " + (S(self.spec["dataset"]) or U.dim("none"))),
                ("filters", f"Filters: {n}"),
                ("time", "Time range: " + S(C.describe_spec(self.spec)["window"])),
                ("storage", "Where to search: " + S(C.describe_spec(self.spec)["storage"])),
                ("fields", "Fields to export: " + ("every field" if self.spec["full_documents"]
                                                   else str(len(self.spec["fields"])))),
                ("import", "Use a search from Abstract  " + U.dim("(saved view or pasted search)")),
                ("reset", "Start a new search"),
            ], hint="go back when done")
            if k is None:
                return
            try:
                getattr(self, "b_" + k)(kql)
            except U.Back:
                pass

    def b_dataset(self, kql):
        k = W.choose_dataset(self.s, self.spec.get("dataset"))
        W.apply_dataset(self.s, self.spec, k)
        self.spec["kql"] = ""

    def b_filters(self, kql):
        prof = self.profile()
        while True:
            items: List[Tuple[Any, str]] = [(i, W.filter_label(f) + U.dim("   (select to remove)"))
                                            for i, f in enumerate(self.spec["filters"])]
            items += W.group_items(self.spec)
            items.append(("add", U.teal("+ Add a filter")))
            if len(self.spec["filters"]) > 1:
                items.append(("match", "Match: " + W.match_label(self.spec)))
            k = choose("Filters", items)
            if k is None:
                return
            if k == "match":
                W.choose_match(self.spec)
                continue
            if k == "add":
                try:
                    W.add_filter(self.s, self.spec, prof)
                except U.Back:
                    pass
            else:
                W.remove_item(self.spec, k)

    def b_time(self, kql):
        W.choose_time(self.spec)

    def b_storage(self, kql):
        W.choose_storage(self.spec)

    def b_fields(self, kql):
        W.fields_menu(self.s, self.spec, self.profile(), done_label="Done")

    def b_import(self, kql):
        W.import_from_abstract(self.s, self.spec)

    def b_reset(self, kql):
        if U.confirm("discard the current search?", False):
            self.spec = C.empty_spec()

    # -- actions --
    def _runnable(self):
        self.s.require()
        if W.cli_only(self.spec):
            U.pause()
            return False
        probs = C.spec_problems(self.spec)
        if probs:
            for p in probs:
                U.err(S(p))
            U.pause()
            return False
        return True

    def do_preview(self):
        if not self._runnable():
            return
        print("\n  " + U.dim("searching…"))
        W.show_preview(W.run_preview(self.s, self.spec, 25, use_cache=False)[0])
        U.pause()

    def validate(self, use_cache=False):
        """The checks, then the shared readiness line. (result, 'ready'|'zero'|'fix'), or (None, None) when
        stopped with Ctrl+C. Menu → Validate always runs fresh; Export reuses an unchanged search's checks."""
        if not use_cache or W.cached(self.s, self.spec, "validate")[0] is None:
            print("\n  " + U.dim("validating — the same checks an export runs, without exporting (Ctrl+C stops it)…"))
        v, age = W.run_validate(self.s, self.spec, use_cache=use_cache)
        if v is None:
            return None, None
        if age is not None:
            U.info(f"Unchanged search: these are the checks from {W.fmt_age(age)}.")
        print()
        W.show_checks(v)
        print()
        return v, W.readiness(v)

    def do_validate(self):
        if not self._runnable():
            return
        self.validate()
        U.pause()

    def do_export(self):
        if self.s.busy():
            return self.follow(W.watch_export(self.s))
        if self.unseen_result():
            return self.follow(self.s.job_status())
        if not self._runnable():
            return
        v, state = self.validate(use_cache=True)
        if v is None:
            U.pause()
            return
        if state == "zero":
            k = choose("Nothing matched", [("anyway", W.EMPTY_EXPORT_LABEL), ("back", "Go back")], back=True)
            if k != "anyway" or not W.confirm_empty_export():
                return
        elif state != "ready":
            U.pause()
            return
        W.large_export_tip()
        fmt = W.choose_format(self.spec)
        self.spec["format"] = fmt
        probs = C.spec_problems(self.spec)
        if probs:
            for p in probs:
                U.err(S(p))
            U.pause()
            return
        d = C.describe_spec(self.spec)
        print(U.clip(f"\n  Exporting {U.bold(S(d['query']))}"))
        print(f"  over {S(d['window'])} to {S(C.home_dir())}")
        if not U.confirm("start the export?", True):
            return
        self.s.start_export(self.spec, {"format": fmt, "merge": True})
        self.follow(W.watch_export(self.s, seq=0))

    def follow(self, st):
        """After watching: the result and its actions, or back to the menu when left running."""
        if st is None:
            return
        r = st.get("result") or {}
        self.seen_job = st.get("id")
        W.show_result(r)
        W.after_export(self.s, r, [("menu", "Main menu")])
        job = self.s.job_status() or {}
        if not job.get("running"):
            self.seen_job = job.get("id")

    def do_exports(self):
        rows = C.list_exports(self.s)
        if not rows:
            print("\n  " + U.dim(f"no exports yet in {S(C.home_dir())}"))
            U.pause()
            return
        colour = {"COMPLETE": U.teal, "PARTIAL": U.amber, "RUNNING": U.blue, "PLANNED": U.dim}
        nw = max(16, min(44, max(U.vlen(S(x["name"])) for x in rows[:40]), U.cols() - 44))
        items = []
        for x in rows[:40]:
            st = x.get("status") or "INCOMPLETE"
            meta = f"{x.get('rows_written') or 0:,} events"
            if x.get("merged_bytes") is not None:
                meta += " · " + U.fmt_bytes(x["merged_bytes"])
            if x.get("tenant"):
                meta += " · " + S(x["tenant"])
            items.append((x["run_dir"], colour.get(st, U.amber)(U.pad(st, 10)) + " " + U.pad(S(x["name"]), nw) + "  "
                          + U.dim(meta)))
        k = choose(f"Export history in {S(C.home_dir())}", items,
                   hint=f"showing the newest 40 of {len(rows)}" if len(rows) > 40 else None)
        if not k:
            return
        x = next(r for r in rows if r["run_dir"] == k)
        self.export_detail(x)

    def export_detail(self, x):
        print()
        print(U.clip("  " + U.bold(S(x["name"])) + "  " + U.dim(S(x.get("status")))))
        win = x.get("window") or {}
        if isinstance(win, dict) and (win.get("start") or win.get("end")):
            win = f"{win.get('start')} → {win.get('end')}"
        for label, val in (("tenant", f"{S(x.get('tenant') or '?')} ({S(x.get('tenant_id') or '?')})"),
                           ("query", S(x.get("query") or "")), ("window", S(win or "")),
                           ("events", f"{x.get('rows_written') or 0:,} of {x.get('rows_expected') or 0:,}"),
                           ("file", S(x.get("merged_file") or "(not combined yet)")
                            + ("  " + U.fmt_bytes(x["merged_bytes"]) if x.get("merged_bytes") is not None else ""))):
            print(U.clip(f"    {U.dim(label.ljust(7))} {val}"))
        if x.get("stored_duplicates"):
            U.info(f"{x['stored_duplicates']:,} duplicate copies were removed; every event is exported once")
        if x.get("coverage"):
            cov = x["coverage"]
            U.note(S(cov.get("detail") if isinstance(cov, dict) else cov))
        acts = []
        mine = self.s.busy() and (self.s.job_status() or {}).get("run_dir") == x["run_dir"]
        if mine:
            acts.append(("watch", U.blue("Watch progress")))
        elif x.get("resumable"):
            acts.append(("resume", U.teal("Resume this export")))
        elif x.get("status") in ("INCOMPLETE", "FAILED", "PLANNED") and self.s.tenant:
            U.note("Connect to the tenant this export belongs to in order to resume it.")
        acts.append(("open", "Open folder"))
        if not mine and x.get("status") != "RUNNING":
            acts.append(("delete", "Delete this export"))
        a = choose("What now?", acts)
        if a == "watch":
            self.follow(W.watch_export(self.s))
        elif a == "resume":
            self.s.require()
            self.s.resume(x["run_dir"])
            self.follow(W.watch_export(self.s, seq=0))
        elif a == "open":
            W.open_folder(x["run_dir"])
            U.pause()
        elif a == "delete" and U.confirm(f"delete {S(x['name'])} and every file in it?", False):
            import abx_web
            abx_web._export_delete(self.s, x["run_dir"])
            U.ok("deleted " + S(x["name"]))
            U.pause()

    def do_fields(self):
        self.s.require()
        prof = self.profile()
        if prof and U.confirm(f"show only the {len(prof['fields'])} fields {S(self.spec['dataset'])} uses "
                              "(with coverage)?", True):
            q = U.ask("search (part of a name, blank for all, < back)")
            rows = [f for f in prof["fields"] if q.lower() in f["name"].lower()]
            U.page("\n".join(f"  {U.bar((f.get('coverage') or 0) / 100.0, 12)} {f.get('coverage') or 0:5.1f}%  "
                             f"{S(f['name'])}" for f in rows) or "  (none)")
            if U.confirm("learn this dataset's fields again now? (samples recent events, 10–30 seconds)", False):
                W.learn_profile(self.s, self.spec["dataset"])
            U.pause()
            return
        if self.spec.get("dataset") and U.confirm(f"learn which fields {S(self.spec['dataset'])} uses now? "
                                                  "(samples recent events, 10–30 seconds)", False):
            W.learn_profile(self.s, self.spec["dataset"])
            U.pause()
            return
        fields = self.s.fields()
        q = U.ask(f"{len(fields)} fields — search (part of a name, blank for all, < back)")
        hits = [S(f) for f in fields if q.lower() in f.lower()]
        cw = min(34, max([U.vlen(h) for h in hits] + [8]) + 2)
        per = max(1, (U.width() - 4) // cw)
        lines = ["  " + "".join(U.pad(h, cw) for h in hits[i:i + per]) for i in range(0, len(hits), per)]
        U.page("\n".join(lines) or "  (none)")
        U.pause()

    def do_saved(self):
        k = choose("Saved searches", [("save", "Save the current search"), ("load", "Load a saved search"),
                                      ("delete", "Delete a saved search"),
                                      ("command", "Show the current search as a command line (for scheduling)"),
                                      ("schedule", "Run it on a schedule (cron, Task Scheduler, GitHub Actions …)")],
                   hint=f"stored in {S(os.path.join(C.home_dir(), 'saved-searches'))}; never contains your key")
        if k == "schedule":
            TP.do_automate(self)
            U.pause()
            return
        if k == "command":
            r = C.command_line(self.spec, (self.s.tenant or {}).get("vendor_account_id"))
            print()
            if r["env"]:
                print("  " + r["env"])
            print("  " + r["command"])
            U.note(r["note"])
            U.pause()
            return
        if k == "save":
            if W.save_search(self.spec):
                U.pause()
        elif k in ("load", "delete"):
            saved = C.saved_list()
            if not saved:
                print("\n  " + U.dim("no saved searches yet"))
                U.pause()
                return
            items = [(x["file"], S(x["name"]) + "  " + U.dim(S((x.get("summary") or {}).get("query", ""))
                                                            + ("   (command line only)" if x.get("mode") == "advanced" else "")))
                     for x in saved]
            f = choose("Which one?", items)
            if not f:
                return
            if k == "load":
                sp = C.saved_load(f)
                if sp.get("mode") == "advanced":
                    U.note(ADVANCED_LOAD)
                    U.pause()
                    return
                if W.unsaved_changes(self.spec) and not U.confirm(
                        "The current search has changes that are not saved. Replace it with " + S(f) + "?", False):
                    U.note("Not loaded. The current search is unchanged.")
                    U.pause()
                    return
                self.spec = sp
                U.ok("loaded " + S(f))
                U.pause()
            elif U.confirm(f"delete {S(f)}?", False):
                C.saved_delete(f)
                U.ok("deleted " + S(f))

    def do_views(self):
        """Every saved view in Abstract, read live: KQL, filter builder, or columns and a time range only. Pick one to
        use it, check them all, or save them all as saved searches."""
        self.s.require()
        print("  " + U.dim("loading this tenant's saved views…"))
        views = self.s.views(refresh=True)
        if not views:
            U.note("This tenant has no saved views.")
            U.pause()
            return
        kinds = {"kql": "KQL", "filters": "filter builder", "replay": "replay", "none": "columns only",
                 "unreadable": "could not be read"}
        n = {k: sum(1 for v in views if v.get("origin", "kql") == k) for k in kinds}
        status = {}
        while True:
            items = [("__check", U.teal("Check every view: which ones are ready, and why not")),
                     ("__sync", U.teal("Save every view as a saved search on this computer")),
                     ("__change", U.teal("Save the current search as a view, or change or delete a view"))]
            for v in views:
                st = status.get(v["id"])
                tag = "" if st is None else ("  " + U.teal("ready") if st is True else "  " + U.dim("cannot convert"))
                items.append((v["id"], S(v["name"]) + U.dim(f"  ({kinds.get(v.get('origin', 'kql'), 'KQL')})") + tag))
            words = {"kql": "KQL", "filters": "filter builder", "replay": "replay",
                     "none": "columns and a time range only", "unreadable": "could not be read"}
            hint = ", ".join(f"{n[k]} {w}" for k, w in words.items() if n[k]) + ". Pick one to use it as the current search."
            k = choose(f"Saved views from Abstract ({len(views)})", items, hint=hint)
            if k is None:
                return
            if k == "__check":
                self.check_views(views, status)
                continue
            if k == "__change":
                TP.view_actions(self, views)
                U.pause()
                views = self.s.views(refresh=True)
                continue
            if k == "__sync":
                print("  " + U.dim("saving every view…"))
                saved = C.sync_views(self.s, views)
                U.ok(f"saved {len(saved)} views as saved searches (main menu → Saved searches; or export --from-saved)")
                U.pause()
                continue
            try:
                r = self.s.import_search(view_id=k)
            except C.E.UserError as ex:
                status[k] = False
                W.problem(ex)
                U.pause()
                continue
            status[k] = True
            print()
            print("  " + U.bold("This will run as") + U.dim(f"  ({S(r['source'])})"))
            for line in r["readable"]:
                print("    " + U.teal("•") + " " + S(line))
            for n in r["notes"]:
                U.note(S(n))
            if W.unsaved_changes(self.spec) and not U.confirm(
                    "The current search has changes that are not saved. Replace it with this view?", False):
                continue
            if U.confirm("Use this search?", True):
                self.spec = r["spec"]
                U.ok("Loaded. Next: Preview, Validate, then Export from the main menu (or Guided search & export).")
                U.pause()
                return

    def check_views(self, views, status):
        """Convert every view the way an export would, without running any of them."""
        ready, refused, cols = [], [], []
        for i, v in enumerate(views, 1):
            print(U.clip("  " + U.dim(f"[{i}/{len(views)}] ") + S(v["name"])), flush=True)
            try:
                r = self.s.import_search(view_id=v["id"])
                status[v["id"]] = True
                (cols if r.get("columns_only") else ready).append(v["name"])
            except C.E.UserError as ex:
                status[v["id"]] = False
                refused.append((v["name"], str(ex)))
        lines = [U.teal(f"  {len(ready)} of {len(views)} saved views are ready to export")]
        lines += ["    " + U.teal("✓") + " " + S(n) for n in ready]
        if cols:
            lines += ["", U.bold(f"  {len(cols)} hold only columns and a time range (load one, then add a dataset or a "
                                 f"filter):")]
            lines += ["    " + U.dim("·") + " " + S(n) for n in cols]
        if refused:
            lines += ["", U.bold(f"  {len(refused)} cannot be converted exactly (nothing is dropped silently):")]
            for n, why in refused:
                lines += ["    " + U.dim("✗") + " " + S(n), "        " + U.dim(S(why))]
        U.page("\n".join(lines))
        U.pause()

    def do_learn(self):
        """Sample every dataset now to learn which fields it uses. Kept in memory for this session only."""
        self.s.require()
        if self.s.busy():
            U.note("An export is running. Learning fields is paused until it finishes, so it does not slow the "
                   "export down.")
            U.pause()
            return
        ds = self.s.datasets()
        if not U.confirm(f"Learn the fields of all {len(ds)} datasets now? This samples recent events from each "
                         f"(about 10–30 seconds per dataset; Ctrl+C stops). Nothing is written to disk.", True):
            return
        n = [0]

        def sink(line):
            n[0] += 1
            print(U.clip(U.dim(f"  [{n[0]}/{len(ds)}]") + S(line)), flush=True)
        try:
            self.s.profile_all(sink=sink, refresh=True)
        except KeyboardInterrupt:
            print()
            U.note(f"Stopped after {n[0]} of {len(ds)} datasets. Those already learned are kept for this session.")
        else:
            U.ok(f"Learned the fields of {len(ds)} datasets. The Fields step and Browse fields now show each "
                 "dataset's fields with coverage.")
        U.pause()

    def do_datasets(self):
        self.s.require()
        print("  " + U.dim("loading datasets…"))
        ds = self.s.datasets(refresh=not self.s.busy())
        w = max(12, min(48, max(U.vlen(S(d["name"])) for d in ds)))
        lines = []
        for d in ds:
            c = d.get("events_24h")
            vol = "volume unknown" if c is None else ("no events in 24h" if c == 0 else f"{c:,} events/24h")
            known = self.s._profiles.get(d["name"].casefold())
            fields = U.teal(f"  {len(known['fields'])} fields learned") if known else ""
            lines.append("  " + U.pad(S(d["name"]), w) + "  " + U.dim(vol) + fields)
        U.page(f"  {len(ds)} datasets (names match in any case; a partial name matches nothing)\n\n" + "\n".join(lines))
        U.pause()

    def do_english(self):
        TP.do_english(self)

    def do_kql(self):
        TP.do_kql(self)

    def do_fieldsets(self):
        TP.do_fieldsets(self)

    def do_content(self):
        TP.do_content(self)
        U.pause()

    def do_automate(self):
        TP.do_automate(self)
        U.pause()

    def do_tenant(self):
        W.capabilities_screen(self.s)

    def do_web(self):
        import abx_web
        print("\n  " + U.dim("starting the browser app — Ctrl+C here stops it and returns to the menu"))
        try:
            abx_web.serve(self.s, spec=self.spec)
        except KeyboardInterrupt:
            print()
        finally:
            W.forget()                                           # the browser app may have validated other searches

    def do_help(self):
        k = choose("Help", [(p, title) for p, title in C.HELP_TOPICS])
        if k:
            U.page(C.help_text(k))
            U.pause()


ADVANCED_LOAD = W.ADVANCED_MSG + " Your current search was kept."
START_MENU = False                                               # set by `abstract_search.py --menu`


def main():
    m = None
    try:
        if U.is_mintty():
            U.note(U.WINPTY_HINT + " (arrow keys and hidden key entry need a Windows console).")
        m = Menu()
        if U.INTERACTIVE and not os.environ.get("ABX_START_MENU") and not START_MENU:
            m.action(m.do_guided)                                # start in the guided flow; m / Home → menu
        if m.quit:
            return 0
        return m.run()
    except U.EndOfInput:
        if m:
            m.shutdown()
        print("  " + U.dim("no more input — exiting"))
        return 1
    except KeyboardInterrupt:
        if m:
            m.shutdown()
        print()
        return 130
    except OSError as ex:                                        # e.g. the exports folder cannot be created
        U.err("Could not start: " + C.humanize(ex) + ". Check that the exports folder ("
              + S(C.home_dir()) + ") can be created and written, or set ABX_OUT_ROOT to another folder.")
        return 2
