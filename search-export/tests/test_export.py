"""End-to-end export tests: abx_engine.main against the in-memory fake tenant."""
import datetime as dt
import json
import os
import unittest

from support import E, EngineCase, FIXED_NOW, T, spread, ids_of, finding

S, END = T(hours=3), T(hours=1)          # a closed two-hour window inside hot storage


class Completeness(EngineCase):
    def test_export_is_complete_and_exact(self):
        fw = spread(self.tenant, S, END, 300, "fw")
        spread(self.tenant, S, END, 200, "dns")
        # just outside the window on both sides (the end second is excluded)
        self.tenant.add(S - dt.timedelta(seconds=1), dataset="fw")
        self.tenant.add(END, dataset="fw")
        self.tenant.add(END + dt.timedelta(milliseconds=400), dataset="fw")
        code, res, run = self.export("--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        self.assertEqual(res["verdict"], "COMPLETE")
        rows = self.merged_rows(run)
        got = ids_of(rows)
        self.assertEqual(len(got), len(set(got)), "an event was written twice")
        self.assertEqual(set(got), {r["id"] for r in fw})
        self.assertEqual(set(got), self.tenant.unique_ids(S, END, "fw"))
        self.assertEqual(res["rows_written"], 300)
        for r in rows:
            t = E.parse_ts(r["@timestamp"])
            self.assertTrue(S <= t < END, r["@timestamp"])
            self.assertEqual(r["data_stream"]["dataset"], "fw")
        self.assertNoUnhandled()

    def test_whole_second_flooring(self):
        base = T(hours=2).replace(microsecond=0)
        e7 = self.tenant.add(base + dt.timedelta(seconds=7, milliseconds=900))
        e8 = self.tenant.add(base + dt.timedelta(seconds=8, milliseconds=200))
        e9 = self.tenant.add(base + dt.timedelta(seconds=9, milliseconds=100))
        spread(self.tenant, S, base, 20, "fw")
        s, e = base + dt.timedelta(seconds=8.5), base + dt.timedelta(seconds=9.5)
        code, res, run = self.export("--dataset", "fw", start=s, end=e)
        self.assertEqual(code, 0, self.log[-5:])
        self.assertEqual(ids_of(self.merged_rows(run)), [e8["id"]])
        self.assertNotIn(e7["id"], ids_of(self.merged_rows(run)))
        self.assertNotIn(e9["id"], ids_of(self.merged_rows(run)))
        sent = [b for m, p, b in self.tenant.calls if p == "/v1/streamviewer/search"
                and "zz_abx_field_probe" not in (b.get("selected_fields") or [])]
        self.assertTrue(sent)
        for b in sent:
            self.assertTrue(b["start_time"].endswith(".000Z") and b["end_time"].endswith(".000Z"), b)


class StoredCopies(EngineCase):
    FIELDS = "@timestamp,id,data_stream.dataset,message,ingested_time,insertion_time"

    def _dense(self, seconds=5, per_second=60):
        """per_second events inside each of `seconds` seconds, so copies can sit ~50 rows from the original."""
        out = []
        base = T(hours=2).replace(microsecond=0)
        for s in range(seconds):
            for i in range(per_second):
                out.append(self.tenant.add(base + dt.timedelta(seconds=s * 7, milliseconds=i)))
        return out, base

    def test_stored_copies_are_credited_not_rewritten(self):
        rows, _ = self._dense()
        spread(self.tenant, S, END, 100, "fw")
        # all events of one second share a timestamp only per-ms, so make a same-timestamp crowd
        crowd_ts = T(hours=2, minutes=30).replace(microsecond=0)
        crowd = [self.tenant.add(crowd_ts) for _ in range(80)]
        injected = 0
        for d, src in zip((0, 1, 10, 25, 50, 49, 30, 5), crowd[:8]):
            self.tenant.add_copy(src, reingested=False, distance=d)          # identical copies
            injected += 1
        for d, src in zip((3, 20, 50, 40, 12, 1), crowd[10:16]):
            self.tenant.add_copy(src, reingested=True, distance=d)           # differ only in ingest bookkeeping
            injected += 1
        far = max(self.tenant.order_distance(r["id"]) for r in crowd[:16])
        self.assertGreaterEqual(far, 45, "the fake did not place copies far apart")
        unique = self.tenant.unique_ids(S, END, "fw")
        code, res, run = self.export("--dataset", "fw", "--fields", self.FIELDS, start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        self.assertEqual(res["verdict"], "COMPLETE")
        got = ids_of(self.merged_rows(run))
        self.assertEqual(len(got), len(set(got)))
        self.assertEqual(set(got), unique)
        self.assertEqual(res["rows_written"], len(unique))
        self.assertEqual(res["stored_duplicates"], injected)
        self.assertEqual(self.manifest(run)["summary"]["reingested_copies"], 6)
        self.assertEqual(res["id_conflicts"], 0)

    def test_copies_far_apart_when_the_slice_is_split(self):
        """Copies ~50 rows away from their original, in a window that must be split to fit one response."""
        crowd_ts = T(hours=2).replace(microsecond=0)
        crowd = [self.tenant.add(crowd_ts) for _ in range(40)]
        spread(self.tenant, S, END, 360, "fw")
        for d, src in zip((39, 30, 20, 1), crowd[:4]):
            self.tenant.add_copy(src, reingested=(d % 2 == 0), distance=d)
        self.assertGreaterEqual(max(self.tenant.order_distance(r["id"]) for r in crowd[:4]), 30)
        unique = self.tenant.unique_ids(S, END, "fw")
        code, res, run = self.export("--dataset", "fw", "--fields", self.FIELDS, "--page-size", "50",
                                     start=S, end=END)
        self.assertEqual(code, 0, self.log[-8:])
        self.assertEqual(set(ids_of(self.merged_rows(run))), unique)
        self.assertEqual(res["rows_written"], len(unique))
        self.assertEqual(res["stored_duplicates"], 4)

    def test_two_different_events_sharing_an_id_are_both_written(self):
        spread(self.tenant, S, END, 50, "fw")
        ts = T(hours=2).replace(microsecond=0)
        self.tenant.add(ts, eid="shared-1", message="first event")
        self.tenant.add(ts, eid="shared-1", message="a different event")
        code, res, run = self.export("--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        msgs = sorted(r["message"] for r in self.merged_rows(run) if r["id"] == "shared-1")
        self.assertEqual(msgs, ["a different event", "first event"])
        self.assertEqual(res["id_conflicts"], 1)
        self.assertEqual(res["rows_written"], 52)
        self.assertEqual(res["verdict"], "COMPLETE")

    def test_id_shared_across_slices_keeps_both_events(self):
        """Two different events share an id but sit in different slices: both are written. id_conflicts is
        counted within a time slice only, by design: a global id map would grow with every exported event."""
        spread(self.tenant, S, END, 200, "fw")
        self.tenant.add(S + dt.timedelta(minutes=5), eid="shared-2", message="early")
        self.tenant.add(END - dt.timedelta(minutes=5), eid="shared-2", message="late")
        code, res, run = self.export("--dataset", "fw", "--page-size", "50", start=S, end=END)
        self.assertEqual(code, 0)
        self.assertEqual(len([r for r in self.merged_rows(run) if r["id"] == "shared-2"]), 2)
        self.assertGreater(len(self.manifest(run)["slices"]), 2)
        self.assertEqual(res["id_conflicts"], 0)


class Splitting(EngineCase):
    def test_slice_larger_than_one_response_splits_and_reconciles(self):
        # 400 events packed into 40 seconds (10 per second): the planned slices hold far more than one response
        base = T(hours=2).replace(microsecond=0)
        for s in range(40):
            for i in range(10):
                self.tenant.add(base + dt.timedelta(seconds=s, milliseconds=i * 50))
        spread(self.tenant, S, END, 50, "dns")
        code, res, run = self.export("--dataset", "fw", "--page-size", "50", start=S, end=END)
        self.assertEqual(code, 0, self.log[-8:])
        self.assertEqual(res["verdict"], "COMPLETE")
        man = self.manifest(run)
        self.assertTrue(any(x["status"] == "SPLIT" for x in man["slices"]), "nothing was split")
        got = ids_of(self.merged_rows(run))
        self.assertEqual(len(got), len(set(got)))
        self.assertEqual(set(got), self.tenant.unique_ids(S, END, "fw"))
        self.assertEqual(res["rows_written"], res["rows_expected"])
        for x in man["slices"]:
            if x["status"] == "COMPLETE":
                self.assertLessEqual(x["server_total"], 50)
                self.assertEqual(x["written"] + x["stored_duplicates"] + x["outside"], x["server_total"])

    def test_v1_refusing_wide_windows_bisects_to_completion(self):
        """Busy tenants answer wide v1 windows with the same 500 they use for zero; the plan must split, not
        treat it as zero."""
        fw = spread(self.tenant, S, END, 240, "fw")
        self.tenant.v1_500_wider_than = 1800
        code, res, run = self.export("--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 0, self.log[-8:])
        self.assertEqual(set(ids_of(self.merged_rows(run))), {r["id"] for r in fw})

    def test_v1_500_on_every_window_never_ends_complete(self):
        spread(self.tenant, S, END, 60, "fw")
        self.tenant.v1_500_wider_than = 0            # v1 refuses everything, even one second
        code, res, run = self.export("--dataset", "fw", start=S, end=END)
        self.assertNotEqual(code, 0)
        self.assertNotEqual(res.get("verdict"), "COMPLETE")
        self.assertIn("refus", res.get("error") or "", res)


    def test_dense_second_with_copies_across_pages_completes(self):
        """Regression (fixed in 1.1.0): one second holding more events than one response, with an identical stored copy on the
        other side of a page boundary, can never complete.

        A slice cannot be narrower than one second, so such a second is paged (run_slice). SliceSink treats a
        byte-identical row seen in a DIFFERENT response as a paging re-serve and does not credit it, so the
        slice never reconciles ("accounted for 121 events but the platform counts 124 (3 re-served across
        pages)"), every retry fails the same way, and resume repeats it: exit 1, INCOMPLETE, forever. With copies
        measured up to ~100 rows apart this hits any second above one response (5,000 rows for a field list,
        2,000 for every field) — high-volume feeds, not typical datasets. It fails safe (INCOMPLETE, never a
        wrong COMPLETE). Fix ideas: for FIELD-LIST exports, retry a one-second slice as a single response at
        page_max (10,000) before paging; every-field pages cannot grow (FULL_DOC_PAGE = 2000 because 5,000
        returned 502s), so there, accept byte-identical cross-page repeats as stored copies when the paged total
        reconciles exactly and the cursor advanced strictly.
        """
        ts = T(hours=2).replace(microsecond=0)
        crowd = [self.tenant.add(ts) for _ in range(120)]
        for d, src in zip((60, 70, 80), crowd[:3]):
            self.tenant.add_copy(src, distance=d)
        spread(self.tenant, S, END, 100, "fw")
        code, res, run = self.export("--dataset", "fw", "--page-size", "50", start=S, end=END)
        self.assertEqual(code, 0, res.get("error"))
        self.assertEqual(set(ids_of(self.merged_rows(run))), self.tenant.unique_ids(S, END, "fw"))


class ZeroAnd500(EngineCase):
    def setUp(self):
        super().setUp()
        self.tenant.extra_datasets.append("fw")   # a configured dataset with no events: a real zero

    def test_v1_500_for_zero_is_confirmed_by_dataview(self):
        self.tenant.v1_500_on_zero = True
        fw = spread(self.tenant, S, S + dt.timedelta(minutes=50), 40, "fw")   # the second hour is empty
        spread(self.tenant, S, END, 40, "dns")
        code, res, run = self.export("--dataset", "fw", "--slice", "30m", start=S, end=END)
        self.assertEqual(code, 0, self.log[-8:])
        self.assertEqual(res["verdict"], "COMPLETE")
        self.assertEqual(set(ids_of(self.merged_rows(run))), {r["id"] for r in fw})
        zero = [x for x in self.manifest(run)["slices"] if x["status"] == "COMPLETE" and x["server_total"] == 0]
        self.assertTrue(zero, "no empty slice was exercised")
        for x in zero:
            self.assertEqual(x["zero_source"], "dataview")
        self.assertTrue(any(p == "/v1/dataview/search" for _, p, _ in self.tenant.calls))
        self.assertIsNone(res.get("error"))

    def test_dataset_with_no_events_completes_as_zero(self):
        self.tenant.v1_500_on_zero = True
        spread(self.tenant, S, END, 40, "dns")
        code, res, run = self.export("--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 0, self.log[-8:])
        self.assertEqual(res["verdict"], "COMPLETE")
        self.assertEqual(res["rows_written"], 0)
        self.assertEqual(self.merged_rows(run), [])


class FilterProofs(EngineCase):
    def test_ignored_filter_is_refused(self):
        spread(self.tenant, S, END, 100, "fw")
        spread(self.tenant, S, END, 100, "dns")
        self.tenant.ignore_filter = {"*"}
        code, res, run = self.export("--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 2)
        self.assertIn("cannot exist", res["error"])                   # the impossible-value control fired
        self.assertFalse(os.path.exists(os.path.join(run, "export.ndjson")))

    def test_ignored_where_filter_is_refused(self):
        spread(self.tenant, S, END, 100, "fw", user_name="bob")
        spread(self.tenant, S, END, 20, "fw", user_name="alice")
        spread(self.tenant, S, END, 100, "dns")
        self.tenant.ignore_filter = {"user_name"}
        code, res, run = self.export("--dataset", "fw", "--where", "user_name=alice", start=S, end=END)
        self.assertEqual(code, 2)
        self.assertIn("filter on user_name", res["error"])
        self.assertIn("cannot exist", res["error"])

    def test_ignored_filter_with_an_empty_sample_hour_is_refused(self):
        """Regression (fixed in 1.1.0): an ignored filter is exported as if it worked when the first hour of the range is empty.

        The impossible-value controls run only on [start, start+1h). When the dataset has no events there, every
        control returns 0 and "passes" without testing anything. The later comparison on the busiest window
        (cmd_export: pre["comparison"] in ("UNKNOWN", "EMPTY")) shows filtered < unfiltered because the DATASET
        filter works, so preflight_gate says PASS and the ignored user_name filter is exported: every fw event
        instead of alice's, verdict COMPLETE, exit 0. Fix idea: when the sample window is empty, re-run the
        impossible-value controls on the busiest counted window as well, not just the comparison.
        """
        late = S + dt.timedelta(minutes=70)                      # nothing in the first hour
        spread(self.tenant, late, END, 100, "fw", user_name="bob")
        alice = spread(self.tenant, late, END, 20, "fw", user_name="alice")
        spread(self.tenant, S, END, 100, "dns")
        self.tenant.ignore_filter = {"user_name"}
        code, res, run = self.export("--dataset", "fw", "--where", "user_name=alice", start=S, end=END)
        if code == 0:
            self.assertEqual(set(ids_of(self.merged_rows(run))), {r["id"] for r in alice},
                             "exported events the filter should have excluded")
        self.assertEqual(code, 2)

    def test_kql_runs_only_where_the_tenant_applies_it(self):
        """KQL runs only after a check proves this tenant applies it; elsewhere it is refused before any export."""
        fw = spread(self.tenant, S, END, 20, "fw")
        spread(self.tenant, S, END, 10, "dns")
        code, res, run = self.export("--kql", 'events | where data_stream.dataset == "fw"', start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        self.assertEqual(res["rows_written"], len(fw))
        self.tenant.ignore_kql = True
        code, res, run = self.export("--kql", 'events | where data_stream.dataset == "fw"', start=S, end=END,
                                     name="b")
        self.assertEqual(code, 2)
        self.assertIn("KQL is not available", res["error"])
        self.assertFalse(os.path.exists(run))

    def test_resuming_a_kql_export_needs_kql_on_the_tenant(self):
        spread(self.tenant, S, END, 30, "fw")
        code, _, run = self.export("--dataset", "fw", "--dry-run", start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        man = self.manifest(run)
        man["query"]["kql"] = 'events | where data_stream.dataset == "fw"'
        man["query"]["dataset"] = None
        man["args"]["dataset"] = None
        with open(os.path.join(run, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(man, fh)
        self.tenant.ignore_kql = True
        code, res = self.run_engine(["export", "--resume", run, "--quiet"])
        self.assertEqual(code, 2)
        self.assertIn("KQL", res["error"])

    def test_unknown_field_is_refused_before_export(self):
        spread(self.tenant, S, END, 10, "fw")
        code, res, run = self.export("--dataset", "fw", "--fields", "@timestamp,id,no_such_field", start=S, end=END)
        self.assertEqual(code, 2)
        self.assertIn("no_such_field", res["error"])


class ResumeAndManifest(EngineCase):
    def _data(self):
        spread(self.tenant, S, END, 400, "fw")
        spread(self.tenant, S, END, 60, "dns")
        crowd_ts = T(hours=2).replace(microsecond=0)
        crowd = [self.tenant.add(crowd_ts) for _ in range(20)]
        for d, src in zip((19, 10, 3), crowd):
            self.tenant.add_copy(src, distance=d)

    def test_resume_after_cancel_matches_an_uninterrupted_run(self):
        self._data()
        n0 = len(self.tenant.calls)
        code, full, run_a = self.export("--dataset", "fw", "--page-size", "50", start=S, end=END, name="a")
        self.assertEqual(code, 0, self.log[-8:])
        self.full_fetches = sum(1 for m, p, b in self.tenant.calls[n0:] if p == "/v1/streamviewer/search"
                                and (b.get("page_size") or 0) > 1
                                and "zz_abx_field_probe" not in (b.get("selected_fields") or []))
        ids_a = ids_of(self.merged_rows(run_a))

        self.cancel_after(5)
        code, res, run_b = self.export("--dataset", "fw", "--page-size", "50", start=S, end=END, name="b")
        self.assertEqual(code, 130, self.log[-8:])
        self.assertFalse(os.path.exists(os.path.join(run_b, "export.ndjson")))
        st = [x["status"] for x in self.manifest(run_b)["slices"]]
        self.assertIn("COMPLETE", st, "the cancel came before any slice was saved as complete")
        self.assertTrue(set(st) - {"COMPLETE", "SPLIT"}, "nothing was left to resume")
        self.tenant.on_request = None
        E._TL.cancel = None

        def fetches():
            return sum(1 for m, p, b in self.tenant.calls if p == "/v1/streamviewer/search"
                       and (b.get("page_size") or 0) > 1 and "zz_abx_field_probe" not in (b.get("selected_fields") or []))
        n0 = fetches()
        code, res = self.run_engine(["export", "--resume", run_b, "--merge", "--quiet"])
        resumed_fetches = fetches() - n0
        self.assertEqual(code, 0, self.log[-8:])
        self.assertEqual(res["verdict"], "COMPLETE")
        ids_b = ids_of(self.merged_rows(run_b))
        self.assertEqual(len(ids_b), len(set(ids_b)), "the resumed export has duplicates")
        self.assertEqual(sorted(ids_b), sorted(ids_a))
        self.assertEqual(res["rows_written"], full["rows_written"])
        self.assertEqual(res["stored_duplicates"], full["stored_duplicates"])
        self.assertLess(resumed_fetches, self.full_fetches, "the resume re-ran the whole export")

    def test_resume_against_a_different_tenant_is_refused(self):
        self._data()
        code, _, run = self.export("--dataset", "fw", "--dry-run", start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        other = type(self.tenant)(vid="VAother0002", name="Test Tenant Beta")
        spread(other, S, END, 10, "fw")
        self.use_tenant(other)
        code, res = self.run_engine(["export", "--resume", run, "--merge", "--quiet"])
        self.assertEqual(code, 2)
        self.assertIn("belongs to", res["error"])
        self.assertFalse([c for c in other.calls if c[1] != "/v2/auth/"], "searched the wrong tenant")

    def test_manifest_part_file_traversal_is_refused(self):
        self._data()
        code, _, run = self.export("--dataset", "fw", "--dry-run", start=S, end=END)
        self.assertEqual(code, 0)
        mp = os.path.join(run, "manifest.json")
        with open(mp, encoding="utf-8") as fh:
            man = json.load(fh)
        escape = "abx-traversal-%s" % os.path.basename(self.home)        # unique: proves nothing was written there
        man["slices"][0]["file"] = "../../" + escape
        with open(mp, "w", encoding="utf-8") as fh:
            json.dump(man, fh)
        with self.assertRaises(E.UserError):
            E.load_manifest(run)
        n_calls = len(self.tenant.calls)
        code, res = self.run_engine(["export", "--resume", run, "--merge", "--quiet"])
        self.assertEqual(code, 2)
        self.assertIn("unexpected part file", res["error"])
        self.assertEqual(len(self.tenant.calls), n_calls, "called the API before refusing the manifest")
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(os.path.dirname(run)), escape)))


class Formats(EngineCase):
    def test_csv_formula_injection_prefix(self):
        bad = ["=HYPERLINK(\"https://example.invalid\",\"x\")", "+1+1", "-2", "@SUM(A1)", "\tTAB", "\rCR", "plain"]
        for i, m in enumerate(bad):
            self.tenant.add(T(hours=2) + dt.timedelta(seconds=i), message=m)
        code, res, run = self.export("--dataset", "fw", "--fields", "@timestamp,id,message", start=S, end=END,
                                     fmt="csv")
        self.assertEqual(code, 0, self.log[-5:])
        got = {r["message"] for r in self.read_csv(os.path.join(run, "export.csv"))}
        for m in bad[:-1]:
            self.assertIn("'" + m, got)
        self.assertIn("plain", got)
        self.assertEqual(E._cell("=1+1"), "'=1+1")
        self.assertEqual(E._cell(5), 5)

    def test_ndjson_gz_roundtrip(self):
        fw = spread(self.tenant, S, END, 30, "fw")
        code, res, run = self.export("--dataset", "fw", start=S, end=END, fmt="ndjson.gz")
        self.assertEqual(code, 0)
        self.assertEqual({r["id"] for r in self.read_rows(os.path.join(run, "export.ndjson.gz"))},
                         {r["id"] for r in fw})


class Transport(EngineCase):
    def test_429_waits_for_retry_after_and_retries(self):
        fw = spread(self.tenant, S, END, 50, "fw")
        self.tenant.rate_limit_429 = 2
        self.tenant.retry_after = "7"
        code, res, run = self.export("--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        self.assertEqual(self.naps.count(7.0), 2, self.naps)
        self.assertEqual(set(ids_of(self.merged_rows(run))), {r["id"] for r in fw})

    def test_502_on_the_first_call_is_retried(self):
        fw = spread(self.tenant, S, END, 50, "fw")
        self.tenant.fail_first_502 = True
        code, res, run = self.export("--dataset", "fw", start=S, end=END)
        self.assertEqual(code, 0, self.log[-5:])
        self.assertTrue(self.naps, "no backoff happened")
        self.assertEqual(set(ids_of(self.merged_rows(run))), {r["id"] for r in fw})


def _today():
    return FIXED_NOW.replace(hour=0, minute=0, second=0, microsecond=0)


class Retention(EngineCase):
    """Recent data keeps hot_days; older events are read with archive jobs. The tool's edge sits EDGE_MARGIN_DAYS
    inside recent data, so the setup removes recent data older than two days ago: if the tool read any of that part
    from recent data instead of the archive, events would be missing."""
    RS, RE = T(days=3), T(hours=1)

    def setUp(self):
        super().setUp()
        self.tenant.hot_days = 4                                     # tool edge: start of yesterday
        self.tenant.hot_since = _today() - dt.timedelta(days=2)      # recent data really holds less than that
        self.fw = spread(self.tenant, self.RS, self.RE, 240, "fw")
        spread(self.tenant, self.RS, self.RE, 80, "dns")

    def ids(self, run):
        got = ids_of(self.merged_rows(run))
        self.assertEqual(len(got), len(set(got)))
        return set(got)

    def test_older_part_is_read_with_archive_jobs(self):
        code, res, run = self.export("--dataset", "fw", start=self.RS, end=self.RE)
        self.assertEqual(code, 0, self.log[-8:])
        man = self.manifest(run)
        self.assertEqual((man["coverage"] or {}).get("switched_to"), "archive")
        self.assertTrue(any(x["engine"] == "coldquery" for x in man["slices"]))
        self.assertTrue(man["jobs"], "archive job ids are kept in the manifest")
        self.assertEqual(self.ids(run), {r["id"] for r in self.fw})
        self.assertFalse(any(c[1] == "/v1/coldstorage/v2/search" for c in self.tenant.calls))

    def test_archive_patterns_and_list_contains_are_exact(self):
        """Measured: the archive matches patterns with exact case and list 'contains' on whole items only, so
        those leaves are sent as 'has a value' and the tool keeps what matches."""
        for i, r in enumerate(self.fw):
            r["user_name"] = ["alice@corp.com", "Bob@corp.com", None][i % 3]
            r["tags"] = [["identity:employee"], ["prod"], []][i % 3]
        self.tenant.types.update({"user_name": "String", "tags": "List(String)"})
        self.tenant.enum = self.tenant.enum + ["tags"]
        for name, leaf, want in [
                ("rx", {"field": "user_name", "operation": "MATCHES_REGEX", "field_type": "String", "case_sensitive": False,
                        "value": "^ALICE"}, lambda r: (r.get("user_name") or "").lower().startswith("alice")),
                ("lc", {"field": "tags", "operation": "CONTAINS", "field_type": "List(String)", "case_sensitive": False,
                        "value": "ident"}, lambda r: any("ident" in t for t in r.get("tags") or []))]:
            cond = {"operator": "and", "conditions": [leaf], "groups": []}
            code, res, run = self.export("--dataset", "fw", "--condition-json", json.dumps(cond), start=self.RS,
                                         end=self.RE, name=name)
            self.assertEqual(code, 0, self.log[-6:])
            self.assertTrue(any(x["engine"] == "coldquery" for x in self.manifest(run)["slices"]))
            self.assertEqual(self.ids(run), {r["id"] for r in self.fw if want(r)}, name)

    def test_job_with_failed_files_is_run_again(self):
        self.tenant.job_failed_files = 1
        code, res, run = self.export("--dataset", "fw", start=self.RS, end=self.RE)
        self.assertEqual(code, 0, self.log[-8:])
        self.assertGreaterEqual(len(self.tenant.jobs), 2)
        self.assertEqual(self.ids(run), {r["id"] for r in self.fw})

    def test_paused_job_is_run_again(self):
        self.tenant.job_pause = 1
        code, res, run = self.export("--dataset", "fw", start=self.RS, end=self.RE)
        self.assertEqual(code, 0, self.log[-8:])
        self.assertGreaterEqual(len(self.tenant.jobs), 2)
        self.assertEqual(self.ids(run), {r["id"] for r in self.fw})

    def test_job_the_platform_no_longer_knows_is_started_again(self):
        self.tenant.job_forget = 1
        code, res, run = self.export("--dataset", "fw", start=self.RS, end=self.RE)
        self.assertEqual(code, 0, self.log[-8:])
        self.assertEqual(self.ids(run), {r["id"] for r in self.fw})

    def test_archive_that_ignores_the_filter_stops_the_export(self):
        self.tenant.job_ignore_filter = True
        code, res, run = self.export("--dataset", "fw", start=self.RS, end=self.RE)
        self.assertNotEqual(code, 0)
        self.assertIn("does not match the search", str(res.get("error")) + " ".join(map(str, self.log[-6:])))

    def test_archive_not_available_means_exit_3(self):
        self.tenant.jobs_enabled = False
        code, res, run = self.export("--dataset", "fw", start=self.RS, end=self.RE)
        self.assertEqual(code, 3, self.log[-8:])
        self.assertTrue(res["coverage_unproven"])
        hot = {r["id"] for r in self.fw if E.parse_ts(r["@timestamp"]) >= self.tenant.hot_since}
        self.assertEqual(self.ids(run), hot)

    def test_exit_3_is_worded_as_partial_with_the_range_not_searched(self):
        import io as _io
        import contextlib as _cl
        import abx_wizard as W
        import abx_core as C
        self.tenant.jobs_enabled = False
        code, res, run = self.export("--dataset", "fw", start=self.RS, end=self.RE)
        self.assertEqual(code, 3)
        self.assertEqual(res["verdict"], "PARTIAL")                        # a script testing "COMPLETE" never mistakes it
        w = res["coverage_warning"]
        self.assertTrue(w.startswith("Partial: recent data only. Older data was not searched."), w)
        self.assertIn("Not searched: " + E.iso(self.RS)[:16].replace("T", " ") + " to ", w)
        self.assertIn(w, res["warnings"])
        self.assertTrue(self.manifest(run)["summary"]["coverage"].startswith("Partial: recent data only."))
        out = _io.StringIO()
        with _cl.redirect_stdout(out):
            W.show_result(res)
        self.assertIn("PARTIAL: RECENT DATA ONLY", out.getvalue())
        self.assertIn("Older data was not searched", out.getvalue())
        self.assertNotIn("COMPLETE", out.getvalue().upper().replace("COMPLETE WITH", ""))
        listed = [x for x in C.list_exports(None) if x["run_dir"] == run]
        self.assertTrue(listed and listed[0]["partial"], listed)
        self.assertEqual((listed[0]["status"], listed[0]["verdict"]), ("PARTIAL", "PARTIAL"))
        self.assertTrue(listed[0]["partial_note"].startswith("Partial: recent data only."))
        self.assertFalse(listed[0]["resumable"])

    def test_a_partial_export_stays_partial_when_run_again(self):
        self.tenant.jobs_enabled = False
        code, res, run = self.export("--dataset", "fw", start=self.RS, end=self.RE)
        self.assertEqual((code, res["verdict"]), (3, "PARTIAL"))
        self.assertEqual(self.manifest(run)["summary"]["verdict"], "PARTIAL")
        code, res = self.run_engine(["export", "--resume", run, "--quiet", "--merge"])
        self.assertEqual((code, res["verdict"], res["exit_code"]), (3, "PARTIAL", 3), self.log[-6:])
        self.assertTrue(res["already_complete"])
        self.assertTrue(res["coverage_warning"].startswith("Partial: recent data only."))
        # once the archive can be read, a new export of the same range is complete
        self.tenant.jobs_enabled = True
        code, res, run2 = self.export("--dataset", "fw", start=self.RS, end=self.RE, name="run2")
        self.assertEqual((code, res["verdict"]), (0, "COMPLETE"), self.log[-6:])

    def test_validate_never_starts_an_archive_job(self):
        code, res = self.run_engine(["export", "--estimate", "--quiet", "--dataset", "fw",
                                     "--start", E.iso(self.RS), "--end", E.iso(self.RE)])
        self.assertEqual(code, 0, self.log[-6:])
        self.assertTrue(res["estimate"]["archive"])
        self.assertFalse([c for c in self.tenant.calls if c[1] == "/v1/coldstorage/query/submit"])

    def test_resume_moves_slices_that_aged_out_to_the_archive(self):
        self.tenant.hot_days, self.tenant.hot_since = None, None    # planned while everything was recent
        code, res, run = self.export("--dataset", "fw", "--dry-run", start=self.RS, end=self.RE)
        self.assertEqual(code, 0, self.log[-6:])
        self.assertFalse(any(x["engine"] == "coldquery" for x in self.manifest(run)["slices"]))
        self.tenant.hot_days, self.tenant.hot_since = 4, _today() - dt.timedelta(days=2)   # days later
        code, res = self.run_engine(["export", "--resume", run, "--merge", "--quiet"])
        self.assertEqual(code, 0, self.log[-8:])
        self.assertTrue(any(x["engine"] == "coldquery" for x in self.manifest(run)["slices"]))
        self.assertEqual(self.ids(run), {r["id"] for r in self.fw})

    def test_job_id_is_saved_before_the_job_finishes(self):
        seen = []

        def look(tenant, method, path, body):           # each time the tool asks for the job's status
            if method == "GET" and path.startswith("/v1/coldstorage/query/job"):
                runs = [d for d in os.listdir(self.home) if os.path.isfile(os.path.join(self.home, d, "manifest.json"))]
                if runs:
                    with open(os.path.join(self.home, runs[0], "manifest.json"), encoding="utf-8") as fh:
                        seen.append(bool(json.load(fh).get("jobs")))
        self.tenant.on_request = look
        code, res, run = self.export("--dataset", "fw", start=self.RS, end=self.RE)
        self.assertEqual(code, 0, self.log[-8:])
        self.assertTrue(seen and seen[0], "the job id must be on disk while the job is still running")

    def test_young_tenant_without_a_readable_setting_stays_on_recent_data(self):
        self.tenant.vendor_readable = False
        self.tenant.hot_days, self.tenant.hot_since = None, None
        young = spread(self.tenant, T(days=1), self.RE, 30, "fw")
        start = E.parse_ts(min(r["@timestamp"] for r in self.tenant.rows))
        code, res, run = self.export("--dataset", "fw", start=start, end=self.RE)
        self.assertEqual(code, 0, self.log[-8:])
        self.assertFalse(any(x["engine"] == "coldquery" for x in self.manifest(run)["slices"]))
        self.assertEqual(self.ids(run), {r["id"] for r in self.fw + young})


if __name__ == "__main__":
    unittest.main()
