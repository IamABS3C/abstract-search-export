"""Third review (2026-10-07): silent zeros from a misspelled --where field or a partial dataset name, export folders
that collided, true/false --where filters, converter crashes, empty values, the tenant guard on the import path,
resume after a finished export, retention notes in warnings, and the datasets / views / searches commands."""
import contextlib
import datetime as dt
import io
import json
import os
import random
import re
import unittest

from support import C, E, EngineCase, FIXED_NOW, KEY, T, spread  # first: puts the kit on the path
import abx_kql as K  # noqa: E402
import abstract_search as A  # noqa: E402

S, END = T(hours=3), T(hours=1)


class _Data(EngineCase):
    def setUp(self):
        super().setUp()
        self.fw = spread(self.tenant, S, END, 60, "fw", user_name="alice", action="Login")
        spread(self.tenant, S, END, 30, "dns", user_name="bob", action="Query")


class SilentZeros(_Data):
    def test_misspelled_where_field_is_refused(self):
        code, res, _ = self.export("--dataset", "fw", "--where", "user_nam=alice", start=S, end=END)
        self.assertEqual(code, 2, res)
        self.assertIn("do not exist on your tenant: user_nam", res["error"])

    def test_misspelled_where_field_is_refused_by_count(self):
        code, res = self.run_engine(["count", "--dataset", "fw", "--where", "user_nam=alice", "--quiet",
                                     "--start", E.iso(S), "--end", E.iso(END)])
        self.assertEqual(code, 2, res)
        self.assertIn("user_nam", res["error"])

    def test_partial_dataset_name_is_refused_with_a_suggestion(self):
        self.tenant.extra_datasets.append("Palo Alto Firewall")
        code, res, _ = self.export("--dataset", "Palo Alto", start=S, end=END)
        self.assertEqual(code, 2, res)
        self.assertIn("Did you mean 'Palo Alto Firewall'", res["error"])

    def test_dataset_name_matches_in_any_case(self):
        code, res, _ = self.export("--dataset", "FW", start=S, end=END)
        self.assertEqual(code, 0, self.log[-6:])
        self.assertEqual(res["rows_written"], len(self.fw))

    def test_unlisted_dataset_can_be_forced(self):
        code, res, _ = self.export("--dataset", "nothing-here", "--allow-unlisted-dataset", start=S, end=END)
        self.assertEqual(code, 0, self.log[-6:])
        self.assertEqual(res["rows_written"], 0)


class Folders(_Data):
    def test_two_exports_in_the_same_second_get_their_own_folders(self):
        argv = ["export", "--dataset", "fw", "--quiet", "--start", E.iso(S), "--end", E.iso(END), "--merge"]
        c1, r1 = self.run_engine(argv + ["--where", "user_name=alice"])
        c2, r2 = self.run_engine(argv)
        self.assertEqual((c1, c2), (0, 0), self.log[-6:])
        self.assertNotEqual(r1["run_dir"], r2["run_dir"])
        self.assertEqual(self.manifest(r1["run_dir"])["query"]["where"], [["user_name", ["alice"]]])

    def test_out_refuses_a_folder_that_holds_an_export(self):
        code, _, run = self.export("--dataset", "fw", start=S, end=END, name="one")
        self.assertEqual(code, 0)
        code, res, _ = self.export("--dataset", "dns", start=S, end=END, name="one")
        self.assertEqual(code, 2)
        self.assertIn("already holds an export", res["error"])
        self.assertEqual(self.manifest(run)["query"]["dataset"], "fw")

    def test_folder_is_printed_after_the_plan(self):
        self.export("--dataset", "fw", start=S, end=END, name="p")
        self.assertTrue(any(str(l).startswith("export folder: ") for l in self.log), self.log)


class Resume(_Data):
    def test_resuming_a_finished_export_is_not_an_error(self):
        code, res, run = self.export("--dataset", "fw", start=S, end=END, name="done")
        self.assertEqual(code, 0)
        n = len(self.tenant.calls)
        code, res = self.run_engine(["export", "--resume", run, "--merge", "--quiet"])
        self.assertEqual(code, 0, res)
        self.assertTrue(res["already_complete"])
        self.assertEqual(res["rows_written"], len(self.fw))
        self.assertFalse([c for c in self.tenant.calls[n:] if "search" in c[1]], "a finished export searched again")

    def test_resume_refuses_options_that_change_the_export(self):
        code, _, run = self.export("--dataset", "fw", start=S, end=END, name="r", merge=False)
        code, res = self.run_engine(["export", "--resume", run, "--where", "action=Login", "--format", "csv"])
        self.assertEqual(code, 2)
        self.assertIn("--where", res["error"])
        self.assertIn("--format", res["error"])

    def test_cancel_reports_the_folder_to_resume(self):
        spread(self.tenant, S, END, 400, "fw")
        self.cancel_after(6)
        code, res, run = self.export("--dataset", "fw", "--page-size", "50", start=S, end=END, name="c")
        self.assertEqual(code, 130, self.log[-6:])
        self.assertEqual(res.get("run_dir"), run)
        self.assertTrue(any("export --resume" in str(l) for l in self.log), self.log[-4:])


class Booleans(_Data):
    def setUp(self):
        super().setUp()
        self.tenant.types["is_admin"] = "Boolean"
        if "is_admin" not in self.tenant.enum:
            self.tenant.enum.append("is_admin")
        for i, r in enumerate(self.fw):
            r["is_admin"] = i % 3 == 0

    def test_true_false_where_exports(self):
        code, res, _ = self.export("--dataset", "fw", "--where", "is_admin=true", start=S, end=END)
        self.assertEqual(code, 0, self.log[-6:])
        self.assertEqual(res["rows_written"], sum(1 for r in self.fw if r["is_admin"]))

    def test_true_false_where_counts(self):
        code, res = self.run_engine(["count", "--dataset", "fw", "--where", "is_admin=false", "--quiet",
                                     "--engines", "v1,dataview", "--start", E.iso(S), "--end", E.iso(END)])
        self.assertEqual(code, 0, res)
        self.assertEqual(res["engines"]["v1"]["total"], sum(1 for r in self.fw if not r["is_admin"]))


class Converter(unittest.TestCase):
    TYPES = {"user_name": "String", "dest_port": "Float64", "source_ipv4": "Ipv4", "is_admin": "Boolean"}

    def conv(self, q):
        return K.convert(q, lambda f: self.TYPES.get(f, "String"))

    def test_odd_inputs_raise_only_convert_error(self):
        for q in ["events | where data_stream.dataset == \"x\" and isnotempty(['@timestamp'])",
                  "events | where isempty(@timestamp)",
                  "events | where ['@timestamp'] in (datetime(2026-01-01), datetime(2026-01-02))",
                  "events | where ['@timestamp'] > datetime(2026-02-30)",
                  "events | where ['@timestamp'] > datetime(2026-01-01T25:00:00Z)",
                  "events | where ['@timestamp'] > datetime(0001-01-01T00:00:00+05:00)",
                  "events | where ['@timestamp'] < datetime(9999-12-31T23:00:00-05:00)",
                  "events | where " + "(" * 600 + "a == \"x\"" + ")" * 600]:
            with self.assertRaises(K.ConvertError, msg=q):
                self.conv(q)

    def test_fuzz_only_convert_error_escapes(self):
        rnd = random.Random(7)
        atoms = ["events", "|", "where", "user_name", "dest_port", "source_ipv4", "is_admin", "['@timestamp']", "==",
                 "!=", "=~", ">", "<", ">=", "in", "(", ")", ",", "and", "or", '"x"', '""', "1", "true",
                 "datetime(2026-01-01)", "datetime(2026-13-01)", "isnotempty(", "isempty(", "ipv4_is_in_range(",
                 "startswith", "project", "sort", "by", "desc", "not", "take", "10", "'y'"]
        for _ in range(3000):
            q = " ".join(rnd.choice(atoms) for _ in range(rnd.randint(1, 14)))
            try:
                self.conv(q)
            except K.ConvertError:
                pass

    def test_empty_and_padded_values_are_refused(self):
        for q, word in [('events | where user_name != ""', "isnotempty"), ('events | where user_name == ""', "isempty"),
                        ('events | where user_name == " admin "', "space"),
                        ('events | where user_name startswith "  ad"', "spaces")]:
            with self.assertRaises(K.ConvertError, msg=q) as cm:
                self.conv(q)
            self.assertIn(word, str(cm.exception), q)

    def test_at_least_and_at_most_convert_exactly(self):
        r = self.conv("events | where dest_port >= 1024 and dest_port <= 2048")
        self.assertEqual([f["op"] for f in r["filters"]], ["IS_GREATER_THAN_OR_EQUAL_TO", "IS_LESS_THAN_OR_EQUAL_TO"])


class ImportGuard(_Data):
    def test_tenant_guard_runs_before_anything_is_read(self):
        self.tenant.views = [{"id": "v1", "name": "Logins", "kql_query": 'events | where action == "Login"'}]
        E.CREDS = dict(E.CREDS, expect_tenant="SOMEONE-ELSE")
        n = len(self.tenant.calls)
        code, res, _ = self.export("--from-view", "Logins", "--expect-tenant", "SOMEONE-ELSE", start=S, end=END)
        self.assertEqual(code, 2)
        self.assertIn("TENANT MISMATCH", res["error"])
        self.assertEqual([c[1] for c in self.tenant.calls[n:]], ["/v2/auth/"])

    def test_missing_view_suggests_close_names(self):
        self.tenant.views = [{"id": "v1", "name": "Failed logins", "kql_query": 'events | where action == "Login"'}]
        code, res, _ = self.export("--from-view", "failed", start=S, end=END)
        self.assertEqual(code, 2)
        self.assertIn("Failed logins", res["error"])


class RetentionWarning(EngineCase):
    def test_retention_note_is_a_warning(self):
        self.tenant.hot_days, self.tenant.cold_days = 4, 5          # the range reaches past the archive too
        spread(self.tenant, T(days=2), T(hours=1), 50, "fw")
        code, res, _ = self.export("--dataset", "fw", start=T(days=7), end=T(hours=1), name="ret")
        self.assertEqual(code, 0, self.log[-6:])
        self.assertTrue(res["coverage_warning"])
        self.assertIn(res["coverage_warning"], res["warnings"])
        self.assertTrue(any(str(l).strip().startswith("! Retention") for l in self.log))


class CsvColumns(_Data):
    def test_repeated_fields_become_one_column_after_timestamp_and_id(self):
        code, res, run = self.export("--dataset", "fw", "--fields", "action,id,action", start=S, end=END, fmt="csv")
        self.assertEqual(code, 0, self.log[-6:])
        with open(os.path.join(run, res["merged_file"]), encoding="utf-8-sig") as fh:
            self.assertEqual(fh.readline().strip(), "@timestamp,id,action")


class Commands(_Data):
    """datasets / views / searches and export --from-saved, as a customer runs them."""

    def setUp(self):
        super().setUp()
        os.environ.update({"ABSTRACT_API_KEY": KEY, "ABSTRACT_VENDOR_ACCOUNT_ID": self.tenant.vid,
                           "ABSTRACT_EXPECT_TENANT": self.tenant.vid, "ABSTRACT_BASE_URL": "https://api.fake.example",
                           "ABX_OUT_ROOT": self.home})
        self._home = C.home_dir
        C.home_dir = lambda: self.home

    def tearDown(self):
        C._kql_off()
        C.home_dir = self._home
        for k in ("ABSTRACT_API_KEY", "ABSTRACT_VENDOR_ACCOUNT_ID", "ABSTRACT_EXPECT_TENANT", "ABSTRACT_BASE_URL"):
            os.environ.pop(k, None)
        super().tearDown()

    def run_cli(self, *argv):
        os.environ["ABSTRACT_API_KEY"] = KEY                    # the session removes it after reading it
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = A.main(list(argv))
        return code, out.getvalue()

    def test_datasets_lists_every_dataset(self):
        self.tenant.extra_datasets.append("Quiet Feed")
        code, out = self.run_cli("datasets", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(d["name"] for d in json.loads(out)), ["Quiet Feed", "dns", "fw"])

    def test_views_lists_every_view_and_whether_it_converts(self):
        self.tenant.views = [{"id": "v1", "name": "Logins", "kql_query": 'events | where action == "Login"'},
                             {"id": "v2", "name": "Either", "kql_query": 'events | where strlen(user_name) > 3'},
                             {"id": "v3", "name": "Empty", "kql_query": None}]
        self.tenant.ignore_kql = True                          # a tenant where KQL is not applied
        code, out = self.run_cli("views", "--json")
        self.assertEqual(code, 0)
        j = json.loads(out)
        self.assertEqual({v["name"]: (v["converts"], v["loads"]) for v in j["views"]},
                         {"Logins": (True, True), "Either": (False, False), "Empty": (False, True)})
        self.assertIn("strlen", [v for v in j["views"] if v["name"] == "Either"][0]["reason"])
        self.assertEqual(j["views_without_a_search"], 1)
        self.tenant.ignore_kql = False                         # where KQL is applied, the same view runs as KQL
        code, out = self.run_cli("views", "--json")
        either = [v for v in json.loads(out)["views"] if v["name"] == "Either"][0]
        self.assertTrue(either["converts"])
        self.assertTrue(either["runs_as"][0].startswith("runs as KQL"))

    def test_searches_and_from_saved(self):
        C.saved_save(dict(C.empty_spec(), name="my fw", dataset="fw", filters=[
            {"field": "user_name", "op": "IS_IN_LIST", "type": "String", "value": "alice"}],
            time={"start": E.iso(S), "end": E.iso(END)}, fields=["@timestamp", "id", "action"]))
        code, out = self.run_cli("searches", "--json")
        self.assertEqual(code, 0)
        self.assertIn("my fw", [x["name"] for x in json.loads(out)])
        argv = A.from_saved_argv(["export", "--from-saved", "my fw", "--merge"])
        self.assertEqual(argv[0], "export")
        self.assertIn("--dataset", argv)
        self.assertIn("--start", argv)
        code, res = self.run_engine(argv + ["--quiet"])
        self.assertEqual(code, 0, self.log[-6:])
        self.assertEqual(res["rows_written"], len(self.fw))

    def test_from_saved_refuses_a_second_search(self):
        C.saved_save(dict(C.empty_spec(), name="s1", dataset="fw"))
        with self.assertRaises(ValueError):
            A.from_saved_argv(["export", "--from-saved", "s1", "--dataset", "dns"])

    def test_capabilities_preview_validate_and_exports(self):
        code, out = self.run_cli("capabilities", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["kql"])
        win = ["--start", E.iso(S), "--end", E.iso(END)]
        code, out = self.run_cli("preview", "--dataset", "fw", "--where", "user_name=alice", "--json", *win)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["total"], len(self.fw))
        code, out = self.run_cli("validate", "--dataset", "fw", "--json", *win)
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["ready"])
        code, out = self.run_cli("validate", "--dataset", "f", "--json", *win)
        self.assertEqual(code, 2)                               # a partial name is refused, as by export
        self.export("--dataset", "fw", start=S, end=END, name="one")
        code, out = self.run_cli("exports", "--json")
        self.assertEqual([x["name"] for x in json.loads(out)], ["one"])
        code, out = self.run_cli("exports", "--delete", "one")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.home, "one")))

    def test_preview_and_validate_take_an_ext_filter_like_export(self):
        self.tenant.enum = self.tenant.enum + ["ext"]
        for i, r in enumerate(self.fw):
            r["ext"] = {"ver": "1.09" if i % 5 == 0 else "1.11"}
        win = ["--start", E.iso(S), "--end", E.iso(END)]
        want = sum(1 for r in self.fw if r["ext"]["ver"] == "1.09")
        code, out = self.run_cli("preview", "--dataset", "fw", "--where", "ext.ver=1.09", "--json", *win)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["total"], want)
        code, out = self.run_cli("validate", "--dataset", "fw", "--where", "ext.ver=1.09", "--json", *win)
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["ready"])

    def test_advanced_options_are_hidden_but_still_work(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            E.build_parser().parse_args(["export", "--help"])
        self.assertNotIn("--engine", out.getvalue())
        self.assertNotIn("--page-size", out.getvalue())
        code, res, _ = self.export("--dataset", "fw", "--engine", "dataview", "--page-size", "50", start=S, end=END,
                                   name="adv")
        self.assertEqual(code, 0, self.log[-6:])
        os.environ["ABX_ADVANCED"] = "1"
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                E.build_parser().parse_args(["export", "--help"])
            self.assertIn("--engine", out.getvalue())
        finally:
            os.environ.pop("ABX_ADVANCED", None)

    def test_fields_refuses_a_partial_dataset_name(self):
        code, _ = self.run_cli("fields", "--dataset", "f")
        self.assertEqual(code, 2)


class FollowUps(_Data):
    """Found by the documentation pass after the first round of fixes."""

    def test_missing_part_files_are_fetched_again_on_resume(self):
        code, res, run = self.export("--dataset", "fw", start=S, end=END, name="parts", merge=False)
        self.assertEqual(code, 0)
        man = self.manifest(run)
        os.remove(os.path.join(run, man["slices"][0]["file"]))
        code, res = self.run_engine(["export", "--resume", run, "--merge", "--quiet"])
        self.assertEqual(code, 0, self.log[-6:])
        self.assertEqual(res["rows_written"], len(self.fw))

    def test_out_refuses_a_folder_with_other_files(self):
        d = os.path.join(self.home, "busy")
        os.makedirs(d)
        with open(os.path.join(d, "notes.txt"), "w") as fh:
            fh.write("x")
        code, res = self.run_engine(["export", "--dataset", "fw", "--out", d, "--quiet",
                                     "--start", E.iso(S), "--end", E.iso(END)])
        self.assertEqual(code, 2)
        self.assertIn("not empty", res["error"])

    def test_resume_refuses_the_default_format_when_the_export_used_another(self):
        code, _, run = self.export("--dataset", "fw", start=S, end=END, name="csvrun", fmt="csv", merge=False)
        code, res = self.run_engine(["export", "--resume", run, "--format", "ndjson.gz"])
        self.assertEqual(code, 2)
        self.assertIn("--format", res["error"])

    def test_saved_search_loads_by_the_name_lists_show(self):
        old = C.home_dir
        C.home_dir = lambda: self.home
        try:
            names = [x["name"] for x in C.saved_list()]          # the copied examples
            self.assertTrue(names)
            self.assertEqual(C.saved_load(names[0])["name"], names[0])
        finally:
            C.home_dir = old

    def test_key_is_removed_even_without_an_account_id(self):
        os.environ["ABSTRACT_API_KEY"] = KEY
        os.environ.pop("ABSTRACT_VENDOR_ACCOUNT_ID", None)
        self.assertIsNone(C.Session().connect_from_env())
        self.assertNotIn("ABSTRACT_API_KEY", os.environ)


class Branding(unittest.TestCase):
    def test_default_terminal_size_shows_the_full_lockup(self):
        import abx_brand
        import abx_ui as U
        saved = {k: getattr(U, k) for k in ("PLAIN", "STDOUT_TTY", "COLOR_LEVEL", "UNICODE", "cols", "rows",
                                            "clear", "_stream_can_encode")}
        try:
            U.PLAIN, U.STDOUT_TTY, U.COLOR_LEVEL, U.UNICODE = False, True, 256, True
            U.cols, U.rows, U.clear = (lambda: 80), (lambda: 24), (lambda: None)
            U._stream_can_encode = lambda *a: True
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                U.header("v0", None)
            text = re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue())
            lockup = re.sub(r"\x1b\[[0-9;]*m", "", abx_brand.LOCKUP[2]).strip()
            self.assertIn(lockup, text)                     # the wordmark, not only the A
        finally:
            for k, v in saved.items():
                setattr(U, k, v)


class Capabilities(_Data):
    """The tenant check decides by behaviour: a product flag that says KQL is on does not make it so."""

    def connect(self):
        s = C.Session()
        s.connect("https://api.fake.example", self.tenant.vid, KEY)
        return s

    def tearDown(self):
        C._kql_off()                                            # module state: never leak into other tests
        super().tearDown()

    def test_kql_applied_archive_and_full_events(self):
        caps = self.connect().capabilities()
        self.assertEqual((caps["kql"], caps["archive"], caps["full_events"]), (True, True, True))
        self.assertTrue(C.KQL_ON)

    def test_flag_on_but_kql_ignored_reads_not_available(self):
        self.tenant.ignore_kql = True
        self.tenant.flags["enable_kql_search_mode"] = True
        s = self.connect()
        caps = s.capabilities()
        self.assertIs(caps["kql"], False)
        self.assertFalse(C.KQL_ON)
        self.assertEqual([i["status"] for i in caps["items"] if i["key"] == "kql"], ["not available"])

    def test_full_event_search_not_enabled(self):
        self.tenant.raw_enabled = False
        self.assertIs(self.connect().capabilities()["full_events"], False)

    def test_kql_that_limits_rows_is_refused_even_where_kql_works(self):
        s = self.connect()
        s.capabilities()
        with self.assertRaises(E.UserError) as cm:
            s.import_search(text='events | where action == "Login" or action == "Query" | take 10')
        self.assertIn("| take", str(cm.exception))
        code, res, _ = self.export("--kql", 'events | where action == "Login" | take 5', start=S, end=END, name="tk")
        self.assertEqual(code, 2)
        self.assertIn("| take", res["error"])

    def test_unconvertible_search_runs_as_kql_only_where_kql_works(self):
        text = 'events | where strlen(user_name) > 3'
        s = self.connect()
        with self.assertRaises(E.UserError):
            s.import_search(text=text)                          # not checked yet: converted or refused
        s.capabilities()
        r = s.import_search(text=text)
        self.assertEqual(r["spec"]["mode"], "kql")
        self.assertIn("--kql", C.query_args(r["spec"]))
        s.disconnect()
        self.assertFalse(C.KQL_ON)


class Storage(EngineCase):
    """One choice of where to search, in every front end: auto, recent data only, archive."""
    RS, RE = T(days=3), T(hours=1)

    def setUp(self):
        super().setUp()
        n = FIXED_NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        self.tenant.hot_days = 4                                    # older events are in the archive only
        self.tenant.hot_since = n - dt.timedelta(days=2)
        self.fw = spread(self.tenant, self.RS, self.RE, 300, "fw")

    def test_auto_reads_the_archive_for_the_older_part(self):
        code, res, _ = self.export("--dataset", "fw", start=self.RS, end=self.RE)
        self.assertEqual(code, 0, self.log[-6:])
        self.assertEqual(res["rows_written"], len(self.fw))

    def test_recent_only_skips_the_archive_and_says_so(self):
        code, res, run = self.export("--dataset", "fw", "--storage", "recent", start=self.RS, end=self.RE)
        self.assertEqual(code, 0, self.log[-6:])
        hot = [r for r in self.fw if E.parse_ts(r["@timestamp"]) >= self.tenant.hot_since]
        self.assertEqual(res["rows_written"], len(hot))
        self.assertTrue(any(w.startswith("Recent data only") for w in res["warnings"]), res["warnings"])
        self.assertFalse(any(c[1].startswith("/v1/coldstorage") and c[0] == "POST" for c in self.tenant.calls))

    def test_archive_searches_archive_storage_for_the_whole_range(self):
        code, res, run = self.export("--dataset", "fw", "--storage", "archive", start=self.RS, end=self.RE)
        self.assertEqual(code, 0, self.log[-6:])
        self.assertEqual(res["rows_written"], len(self.fw))
        self.assertEqual({x["engine"] for x in self.manifest(run)["slices"]}, {"coldquery"})

    def test_storage_is_part_of_the_saved_search(self):
        sp = C.normalize_spec(dict(C.empty_spec(), dataset="fw", storage="recent"))
        self.assertEqual(C.query_args(sp)[-2:], ["--storage", "recent"])
        self.assertEqual(C.normalize_spec({"storage": "nonsense"})["storage"], "auto")


class MatchAny(_Data):
    """Filters where at least one must match, the same in the browser, terminal and command line."""

    def test_command_line_match_any(self):
        for i, r in enumerate(self.fw):
            r["user_name"] = ["alice", "carol", "dave"][i % 3]
        code, res, _ = self.export("--dataset", "fw", "--where", "user_name=alice", "--where", "user_name=carol",
                                   "--match", "any", start=S, end=END)
        self.assertEqual(code, 0, self.log[-6:])
        self.assertEqual(res["rows_written"], sum(1 for r in self.fw if r["user_name"] in ("alice", "carol")))

    def test_saved_search_match_any_runs_as_one_or_group(self):
        sp = C.normalize_spec(dict(C.empty_spec(), dataset="fw", match="any", filters=[
            {"field": "user_name", "op": "IS_IN_LIST", "type": "String", "value": "alice"},
            {"field": "action", "op": "IS_IN_LIST", "type": "String", "value": "Query"}]))
        cond = C.build_condition(sp)
        self.assertEqual(cond["groups"][0]["operator"], "or")
        self.assertIn("(", C.describe_spec(sp)["query"])
        sp["time"] = {"start": E.iso(S), "end": E.iso(END)}
        code, res = self.run_engine(["export", "--quiet", "--format", "ndjson", "--merge"] + C.query_args(sp)
                                    + C.window_args(sp) + C.fields_args(sp))
        self.assertEqual(code, 0, self.log[-6:])
        self.assertEqual(res["rows_written"], len(self.fw))      # every fw event is alice (dns ones are excluded)


class CommandLine(_Data):
    """Copy as command line: the generated command exports exactly what the app would."""

    def test_generated_command_matches_the_app(self):
        import shlex
        for i, r in enumerate(self.fw):
            r["user_name"] = ["alice", "carol", "dave"][i % 3]
        sp = C.normalize_spec(dict(C.empty_spec(), dataset="fw", match="any", time={"start": E.iso(S), "end": E.iso(END)},
                                   filters=[{"field": "user_name", "op": "IS_IN_LIST", "type": "String", "value": "alice"},
                                            {"field": "user_name", "op": "IS_IN_LIST", "type": "String", "value": "dave"}]))
        r = C.command_line(sp, self.tenant.vid)
        self.assertIn("--match any", r["command"])
        argv = shlex.split(r["command"])[2:] + ["--quiet"]
        code, res = self.run_engine(argv)
        self.assertEqual(code, 0, self.log[-6:])
        self.assertEqual(res["rows_written"], sum(1 for x in self.fw if x["user_name"] in ("alice", "dave")))

    def test_other_operators_run_from_the_saved_search_and_never_name_hidden_options(self):
        sp = C.normalize_spec(dict(C.empty_spec(), dataset="fw", filters=[
            {"field": "user_name", "op": "STARTS_WITH", "type": "String", "value": "al"}]))
        r = C.command_line(sp)
        self.assertIn("--from-saved", r["command"])
        self.assertNotIn("--condition", r["command"])
        self.assertIn("Save this search first", r["note"])
        self.assertIn("'@timestamp,id", C.command_line(C.normalize_spec(dict(C.empty_spec(), dataset="fw")))["command"])


def _command_line(*argv):
    """A command line as the person would type it on this system: POSIX quoting, or Windows quoting on Windows."""
    if os.name == "nt":
        import subprocess
        return subprocess.list2cmdline(argv)
    import shlex
    return " ".join(shlex.quote(a) for a in argv)


class KeyFromSecretManager(_Data):
    """ABSTRACT_API_KEY_COMMAND: the key is read from a secret manager when needed, never from a file."""

    def tearDown(self):
        for k in ("ABSTRACT_API_KEY_COMMAND", "ABSTRACT_VENDOR_ACCOUNT_ID", "ABSTRACT_API_KEY", "ABSTRACT_BASE_URL"):
            os.environ.pop(k, None)
        E._KEY_FROM_COMMAND.clear()
        super().tearDown()

    def test_key_command_connects_and_the_key_is_not_left_in_the_environment(self):
        import sys
        os.environ["ABSTRACT_API_KEY_COMMAND"] = _command_line(sys.executable, "-c", f"print({KEY!r})")
        os.environ["ABSTRACT_VENDOR_ACCOUNT_ID"] = self.tenant.vid
        os.environ["ABSTRACT_BASE_URL"] = "https://api.fake.example"
        s = C.Session()
        self.assertEqual(s.connect_from_env()["vendor_account_id"], self.tenant.vid)
        self.assertNotIn("ABSTRACT_API_KEY", os.environ)

    def test_a_failing_command_is_reported_without_its_output(self):
        import sys
        os.environ["ABSTRACT_API_KEY_COMMAND"] = _command_line(
            sys.executable, "-c", "import sys; print('secret-ish-output'); sys.exit(3)")
        os.environ["ABSTRACT_VENDOR_ACCOUNT_ID"] = self.tenant.vid
        with self.assertRaises(E.UserError) as cm:
            C.Session().connect_from_env()
        self.assertIn("exit 3", str(cm.exception))
        self.assertNotIn("secret-ish-output", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
