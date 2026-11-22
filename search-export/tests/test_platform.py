"""KQL checks and native KQL, plain English to KQL, fieldsets and saved views (plan, confirm, apply), and the endpoint
list. Every value is invented."""
import contextlib
import glob
import io
import json
import os
import unittest

from support import C, E, EngineCase, KEY, T, spread

import abx_platform as P
import abstract_search as A

S, END = T(hours=3), T(hours=1)


class _Base(EngineCase):
    def setUp(self):
        super().setUp()
        self.tenant.types.update({"user_name": "String", "action": "String"})
        self.rows = spread(self.tenant, S, END, 60, "fw")
        for i, r in enumerate(self.rows):
            r["user_name"] = ["alice@example.com", "bob@example.org", "svc@example.com"][i % 3]
            r["action"] = ["Login", "Logout"][i % 2]
        spread(self.tenant, S, END, 20, "dns")
        self.s = C.Session()
        self.s.connect("https://api.fake.example", self.tenant.vid, KEY)
        self.win = {"start": E.iso(S)[:19] + "Z", "end": E.iso(END)[:19] + "Z"}


class KqlCheck(_Base):
    def test_a_filter_query_converts_and_exports_exactly(self):
        c = self.s.kql_check('events | where data_stream.dataset == "fw" and action == "Login"')
        self.assertEqual(c["runs_as"], "filters")
        self.assertEqual(c["dataset"], "fw")
        self.assertTrue(c["filters"])

    def test_a_bare_condition_gets_events_in_front(self):
        self.assertTrue(self.s.kql_check('action == "Login"')["kql"].startswith("events | where "))

    def test_a_summarize_runs_as_a_result_table_where_kql_applies(self):
        c = self.s.kql_check("events | where action == \"Login\" | summarize n = count() by user_name")
        self.assertEqual(c["runs_as"], "kql_result")
        self.assertEqual((c["server"]["refusal"] or {}).get("construct"), "summarize")

    def test_a_project_that_computes_columns_is_a_result_table(self):
        c = self.s.kql_check('events | where action == "Login" | project user_name, n = strlen(user_name)')
        self.assertEqual(c["runs_as"], "kql_result")
        self.assertIn("computes new columns", c["reason"])

    def test_late_arriving_events_do_not_make_the_kql_check_undecided(self):
        seen = {"n": 0}
        late = T(minutes=30)

        def hook(tenant, method, path, body):           # one event lands between the probe's counts, as on a busy tenant
            if path == "/v1/dataview/search":
                seen["n"] += 1
                if seen["n"] == 2:
                    tenant.add(late, dataset="fw")
        spread(self.tenant, T(minutes=70), T(minutes=11), 50, "fw")
        self.tenant.on_request = hook
        self.assertTrue(self.s.kql_on())

    def test_a_summarize_is_refused_where_kql_is_ignored(self):
        self.tenant.ignore_kql = True                       # the tenant returns everything for any KQL: not applied
        c = self.s.kql_check("events | summarize n = count() by user_name")
        self.assertEqual(c["runs_as"], "refused")
        self.assertIn("not available", c["reason"])

    def test_the_platform_validator_refusal_is_shown_with_its_suggestion(self):
        self.tenant.kql_violations = [{"kind": "unknown_field", "reason": "field `usr` is not in the index schema",
                                       "suggestion": "did you mean 'user_name'?", "field": "usr"}]
        c = self.s.kql_check('events | where usr == "x"')
        self.assertEqual(c["runs_as"], "refused")
        self.assertFalse(c["valid"])
        self.assertIn("user_name", c["reason"])

    def test_a_result_table_preview_and_save(self):
        r = self.s.kql_preview('events | where action == "Login" | summarize n = count() by user_name', self.win)
        self.assertEqual(r["runs_as"], "kql_result")
        self.assertEqual(sum(x["n"] for x in r["rows"]), 30)
        saved = self.s.kql_result_export('events | where action == "Login" | summarize n = count() by user_name',
                                         self.win, "csv", "logins by user")
        self.assertTrue(os.path.isfile(saved["file"]))
        with open(saved["file"], encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("user_name", text.splitlines()[0])
        self.assertNotIn(KEY, text)


class ResultTableFolders(_Base):
    def test_a_saved_result_table_is_not_listed_as_a_broken_export(self):
        saved = self.s.kql_result_export('events | where action == "Login" | summarize n = count() by user_name',
                                         self.win, "json", "result")
        names = [os.path.basename(x.get("run_dir", "")) for x in C.list_exports(None)]
        self.assertNotIn(os.path.basename(saved["folder"]), names)
        code, out, err = Cli.cli(self, "exports", "--json")
        self.assertEqual(code, 0, err)
        self.assertNotIn(os.path.basename(saved["folder"]), out)


class PlainEnglish(_Base):
    def test_the_assistant_writes_kql_which_is_checked_but_never_run(self):
        self.tenant.nl_answers["logins by alice"] = {"kql": 'events | where user_name == "alice@example.com" and '
                                                             'action == "Login"', "explanation": "Alice's logins."}
        before = sum(1 for c in self.tenant.calls if c[1] == "/v1/dataview/search")
        r = self.s.nl_to_kql("logins by alice")
        self.assertEqual(r["check"]["runs_as"], "filters")
        self.assertEqual(r["explanation"], "Alice's logins.")
        after = sum(1 for c in self.tenant.calls if c[1] == "/v1/dataview/search" and
                    "alice" in json.dumps(c[2] or {}))
        self.assertEqual(after, 0, "the generated query must not run until the user asks")
        del before

    def test_no_query_comes_back_with_the_reason(self):
        r = self.s.nl_to_kql("group firewall denies by source")
        self.assertEqual(r["kql"], "")
        self.assertIn("could not", r["explanation"])

    def test_not_available_is_said_plainly(self):
        self.tenant.nl_enabled = False
        with self.assertRaises(E.UserError) as cm:
            self.s.nl_to_kql("anything")
        self.assertIn("not available on this tenant", str(cm.exception))


class Fieldsets(_Base):
    def test_create_needs_a_plan_then_one_confirm(self):
        plan = self.s.fieldset_plan("create", {"name": "Analyst", "fields": ["@timestamp", "user_name", "ext.role"]})
        self.assertEqual(self.tenant.fieldsets, {}, "a plan changes nothing")
        self.assertEqual(plan["request"], "POST /v1/streamviewer/field-set")
        r = self.s.apply_plan(plan["token"])
        self.assertEqual(self.tenant.fieldsets[r["id"]]["fields"], ["@timestamp", "user_name", "ext.role"])
        with self.assertRaises(E.UserError):
            self.s.apply_plan(plan["token"])                # single use

    def test_every_review_says_who_will_see_it(self):
        import abx_platform as P
        priv = self.s.fieldset_plan("create", {"name": "A", "fields": ["@timestamp"]})
        self.assertTrue(priv["visibility"].startswith("Only you will see this"))
        pub = self.s.fieldset_plan("create", {"name": "B", "fields": ["@timestamp"], "share_level": "public"})
        self.assertEqual(pub["visibility"], "Everyone on the tenant will see this.")
        fid = self.s.apply_plan(priv["token"])["id"]
        upd = self.s.fieldset_plan("update", {"name": "A2"}, fid)
        self.assertEqual(upd["visibility"], "This changes it for everyone who can see it.")
        shared = self.s.fieldset_plan("update", {"share_level": "public"}, fid)
        self.assertIn("Everyone on the tenant will see this.", shared["visibility"])
        self.assertEqual(self.s.fieldset_plan("delete", None, fid)["visibility"], P.SEEN_CHANGE)
        spec = C.normalize_spec(dict(C.empty_spec(), name="V", dataset="fw", time={"last": "1h"}))
        self.assertEqual(self.s.view_plan("create", spec)["visibility"], P.SEEN_VIEW)

    def test_a_computed_column_or_unknown_field_is_refused(self):
        with self.assertRaises(E.UserError) as cm:
            self.s.fieldset_plan("create", {"name": "x", "fields": ["@timestamp", "region"]})
        self.assertIn("computed column", str(cm.exception))
        with self.assertRaises(E.UserError):
            self.s.fieldset_plan("create", {"name": "x", "fields": ["user.nam"]})

    def test_update_shows_what_is_added_and_removed(self):
        fid = self.s.apply_plan(self.s.fieldset_plan("create", {"name": "A", "fields": ["@timestamp", "action"]})["token"])["id"]
        plan = self.s.fieldset_plan("update", {"fields": ["@timestamp", "user_name"]}, fid)
        ch = next(c for c in plan["changes"] if c["key"] == "fields")
        self.assertEqual((ch["added"], ch["removed"]), (["user_name"], ["action"]))
        self.assertEqual(plan["body"], {"fields": ["@timestamp", "user_name"]})
        self.s.apply_plan(plan["token"])
        self.assertEqual(self.tenant.fieldsets[fid]["fields"], ["@timestamp", "user_name"])

    def test_a_change_made_by_someone_else_after_the_review_is_refused(self):
        fid = self.s.apply_plan(self.s.fieldset_plan("create", {"name": "A", "fields": ["@timestamp"]})["token"])["id"]
        plan = self.s.fieldset_plan("update", {"name": "B"}, fid)
        self.tenant.fieldsets[fid]["updated_at"] = "2026-11-20T09:09:09Z"
        with self.assertRaises(E.UserError) as cm:
            self.s.apply_plan(plan["token"])
        self.assertIn("changed by someone else", str(cm.exception))

    def test_delete_and_the_change_log(self):
        fid = self.s.apply_plan(self.s.fieldset_plan("create", {"name": "A", "fields": ["@timestamp"]})["token"])["id"]
        self.s.apply_plan(self.s.fieldset_plan("delete", None, fid)["token"])
        self.assertNotIn(fid, self.tenant.fieldsets)
        logs = glob.glob(os.path.join(E.log_dir(), "changes-*.log"))
        self.assertTrue(logs)
        with open(logs[0], encoding="utf-8") as fh:
            lines = [json.loads(x) for x in fh]
        self.assertEqual([x["op"] for x in lines][-2:], ["create", "delete"])
        self.assertNotIn(KEY, json.dumps(lines))


class Views(_Base):
    def test_save_a_search_as_a_view(self):
        spec = C.normalize_spec(dict(C.empty_spec(), name="Logins", dataset="fw",
                                     filters=[{"field": "action", "op": "IS_IN_LIST", "value": "Login", "type": "String"}],
                                     time={"last": "24h"}, fields=["@timestamp", "id", "user_name"]))
        plan = self.s.view_plan("create", spec)
        body = plan["body"]
        self.assertEqual(body["name"], "Logins")
        self.assertNotIn("condition", body)              # measured: a view keeps nothing sent as condition
        self.assertEqual(body["query"][0]["value"][0]["field_operation"], "IS_IN_LIST")
        self.assertEqual(body["time_selection"]["type"], "Relative")
        self.assertEqual(body["time_selection"]["numeric_value"], 24)
        self.assertNotIn("id", body["fields"])
        vid = self.s.apply_plan(plan["token"])["id"]
        self.assertTrue(any(v["id"] == vid for v in self.tenant.views))
        upd = self.s.view_plan("update", None, vid, name="Logins (fw)")
        self.assertEqual(upd["body"], {"name": "Logins (fw)"})
        self.s.apply_plan(upd["token"])
        self.s.apply_plan(self.s.view_plan("delete", id_=vid)["token"])
        self.assertFalse(any(v["id"] == vid for v in self.tenant.views))

    def test_a_saved_builder_view_loads_back_as_the_same_search(self):
        spec = C.normalize_spec(dict(C.empty_spec(), name="Round trip", dataset="fw",
                                     filters=[{"field": "action", "op": "IS_IN_LIST", "value": "Login", "type": "String"},
                                              {"field": "user_name", "op": "DOES_NOT_CONTAIN", "value": "svc",
                                               "type": "String", "or_empty": True}],
                                     time={"start": self.win["start"], "end": self.win["end"]},
                                     fields=["@timestamp", "id", "user_name"]))
        self.tenant.kql_validate = True
        vid = self.s.apply_plan(self.s.view_plan("create", spec)["token"])["id"]
        back = self.s.import_search(view_id=vid)["spec"]
        self.assertEqual(self.s.preview(back, 5)["total"], self.s.preview(spec, 5)["total"])
        self.assertEqual(back["time"], spec["time"])

    def test_a_kql_view_keeps_its_kql(self):
        spec = dict(C.empty_spec(), name="KQL", mode="kql", kql='events | where action == "Login"', time={"last": "1h"})
        self.s.kql_on()
        plan = self.s.view_plan("create", spec)
        self.assertEqual(plan["body"]["kql_query"], 'events | where action == "Login"')
        self.assertNotIn("query", plan["body"])


class Cli(_Base):
    def setUp(self):
        super().setUp()
        E.LOG_SINK = None                                  # the result line reaches standard output, as on a terminal

    def cli(self, *argv):
        os.environ.update({"ABSTRACT_API_KEY": KEY, "ABSTRACT_VENDOR_ACCOUNT_ID": self.tenant.vid,
                           "ABSTRACT_EXPECT_TENANT": self.tenant.vid, "ABSTRACT_BASE_URL": "https://api.fake.example"})
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = A.main(list(argv))
        finally:
            for k in ("ABSTRACT_API_KEY", "ABSTRACT_VENDOR_ACCOUNT_ID", "ABSTRACT_EXPECT_TENANT", "ABSTRACT_BASE_URL"):
                os.environ.pop(k, None)
        return code, out.getvalue(), err.getvalue()

    def test_a_write_without_yes_only_shows_the_plan(self):
        code, out, err = self.cli("fieldsets", "create", "--name", "Ops", "--fields", "@timestamp,action")
        self.assertEqual(code, 3, err)
        self.assertIn("Nothing was changed", out)
        self.assertEqual(self.tenant.fieldsets, {})
        code, out, err = self.cli("fieldsets", "create", "--name", "Ops", "--fields", "@timestamp,action", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.tenant.fieldsets), 1)
        code, out, err = self.cli("fieldsets", "--json")
        self.assertEqual(json.loads(out)[0]["name"], "Ops")

    def test_kql_check_and_export_with_kql(self):
        code, out, err = self.cli("kql", "check", 'events | where data_stream.dataset == "fw" and action == "Login"', "--json")
        self.assertEqual(json.loads(out)["runs_as"], "filters")
        run = os.path.join(self.home, "k1")
        code, out, err = self.cli("export", "--kql", 'events | where data_stream.dataset == "fw" and action == "Login"',
                                  "--start", E.iso(S), "--end", E.iso(END), "--out", run, "--format", "ndjson",
                                  "--merge", "--quiet")
        self.assertEqual(code, 0, err[-600:])
        res = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(res["verdict"], "COMPLETE")
        self.assertEqual(res["rows_written"], 30)

    def test_export_refuses_a_result_table_query(self):
        code, out, err = self.cli("export", "--kql", "events | summarize n = count() by user_name", "--last", "1h",
                                  "--out", os.path.join(self.home, "k2"), "--quiet")
        self.assertEqual(code, 2)
        self.assertIn("kql save", err)

    def test_endpoints_lists_what_was_used(self):
        self.s.kql_check('action == "Login"')
        rows = {(r["method"], r["path"]): r for r in self.s.endpoints()}
        self.assertEqual(rows[("POST", "/v1/kql/to-conditions")]["status"], "ok")
        self.assertEqual(rows[("POST", "/v1/streamviewer/field-set")]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
