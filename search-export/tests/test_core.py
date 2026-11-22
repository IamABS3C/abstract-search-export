"""abx_core: the session layer every front end uses, plus its safety helpers."""
import datetime as dt
import json
import os
import tempfile
import unittest

from support import C, E, EngineCase, FIXED_NOW, T, spread, ids_of

S, END = T(hours=3), T(hours=1)


class Sanitising(unittest.TestCase):
    def test_sanitize_neutralises_escape_sequences(self):
        s = C.sanitize("red\x1b[31mALERT\x1b[0m\x07\x9b‮")
        self.assertNotIn("\x1b", s)
        self.assertNotIn("\x07", s)
        self.assertNotIn("\x9b", s)
        self.assertNotIn("‮", s)
        self.assertIn("\\x1b[31m", s)

    def test_cell_sanitises_then_truncates(self):
        c = C.cell("\x1b]0;owned\x07" + "x" * 50, 20)
        self.assertNotIn("\x1b", c)
        self.assertLessEqual(len(c), 20)
        self.assertTrue(c.endswith("…"))
        self.assertNotIn("\x1b", C.cell({"k": "\x1b[2J"}))

    def test_humanize_is_sanitised(self):
        self.assertNotIn("\x1b", C.humanize("/v1/streamviewer/search: HTTP 500 — \x1b[31m"))


class SpecProblems(unittest.TestCase):
    def test_empty_search(self):
        p = C.spec_problems(C.empty_spec())
        self.assertTrue(p)
        self.assertTrue(any("dataset" in x for x in p), p)

    def test_cidr_typo(self):
        sp = C.empty_spec()
        sp["filters"] = [{"field": "source_ipv4", "type": "Ipv4", "op": "IS_IN_SUBNET", "value": "10.0.0.0/33"}]
        self.assertTrue(any("CIDR" in x for x in C.spec_problems(sp)))
        sp["filters"][0]["value"] = "10.0.0.0/8"
        self.assertEqual(C.spec_problems(sp), [])

    def test_csv_with_every_field_is_allowed(self):
        sp = C.empty_spec()
        sp.update(dataset="fw", full_documents=True, format="csv")
        self.assertEqual(C.spec_problems(sp), [])

    def test_runnable_spec_has_no_problems(self):
        sp = C.empty_spec()
        sp["dataset"] = "fw"
        self.assertEqual(C.spec_problems(sp), [])


class HomeAndTickets(EngineCase):
    def test_inside_home_refuses_outside_paths_and_symlinks(self):
        inside = os.path.join(self.home, "run1")
        os.makedirs(inside)
        with open(os.path.join(inside, "export.ndjson"), "w") as fh:
            fh.write("{}\n")
        self.assertEqual(C._inside_home(inside, want_dir=True), os.path.realpath(inside))
        outside = tempfile.mkdtemp(prefix="abx-outside-")
        try:
            with open(os.path.join(outside, "secret.txt"), "w") as fh:
                fh.write("x")
            for p in (outside, os.path.join(outside, "secret.txt"), os.path.join(self.home, "..", "etc"),
                      os.path.join(inside, "..", "..", os.path.basename(outside))):
                with self.assertRaises(E.UserError):
                    C._inside_home(p)
            link = os.path.join(self.home, "escape")
            os.symlink(outside, link)
            with self.assertRaises(E.UserError):
                C._inside_home(link, want_dir=True)
            with self.assertRaises(E.UserError):
                C._inside_home(os.path.join(link, "secret.txt"), want_file=True)
            with self.assertRaises(E.UserError):
                C._inside_home(self.home, want_file=True)            # the home itself is not a file
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)

    def test_download_ticket_is_single_use(self):
        s = C.Session()
        f = os.path.join(self.home, "run1", "export.ndjson")
        os.makedirs(os.path.dirname(f))
        with open(f, "w") as fh:
            fh.write("{}\n")
        t = s.download_ticket(f)
        self.assertEqual(s.redeem_ticket(t), os.path.realpath(f))
        with self.assertRaises(E.UserError):
            s.redeem_ticket(t)
        with self.assertRaises(E.UserError):
            s.redeem_ticket("not-a-ticket")
        with self.assertRaises(E.UserError):
            s.download_ticket("/etc/hosts")


class SessionFlows(EngineCase):
    def _connect(self):
        s = C.Session()
        t = s.connect("https://api.fake.example", self.tenant.vid, "test-key-0123456789abcdef")
        self.assertEqual(t["vendor_account_id"], self.tenant.vid)
        return s

    def _spec(self):
        sp = C.empty_spec()
        sp.update(dataset="fw", time={"start": E.iso(S), "end": E.iso(END)}, format="ndjson")
        return sp

    def test_validate_then_export_through_the_session(self):
        fw = spread(self.tenant, S, END, 150, "fw")
        spread(self.tenant, S, END, 80, "dns")
        s = self._connect()
        v = s.validate(self._spec())
        self.assertTrue(v["ok"], v["checks"])
        self.assertTrue(v["ready"])
        self.assertEqual(v["plan"]["total"], 150)
        self.assertTrue(any(c["name"] == "Cross-check" and c["status"] == "PASS" for c in v["checks"]), v["checks"])
        s.start_export(self._spec())
        st = s.wait(poll=0.01)
        res = st["result"]
        self.assertEqual(res["exit_code"], 0, st["lines"][-5:])
        self.assertEqual(res["verdict"], "COMPLETE")
        rows = self.read_rows(os.path.join(res["run_dir"], res["merged_file"]))
        self.assertEqual(set(ids_of(rows)), {r["id"] for r in fw})
        ex = C.list_exports(s)
        self.assertEqual(ex[0]["status"], "COMPLETE")
        self.assertEqual(ex[0]["rows_written"], 150)
        self.assertEqual(self.tenant.unhandled, [])

    def test_export_reuses_the_window_validate_checked(self):
        """A relative range exported shortly after Validate uses exactly the validated window, so the export's
        totals match what the user saw; a changed search gets a freshly resolved window."""
        spread(self.tenant, T(hours=3), FIXED_NOW, 300, "fw")
        s = self._connect()
        sp = C.empty_spec()
        sp.update(dataset="fw", time={"last": "1h"}, format="ndjson")
        v = s.validate(sp)
        self.assertTrue(v["ready"], v["checks"])
        E.now = lambda: FIXED_NOW + dt.timedelta(minutes=5)          # time passes before Export is pressed
        s.start_export(sp)
        res = s.wait(poll=0.01)["result"]
        self.assertEqual(res["exit_code"], 0)
        with open(os.path.join(res["run_dir"], "manifest.json")) as fh:
            man = json.load(fh)
        self.assertEqual([man["window"]["start"], man["window"]["end"]], v["window"])
        self.assertEqual(res["rows_written"], v["plan"]["total"])
        changed = dict(sp, fields=["@timestamp", "id", "message"])
        s.start_export(changed)
        res2 = s.wait(poll=0.01)["result"]
        with open(os.path.join(res2["run_dir"], "manifest.json")) as fh:
            man2 = json.load(fh)
        self.assertNotEqual([man2["window"]["start"], man2["window"]["end"]], v["window"])

    def test_preview_counts_and_samples(self):
        spread(self.tenant, S, END, 120, "fw")
        s = self._connect()
        p = s.preview(self._spec(), 25)
        self.assertEqual(p["total"], 120)
        self.assertEqual(len(p["rows"]), 25)

    def test_session_resume_refuses_another_tenant(self):
        spread(self.tenant, S, END, 30, "fw")
        code, _, run = self.export("--dataset", "fw", "--dry-run", start=S, end=END)
        self.assertEqual(code, 0)
        other = type(self.tenant)(vid="VAother0002", name="Test Tenant Beta")
        self.tenant = other
        self.use_tenant(other)
        s = self._connect()
        with self.assertRaises(E.UserError):
            s.resume(run)

    def test_session_resume_refuses_folders_outside_home(self):
        s = self._connect()
        outside = tempfile.mkdtemp(prefix="abx-outside-")
        try:
            with self.assertRaises(E.UserError):
                s.resume(outside)
        finally:
            os.rmdir(outside)

    def test_api_key_is_never_written(self):
        spread(self.tenant, S, END, 20, "fw")
        s = self._connect()
        s.start_export(self._spec())
        s.wait(poll=0.01)
        for dp, _, fns in os.walk(self.home):
            for fn in fns:
                with open(os.path.join(dp, fn), "rb") as fh:
                    self.assertNotIn(b"test-key-0123456789abcdef", fh.read(), fn)


if __name__ == "__main__":
    unittest.main()
