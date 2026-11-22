"""Saved views and fieldsets as code: export, stable files, compare, apply to another tenant with fieldsets resolved by
name, prune, stale reviews, partial failure, settings this tool does not know, and the routes and MCP tools.
Every value is invented."""
import contextlib
import glob
import http.client
import io
import json
import os
import threading
import unittest

from support import C, E, EngineCase, FakeTenant, FakeTransport, KEY, T, spread

import abx_content as CT
import abx_mcp as M
import abx_web as W
import abstract_search as A
from fake_tenant import Reject

S, END = T(hours=3), T(hours=1)
RANGE = {"type": "Relative", "numeric_value": 24, "option_selected": "hours", "start_date": None, "end_date": None}
QUERY = [{"id": "g1", "operator": "and", "value": [
    {"id": "n1", "depth": 1, "field": "action", "index": 0, "value": "Login", "parentId": "g1", "fieldType": "String",
     "field_operation": "IS_IN_LIST", "subFieldOperation": "", "eventConditionType": "condition"}]}]


def _rb(p):
    with open(p, "rb") as fh:
        return fh.read()


def _js(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def seed(t):
    t.fieldsets["fsA1"] = {"id": "fsA1", "name": "Identity columns", "fields": ["@timestamp", "user_name", "action"],
                           "description": "who did what", "tags": None, "share_level": "private",
                           "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-01T00:00:00Z",
                           "created_by": "ann@example.com", "updated_by": None}
    t.fieldsets["fsA2"] = {"id": "fsA2", "name": "Bare", "fields": ["@timestamp", "message"], "description": None,
                           "tags": None, "share_level": "public", "created_at": "2026-11-01T00:00:00Z",
                           "updated_at": "2026-11-01T00:00:00Z", "created_by": "ann@example.com", "updated_by": None}
    t.views = [
        {"id": "vwA1", "name": "Logins", "kql_query": None, "query": QUERY, "time_selection": RANGE,
         "fields": [], "fieldset_ids": ["fsA1"], "updated_at": "2026-11-02T00:00:00Z"},
        {"id": "vwA2", "name": "Logins as KQL", "kql_query": 'events | where action == "Login"', "query": None,
         "time_selection": RANGE, "fields": ["@timestamp", "action"], "updated_at": "2026-11-02T00:00:00Z"},
    ]


class _Two(EngineCase):
    """Tenant A (self.tenant) holds content; tenant B starts empty. One fake transport at a time, like one network."""

    def setUp(self):
        super().setUp()
        seed(self.tenant)
        spread(self.tenant, S, END, 10, "fw")
        self.a = C.Session()
        self.a.connect("https://api.fake.example", self.tenant.vid, KEY)
        self.b_tenant = FakeTenant(vid="VAtest0002", name="Test Tenant Beta")
        spread(self.b_tenant, S, END, 10, "fw")
        self.out = os.path.join(self.home, "bundle")

    def tearDown(self):
        unhandled = list(self.b_tenant.unhandled)
        super().tearDown()
        self.assertEqual(unhandled, [])

    def on_a(self):
        E._OPENER = FakeTransport(self.tenant)
        E.CREDS = dict(E.CREDS or {}, vendor_account_id=self.tenant.vid)

    def on_b(self):
        E._OPENER = FakeTransport(self.b_tenant)
        if not getattr(self, "b", None):
            self.b = C.Session()
            self.b.connect("https://api.fake.example", self.b_tenant.vid, KEY)
        return self.b


class Export(_Two):
    def test_bundle_shape_and_stable_files(self):
        r = self.a.content_export(self.out)
        self.assertEqual(r["counts"], {"views": 2, "fieldsets": 2})
        meta = _js(os.path.join(self.out, "bundle.json"))
        self.assertEqual(meta["schema"], "abstract.content/v1")
        self.assertEqual(meta["source"]["tenant_id"], self.tenant.vid)
        v = _js(os.path.join(self.out, "views", "logins.json"))
        self.assertEqual(v["fieldsets"], ["Identity columns"])          # by name, never the id
        self.assertEqual(v["query"], QUERY)                             # the search exactly as Abstract stores it
        self.assertNotIn("id", v)
        self.assertNotIn("updated_at", v)
        first = {p: _rb(p) for p in glob.glob(os.path.join(self.out, "**", "*.json"), recursive=True)}
        self.a.content_export(self.out)
        second = {p: _rb(p) for p in glob.glob(os.path.join(self.out, "**", "*.json"), recursive=True)}
        self.assertEqual(first, second, "exporting the same tenant twice must give the same files")

    def test_a_chosen_view_brings_the_fieldset_it_uses(self):
        r = self.a.content_export(self.out, views=["Logins"], fieldsets=[])
        self.assertEqual(r["counts"], {"views": 1, "fieldsets": 1})
        self.assertTrue(os.path.isfile(os.path.join(self.out, "fieldsets", "identity-columns.json")))

    def test_settings_this_tool_does_not_know_are_kept_and_never_sent(self):
        self.tenant.views[0]["fancy_layout"] = {"split": 2}
        self.a.content_export(self.out)
        v = _js(os.path.join(self.out, "views", "logins.json"))
        self.assertEqual(v["unsupported"], {"fancy_layout": {"split": 2}})
        self.assertTrue(any("fancy_layout" in n for n in v["notes"]))
        b = self.on_b()
        plan = b.content_plan(CT.read_folder(self.out))
        b.apply_plan(plan["token"])
        sent = [c[2] for c in self.b_tenant.calls if c[1] == "/v1/streamviewer/view" and c[0] == "POST"]
        self.assertTrue(sent)
        self.assertTrue(all("unsupported" not in x and "fancy_layout" not in x and "notes" not in x for x in sent))


class DiffAndApply(_Two):
    def test_compare_after_export_is_all_same_then_shows_each_status(self):
        self.a.content_export(self.out)
        doc = CT.read_folder(self.out)
        self.assertEqual({r["status"] for r in self.a.content_diff(doc)}, {"same"})
        self.tenant.fieldsets["fsA1"]["fields"] = ["@timestamp", "user_name"]
        self.tenant.views.append({"id": "vwA3", "name": "Only here", "kql_query": "events", "fields": ["@timestamp"]})
        doc["views"].append({"name": "Brand new", "kql_query": "events", "fields": ["@timestamp"]})
        st = {(r["kind"], r["name"]): r["status"] for r in self.a.content_diff(doc)}
        self.assertEqual(st[("fieldset", "Identity columns")], "changed")
        self.assertEqual(st[("view", "Only here")], "only_in_tenant")
        self.assertEqual(st[("view", "Brand new")], "new")

    def test_copy_to_another_tenant_resolves_fieldsets_by_name(self):
        self.a.content_export(self.out)
        b = self.on_b()
        plan = b.content_plan(CT.read_folder(self.out))
        self.assertEqual(plan["counts"], {"create": 4, "update": 0, "delete": 0})
        self.assertEqual(self.b_tenant.fieldsets, {}, "a plan changes nothing")
        r = b.apply_plan(plan["token"])
        self.assertIsNone(r["failed"])
        self.assertEqual(len(r["done"]), 4)
        fs = {f["name"]: f["id"] for f in self.b_tenant.fieldsets.values()}
        view = next(v for v in self.b_tenant.views if v["name"] == "Logins")
        self.assertEqual(view["fieldset_ids"], [fs["Identity columns"]])
        self.assertNotIn("fsA1", json.dumps(self.b_tenant.views), "a source tenant id travelled")
        again = b.content_plan(CT.read_folder(self.out))
        self.assertEqual(again["items"], [])
        self.assertIsNone(again["token"])

    def test_prune_deletes_only_with_prune_and_says_so(self):
        self.a.content_export(self.out)
        b = self.on_b()
        b.apply_plan(b.content_plan(CT.read_folder(self.out))["token"])
        self.b_tenant.views.append({"id": "vwB9", "name": "Local only", "kql_query": "events", "fields": ["@timestamp"],
                                    "updated_at": "2026-11-03T00:00:00Z"})
        plain = b.content_plan(CT.read_folder(self.out))
        self.assertEqual(plain["counts"]["delete"], 0)
        pruned = b.content_plan(CT.read_folder(self.out), prune=True)
        self.assertEqual(pruned["counts"]["delete"], 1)
        self.assertTrue(pruned["warnings"][0].startswith("PRUNE"))
        b.apply_plan(pruned["token"])
        self.assertFalse(any(v["name"] == "Local only" for v in self.b_tenant.views))

    def test_prune_with_a_views_only_bundle_leaves_fieldsets_alone(self):
        r = self.a.content_export(self.out, kind="views")
        self.assertEqual(r["counts"], {"views": 2, "fieldsets": 1})          # the fieldset a view uses comes along
        self.assertEqual(_js(os.path.join(self.out, "bundle.json"))["scope"],
                         {"views": "all", "fieldsets": "some"})
        self.tenant.fieldsets["fsA9"] = {"id": "fsA9", "name": "Not in the bundle", "fields": ["@timestamp"],
                                         "share_level": "private", "updated_at": "2026-11-05T00:00:00Z"}
        self.tenant.views.append({"id": "vwA9", "name": "Extra view", "kql_query": "events", "fields": ["@timestamp"],
                                  "updated_at": "2026-11-05T00:00:00Z"})
        plan = self.a.content_plan(CT.read_folder(self.out), prune=True)
        deleted = [(i["kind"], i["name"]) for i in plan["items"] if i["op"] == "delete"]
        self.assertEqual(deleted, [("view", "Extra view")])
        self.assertTrue(any("leaves fieldsets alone" in w for w in plan["warnings"]))
        self.a.apply_plan(plan["token"])
        self.assertIn("fsA9", self.tenant.fieldsets)
        self.assertIn("fsA2", self.tenant.fieldsets)

    def test_a_hand_made_bundle_prunes_only_the_kinds_it_holds(self):
        doc = {"schema": CT.SCHEMA, "views": [], "fieldsets": [{"name": "Bare", "fields": ["@timestamp", "message"],
                                                                 "share_level": "public"}]}
        plan = self.a.content_plan(doc, prune=True)
        kinds = {i["kind"] for i in plan["items"] if i["op"] == "delete"}
        self.assertEqual(kinds, {"fieldset"})                              # views untouched: the bundle holds none

    def test_a_review_works_once_and_a_change_after_it_stops_the_apply(self):
        self.a.content_export(self.out)
        self.tenant.fieldsets["fsA1"]["fields"] = ["@timestamp", "user_name"]
        plan = self.a.content_plan(CT.read_folder(self.out))          # updates fsA1 back to the bundle's fields
        self.assertEqual(plan["counts"]["update"], 1)
        self.tenant.fieldsets["fsA1"]["updated_at"] = "2026-11-09T09:09:09Z"  # someone else changes it meanwhile
        r = self.a.apply_plan(plan["token"])
        self.assertIn("changed by someone else", r["failed"]["error"])
        with self.assertRaises(E.UserError):
            self.a.apply_plan(plan["token"])

    def test_partial_failure_says_what_was_done_and_what_was_not(self):
        self.a.content_export(self.out)
        b = self.on_b()

        def refuse(tenant, method, path, body):
            if method == "POST" and path == "/v1/streamviewer/view" and body.get("name") == "Logins as KQL":
                raise Reject(422, {"detail": [{"msg": "invented refusal"}]})
        self.b_tenant.on_request = refuse
        r = b.apply_plan(b.content_plan(CT.read_folder(self.out))["token"])
        self.assertEqual(r["failed"]["name"], "Logins as KQL")
        self.assertEqual([d["kind"] for d in r["done"]].count("fieldset"), 2)
        rest = [d["name"] for d in r["done"] if d["kind"] == "view"] + [x["name"] for x in r["not_attempted"]]
        self.assertEqual(rest, ["Logins"])                          # done before the failure, or reported as not tried

    def test_a_bundle_applied_once_then_compares_as_the_same(self):
        b = self.on_b()
        doc = {"schema": CT.SCHEMA, "fieldsets": [],
               "views": [{"name": "Short", "kql_query": "events", "fields": ["@timestamp"],
                          "time_selection": {"type": "Relative", "numeric_value": 1, "option_selected": "hours"}}]}
        b.apply_plan(b.content_plan(doc)["token"])
        self.assertEqual(b.content_plan(doc)["items"], [])
        self.assertEqual({r["status"] for r in b.content_diff(doc) if r["name"] == "Short"}, {"same"})

    def test_a_view_without_order_settings_updates_to_the_defaults_never_null(self):
        self.tenant.views[1]["order_by"] = "user_name"                     # someone sorted it differently
        doc = {"schema": CT.SCHEMA, "fieldsets": [],
               "views": [{"name": "Logins as KQL", "kql_query": 'events | where action == "Login"',
                          "time_selection": RANGE, "fields": ["@timestamp", "action"]}]}
        plan = self.a.content_plan(doc)
        self.assertEqual(plan["items"][0]["body"], {"order_by": "@timestamp"})
        r = self.a.apply_plan(plan["token"])
        self.assertIsNone(r["failed"])
        self.assertEqual(self.a.content_plan(doc)["items"], [])

    def test_deleting_a_fieldset_names_the_views_that_use_it(self):
        plan = self.a.fieldset_plan("delete", None, "fsA1")
        loud = [w for w in plan["warnings"] if w.startswith("VIEWS")]
        self.assertTrue(any("Logins" in w and "VIEWS AFFECTED" in w for w in loud), plan["warnings"])
        self.assertTrue(any(w.startswith("VIEWS LEFT WITH NO COLUMNS") for w in loud))    # Logins has no own fields
        self.assertTrue(any("Checked 2 saved view(s)" in w for w in plan["warnings"]))
        quiet = self.a.fieldset_plan("delete", None, "fsA2")
        self.assertTrue(any(w.startswith("No saved view uses this fieldset") for w in quiet["warnings"]))

    def test_prune_that_deletes_a_fieldset_names_the_views_it_breaks(self):
        doc = {"schema": CT.SCHEMA, "views": [], "fieldsets": [{"name": "Bare", "fields": ["@timestamp", "message"],
                                                                 "share_level": "public"}]}
        plan = self.a.content_plan(doc, prune=True)
        self.assertTrue(plan["warnings"][0].startswith("PRUNE"))
        self.assertTrue(any("VIEWS AFFECTED" in w and "Logins" in w for w in plan["warnings"]))

    def test_a_tampered_or_missing_file_is_reported(self):
        self.a.content_export(self.out)
        p = os.path.join(self.out, "fieldsets", "bare.json")
        with open(p, "a") as fh:
            fh.write(" ")
        os.remove(os.path.join(self.out, "views", "logins.json"))
        notes = CT.read_folder(self.out)["notes"]
        self.assertTrue(any("bare.json was edited" in n for n in notes))
        self.assertTrue(any("logins.json is listed" in n for n in notes))

    def test_not_a_bundle_is_refused(self):
        with self.assertRaises(E.UserError):
            CT.validate({"schema": "something/else", "views": [], "fieldsets": []})
        with self.assertRaises(E.UserError):
            CT.validate({"schema": CT.SCHEMA, "views": [{"kql_query": "events"}], "fieldsets": []})

    def test_two_items_with_one_name_export_and_are_skipped_when_applied(self):
        self.tenant.fieldsets["fsA3"] = dict(self.tenant.fieldsets["fsA2"], id="fsA3", fields=["@timestamp"])
        r = self.a.content_export(self.out)
        self.assertEqual(r["counts"]["fieldsets"], 3)
        self.assertTrue(any("are called 'Bare'" in p for p in r["problems"]))
        b = self.on_b()
        plan = b.content_plan(CT.read_folder(self.out))
        self.assertTrue(any(sk["name"] == "Bare" for sk in plan["skipped"]))
        self.assertFalse(any(i["name"] == "Bare" for i in plan["items"]))


class Cli(_Two):
    def cli(self, *argv):
        os.environ.update({"ABSTRACT_API_KEY": KEY, "ABSTRACT_VENDOR_ACCOUNT_ID": self.tenant.vid,
                           "ABSTRACT_EXPECT_TENANT": self.tenant.vid, "ABSTRACT_BASE_URL": "https://api.fake.example"})
        out, err = io.StringIO(), io.StringIO()
        E.LOG_SINK = None
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = A.main(list(argv))
        finally:
            for k in ("ABSTRACT_API_KEY", "ABSTRACT_VENDOR_ACCOUNT_ID", "ABSTRACT_EXPECT_TENANT", "ABSTRACT_BASE_URL"):
                os.environ.pop(k, None)
        return code, out.getvalue(), err.getvalue()

    def test_export_diff_apply_on_the_command_line(self):
        code, out, err = self.cli("content", "export", self.out)
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("content", "diff", self.out)
        self.assertIn("4 of 4 compared items match", out)
        self.tenant.fieldsets.pop("fsA2")
        code, out, err = self.cli("content", "apply", self.out)
        self.assertEqual(code, 3, err)
        self.assertIn("Nothing was changed", out)
        code, out, err = self.cli("content", "apply", self.out, "--yes")
        self.assertEqual(code, 0, err)
        self.assertTrue(any(f["name"] == "Bare" for f in self.tenant.fieldsets.values()))


class Web(_Two):
    def setUp(self):
        super().setUp()
        self.auth = W._Auth()
        self.httpd = W._bind(0, W._handler(self.a, self.auth, None))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.token = self.auth.exchange(self.auth.new_code())

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": "127.0.0.1:%d" % self.port, "X-ABX-Token": self.token}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        text = r.read().decode()
        c.close()
        self.assertNotIn(KEY, text)
        return r.status, json.loads(text)

    def test_export_plan_and_apply_through_the_routes(self):
        st, r = self.call("GET", "/api/content-export")
        self.assertEqual(st, 200, r)
        self.assertTrue(os.path.isfile(os.path.join(r["dir"], "bundle.json")))
        doc = r["bundle"]
        doc["fieldsets"].append({"name": "From the page", "fields": ["@timestamp", "action"]})
        st, d = self.call("POST", "/api/content-diff", {"bundle": doc})
        self.assertIn("new", {x["status"] for x in d["diff"]})
        st, p = self.call("POST", "/api/content-plan", {"bundle": doc})
        self.assertEqual(p["counts"]["create"], 1)
        st, bad = self.call("POST", "/api/fieldset-apply", {"token": p["token"]})
        self.assertEqual(st, 400)                                   # a bundle review is confirmed as a bundle
        st, done = self.call("POST", "/api/content-apply", {"token": p["token"]})
        self.assertEqual(st, 200, done)
        self.assertTrue(any(f["name"] == "From the page" for f in self.tenant.fieldsets.values()))
        st, err = self.call("POST", "/api/content-plan", {"bundle": {"schema": "nope"}})
        self.assertEqual(st, 400)

    def test_export_one_kind_through_the_route(self):
        st, r = self.call("GET", "/api/content-export?kind=fieldsets")
        self.assertEqual(st, 200, r)
        self.assertEqual((len(r["bundle"]["views"]), len(r["bundle"]["fieldsets"])), (0, 2))
        self.assertEqual(os.listdir(os.path.join(r["dir"], "views")), [])

    def test_the_content_folder_is_not_an_export(self):
        self.call("GET", "/api/content-export")
        st, r = self.call("GET", "/api/exports")
        self.assertEqual(st, 200)
        self.assertFalse(any("content" in str(x.get("run_dir", "")) for x in r["exports"]))

    def test_automation_route(self):
        spec = dict(C.empty_spec(), name="fw logins", dataset="fw",
                    filters=[{"field": "action", "op": "IS_IN_LIST", "value": "Login", "type": "String"}])
        st, r = self.call("POST", "/api/automation", {"spec": spec, "target": "systemd",
                                                      "options": {"every": "1h", "save": True}})
        self.assertEqual(st, 200, r)
        self.assertTrue(any(f["path"].endswith(".timer") for f in r["files"]))
        self.assertTrue(os.path.isfile(os.path.join(r["dir"], "run-export.sh")))
        st, r = self.call("POST", "/api/automation", {"spec": spec, "target": "nowhere"})
        self.assertEqual(st, 400)


class Mcp(_Two):
    def setUp(self):
        super().setUp()
        os.environ.update({"ABSTRACT_API_KEY": KEY, "ABSTRACT_VENDOR_ACCOUNT_ID": self.tenant.vid,
                           "ABSTRACT_EXPECT_TENANT": self.tenant.vid, "ABSTRACT_BASE_URL": "https://api.fake.example"})
        self.srv = M.Server(allow_writes=True)

    def tearDown(self):
        for k in ("ABSTRACT_API_KEY", "ABSTRACT_VENDOR_ACCOUNT_ID", "ABSTRACT_EXPECT_TENANT", "ABSTRACT_BASE_URL"):
            os.environ.pop(k, None)
        super().tearDown()

    def call(self, name, **args):
        r = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name,
                                                                                         "arguments": args}})
        self.assertNotIn(KEY, json.dumps(r))
        return r["result"]

    def test_export_compare_plan_and_apply(self):
        r = self.call("content_export", dir=self.out)
        self.assertFalse(r.get("isError"), r)
        self.tenant.fieldsets.pop("fsA2")
        d = self.call("content_diff", dir=self.out)
        self.assertIn("new", {x["status"] for x in d["structuredContent"]["diff"]})
        p = self.call("content_plan", dir=self.out)["structuredContent"]
        self.assertIn("explicit yes", p["instruction"])
        done = self.call("apply_change", token=p["token"])
        self.assertFalse(done.get("isError"), done)
        self.assertTrue(any(f["name"] == "Bare" for f in self.tenant.fieldsets.values()))

    def test_automation_recipe_tool(self):
        spec = dict(C.empty_spec(), name="dns", dataset="dns")
        r = self.call("automation_recipe", target="github-actions", spec=spec, every="6h")
        self.assertFalse(r.get("isError"), r)
        files = {f["path"]: f["text"] for f in r["structuredContent"]["files"]}
        self.assertIn("secrets.ABSTRACT_API_KEY", files["abstract-export-dns.yml"])


if __name__ == "__main__":
    unittest.main()
