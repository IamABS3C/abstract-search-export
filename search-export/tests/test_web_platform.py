"""The browser app's KQL, plain English, field values, fieldset and saved-view routes, driven through the real HTTP
handler against the fake tenant. Every write is a plan first and an apply with its token; the key never comes back."""
import http.client
import json
import threading
import unittest

from support import C, E, EngineCase, KEY, T, spread

import abx_web as W

S, END = T(hours=3), T(hours=1)


class WebPlatform(EngineCase):
    def setUp(self):
        super().setUp()
        self.tenant.types.update({"user_name": "String", "action": "String"})
        for i, r in enumerate(spread(self.tenant, S, END, 40, "fw")):
            r["user_name"] = ["alice@example.com", "bob@example.org"][i % 2]
            r["action"] = ["Login", "Logout"][i % 2]
        self.tenant.suggest["user_name"] = ["alice@example.com", "bob@example.org"]
        self.session = C.Session()
        self.session.connect("https://api.fake.example", self.tenant.vid, KEY)
        self.auth = W._Auth()
        self.httpd = W._bind(0, W._handler(self.session, self.auth, None))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.token = self.auth.exchange(self.auth.new_code())
        self.win = {"start": E.iso(S)[:19] + "Z", "end": E.iso(END)[:19] + "Z"}
        self.seen = []

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        blob = "\n".join(self.seen)
        self.assertNotIn(KEY, blob, "the API key came back to the page")
        super().tearDown()

    def call(self, method, path, body=None, token=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": "127.0.0.1:%d" % self.port}
        if token:
            h["X-ABX-Token"] = self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        text = r.read().decode("utf-8", "replace")
        c.close()
        self.seen.append(text)
        try:
            return r.status, json.loads(text)
        except ValueError:
            return r.status, text

    def test_kql_check_preview_and_save(self):
        st, r = self.call("POST", "/api/kql-check", {"kql": 'events | where data_stream.dataset == "fw" and action == "Login"'})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["runs_as"], "filters")
        st, r = self.call("POST", "/api/kql-preview", {"kql": "events | summarize n = count() by user_name",
                                                       "time": self.win, "rows": 50})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["runs_as"], "kql_result")
        self.assertEqual(sum(x["n"] for x in r["rows"]), 40)
        st, r = self.call("POST", "/api/kql-save", {"kql": "events | summarize n = count() by user_name",
                                                    "time": self.win, "format": "json", "name": "by user"})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["file"].endswith("result.json"))
        st, r = self.call("POST", "/api/kql-check", {"kql": ""})
        self.assertEqual(st, 400)

    def test_plain_english_writes_kql_and_runs_nothing(self):
        self.tenant.nl_answers["alice logins"] = {"kql": 'events | where user_name == "alice@example.com"',
                                                  "explanation": "Alice."}
        n = len([c for c in self.tenant.calls if c[1] == "/v1/dataview/search"])
        st, r = self.call("POST", "/api/nl", {"text": "alice logins"})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["check"]["runs_as"], "filters")
        searched = [c for c in self.tenant.calls[n:] if c[1] == "/v1/dataview/search" and "alice" in json.dumps(c[2] or {})]
        self.assertEqual(searched, [])

    def test_field_catalog_values_and_builder_as_kql(self):
        st, r = self.call("GET", "/api/field-catalog")
        self.assertEqual(st, 200)
        self.assertTrue(any(f["name"] == "user_name" for f in r["fields"]))
        st, r = self.call("GET", "/api/field-values?field=user_name&prefix=al")
        self.assertEqual((st, r["values"]), (200, ["alice@example.com"]))
        spec = dict(C.empty_spec(), dataset="fw", filters=[{"field": "action", "op": "IS_IN_LIST", "value": "Login", "type": "String"}])
        st, r = self.call("POST", "/api/spec-kql", {"spec": spec})
        self.assertEqual(st, 200, r)
        self.assertTrue(r["kql"].startswith("events"))

    def test_fieldset_writes_need_a_reviewed_token(self):
        st, r = self.call("POST", "/api/fieldset-apply", {})
        self.assertEqual(st, 400)
        st, r = self.call("POST", "/api/fieldset-apply", {"token": "not-a-plan"})
        self.assertEqual(st, 400)
        self.assertEqual(self.tenant.fieldsets, {})
        st, plan = self.call("POST", "/api/fieldset-plan", {"op": "create", "data": {"name": "Triage", "fields": ["@timestamp", "user_name"]}})
        self.assertEqual(st, 200, plan)
        self.assertEqual(self.tenant.fieldsets, {}, "a plan writes nothing")
        st, r = self.call("POST", "/api/view-apply", {"token": plan["token"]})
        self.assertEqual(st, 400, "a fieldset review cannot be confirmed as a view change")
        st, r = self.call("POST", "/api/fieldset-apply", {"token": plan["token"]})
        self.assertEqual(st, 200, r)
        st, r = self.call("GET", "/api/fieldsets")
        self.assertEqual([f["name"] for f in r["fieldsets"]], ["Triage"])
        st, r = self.call("GET", "/api/fieldset?id=" + r["fieldsets"][0]["id"])
        self.assertEqual(r["fields"], ["@timestamp", "user_name"])
        st, r = self.call("POST", "/api/fieldset-plan", {"op": "create", "data": {"name": "x", "fields": ["region"]}})
        self.assertEqual(st, 400)
        self.assertIn("computed column", r["error"])

    def test_view_save_update_delete(self):
        spec = dict(C.empty_spec(), dataset="fw", time={"last": "24h"})
        st, plan = self.call("POST", "/api/view-plan", {"op": "create", "spec": spec, "name": "All fw"})
        self.assertEqual(st, 200, plan)
        st, r = self.call("POST", "/api/view-apply", {"token": plan["token"]})
        self.assertEqual(st, 200, r)
        vid = r["id"]
        st, plan = self.call("POST", "/api/view-plan", {"op": "update", "id": vid, "name": "All firewall"})
        self.assertEqual(plan["body"], {"name": "All firewall"})
        self.call("POST", "/api/view-apply", {"token": plan["token"]})
        st, plan = self.call("POST", "/api/view-plan", {"op": "delete", "id": vid})
        st, r = self.call("POST", "/api/view-apply", {"token": plan["token"]})
        self.assertEqual(st, 200, r)
        self.assertFalse(any(v["id"] == vid for v in self.tenant.views))
        st, r = self.call("POST", "/api/view-plan", {"op": "rename"})
        self.assertEqual(st, 400)

    def test_endpoints_static_files_and_auth(self):
        st, r = self.call("GET", "/api/endpoints")
        self.assertEqual(st, 200)
        self.assertTrue(any(x["path"] == "/v1/kql/to-conditions" for x in r["endpoints"]))
        st, _ = self.call("GET", "/static/not-shipped.json", token=False)
        self.assertEqual(st, 404)                         # only the named static files are served
        for path in ("/static/workbench.js", "/static/app.css"):
            st, _ = self.call("GET", path, token=False)
            self.assertEqual(st, 200, path)
        st, r = self.call("POST", "/api/kql-check", {"kql": "events"}, token=False)
        self.assertEqual(st, 403)


if __name__ == "__main__":
    unittest.main()
