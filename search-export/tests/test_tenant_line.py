"""Which tenant am I on? Every command that connects names the tenant on standard error, and the vendor account ID
plus a key is all a person has to set: ABSTRACT_EXPECT_TENANT is optional, because the key is already checked against
the vendor account ID. A key for another account is refused on every path (command line, engine, MCP)."""
import contextlib
import io
import os
import unittest

from support import C, E, EngineCase, KEY, T, spread

import abstract_search as A
import abx_mcp as M

S, END = T(hours=3), T(hours=1)
ENV = ("ABSTRACT_API_KEY", "ABSTRACT_VENDOR_ACCOUNT_ID", "ABSTRACT_EXPECT_TENANT", "ABSTRACT_BASE_URL")

# Every command that reads the tenant, with arguments that make it connect. (searches, exports and endpoints read
# only this computer and never connect.)
CONNECTING = [
    ["capabilities"],
    ["datasets"],
    ["views"],
    ["fields", "--dataset", "fw"],
    ["fieldsets", "list"],
    ["preview", "--dataset", "fw", "--last", "1h"],
    ["validate", "--dataset", "fw", "--last", "1h"],
    ["kql", "check", 'events | where data_stream.dataset == "fw"'],
    ["content", "export", "{home}/bundle"],
]


class _Cli(EngineCase):
    def setUp(self):
        super().setUp()
        spread(self.tenant, S, END, 60, "fw")
        os.environ.update({"ABSTRACT_API_KEY": KEY, "ABSTRACT_VENDOR_ACCOUNT_ID": self.tenant.vid,
                           "ABSTRACT_BASE_URL": "https://api.fake.example", "ABX_OUT_ROOT": self.home})
        os.environ.pop("ABSTRACT_EXPECT_TENANT", None)
        self._home = C.home_dir
        C.home_dir = lambda: self.home
        E.LOG_SINK = None                              # log lines reach standard error, as on a real terminal
        E.CREDS = None                                 # the command line reads the environment, like a real run

    def tearDown(self):
        C.home_dir = self._home
        for k in ENV:
            os.environ.pop(k, None)
        super().tearDown()

    def cli(self, *argv):
        os.environ["ABSTRACT_API_KEY"] = KEY           # the session removes it after reading it
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = A.main([a.replace("{home}", self.home) for a in argv])
            except SystemExit as ex:
                code = ex.code
        return code, out.getvalue(), err.getvalue()


class NamesTheTenant(_Cli):
    def test_every_connecting_command_names_the_tenant(self):
        for argv in CONNECTING:
            with self.subTest(command=" ".join(argv)):
                code, out, err = self.cli(*argv)
                self.assertEqual(code, 0, err)
                self.assertIn(f"Connected to {self.tenant.name} ({self.tenant.vid})", err)

    def test_the_tenant_line_stays_off_standard_output_so_json_is_clean(self):
        code, out, err = self.cli("capabilities", "--json")
        self.assertEqual(code, 0, err)
        self.assertNotIn("Connected to", out)
        self.assertIn("Connected to", err)

    def test_quiet_hides_the_tenant_line(self):
        code, out, err = self.cli("datasets", "--quiet")
        self.assertEqual(code, 0, err)
        self.assertNotIn("Connected to", err)

    def test_an_export_names_the_tenant(self):
        code, out, err = self.cli("export", "--dataset", "fw", "--start", E.iso(S), "--end", E.iso(END),
                                  "--out", os.path.join(self.home, "run"), "--merge")
        self.assertEqual(code, 0, err)
        self.assertIn(f"Connected to {self.tenant.name} ({self.tenant.vid})", err)


class OnlyTheVendorAccountAndKey(_Cli):
    def test_no_expect_tenant_is_needed(self):
        code, out, err = self.cli("datasets")
        self.assertEqual(code, 0, err)
        self.assertNotIn("ABSTRACT_EXPECT_TENANT", err)

    def test_an_export_runs_without_expect_tenant(self):
        code, out, err = self.cli("export", "--dataset", "fw", "--start", E.iso(S), "--end", E.iso(END),
                                  "--out", os.path.join(self.home, "run"), "--merge", "--quiet")
        self.assertEqual(code, 0, err)

    def test_a_key_for_another_account_is_refused(self):
        os.environ["ABSTRACT_VENDOR_ACCOUNT_ID"] = "VAsomeoneelse"      # the fake's key belongs to VAtest0001
        code, out, err = self.cli("datasets")
        self.assertEqual(code, 2)
        self.assertIn("vendor account", err)          # Abstract refuses the key for that account (HTTP 403)
        self.assertNotIn("Connected to", err)

    def test_a_set_expect_tenant_is_still_enforced(self):
        os.environ["ABSTRACT_EXPECT_TENANT"] = "VAsomeoneelse"
        code, out, err = self.cli("datasets")
        self.assertEqual(code, 2)
        self.assertIn("TENANT MISMATCH", err)

    def test_the_engine_refuses_a_key_for_another_account(self):
        E.CREDS = {"base_url": "https://api.fake.example", "vendor_account_id": "VAsomeoneelse", "api_key": KEY}
        E._TL.result = {}
        code = E.main(["export", "--dataset", "fw", "--start", E.iso(S), "--end", E.iso(END),
                       "--out", os.path.join(self.home, "x"), "--quiet"])
        self.assertEqual(code, 2)
        self.assertIn("vendor account", E.thread_result().get("error", ""))

    def test_the_engine_runs_without_expect_tenant(self):
        E.CREDS = {"base_url": "https://api.fake.example", "vendor_account_id": self.tenant.vid, "api_key": KEY}
        E._TL.result = {}
        code = E.main(["export", "--dataset", "fw", "--start", E.iso(S), "--end", E.iso(END),
                       "--out", os.path.join(self.home, "y"), "--quiet", "--merge"])
        self.assertEqual(code, 0, E.thread_result())


class McpTenant(EngineCase):
    def setUp(self):
        super().setUp()
        os.environ.update({"ABSTRACT_API_KEY": KEY, "ABSTRACT_VENDOR_ACCOUNT_ID": self.tenant.vid,
                           "ABSTRACT_BASE_URL": "https://api.fake.example"})
        os.environ.pop("ABSTRACT_EXPECT_TENANT", None)

    def tearDown(self):
        for k in ENV:
            os.environ.pop(k, None)
        super().tearDown()

    def status(self):
        r = M.Server().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "status", "arguments": {}}})
        return r["result"]

    def test_connects_with_only_the_vendor_account_and_key_and_names_the_tenant(self):
        r = self.status()
        self.assertTrue(r["structuredContent"]["connected"], r)
        self.assertEqual(r["structuredContent"]["vendor_account_id"], self.tenant.vid)
        self.assertIn(self.tenant.name, r["content"][0]["text"])

    def test_refuses_a_key_for_another_account(self):
        os.environ["ABSTRACT_VENDOR_ACCOUNT_ID"] = "VAsomeoneelse"
        r = self.status()
        self.assertFalse(r["structuredContent"]["connected"])
        self.assertIn("vendor account", r["content"][0]["text"])

    def test_a_set_expect_tenant_is_still_enforced(self):
        os.environ["ABSTRACT_EXPECT_TENANT"] = "VAsomeoneelse"
        r = self.status()
        self.assertFalse(r["structuredContent"]["connected"])
        self.assertIn("mismatch", r["content"][0]["text"].lower())


class Status(_Cli):
    def test_status_names_the_tenant_and_the_key_source_never_the_key(self):
        code, out, err = self.cli("status")
        self.assertEqual(code, 0, err)
        self.assertIn(self.tenant.name, out)
        self.assertIn("ABSTRACT_API_KEY", out)
        self.assertNotIn(KEY, out + err)

    def test_status_json(self):
        import json
        code, out, err = self.cli("status", "--json")
        self.assertEqual(code, 0, err)
        j = json.loads(out)
        self.assertEqual((j["connected"], j["vendor_account_id"], j["pinned_tenant"]), (True, self.tenant.vid, None))
        self.assertNotIn(KEY, out)

    def test_status_names_only_the_program_of_a_key_command(self):
        import sys
        os.environ.pop("ABSTRACT_API_KEY", None)
        os.environ["ABSTRACT_API_KEY_COMMAND"] = f'"{sys.executable}" -c "print({KEY!r})"'
        try:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = A.main(["status"])
        finally:
            os.environ.pop("ABSTRACT_API_KEY_COMMAND", None)
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("ABSTRACT_API_KEY_COMMAND (runs " + os.path.basename(sys.executable), out.getvalue())
        self.assertNotIn(KEY, out.getvalue() + err.getvalue())
        self.assertNotIn("print(", out.getvalue())

    def test_status_without_settings_says_what_to_set(self):
        os.environ.pop("ABSTRACT_VENDOR_ACCOUNT_ID")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = A.main(["status"])
        self.assertEqual(code, 2)
        self.assertIn("ABSTRACT_VENDOR_ACCOUNT_ID", out.getvalue())



class UnfilledSettings(unittest.TestCase):
    def test_placeholder_values_are_treated_as_not_set(self):
        env = {"ABSTRACT_API_KEY": "${user_config.api_key}", "ABSTRACT_API_KEY_COMMAND": " ${user_config.cmd} ",
               "ABSTRACT_VENDOR_ACCOUNT_ID": "AbC123xYz0", "OTHER": "${keep}"}
        A.drop_unfilled_settings(env)
        self.assertEqual(env, {"ABSTRACT_VENDOR_ACCOUNT_ID": "AbC123xYz0", "OTHER": "${keep}"})


if __name__ == "__main__":
    unittest.main()
