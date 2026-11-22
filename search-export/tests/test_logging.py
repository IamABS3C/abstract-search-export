"""Seeing what the tool does: the API call trace, --show-api, the daily error log and each export's run.log.
The key must never appear in any of them."""
import contextlib
import glob
import io
import json
import os
import unittest

from support import C, E, EngineCase, KEY, T, spread

import abstract_search as A

S, END = T(hours=3), T(hours=1)


class _Cli(EngineCase):
    def setUp(self):
        super().setUp()
        spread(self.tenant, S, END, 120, "fw")
        os.environ.update({"ABSTRACT_API_KEY": KEY, "ABSTRACT_VENDOR_ACCOUNT_ID": self.tenant.vid,
                           "ABSTRACT_EXPECT_TENANT": self.tenant.vid, "ABSTRACT_BASE_URL": "https://api.fake.example",
                           "ABX_OUT_ROOT": self.home})
        self._home = C.home_dir
        C.home_dir = lambda: self.home
        self._show = E.SHOW_API
        E.LOG_SINK = None                              # let log lines reach standard error, as on a real terminal

    def tearDown(self):
        C.home_dir = self._home
        E.SHOW_API = self._show
        for k in ("ABSTRACT_API_KEY", "ABSTRACT_VENDOR_ACCOUNT_ID", "ABSTRACT_EXPECT_TENANT", "ABSTRACT_BASE_URL"):
            os.environ.pop(k, None)
        super().tearDown()

    def cli(self, *argv):
        os.environ["ABSTRACT_API_KEY"] = KEY
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = A.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def logs(self):
        text = ""
        for p in glob.glob(os.path.join(self.home, "logs", "*.log")) + glob.glob(os.path.join(self.home, "*", "run.log")):
            with open(p, encoding="utf-8") as fh:
                text += fh.read()
        return text


class ApiTrace(_Cli):
    def test_every_call_is_traced_and_shown_on_standard_error_only(self):
        before = E.API_SEQ[0]
        win = ["--start", E.iso(S), "--end", E.iso(END)]
        code, out, err = self.cli("--show-api", "preview", "--dataset", "fw", "--where", "user_name !contains corp",
                                  "--json", *win)
        self.assertEqual(code, 0, err)
        json.loads(out)                                   # stdout is still exactly the JSON result
        calls = [c for c in E.API_TRACE if c["n"] > before]
        self.assertTrue(calls)
        self.assertEqual(err.count("API POST") + err.count("API GET"), len(calls))
        self.assertIn("/v1/streamviewer/search", err)
        self.assertNotIn(KEY, out + err)

    def test_export_writes_run_log_and_the_last_line_stays_json(self):
        run = os.path.join(self.home, "r1")
        code, out, err = self.cli("--show-api", "export", "--dataset", "fw", "--where", "user_name !contains corp",
                                  "--start", E.iso(S), "--end", E.iso(END), "--out", run, "--format", "ndjson",
                                  "--merge", "--quiet")
        self.assertEqual(code, 0, err[-800:])
        res = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(res["verdict"], "COMPLETE")
        with open(os.path.join(run, "run.log"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Abstract Search & Export", text)
        self.assertIn("API POST /v1/streamviewer/search", text)
        self.assertIn("filter check", text)               # lines from before the folder existed are kept too
        self.assertNotIn(KEY, text + out + err)

    def test_calls_are_shown_by_default_with_the_key_masked_and_hidden_on_request(self):
        win = ["--start", E.iso(S), "--end", E.iso(END)]
        E.SHOW_API = True                                 # the shipped default
        code, out, err = self.cli("preview", "--dataset", "fw", "--json", *win)
        self.assertEqual(code, 0, err)
        self.assertIn("API POST /v1/streamviewer/search", err)
        self.assertIn("Authorization: Bearer ******** (masked)", err)
        self.assertNotIn(KEY, out + err)
        E.SHOW_API = True
        code, out, err = self.cli("--hide-api", "preview", "--dataset", "fw", "--json", *win)
        self.assertNotIn("API POST", err)
        E.SHOW_API = True
        code, out, err = self.cli("export", "--dataset", "fw", *win, "--out", os.path.join(self.home, "q"), "--quiet")
        self.assertNotIn("API POST", err, "--quiet keeps a scheduled job's output quiet")
        rec = [c for c in E.API_TRACE][-1]
        self.assertEqual(rec["headers"]["Authorization"], "Bearer ******** (masked)")

    def test_secrets_in_a_request_body_are_redacted(self):
        self.assertEqual(E.redact({"api_key": "x", "nested": [{"Token": "y", "ok": 1}]}),
                         {"api_key": "***", "nested": [{"Token": "***", "ok": 1}]})


class ErrorLog(_Cli):
    def test_an_error_is_logged_and_the_message_names_the_log(self):
        code, out, err = self.cli("preview", "--dataset", "fw", "--where", "no_such_field == 1", "--last", "1h")
        self.assertEqual(code, 2)
        self.assertIn("kept in the log:", err)
        text = self.logs()
        self.assertIn("no_such_field", text)
        self.assertNotIn(KEY, text)
        mode = os.stat(glob.glob(os.path.join(self.home, "logs", "*.log"))[0]).st_mode & 0o777
        if os.name != "nt":
            self.assertEqual(mode, 0o600)

    def test_a_failed_call_is_logged_without_the_key(self):
        from fake_tenant import Reject

        def refuse(tenant, method, path, body):
            if path.startswith("/v1/streamviewer/search") and '"corp"' in json.dumps(body):   # the user's search
                raise Reject(422, {"detail": "a refused search for the test"})
        self.tenant.on_request = refuse
        code, out, err = self.cli("preview", "--dataset", "fw", "--where", "user_name contains corp", "--json",
                                  "--start", E.iso(S), "--end", E.iso(END))
        self.assertNotEqual(code, 0)
        text = self.logs()
        self.assertIn("API POST /v1/streamviewer/search -> 422", text)
        self.assertIn("a refused search for the test", text)
        self.assertNotIn(KEY, text + out + err)

    def test_a_fault_in_the_tool_is_logged_with_its_details(self):
        real = A.preview_cmd
        A.preview_cmd = lambda argv: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            code, out, err = self.cli("preview", "--dataset", "fw", "--last", "1h")
        finally:
            A.preview_cmd = real
        self.assertEqual(code, 2)
        self.assertIn("something went wrong in the tool itself", err)
        text = self.logs()
        self.assertIn("Traceback", text)
        self.assertIn("RuntimeError: boom", text)

    def test_a_deliberate_probe_error_is_not_logged_as_a_failure(self):
        code, out, err = self.cli("--show-api", "fields", "--dataset", "fw")
        self.assertIn("expected: a field that cannot exist", err)
        self.assertNotIn("zz_abx_field_probe", self.logs())

    def test_the_logs_folder_is_not_an_export(self):
        E.log_error("ERROR", "x")
        self.assertTrue(os.path.isdir(os.path.join(self.home, "logs")))
        self.assertEqual([x for x in C.list_exports(None) if os.path.basename(x.get("run_dir", "")) == "logs"], [])


class WebCalls(EngineCase):
    def test_the_api_calls_view_never_holds_the_key(self):
        s = C.Session()
        s.connect("https://api.fake.example", self.tenant.vid, KEY)
        sp = C.normalize_spec(dict(C.empty_spec(), dataset="fw", time={"start": E.iso(S), "end": E.iso(END)}))
        spread(self.tenant, S, END, 30, "fw")
        s.preview(sp, 10)
        blob = json.dumps(list(E.API_TRACE))
        self.assertIn("/v1/streamviewer/search", blob)
        self.assertNotIn(KEY, blob)


if __name__ == "__main__":
    unittest.main()
