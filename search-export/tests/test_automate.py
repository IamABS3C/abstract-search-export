"""Automation recipes: every target's files, the tenant guard, no key in any file, files that parse, and the shell
script run end to end with a stand-in app (exit codes 0, 3 and a failure, the webhook alert, the log, resume)."""
import contextlib
import http.server
import io
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from support import C, E, EngineCase, KEY

import abx_automate as AU
import abstract_search as A

VID = "AbC123xYz0"
SPEC = C.normalize_spec(dict(C.empty_spec(), name="Okta failures", dataset="Okta",
                             filters=[{"field": "event.outcome", "op": "IS_IN_LIST", "value": "failure",
                                       "type": "String"}],
                             fields=["@timestamp", "id", "user_name"]))


def _yaml_ok(text):
    """A structural check of the YAML this module writes (no YAML parser in the standard library): no tabs, 2-space
    indentation, every line a key, a list item, a comment or blank, and quotes balanced."""
    for n, line in enumerate(text.splitlines(), 1):
        if "\t" in line:
            return f"line {n}: tab"
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        ind = len(line) - len(line.lstrip(" "))
        if ind % 2:
            return f"line {n}: odd indentation"
        body = line.strip()
        if not (body.startswith("- ") or re.match(r"^[A-Za-z_][\w.-]*:( |$)", body)):
            return f"line {n}: not a key or list item: {body}"
        if body.count('"') % 2:
            return f"line {n}: unbalanced quotes"
    return None


class Recipes(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="abx-auto-")
        self._env = os.environ.get("ABX_OUT_ROOT")
        os.environ["ABX_OUT_ROOT"] = self.home

    def tearDown(self):
        if self._env is not None:
            os.environ["ABX_OUT_ROOT"] = self._env
        shutil.rmtree(self.home, ignore_errors=True)

    def test_every_target_has_its_files_the_guard_and_no_key(self):
        for target in AU.TARGETS:
            for every in ("15m", "1d"):
                r = AU.recipe(SPEC, target, VID, every=every, at="02:30")
                blob = "\n".join(f["text"] for f in r["files"])
                self.assertIn(VID, blob, target)
                self.assertTrue("ABSTRACT_EXPECT_TENANT" in blob, target)
                self.assertNotIn(KEY, blob)
                self.assertFalse(re.search(r"(?i)bearer\s+\S{12,}", blob), target)
                self.assertTrue(any(f["path"] == "README.txt" for f in r["files"]))
                self.assertTrue(r["steps"])

    def test_the_search_travels_as_files_not_quoted_arguments(self):
        r = AU.recipe(SPEC, "cron", VID)
        files = {f["path"]: f["text"] for f in r["files"]}
        cond = json.loads(files["search-condition.json"])
        self.assertEqual(cond["conditions"][0]["values"], ["failure"])
        self.assertIn('--condition-file "$HERE/search-condition.json"', files["run-export.sh"])
        kql = AU.recipe(dict(C.empty_spec(), name="k", mode="kql", kql='events | where action == "Login"'),
                        "cron", VID)
        kf = {f["path"]: f["text"] for f in kql["files"]}
        self.assertEqual(kf["search.kql"].strip(), 'events | where action == "Login"')
        self.assertIn('--kql-file "$HERE/search.kql"', kf["run-export.sh"])

    def test_a_field_list_starting_with_at_timestamp_stays_an_argument(self):
        for target, name in (("cron", "run-export.sh"), ("windows-task", "run-export.ps1")):
            text = next(f["text"] for f in AU.recipe(SPEC, target, VID)["files"] if f["path"] == name)
            self.assertIn("@timestamp,id,user_name", text)
            self.assertNotIn("timestamp,id,user_name\"", text.replace("@timestamp,id,user_name", ""))
            self.assertNotIn("\x00", text)

    def test_launchd_plist_parses(self):
        r = AU.recipe(SPEC, "launchd", VID, every="1d", at="03:15")
        pl = next(f for f in r["files"] if f["path"].endswith(".plist"))
        d = plistlib.loads(pl["text"].encode())
        self.assertEqual(d["StartCalendarInterval"], {"Hour": 3, "Minute": 15})
        self.assertIn("security find-generic-password", d["EnvironmentVariables"]["ABSTRACT_API_KEY_COMMAND"])
        r = AU.recipe(SPEC, "launchd", VID, every="1h")
        pl = next(f for f in r["files"] if f["path"].endswith(".plist"))
        self.assertEqual(plistlib.loads(pl["text"].encode())["StartInterval"], 3600)

    def test_yaml_files_are_well_formed(self):
        for target, name in (("github-actions", ".yml"), ("container-cron", "cronjob.yaml")):
            r = AU.recipe(SPEC, target, VID, every="6h")
            y = next(f for f in r["files"] if f["path"].endswith(name))
            self.assertIsNone(_yaml_ok(y["text"]), y["text"])
            try:
                import yaml  # type: ignore  # optional: a full parse where PyYAML happens to be installed
            except ImportError:
                continue
            doc = yaml.safe_load(y["text"])
            self.assertTrue(doc)
        gha = next(f for f in AU.recipe(SPEC, "github-actions", VID)["files"] if f["path"].endswith(".yml"))["text"]
        self.assertIn("secrets.ABSTRACT_API_KEY", gha)
        self.assertIn('cron: "20 0 * * *"', gha)
        k8s = next(f for f in AU.recipe(SPEC, "container-cron", VID)["files"] if f["path"] == "cronjob.yaml")["text"]
        self.assertIn("secretKeyRef", k8s)
        self.assertIn("concurrencyPolicy: Forbid", k8s)

    def test_systemd_and_windows_files(self):
        r = AU.recipe(SPEC, "systemd", VID, every="15m")
        timer = next(f for f in r["files"] if f["path"].endswith(".timer"))["text"]
        self.assertIn("OnCalendar=*:0/15", timer)
        self.assertIn("Persistent=true", timer)
        r = AU.recipe(SPEC, "windows-task", VID, every="1d", at="01:00")
        ps = {f["path"]: f["text"] for f in r["files"]}
        self.assertIn("$PSScriptRoot", ps["run-export.ps1"])
        self.assertIn("Register-ScheduledTask", ps["register-task.ps1"])
        for text in ps.values():                              # Windows PowerShell 5.1 has no && / || / ?. / ??
            self.assertNotRegex(text, r"&&|\|\||\?\.|\?\?")

    def test_bad_choices_are_refused(self):
        for kw in ({"target": "cron", "every": "2d"}, {"target": "atari"}, {"target": "cron", "at": "25:00"}):
            with self.assertRaises(E.UserError):
                AU.recipe(SPEC, kw.pop("target"), VID, **kw)
        with self.assertRaises(E.UserError):
            AU.recipe(SPEC, "cron", "")

    def test_cli_writes_the_files_with_modes(self):
        out = os.path.join(self.home, "job")
        saved = os.path.join(self.home, "saved-searches")
        os.makedirs(saved, exist_ok=True)
        with open(os.path.join(saved, "okta-failures.json"), "w") as fh:
            json.dump(SPEC, fh)
        o, e = io.StringIO(), io.StringIO()
        real = C.home_dir
        C.home_dir = lambda: self.home
        try:
            with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
                code = A.main(["automate", "cron", "--from-saved", "Okta failures", "--vendor-account", VID,
                               "--out", out])
        finally:
            C.home_dir = real
        self.assertEqual(code, 0, e.getvalue())
        if os.name != "nt":
            self.assertEqual(os.stat(os.path.join(out, "run-export.sh")).st_mode & 0o777, 0o755)
        self.assertIn("crontab -e", o.getvalue())


@unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell")
class ShellScript(unittest.TestCase):
    """The generated run-export.sh, run for real: a stand-in app records its arguments and exits with a chosen code."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="abx-sh-")
        r = AU.recipe(SPEC, "cron", VID, every="1h")
        AU.write(r, os.path.join(self.dir, "job"))
        self.script = os.path.join(self.dir, "job", "run-export.sh")
        self.app = os.path.join(self.dir, "fake_app.py")
        with open(self.app, "w") as fh:
            fh.write("import json,os,sys\n"
                     "open(os.environ['ARGS_FILE'],'a').write(json.dumps(sys.argv[1:])+'\\n')\n"
                     "if '--out' in sys.argv:\n"
                     "    o=sys.argv[sys.argv.index('--out')+1]; os.makedirs(o,exist_ok=True)\n"
                     "    open(os.path.join(o,'manifest.json'),'w').write('{}')\n"
                     "print(json.dumps({'verdict':'COMPLETE','rows_written':0 if os.environ.get('ZERO') else 5,"
                     "'zero_result':bool(os.environ.get('ZERO'))}))\n"
                     "sys.exit(int(os.environ.get('RC','0')))\n")
        self.hits = []
        test = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                test.hits.append(json.loads(self.rfile.read(n)))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_it(self, rc, webhook=True, key=True, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("ABSTRACT_") and not k.startswith("ABX_")}
        env.update(ABX_PYTHON=sys.executable, ABX_APP=self.app, ABX_OUT_ROOT=os.path.join(self.dir, "out"),
                   ARGS_FILE=os.path.join(self.dir, "args.txt"), RC=str(rc), **extra)
        if key:
            env["ABSTRACT_API_KEY_COMMAND"] = "echo not-a-real-key"
        if webhook:
            env["ABX_ALERT_WEBHOOK"] = "http://127.0.0.1:%d/hook" % self.httpd.server_address[1]
        return subprocess.run(["sh", self.script], env=env, capture_output=True, text=True, timeout=60)

    def args(self):
        with open(os.path.join(self.dir, "args.txt")) as fh:
            return [json.loads(x) for x in fh]

    def test_syntax(self):
        self.assertEqual(subprocess.run(["sh", "-n", self.script]).returncode, 0)

    def test_success_warning_and_failure(self):
        self.assertEqual(self.run_it(0).returncode, 0)
        first = self.args()[0]
        self.assertEqual(first[0], "export")
        s, e = first[first.index("--start") + 1], first[first.index("--end") + 1]
        self.assertRegex(s, r"^\d{4}-\d\d-\d\dT\d\d:00:00Z$")      # a whole hour, back to back with the next run
        self.assertRegex(e, r"^\d{4}-\d\d-\d\dT\d\d:00:00Z$")
        self.assertEqual(self.hits, [])                           # a run with events: no alert
        self.assertEqual(self.run_it(3).returncode, 0)            # same window again: the folder exists, so it resumes
        self.assertIn("--resume", self.args()[1])
        self.assertEqual([h["reason"] for h in self.hits], ["warning"])   # exit 3 is a success that still alerts
        self.hits.clear()
        shutil.rmtree(os.path.join(self.dir, "out", "okta-failures"))
        p = self.run_it(2)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(len(self.hits), 1)
        self.assertEqual(self.hits[0]["exit_code"], 2)
        self.assertEqual(self.hits[0]["tenant"], VID)
        logs = os.listdir(os.path.join(self.dir, "out", "automation-logs"))
        self.assertTrue(logs)

    def test_an_empty_run_alerts_by_default_and_can_be_quietened(self):
        self.assertEqual(self.run_it(0, ZERO="1").returncode, 0)
        self.assertEqual([h["reason"] for h in self.hits], ["empty"])
        logs = [os.path.join(self.dir, "out", "automation-logs", f)
                for f in os.listdir(os.path.join(self.dir, "out", "automation-logs"))]
        texts = []
        for p in logs:
            with open(p) as fh:
                texts.append(fh.read())
        self.assertTrue(any("NO events matched" in t for t in texts))
        self.hits.clear()
        shutil.rmtree(os.path.join(self.dir, "out", "okta-failures"))
        self.assertEqual(self.run_it(0, ZERO="1", ABX_ALERT_ON_EMPTY="0").returncode, 0)
        self.assertEqual(self.hits, [])

    def test_first_run_on_a_bare_scheduler_environment(self):
        """cron starts jobs with almost no environment: no PATH beyond the system, no output folder yet."""
        env = {"HOME": self.dir, "PATH": "/usr/bin:/bin", "ABX_APP": self.app, "ABX_PYTHON": sys.executable,
               "ABSTRACT_API_KEY_COMMAND": "echo k", "ARGS_FILE": os.path.join(self.dir, "args.txt"), "RC": "0"}
        p = subprocess.run(["sh", self.script], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "AbstractExports", "okta-failures")))
        args = self.args()[0]
        self.assertTrue(os.path.isfile(args[args.index("--condition-file") + 1]))   # the search file resolves
        env["ABX_APP"] = os.path.join(self.dir, "missing.py")
        p = subprocess.run(["sh", self.script], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 2)
        self.assertIn("ABX_APP", p.stderr)

    def test_every_recipe_says_where_alerts_go(self):
        for target in AU.TARGETS:
            r = AU.recipe(SPEC, target, VID)
            readme = next(f["text"] for f in r["files"] if f["path"] == "README.txt")
            self.assertIn("ABX_ALERT_WEBHOOK", readme)
            self.assertIn("go nowhere until you set it", readme)
            self.assertIn("ABX_ALERT_ON_EMPTY=0", readme)

    def test_refuses_to_run_without_a_key_source(self):
        p = self.run_it(0, key=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("ABSTRACT_API_KEY_COMMAND", p.stderr)


if __name__ == "__main__":
    unittest.main()
