"""Round-2 fixes: each test names the ledger item it proves (engine + core)."""
import _thread
import datetime as dt
import gzip
import json
import os
import tempfile
import threading
import time
import unittest

from support import C, E, EngineCase, KEY, T, spread, ids_of

S, END = T(hours=3), T(hours=1)
CAN_LINK = hasattr(os, "symlink") and os.name != "nt"


def fetches(tenant):
    return [b for m, p, b in tenant.calls if p == "/v1/streamviewer/search" and (b.get("page_size") or 0) > 1
            and "zz_abx_field_probe" not in (b.get("selected_fields") or [])]


class _Session(EngineCase):
    def connect(self):
        s = C.Session()
        s.connect("https://api.fake.example", self.tenant.vid, KEY)
        return s

    @staticmethod
    def spec(**kw):
        sp = C.empty_spec()
        sp.update(dataset="fw", time={"start": E.iso(S), "end": E.iso(END)}, format="ndjson")
        sp.update(kw)
        return sp


# ── DI-4: a zero is a claim ──
class ZeroClaims(EngineCase):
    def setUp(self):
        super().setUp()
        self.tenant.extra_datasets.append("fw")   # a configured dataset with no events: a real zero

    def test_zero_export_is_confirmed_by_a_second_engine(self):
        spread(self.tenant, S, END, 40, "dns")
        code, res, run = self.export("--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        self.assertEqual(res["verdict"], "COMPLETE")
        self.assertTrue(res["zero_result"])
        self.assertEqual(res["zero_confirmed_by"], "dataview")
        self.assertIn("Nothing matched. A zero is a claim: confirmed by dataview.", res["warnings"])
        self.assertEqual(self.manifest(run)["summary"]["zero_confirmed_by"], "dataview")
        self.assertTrue(any("Nothing matched" in line for line in self.log))

    def test_zero_settled_by_dataview_for_v1_counts_as_confirmed(self):
        self.tenant.v1_500_on_zero = True
        spread(self.tenant, S, END, 40, "dns")
        code, res, _ = self.export("--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        self.assertEqual(res["zero_confirmed_by"], "dataview")

    def test_a_non_empty_export_has_no_zero_warning(self):
        spread(self.tenant, S, END, 40, "fw")
        code, res, _ = self.export("--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 0)
        self.assertFalse(res["zero_result"])
        self.assertIsNone(res["zero_confirmed_by"])
        self.assertEqual(res["warnings"], [])
        self.assertEqual(res["bad_timestamps"], 0)
        self.assertEqual(res["filter_unproven"], [])


# ── DI-9: unproven filters are surfaced, not blocked ──
class FilterUnproven(EngineCase):
    def test_unproven_leaf_is_reported_and_the_export_proceeds(self):
        spread(self.tenant, S, END, 60, "fw", user_name="bob")
        spread(self.tenant, S, END, 60, "dns")
        cond = {"operator": "and", "groups": [], "conditions": [
            {"field": "user_name", "operation": "IS_NOT_IN_LIST", "field_type": "String", "values": ["alice"]}]}
        code, res, run = self.export("--dataset", "fw", "--condition-json", json.dumps(cond), start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        self.assertEqual(res["verdict"], "COMPLETE")
        self.assertEqual(res["filter_unproven"], ["user_name"])
        self.assertTrue(any("user_name" in w for w in res["warnings"]), res["warnings"])
        man = self.manifest(run)
        self.assertEqual(man["preflight"]["filter_unproven"], ["user_name"])
        self.assertEqual(man["summary"]["filter_unproven"], ["user_name"])
        self.assertTrue(any("Not proven on this tenant" in line for line in self.log))


# ── DI-12: split parents are reconciled with their children ──
class SplitDrift(EngineCase):
    def _crowd(self):
        base = T(hours=2).replace(microsecond=0)
        for s in range(40):
            for i in range(10):
                self.tenant.add(base + dt.timedelta(seconds=s, milliseconds=i * 50))
        return base

    def test_split_parent_matches_its_children(self):
        self._crowd()
        code, res, run = self.export("--dataset", "fw", "--page-size", "50", start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        split = [x for x in self.manifest(run)["slices"] if x["status"] == "SPLIT" and "split_drift" in x]
        self.assertTrue(split, "no split parent was reconciled")
        for x in split:
            self.assertEqual(x["split_drift"], 0, x)
            self.assertEqual(x["children_total"], x["server_total"])

    def test_late_event_after_a_split_is_recorded_as_drift_not_an_error(self):
        self._crowd()
        run_dir = os.path.join(self.home, "run")
        state = {"added": None}

        def hook(tenant, method, path, body):
            if state["added"] or not any("split into" in line for line in self.log):
                return
            with open(os.path.join(run_dir, "manifest.json"), encoding="utf-8") as fh:
                parent = next(x for x in json.load(fh)["slices"] if x["status"] == "SPLIT")
            state["added"] = parent["i"]
            tenant.add(E.parse_ts(parent["end"]) - dt.timedelta(milliseconds=1500), dataset="fw")

        self.tenant.on_request = hook
        code, res, run = self.export("--dataset", "fw", "--page-size", "50", start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        self.assertEqual(res["verdict"], "COMPLETE")
        parent = next(x for x in self.manifest(run)["slices"] if x["i"] == state["added"])
        self.assertEqual(parent["split_drift"], 1, parent)
        self.assertTrue(any("before it was split" in line for line in self.log))
        self.assertIsNone(res["error"])


# ── DI-14: raw semantics ──
class RawSemantics(_Session):
    def test_raw_engine_refuses_dataset_shorthand(self):
        spread(self.tenant, S, END, 20, "fw")
        code, res, _ = self.export("--engine", "raw", "--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 2)
        self.assertIn("raw engine", res["error"])
        self.assertEqual(self.tenant.calls, [], "called the API before refusing")

    def test_count_labels_raw_and_leaves_it_out_of_the_verdict(self):
        spread(self.tenant, S, END, 30, "fw")
        self.tenant.raw_extra = 3
        code, res = self.run_engine(["count", "--dataset", "fw", "--start", E.iso(S), "--end", E.iso(END), "--quiet"])
        self.assertEqual(code, 0, res)
        self.assertEqual(res["verdict"], "PASS")
        self.assertTrue(res["agree"])
        self.assertEqual(res["engines"]["raw"]["semantics"], "phrase match (approximate)")
        self.assertEqual(res["engines"]["raw"]["total"], 33)
        self.assertEqual(res["engines"]["v1"]["total"], 30)

    def test_validate_cross_check_never_uses_raw(self):
        spread(self.tenant, S, END, 30, "fw")
        s = self.connect()
        n0 = len(self.tenant.calls)
        v = s.validate(self.spec())
        self.assertTrue(v["ready"], v["checks"])
        self.assertFalse([p for _, p, _ in self.tenant.calls[n0:] if p == "/v2/streamviewer/raw-search"])


# ── DI-16: the cross-check is independent of the zero ──
class CrossCheckIndependence(_Session):
    def setUp(self):
        super().setUp()
        self.tenant.extra_datasets.append("fw")   # a configured dataset with no events: a real zero

    def test_estimate_reports_the_zero_source(self):
        self.tenant.v1_500_on_zero = True
        spread(self.tenant, S, END, 20, "dns")
        code, res = self.run_engine(["export", "--estimate", "--quiet", "--dataset", "fw",
                                     "--start", E.iso(S), "--end", E.iso(END)])
        self.assertEqual(code, 0, res)
        self.assertEqual(res["estimate"]["zero_source"], ["dataview"])

    def test_cross_check_skips_the_engine_that_settled_the_zero(self):
        self.tenant.v1_500_on_zero = True
        spread(self.tenant, S, END, 20, "dns")
        v = self.connect().validate(self.spec())
        # dataview settled the zero, so it cannot confirm it; the old archive search is no longer a second counter
        # (it refuses every filtered search on production tenants), so the zero stays unconfirmed — and says so.
        self.assertIsNone(v["zero_confirmed_by"])
        nm = [c for c in v["checks"] if c["name"] == "Nothing matched"]
        self.assertIn("no second, independent search confirmed it", nm[0]["detail"])
        self.assertEqual([c["status"] for c in nm], ["WARN"])
        self.assertFalse(v["ready"])

    def test_clean_v1_zero_is_confirmed_by_dataview(self):
        spread(self.tenant, S, END, 20, "dns")
        v = self.connect().validate(self.spec())
        self.assertEqual(v["zero_confirmed_by"], "dataview")
        self.assertTrue(v["ok"])
        self.assertFalse(any(c["name"] == "Export plan" for c in v["checks"]))


# ── DI-23: parts are counted before they are combined ──
class MergeChecks(EngineCase):
    def _truncate_and_resume(self, fmt, cut):
        fw = spread(self.tenant, S, END, 200, "fw")
        code, res, run = self.export("--dataset", "fw", "--page-size", "50", start=S, end=END, fmt=fmt, merge=False)
        self.assertEqual(code, 0, self.log[-5:])
        man = self.manifest(run)
        victim = next(x for x in man["slices"] if x["status"] == "COMPLETE" and x["written"] > 3)
        path = os.path.join(run, victim["file"])
        cut(path)
        code, res = self.run_engine(["export", "--resume", run, "--merge", "--quiet"])
        self.assertEqual(code, 1, res)
        self.assertIn(victim["file"], res["error"])
        self.assertFalse(os.path.exists(os.path.join(run, "export." + fmt)))
        self.assertEqual(next(x for x in self.manifest(run)["slices"] if x["i"] == victim["i"])["status"], "INCOMPLETE")
        code, res = self.run_engine(["export", "--resume", run, "--merge", "--quiet"])
        self.assertEqual(code, 0, res)
        out = os.path.join(run, "export." + fmt)
        got = [r["id"] for r in self.read_csv(out)] if fmt == "csv" else ids_of(self.read_rows(out))
        self.assertEqual(sorted(got), sorted(r["id"] for r in fw))

    def test_truncated_ndjson_part_is_fetched_again(self):
        def cut(p):
            with open(p, encoding="utf-8") as fh:
                lines = fh.readlines()
            with open(p, "w", encoding="utf-8") as fh:
                fh.writelines(lines[:-1])
        self._truncate_and_resume("ndjson", cut)

    def test_truncated_gzip_part_is_fetched_again(self):
        def cut(p):
            with open(p, "rb") as fh:
                data = fh.read()
            with open(p, "wb") as fh:
                fh.write(data[:-12])
        self._truncate_and_resume("ndjson.gz", cut)

    def test_csv_part_with_a_missing_row_is_fetched_again(self):
        def cut(p):
            with open(p, encoding="utf-8-sig", newline="") as fh:
                lines = fh.readlines()
            with open(p, "w", encoding="utf-8-sig", newline="") as fh:
                fh.writelines(lines[:-1])
        self._truncate_and_resume("csv", cut)

    def test_merged_file_is_oldest_slice_first_and_newest_first_inside(self):
        spread(self.tenant, S, END, 200, "fw")
        code, res, run = self.export("--dataset", "fw", "--page-size", "50", start=S, end=END)
        self.assertEqual(code, 0)
        slices = sorted((x for x in self.manifest(run)["slices"] if x["status"] == "COMPLETE"), key=lambda x: x["start"])
        self.assertGreater(len(slices), 2)

        def owner(ts):
            k = E.ts_key(ts)
            return next(i for i, x in enumerate(slices) if E.ts_key(x["start"]) <= k < E.ts_key(x["end"]))
        rows = self.merged_rows(run)
        owners = [owner(r["@timestamp"]) for r in rows]
        self.assertEqual(owners, sorted(owners), "slices are not oldest first")
        for i in set(owners):
            ts = [E.parse_ts(r["@timestamp"]) for r, o in zip(rows, owners) if o == i]
            self.assertEqual(ts, sorted(ts, reverse=True), "events inside a slice are not newest first")


# ── DI-24 / PORT-7: time parsing ──
class Timestamps(EngineCase):
    ACCEPTED = ["2026-09-28T00:00:00Z", "2026-09-28 00:00:00+0000", "2026-09-28T00:00:00,000z", "2026-09-28T00:00Z",
                "2026-09-28", "2026-09-28t00:00:00.000000999Z", "2026-09-28T05:30:00+05:30", "2026-09-27T19:00:00-05:00"]
    REJECTED = ["20260928T000000Z", "2026-09-28T24:00:00Z", "2026-13-01", "2026-02-30", "2026-09-28T00:00:00+24:00",
                "2026-09-28T00:00:00+05:61", "2026-09-28T00:00:60Z", "yesterday", "", "1790000000"]

    def test_parse_ts_accepts_only_the_documented_forms(self):
        want = dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc)
        for s in self.ACCEPTED:
            self.assertEqual(E.parse_ts(s).replace(microsecond=0), want, s)
        for s in self.REJECTED:
            with self.assertRaises(ValueError, msg=s):
                E.parse_ts(s)
        with self.assertRaises(E.UserError):
            E.parse_user_ts("2026-09-28T24:00:00Z")

    def test_ts_key_reads_epoch_seconds_and_milliseconds(self):
        self.assertEqual(E.ts_key(1790000000), "2026-09-21T14:13:20")
        self.assertEqual(E.ts_key(1790000000999), "2026-09-21T14:13:20")
        self.assertEqual(E.ts_key(1790000000.75), "2026-09-21T14:13:20")
        for bad in (True, float("nan"), 1e300, "junk"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                E.ts_key(bad)

    def test_unreadable_timestamps_are_counted_and_reported(self):
        fw = spread(self.tenant, S, END, 30, "fw")
        fw[3]["_ts_out"] = "not-a-time"
        fw[9]["_ts_out"] = {"weird": 1}
        fw[12]["_ts_out"] = int(E.parse_ts(fw[12]["@timestamp"]).timestamp() * 1000)     # epoch ms: readable
        code, res, run = self.export("--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        self.assertEqual(res["verdict"], "COMPLETE")
        self.assertEqual(res["bad_timestamps"], 2)
        self.assertEqual(self.manifest(run)["summary"]["bad_timestamps"], 2)
        self.assertTrue(any("could not read as a time" in w for w in res["warnings"]))
        self.assertEqual(set(ids_of(self.merged_rows(run))), {r["id"] for r in fw})


# ── DI-26: strict UTF-8 ──
class StrictUtf8(EngineCase):
    def test_invalid_utf8_is_retried_never_rewritten(self):
        spread(self.tenant, S, END, 40, "fw")
        self.tenant.bad_utf8 = 1
        code, res, run = self.export("--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        self.assertEqual(self.tenant.bad_utf8, 0, "the corrupt answer was never served")
        self.assertFalse([r for r in self.merged_rows(run) if "�" in r.get("message", "")])
        self.assertTrue(any("not valid UTF-8" in line for line in self.log))

    def test_decode_raises_a_shape_error(self):
        with self.assertRaises(E.Retryable) as cm:
            E._decode(b'{"a": "\xff"}', "/x")
        self.assertEqual(cm.exception.kind, "shape")


# ── DI-27: CSV cells ──
class CsvCells(EngineCase):
    def test_csv_writes_true_false_and_empty_cells(self):
        t = type(self.tenant)(enum=list(self.tenant.enum) + ["flag", "note"])
        self.tenant = t
        self.use_tenant(t)
        spread(t, S, END, 10, "fw")
        t.add(T(hours=2), flag=True, note="")
        t.add(T(hours=2, seconds=5), flag=False, note=None)
        code, res, run = self.export("--dataset", "fw", "--fields", "@timestamp,id,flag,note", start=S, end=END,
                                     fmt="csv")
        self.assertEqual(code, 0, self.log[-5:])
        rows = self.read_csv(os.path.join(run, "export.csv"))
        self.assertEqual(sorted(r["flag"] for r in rows if r["flag"]), ["false", "true"])
        self.assertEqual({r["note"] for r in rows}, {""})
        self.assertEqual((E._cell(True), E._cell(False), E._cell(None), E._cell(0)), ("true", "false", "", 0))


# ── SEC-1: no symbolic links inside an export folder ──
@unittest.skipUnless(CAN_LINK, "needs symbolic links")
class Symlinks(EngineCase):
    def setUp(self):
        super().setUp()
        self.outside = tempfile.mkdtemp(prefix="abx-outside-")
        self.target = os.path.join(self.outside, "victim.txt")
        with open(self.target, "w") as fh:
            fh.write("SECRET")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.outside, ignore_errors=True)
        super().tearDown()

    def _untouched(self):
        with open(self.target) as fh:
            self.assertEqual(fh.read(), "SECRET")

    def _planned(self):
        spread(self.tenant, S, END, 40, "fw")
        code, _, run = self.export("--dataset", "fw", "--dry-run", start=S, end=END)
        self.assertEqual(code, 0)
        return run

    def test_symlinked_part_file_is_refused_on_resume(self):
        run = self._planned()
        os.symlink(self.target, os.path.join(run, self.manifest(run)["slices"][0]["file"]))
        with self.assertRaises(E.UserError):
            E.load_manifest(run)
        code, res = self.run_engine(["export", "--resume", run, "--merge", "--quiet"])
        self.assertEqual(code, 2)
        self.assertIn("symbolic link", res["error"])
        self._untouched()

    def test_symlinked_manifest_is_refused(self):
        run = self._planned()
        real = os.path.join(self.outside, "manifest.json")
        os.replace(os.path.join(run, "manifest.json"), real)
        os.symlink(real, os.path.join(run, "manifest.json"))
        with self.assertRaises(E.UserError):
            E.load_manifest(run)

    def test_writer_never_writes_through_a_link(self):
        link = os.path.join(self.home, "part-00000.ndjson")
        os.symlink(self.target, link)
        for fmt in ("ndjson", "ndjson.gz", "csv"):
            with self.assertRaises(E.Fatal):
                E.Writer(link, fmt, ["id"])
        self._untouched()

    def test_merge_refuses_a_symlinked_part(self):
        spread(self.tenant, S, END, 40, "fw")
        code, _, run = self.export("--dataset", "fw", start=S, end=END, merge=False)
        self.assertEqual(code, 0)
        man = self.manifest(run)
        part = os.path.join(run, man["slices"][0]["file"])
        os.remove(part)
        os.symlink(self.target, part)
        with self.assertRaises(E.Fatal):
            E.merge_parts(run, man)
        self._untouched()

    def test_planted_temporary_files_are_not_followed(self):
        run = self._planned()
        os.symlink(self.target, os.path.join(run, ".manifest.json.tmp"))
        E.save_manifest(run, self.manifest(run))
        self._untouched()
        self.assertFalse(os.path.islink(os.path.join(run, "manifest.json")))
        code, res = self.run_engine(["export", "--resume", run, "--merge", "--quiet"])
        self.assertEqual(code, 0, res)
        os.symlink(self.target, os.path.join(run, "export.ndjson.tmp"))
        man = self.manifest(run)
        for x in man["slices"]:
            with open(os.path.join(run, x["file"]), "w") as fh:
                fh.write("".join(json.dumps({"id": i}) + "\n" for i in range(x["written"])))
        E.merge_parts(run, man, keep_parts=True)
        self._untouched()


# ── PERF-8 / UX-16: Validate can be stopped ──
class ValidateCancel(_Session):
    def test_cancel_validate_stops_a_running_validate(self):
        spread(self.tenant, S, END, 60, "fw")
        s = self.connect()
        seen = {"n": 0}

        def hook(tenant, method, path, body):
            if path in tenant.SEARCHES:
                seen["n"] += 1
                if seen["n"] == 3:
                    self.assertTrue(s.cancel_validate())

        self.tenant.on_request = hook
        with self.assertRaises(E.Cancelled) as cm:
            s.validate(self.spec())
        self.assertIn("Validation stopped", str(cm.exception))
        n = seen["n"]
        self.tenant.on_request = None
        self.assertLess(n, 6, "the validate kept searching after it was stopped")
        self.assertIsNone(E._TL.cancel)
        self.assertTrue(s.validate(self.spec())["ready"], "a stopped validate left the next one cancelled")

    def test_a_set_cancel_event_stops_before_any_search(self):
        spread(self.tenant, S, END, 20, "fw")
        s = self.connect()
        ev = threading.Event()
        ev.set()
        n0 = len(self.tenant.calls)
        with self.assertRaises(E.Cancelled):
            s.validate(self.spec(), cancel=ev)
        self.assertEqual(self.tenant.calls[n0:], [])

    def test_cancel_validate_when_idle_is_safe(self):
        self.assertFalse(self.connect().cancel_validate())


# ── PERF-14: no catalogue searches during an export ──
class BusyRules(_Session):
    def test_type_lookups_and_dataset_refresh_wait_for_the_export(self):
        spread(self.tenant, S, END, 20, "fw")
        s = self.connect()
        self.assertEqual(s.field_type("dest_port"), "Float64")
        cached = s.datasets()
        s.job = {"running": True, "run_dir": None}
        n0 = len(self.tenant.calls)
        self.assertEqual(s.field_type("dest_port"), "Float64")          # already learned: no search
        with self.assertRaises(C.Busy):
            s.field_type("source_ipv4")
        with self.assertRaises(C.Busy):
            s.datasets(refresh=True)
        self.assertEqual(s.datasets(), cached)
        s._datasets = None
        with self.assertRaises(C.Busy):
            s.datasets()
        self.assertEqual(self.tenant.calls[n0:], [])
        s.job["running"] = False


# ── UX-14 (k) ──
class Humanize(unittest.TestCase):
    def test_type_errors_use_customer_words(self):
        m = C.humanize("/v1/streamviewer/search: the platform rejected the request (HTTP 422) — {\"detail\": "
                       "\"Invalid field type for field 'dest_port'. Expected 'Float64'\"}")
        self.assertIn("dest_port is a number field", m)
        self.assertNotIn("Float64", m)
        self.assertIn("IP address field", C.humanize("Invalid field type for field 'src'. Expected 'Ipv4'"))


# ── UX-53: resume remembers --merge ──
class ResumeMerge(_Session):
    def test_resume_merges_when_the_original_run_did(self):
        spread(self.tenant, S, END, 300, "fw")
        self.cancel_after(3)
        code, _, run = self.export("--dataset", "fw", "--page-size", "50", start=S, end=END)
        self.assertEqual(code, 130, self.log[-5:])
        self.assertTrue(self.manifest(run)["args"]["merge"])
        self.tenant.on_request = None
        E._TL.cancel = None
        code, res = self.run_engine(["export", "--resume", run, "--quiet"])
        self.assertEqual(code, 0, res)
        self.assertEqual(res["merged_file"], "export.ndjson")

    def test_session_resume_keeps_an_unmerged_run_unmerged(self):
        spread(self.tenant, S, END, 40, "fw")
        code, _, run = self.export("--dataset", "fw", "--dry-run", start=S, end=END, merge=False)
        self.assertEqual(code, 0)
        s = self.connect()
        s.resume(run)
        res = s.wait(poll=0.01)["result"]
        self.assertEqual(res["exit_code"], 0, res)
        self.assertIsNone(res["merged_file"])
        self.assertFalse(os.path.exists(os.path.join(run, "export.ndjson")))

    def test_session_resume_of_an_older_manifest_still_merges(self):
        spread(self.tenant, S, END, 40, "fw")
        code, _, run = self.export("--dataset", "fw", "--dry-run", start=S, end=END, merge=False)
        man = self.manifest(run)
        del man["args"]["merge"]
        E.save_manifest(run, man)
        s = self.connect()
        s.resume(run)
        res = s.wait(poll=0.01)["result"]
        self.assertEqual(res["merged_file"], "export.ndjson", res)
        for k in ("zero_result", "bad_timestamps", "filter_unproven", "warnings"):
            self.assertIn(k, res)


# ── KQL removed (2026-10-01): saved KQL searches explain themselves and are never run ──
class KqlByTenant(_Session):
    """KQL came back in 0.3.0 as a per-tenant capability: it runs only where this session proved the tenant applies
    it (a value that cannot exist matches nothing). Where the tenant ignores KQL it is refused, with the reason."""

    def _spec(self):
        sp = C.empty_spec()
        sp.update(mode="kql", kql='events | where data_stream.dataset == "fw"', time={"start": E.iso(S), "end": E.iso(END)})
        return sp

    def test_a_kql_search_is_refused_where_the_tenant_ignores_kql(self):
        spread(self.tenant, S, END, 20, "fw")
        self.tenant.ignore_kql = True
        s = self.connect()
        sp = self._spec()
        for call in (lambda: s.preview(sp, 5), lambda: s.start_export(sp)):
            with self.assertRaises(E.UserError) as cm:
                call()
            self.assertIn("KQL", str(cm.exception))
        v = s.validate(sp)
        self.assertFalse(v["ok"])
        self.assertEqual(self.tenant.unhandled, [])

    def test_a_kql_search_runs_where_the_tenant_applies_kql(self):
        spread(self.tenant, S, END, 20, "fw")
        spread(self.tenant, S, END, 7, "dns")
        s = self.connect()
        r = s.preview(self._spec(), 5)
        self.assertEqual(r["total"], 20)
        self.assertTrue(s.validate(self._spec())["ok"])
        self.assertEqual(self.tenant.unhandled, [])

    def test_the_old_probe_name_stays_gone(self):
        self.assertFalse(hasattr(C.Session, "kql_supported"))
        self.assertTrue(hasattr(C.Session, "kql_on"))


# ── PORT-22: the CLI runs its command on a worker thread ──
class CliWorker(_Session):
    def test_the_cli_command_runs_on_a_worker_and_returns_its_result(self):
        spread(self.tenant, S, END, 40, "fw")
        names = set()
        self.tenant.on_request = lambda t, m, p, b: names.add(threading.current_thread().name)
        code, res, run = self.export("--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 0)
        self.assertEqual(res["rows_written"], 40)
        self.assertEqual(names, {"abx-cli"})

    def test_ctrl_c_stops_the_export_promptly_and_leaves_it_resumable(self):
        spread(self.tenant, S, END, 300, "fw")
        seen = {"n": 0}

        def hook(tenant, method, path, body):
            if path == "/v1/streamviewer/search" and (body.get("page_size") or 0) > 1 \
                    and "zz_abx_field_probe" not in (body.get("selected_fields") or []):
                seen["n"] += 1
                if seen["n"] == 3:
                    _thread.interrupt_main()                     # what Ctrl+C does
                    time.sleep(0.7)                              # the main thread handles it while this read "blocks"

        self.tenant.on_request = hook
        code, res, run = self.export("--dataset", "fw", "--page-size", "50", start=S, end=END)
        self.assertEqual(code, 130, self.log[-5:])
        self.assertTrue(any("press Ctrl+C again" in line for line in self.log))
        self.assertFalse(os.path.exists(os.path.join(run, ".lock")), "the lock was not released")
        self.assertLessEqual(seen["n"], 4, "the export kept fetching after Ctrl+C")
        self.tenant.on_request = None
        code, res = self.run_engine(["export", "--resume", run, "--quiet"])
        self.assertEqual(code, 0, res)
        self.assertEqual(res["rows_written"], 300)

    def test_front_ends_run_on_their_own_thread(self):
        spread(self.tenant, S, END, 40, "fw")
        names = set()
        s = self.connect()
        self.tenant.on_request = lambda t, m, p, b: names.add(threading.current_thread().name)
        s.validate(self.spec())
        self.assertNotIn("abx-cli", names)


class LockProbe(unittest.TestCase):
    def test_pid_probe_never_signals_a_process_on_windows(self):
        self.assertTrue(E.pid_alive(os.getpid()))
        self.assertFalse(E.pid_alive(0))
        real_kill, real_name = os.kill, os.name

        def no_kill(*a):
            raise AssertionError("os.kill called: on Windows it terminates the process")
        os.kill = no_kill
        try:
            os.name = "nt"
            self.assertTrue(E.pid_alive(os.getpid() + 1))
        finally:
            os.kill, os.name = real_kill, real_name


# ── CQ-14: bounded retries inside a slice attempt ──
class SliceRetries(EngineCase):
    def test_fetches_and_counts_in_a_slice_use_two_attempts(self):
        spread(self.tenant, S, END, 200, "fw")
        real, seen = E.with_retry, []

        def spy(fn, what, quiet, attempts=3):
            seen.append((what, attempts))
            return real(fn, what, quiet, attempts)
        E.with_retry = spy
        try:
            code, _, _ = self.export("--dataset", "fw", "--page-size", "50", start=S, end=END)
        finally:
            E.with_retry = real
        self.assertEqual(code, 0)
        slice_calls = [a for w, a in seen if w in ("fetch", "count")]
        self.assertTrue(slice_calls)
        self.assertEqual(set(slice_calls), {2})


# ── CQ-20: typed errors, preview = export, progress to 100 ──
class TypedErrors(_Session):
    def test_backend_exception_has_its_own_kind(self):
        for code, payload, kind in ((500, {"message": "Exception occurred in get_events operation."}, "backend_exception"),
                                    (500, {"message": "database unavailable"}, "backend"),
                                    (500, {"detail": "Exception occurred in get_events"}, "backend_exception"),
                                    (500, "<html>Exception occurred</html>", "backend_exception"),
                                    (200, {"message": "Exception occurred in get_events operation."}, "backend_exception")):
            with self.assertRaises(E.Retryable) as cm:
                E.classify_error(code, payload, "/v1/streamviewer/search")
            self.assertEqual(cm.exception.kind, kind, payload)

    def test_only_the_measured_exception_is_settled_as_a_zero(self):
        spread(self.tenant, S, END, 20, "dns")
        self.tenant.v1_500_on_zero = True
        client = E.Client("https://api.fake.example", KEY, self.tenant.vid, retries=0, quiet=True)
        q = E.Query(E.argparse.Namespace(dataset="fw"))
        eng = E.V1Engine(client, q, ["id"], 1, quiet=True)
        self.assertEqual(eng.count(S, END), 0)
        self.assertEqual(eng.zero_source, "dataview")
        for body in ({"detail": "Exception occurred in get_events operation."}, "Exception occurred in get_events"):
            self.tenant.v1_500_body = body                      # the text outside "message", or a plain-text body
            self.assertEqual(eng.count(S, END), 0, body)
            self.assertEqual(eng.zero_source, "dataview")
        self.tenant.v1_500_body = None
        self.tenant.v1_500_message = "database unavailable"
        with self.assertRaises(E.Retryable) as cm:
            eng.count(S, END)
        self.assertEqual(cm.exception.kind, "backend")

    def test_preview_applies_the_dataset_guard(self):
        spread(self.tenant, S, END, 30, "fw")
        spread(self.tenant, S, END, 30, "dns")
        self.tenant.ignore_filter = {"*"}
        with self.assertRaises(E.UserError) as cm:
            self.connect().preview(self.spec(), 25)
        self.assertIn("another dataset", str(cm.exception))

    def test_preview_uses_the_export_field_selection(self):
        spread(self.tenant, S, END, 30, "fw")
        p = self.connect().preview(self.spec(fields=["@timestamp", "id", "message"]), 10)
        self.assertEqual(p["total"], 30)
        self.assertIn("data_stream.dataset", p["columns"])            # added by the export for its dataset guard

    def test_progress_reaches_exactly_100_with_stored_copies(self):
        spread(self.tenant, S, END, 100, "fw")
        crowd_ts = T(hours=2).replace(microsecond=0)
        crowd = [self.tenant.add(crowd_ts) for _ in range(10)]
        for src in crowd[:4]:
            self.tenant.add_copy(src)
        s = self.connect()
        s.start_export(self.spec())
        st = s.wait(poll=0.01)
        self.assertEqual(st["result"]["exit_code"], 0, st["lines"][-5:])
        self.assertEqual(st["result"]["stored_duplicates"], 4)
        self.assertEqual(st["progress"]["rows_expected"], st["progress"]["rows_written"])
        self.assertEqual(st["progress"]["pct"], 100.0)


# ── CQ-27: only real answers are cached; dataset paging ──
class CatalogueCaching(_Session):
    def test_field_type_is_not_cached_after_an_error_answer(self):
        s = self.connect()
        self.tenant.v1_error_body = 1
        self.assertEqual(s.field_type("dest_port"), "String")          # an error body: a guess, not remembered
        self.assertEqual(s.field_type("dest_port"), "Float64")
        n0 = len(self.tenant.calls)
        self.assertEqual(s.field_type("dest_port"), "Float64")
        self.assertEqual(self.tenant.calls[n0:], [])
        self.tenant.fail_first_502 = True
        self.tenant._first_done = False
        s.client.retries = 0
        self.assertEqual(s.field_type("user_name"), "String")          # a transport failure: not remembered
        self.assertNotIn("user_name", s._types)

    def test_datasets_page_past_a_short_page(self):
        spread(self.tenant, S, END, 10, "fw")
        self.tenant.extra_datasets = ["ds-%03d" % i for i in range(120)]
        self.tenant.config_page_cap = 50
        names = {d["name"] for d in self.connect().datasets()}
        self.assertEqual(len(names), 121)


if __name__ == "__main__":
    unittest.main()
