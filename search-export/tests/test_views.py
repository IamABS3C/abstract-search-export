"""Every saved view loads: KQL views, views built with Abstract's filter builder (their conditions are only in the
single-view record), and views that hold only columns and a time range. The shapes follow real saved views; every
value is invented."""
import contextlib
import io
import json
import os
import unittest

from support import C, E, EngineCase, KEY, T, spread

import abx_kql as K
import abstract_search as A

S, END = T(hours=3), T(hours=1)


def leaf(field, op, value, ftype="String", **kw):
    out = {"id": "n-" + field, "depth": 0, "field": field, "index": 0, "value": value, "parentId": None,
           "fieldType": ftype, "field_operation": op, "subFieldOperation": "", "eventConditionType": "condition"}
    out.update(kw)
    return out


def group(op, *items):
    return {"id": "g", "operator": op, "value": list(items)}


RANGE = {"type": "Range", "start_date": E.iso(S)[:23] + "000Z", "end_date": E.iso(END)[:23] + "000Z",
         "numeric_value": None, "option_selected": None, "end_numeric_value": None, "end_option_selected": None}
RELATIVE = {"type": "Relative", "numeric_value": 24, "option_selected": "hours", "start_date": None, "end_date": None}


class _Views(EngineCase):
    def setUp(self):
        super().setUp()
        self.tenant.types.update({"user_name": "String", "action": "String", "source_ipv4": "Ipv4", "tags": "List(String)",
                                  "ext.user_type": "String"})
        self.tenant.enum = self.tenant.enum + ["source_ipv4", "tags", "ext"]
        self.fw = spread(self.tenant, S, END, 60, "fw")
        for i, r in enumerate(self.fw):
            r["user_name"] = ["alice@corp.com", "bob@partner.io", "svc-backup@corp.com", None][i % 4]
            r["action"] = ["Login", "Logout", "ContentTransfer"][i % 3]
            r["source_ipv4"] = ["10.1.2.3", "52.1.1.1"][i % 2]
            r["tags"] = [["prod"], []][i % 2]
            r["ext"] = {"user_type": ["Standard", "AutomatedProcess"][i % 2]}
        spread(self.tenant, S, END, 20, "dns")
        self.tenant.views = [
            {"id": "vf1", "name": "Builder: transfers by people", "kql_query": None, "time_selection": RANGE,
             "fields": ["@timestamp", "user_name", "action"],
             "query": [group("and", leaf("data_stream.dataset", "EQUALS", "FW"), leaf("action", "EQUALS", "contenttransfer"),
                             leaf("user_name", "CONTAINS", "corp"))]},
            {"id": "vf2", "name": "Builder: either", "kql_query": None, "time_selection": RANGE, "fields": [],
             "query": [{"id": "g0", "operator": "and", "value": [
                 group("and", leaf("data_stream.dataset", "EQUALS", "fw")),
                 group("or", leaf("action", "EQUALS", "Logout"), leaf("source_ipv4", "EQUALS", "52.1.1.1", "Ipv4"))]}]},
            {"id": "vf3", "name": "Builder: list", "kql_query": None, "time_selection": RELATIVE, "fields": [],
             "query": [group("and", leaf("data_stream.dataset", "EQUALS", "fw"),
                             leaf("user_name", "IS_IN_LIST", "alice@corp.com, bob@partner.io"))]},
            {"id": "vc", "name": "Columns only", "kql_query": None, "time_selection": RELATIVE, "fields": ["@timestamp", "action"],
             "query": None},
            {"id": "vk", "name": "KQL view", "kql_query": 'events | where data_stream.dataset == "fw" and action == "Login"',
             "time_selection": RANGE, "fields": ["@timestamp", "action"]},
            {"id": "vx", "name": "Builder: ext and list", "kql_query": None, "time_selection": RANGE, "fields": [],
             "query": [group("and", leaf("data_stream.dataset", "EQUALS", "fw"), leaf("ext.user_type", "CONTAINS", "auto"),
                             leaf("tags", "IS_NOT_EMPTY", "", "List(String)"))]},
            {"id": "vb", "name": "Both, KQL unusable", "kql_query": 'events | where strlen(user_name) > 3',
             "time_selection": RANGE, "fields": [],
             "query": [group("and", leaf("data_stream.dataset", "EQUALS", "fw"), leaf("action", "EQUALS", "Login"))]},
            {"id": "vr", "name": "Replay of a detection", "kql_query": None, "query": None, "time_selection": None,
             "fields": ["@timestamp", "action"], "queries": [{"nanoid": "rp01", "created_at": "2026-09-01T00:00:00Z"}]},
            {"id": "vm", "name": "Model lookup", "kql_query": None, "time_selection": RANGE, "fields": [],
             "query": [group("and", leaf("user_name", "EQUALS", "x", eventConditionType="model"))]},
        ]

    def connect(self):
        self.tenant.jobs["rp01"] = {"nanoid": "rp01", "start": E.iso(S), "end": E.iso(END), "polls": 99, "final": "COMPLETED",
                                    "cancelled": False, "condition": {"operator": "and", "groups": [], "conditions": [
                                        {"field": "action", "field_operation": "EQUALS", "fieldType": "String", "value": "Logout"}]}}
        s = C.Session()
        s.connect("https://api.fake.example", self.tenant.vid, KEY)
        return s

    def ids(self, pred):
        return {r["id"] for r in self.fw if pred(r)}


class Listing(_Views):
    def test_every_view_is_listed_with_what_it_holds(self):
        vs = {v["id"]: v for v in self.connect().views()}
        self.assertEqual({k: v["origin"] for k, v in vs.items()},
                         {"vf1": "filters", "vf2": "filters", "vf3": "filters", "vc": "none", "vk": "kql", "vm": "filters",
                          "vx": "filters", "vb": "kql", "vr": "replay"})
        self.assertIn("model", vs["vm"]["problem"])
        self.assertEqual(vs["vf3"]["time"], {"last": "24h"})
        self.assertEqual(vs["vf1"]["time"], {"start": E.iso(S)[:19] + "Z", "end": E.iso(END)[:19] + "Z"})

    def test_the_builder_conditions_become_kql(self):
        k = K.structured_to_kql(self.tenant.views[1]["query"])
        self.assertEqual(k, 'events | where [\'data_stream.dataset\'] =~ "fw" and (['
                         '\'action\'] =~ "Logout" or [\'source_ipv4\'] =~ "52.1.1.1")')


class Loading(_Views):
    def preview_ids(self, s, spec):
        sp = dict(spec, format="ndjson")
        s.start_export(sp)
        res = s.wait(poll=0.01)["result"]
        self.assertEqual(res["exit_code"], 0, res)
        with open(os.path.join(res["run_dir"], res["merged_file"]), encoding="utf-8") as fh:
            return {json.loads(x)["id"] for x in fh}

    def test_a_builder_view_exports_exactly_with_its_time_and_columns(self):
        s = self.connect()
        r = s.import_search(view_id="vf1")
        sp = r["spec"]
        self.assertEqual(sp["dataset"], "FW")
        self.assertEqual(sp["time"], {"start": E.iso(S)[:19] + "Z", "end": E.iso(END)[:19] + "Z"})
        self.assertEqual(sp["fields"], ["@timestamp", "id", "user_name", "action"])
        self.assertTrue(any("filter builder" in n for n in r["notes"]))
        want = self.ids(lambda x: x["action"].lower() == "contenttransfer" and "corp" in (x["user_name"] or ""))
        self.assertTrue(want)
        self.assertEqual(self.preview_ids(s, sp), want)

    def test_a_builder_view_with_or_exports_exactly(self):
        s = self.connect()
        sp = s.import_search(view_id="vf2")["spec"]
        self.assertEqual(len(sp["any_groups"]), 1)
        want = self.ids(lambda x: x["action"].lower() == "logout" or x["source_ipv4"] == "52.1.1.1")
        self.assertEqual(self.preview_ids(s, sp), want)

    def test_a_list_view_and_a_relative_range(self):
        s = self.connect()
        sp = s.import_search(view_id="vf3")["spec"]
        self.assertEqual(sp["time"], {"last": "24h"})
        self.assertEqual(sp["filters"][0]["op"], "IS_IN_LIST")
        self.assertEqual(sp["filters"][0]["value"], "alice@corp.com,bob@partner.io")

    def test_a_columns_only_view_loads_and_asks_for_a_filter(self):
        r = self.connect().import_search(view_id="vc")
        self.assertTrue(r["columns_only"])
        self.assertEqual(r["spec"]["fields"], ["@timestamp", "id", "action"])
        self.assertEqual(r["spec"]["time"], {"last": "24h"})
        self.assertFalse(r["spec"]["dataset"] or r["spec"]["filters"])
        self.assertTrue(C.spec_problems(r["spec"]))

    def test_a_kql_view_takes_the_view_time_range_and_columns(self):
        sp = self.connect().import_search(view_id="vk")["spec"]
        self.assertEqual(sp["fields"], ["@timestamp", "id", "action"])
        self.assertEqual(sp["time"]["start"], E.iso(S)[:19] + "Z")

    def test_ext_fields_and_list_fields_in_a_builder_view(self):
        s = self.connect()
        sp = s.import_search(view_id="vx")["spec"]
        self.assertEqual({f["field"]: f["type"] for f in sp["filters"]}, {"ext.user_type": "String", "tags": "List(String)"})
        self.assertEqual(self.preview_ids(s, sp), self.ids(lambda x: "auto" in x["ext"]["user_type"].lower() and x["tags"]))

    def test_a_view_whose_kql_cannot_be_used_falls_back_to_its_builder_conditions(self):
        s = self.connect()
        r = s.import_search(view_id="vb")
        self.assertIn("filter-builder conditions are used instead", r["notes"][0])
        self.assertEqual(self.preview_ids(s, r["spec"]), self.ids(lambda x: x["action"].lower() == "login"))

    def test_a_replay_view_runs_the_search_it_replayed(self):
        s = self.connect()
        r = s.import_search(view_id="vr")
        self.assertTrue(any("replay" in n for n in r["notes"]))
        self.assertEqual(r["spec"]["time"], {"start": E.iso(S)[:19] + "Z", "end": E.iso(END)[:19] + "Z"})
        self.assertEqual(self.preview_ids(s, dict(r["spec"], dataset="fw")),
                         self.ids(lambda x: x["action"].lower() == "logout"))

    def test_a_model_condition_is_refused_by_name(self):
        with self.assertRaises(E.UserError) as cm:
            self.connect().import_search(view_id="vm")
        self.assertIn("model", str(cm.exception))


class CommandLine(_Views):
    def setUp(self):
        super().setUp()
        os.environ.update({"ABSTRACT_API_KEY": KEY, "ABSTRACT_VENDOR_ACCOUNT_ID": self.tenant.vid,
                           "ABSTRACT_EXPECT_TENANT": self.tenant.vid, "ABSTRACT_BASE_URL": "https://api.fake.example",
                           "ABX_OUT_ROOT": self.home})
        self._home = C.home_dir
        C.home_dir = lambda: self.home

    def tearDown(self):
        C.home_dir = self._home
        for k in ("ABSTRACT_API_KEY", "ABSTRACT_VENDOR_ACCOUNT_ID", "ABSTRACT_EXPECT_TENANT", "ABSTRACT_BASE_URL"):
            os.environ.pop(k, None)
        super().tearDown()

    def cli(self, *argv):
        os.environ["ABSTRACT_API_KEY"] = KEY
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = A.main(list(argv))
        return code, out.getvalue()

    def test_export_from_a_builder_view_with_or_is_exact(self):
        code, res, run = self.export("--from-view", "Builder: either", start=S, end=END, name="fv")
        self.assertEqual(code, 0, self.log[-6:])
        with open(os.path.join(run, res["merged_file"]), encoding="utf-8") as fh:
            got = {json.loads(x)["id"] for x in fh}
        self.assertEqual(got, self.ids(lambda x: x["action"].lower() == "logout" or x["source_ipv4"] == "52.1.1.1"))

    def test_export_from_a_columns_only_view_is_refused(self):
        code, res, run = self.export("--from-view", "Columns only", start=S, end=END, name="fc")
        self.assertEqual(code, 2)
        self.assertIn("holds no filter", res["error"])

    def test_views_lists_all_and_save_syncs_them(self):
        code, out = self.cli("views", "--json")
        j = json.loads(out)
        self.assertEqual(len(j["views"]), 9)
        kinds = {v["name"]: (v["kind"], v["converts"], v["loads"]) for v in j["views"]}
        self.assertEqual(kinds["Builder: either"], ("filter builder", True, True))
        self.assertEqual(kinds["Columns only"], ("columns only", False, True))
        self.assertEqual(kinds["Model lookup"], ("filter builder", False, False))
        C.saved_save(dict(C.empty_spec(), name="KQL view", dataset="mine"))       # a search of your own, same name
        code, out = self.cli("views", "--save", "--json")
        self.assertEqual(code, 0)
        names = {x["name"]: x for x in C.saved_list()}
        for n in ("Builder: transfers by people", "Builder: either", "Builder: list", "Columns only", "KQL view (view)"):
            self.assertIn(n, names)
        self.assertEqual(C.saved_load("KQL view")["dataset"], "mine", "your own search is never replaced")
        self.assertEqual(C.saved_load("Builder: either")["source_view"], "vf2")
        before = len(C.saved_list())
        self.cli("views", "--save", "--json")                                    # syncing again updates in place
        self.assertEqual(len(C.saved_list()), before)


if __name__ == "__main__":
    unittest.main()
