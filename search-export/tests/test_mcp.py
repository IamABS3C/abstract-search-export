"""MCP mode: the JSON-RPC handshake, every tool's schema, KQL checks, a fieldset plan and apply round trip, errors as
isError results, standard output carrying protocol messages only, the key never in any output, and the local HTTP
transport's bearer token and origin checks. Every value is invented."""
import contextlib
import io
import json
import os
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request

from support import C, E, EngineCase, KEY, T, spread

import abx_mcp as M

S, END = T(hours=3), T(hours=1)
ENV = ("ABSTRACT_API_KEY", "ABSTRACT_VENDOR_ACCOUNT_ID", "ABSTRACT_EXPECT_TENANT", "ABSTRACT_BASE_URL")


class _Mcp(EngineCase):
    def setUp(self):
        super().setUp()
        self.tenant.types.update({"user_name": "String", "action": "String"})
        for i, r in enumerate(spread(self.tenant, S, END, 40, "fw")):
            r["user_name"] = ["alice@example.com", "bob@example.org"][i % 2]
            r["action"] = ["Login", "Logout"][i % 2]
        os.environ.update({"ABSTRACT_API_KEY": KEY, "ABSTRACT_VENDOR_ACCOUNT_ID": self.tenant.vid,
                           "ABSTRACT_EXPECT_TENANT": self.tenant.vid, "ABSTRACT_BASE_URL": "https://api.fake.example"})
        self.srv = M.Server()
        self.n = 0

    def tearDown(self):
        for k in ENV:
            os.environ.pop(k, None)
        super().tearDown()

    def rpc(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method}
        if params is not None:
            msg["params"] = params
        return self.srv.handle(msg)

    def call(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        self.assertIn("result", r, r)
        return r["result"]


class Handshake(_Mcp):
    def test_initialize_and_versions(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertIn("plan", r["result"]["instructions"])
        r = self.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.rpc("ping")["result"], {})
        self.assertEqual(self.rpc("no/such")["error"]["code"], -32601)

    def test_every_tool_has_a_valid_schema(self):
        tools = self.rpc("tools/list")["result"]["tools"]
        names = {t["name"] for t in tools}
        want = {"status", "capabilities", "datasets", "fields", "field_values", "kql_check", "nl_to_kql",
                "kql_preview", "kql_save", "preview", "validate", "export_start", "export_status", "export_cancel",
                "exports_list", "views_list", "view_import", "view_plan", "fieldsets_list", "fieldset_get",
                "fieldset_plan", "apply_change", "endpoints", "content_export", "content_diff",
                "content_plan", "automation_recipe"}
        self.assertEqual(names, want)
        for t in tools:
            sch = t["inputSchema"]
            self.assertEqual(sch["type"], "object", t["name"])
            self.assertIsInstance(sch.get("properties"), dict, t["name"])
            for req in sch.get("required", []):
                self.assertIn(req, sch["properties"], t["name"])
            self.assertTrue(t["description"], t["name"])
            json.dumps(t)

    def test_resources_and_prompts(self):
        res = self.rpc("resources/list")["result"]["resources"]
        self.assertEqual(res, [])                         # nothing to read until a tenant is connected
        self.assertEqual(self.rpc("resources/read", {"uri": "abstract://fields"})["error"]["code"], -32002)
        self.assertEqual(self.rpc("resources/read", {"uri": "abstract://nope"})["error"]["code"], -32002)
        self.call("status")
        res = self.rpc("resources/list")["result"]["resources"]
        self.assertEqual([r["uri"] for r in res], ["abstract://fields"])
        got = self.rpc("resources/read", {"uri": "abstract://fields"})["result"]["contents"][0]
        self.assertEqual(got["mimeType"], "application/json")
        json.loads(got["text"])
        names = [p["name"] for p in self.rpc("prompts/list")["result"]["prompts"]]
        self.assertIn("question-to-kql", names)
        msg = self.rpc("prompts/get", {"name": "question-to-kql", "arguments": {"question": "who logged in"}})
        self.assertIn("who logged in", msg["result"]["messages"][0]["content"]["text"])


class Tools(_Mcp):
    def test_status_connects_from_the_environment(self):
        r = self.call("status")
        self.assertTrue(r["structuredContent"]["connected"])
        self.assertEqual(r["structuredContent"]["vendor_account_id"], self.tenant.vid)

    def test_status_explains_what_to_set(self):
        os.environ.pop("ABSTRACT_VENDOR_ACCOUNT_ID")
        r = self.call("status")
        self.assertFalse(r["structuredContent"]["connected"])
        self.assertIn("ABSTRACT_VENDOR_ACCOUNT_ID", r["content"][0]["text"])

    def test_kql_check(self):
        r = self.call("kql_check", kql='events | where data_stream.dataset == "fw" and action == "Login"')
        self.assertFalse(r["isError"])
        self.assertEqual(r["structuredContent"]["runs_as"], "filters")

    def test_preview_states_the_window_and_caps_rows(self):
        spec = {"dataset": "fw", "time": {"start": E.iso(S)[:19] + "Z", "end": E.iso(END)[:19] + "Z"}}
        r = self.call("preview", spec=spec, rows=5)
        self.assertEqual(r["structuredContent"]["total"], 40)
        self.assertEqual(len(r["structuredContent"]["rows"]), 5)
        self.assertIn("UTC", r["content"][0]["text"])

    def test_read_only_by_default_plans_still_work_and_apply_is_refused(self):
        self.assertFalse(self.srv.allow_writes)
        st = self.call("status")["structuredContent"]
        self.assertEqual((st["writes"], st["max_rows"]), (False, M.MAX_ROWS))
        r = self.call("fieldset_plan", op="create", data={"name": "Ops", "fields": ["@timestamp", "action"]})
        plan = r["structuredContent"]
        self.assertIn("read-only", plan["instruction"])
        done = self.call("apply_change", token=plan["token"])
        self.assertTrue(done["isError"])
        text = done["content"][0]["text"]
        self.assertIn("--allow-writes", text)
        self.assertIn("ABSTRACT_MCP_ALLOW_WRITES=1", text)
        self.assertIn("cannot check a \"yes\" typed in the chat", text)
        self.assertEqual(self.tenant.fieldsets, {})

    def test_the_allow_writes_flag_on_the_command_line(self):
        captured = {}
        real = M.Server

        class Spy(real):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                captured["writes"] = self.allow_writes
        M.Server = Spy
        try:
            for argv, want in ((["--allow-writes"], True), ([], False)):
                with contextlib.redirect_stderr(io.StringIO()) as err, \
                        contextlib.redirect_stdout(io.StringIO()), \
                        unittest.mock.patch.object(M.sys, "stdin", io.StringIO("")):
                    M.main(argv)
                self.assertEqual(captured["writes"], want)
                self.assertIn("ON" if want else "read-only", err.getvalue())
        finally:
            M.Server = real

    def test_the_environment_switch_turns_writes_on(self):
        os.environ["ABSTRACT_MCP_ALLOW_WRITES"] = "1"
        try:
            self.assertTrue(M.Server().allow_writes)
        finally:
            os.environ.pop("ABSTRACT_MCP_ALLOW_WRITES", None)
        self.assertFalse(M.Server().allow_writes)

    def test_row_cap_from_the_environment(self):
        for val, want in (("120", 120), ("9999", 500), ("0", 1), ("junk", 50), (None, 50)):
            if val is None:
                os.environ.pop("ABSTRACT_MCP_MAX_ROWS", None)
            else:
                os.environ["ABSTRACT_MCP_MAX_ROWS"] = val
            self.assertEqual(M._rows_cap(), want, val)
        os.environ.pop("ABSTRACT_MCP_MAX_ROWS", None)
        spec = {"dataset": "fw", "time": {"start": E.iso(S)[:19] + "Z", "end": E.iso(END)[:19] + "Z"}}
        saved = M.MAX_ROWS
        M.MAX_ROWS = 3
        try:
            r = self.call("preview", spec=spec, rows=40)
        finally:
            M.MAX_ROWS = saved
        self.assertEqual(len(r["structuredContent"]["rows"]), 3)

    def test_fieldset_plan_then_apply_once(self):
        self.srv.allow_writes = True                    # started with --allow-writes
        r = self.call("fieldset_plan", op="create", data={"name": "Ops", "fields": ["@timestamp", "action"]})
        plan = r["structuredContent"]
        self.assertIn("explicit yes", plan["instruction"])
        self.assertEqual(self.tenant.fieldsets, {}, "a plan changes nothing")
        done = self.call("apply_change", token=plan["token"])
        self.assertFalse(done["isError"], done)
        self.assertEqual(len(self.tenant.fieldsets), 1)
        again = self.call("apply_change", token=plan["token"])
        self.assertTrue(again["isError"])

    def test_a_token_from_elsewhere_is_refused(self):
        self.srv.allow_writes = True
        self.call("status")
        foreign = self.srv.s.fieldset_plan("create", {"name": "X", "fields": ["@timestamp"]})["token"]
        r = self.call("apply_change", token=foreign)
        self.assertTrue(r["isError"])
        self.assertEqual(self.tenant.fieldsets, {})

    def test_errors_are_tool_results(self):
        r = self.call("fieldset_plan", op="create", data={"name": "x", "fields": ["not_a_field"]})
        self.assertTrue(r["isError"])
        self.assertIn("not", r["content"][0]["text"])
        r = self.call("kql_check")
        self.assertTrue(r["isError"])
        self.assertEqual(self.rpc("tools/call", {"name": "nope"})["error"]["code"], -32602)


class Stdio(_Mcp):
    def test_stdout_carries_only_protocol_and_never_the_key(self):
        E.LOG_SINK = None                                 # let the engine's log lines go where they would really go
        E.SHOW_API = True                                 # and the live API trace too
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "status", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "preview", "arguments": {"spec": {"dataset": "fw", "time": {
                    "start": E.iso(S)[:19] + "Z", "end": E.iso(END)[:19] + "Z"}}}}},
        ]
        inp = io.StringIO("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
        out, stray, err = io.StringIO(), io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stray), contextlib.redirect_stderr(err):
            self.srv.run_stdio(inp, out)
        self.assertEqual(stray.getvalue(), "", "something other than the protocol wrote to standard output")
        got = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([g.get("id") for g in got], [1, 2, 3, None])
        self.assertEqual(got[-1]["error"]["code"], -32700)
        self.assertIn("API POST", err.getvalue())         # the trace went to standard error
        self.assertNotIn(KEY, out.getvalue() + err.getvalue())


class Http(_Mcp):
    def setUp(self):
        super().setUp()
        self.httpd, self.token = M.make_http(self.srv, 0)
        self.port = self.httpd.server_address[1]
        self.th = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.th.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def post(self, body, token=None, origin=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        if origin:
            h["Origin"] = origin
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/mcp", data=json.dumps(body).encode(), headers=h,
                                     method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null"), dict(r.headers)
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"null"), dict(e.headers)
            finally:
                e.close()

    def test_token_and_origin(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        self.assertEqual(self.post(init)[0], 401)
        self.assertEqual(self.post(init, token="wrong")[0], 401)
        self.assertEqual(self.post(init, token=self.token, origin="https://evil.example")[0], 403)
        code, body, hdrs = self.post(init, token=self.token, origin="http://localhost:3000")
        self.assertEqual(code, 200)
        self.assertEqual(body["result"]["serverInfo"]["name"], "abstract-search-export")
        self.assertTrue(hdrs.get("Mcp-Session-Id"))
        code, body, _ = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, token=self.token)
        self.assertEqual(code, 202)
        code, body, _ = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": "status", "arguments": {}}}, token=self.token)
        self.assertTrue(body["result"]["structuredContent"]["connected"])
        self.assertNotIn(KEY, json.dumps(body))


if __name__ == "__main__":
    unittest.main()
