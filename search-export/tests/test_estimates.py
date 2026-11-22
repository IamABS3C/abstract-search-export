"""How long an export will take: the estimate Validate shows, the time left while it runs, and the time pickers."""
import os
import time
import unittest

from support import C, E, EngineCase, KEY, T, spread

import abx_wizard as W


class Estimates(unittest.TestCase):
    def test_rates_are_the_measured_ones(self):
        e = E.estimate_time(70000)
        self.assertEqual(e["recent_s"], 100)
        self.assertEqual(e["total_s"], 100 + E.SETUP_S)
        self.assertGreater(E.estimate_time(70000, every_field=True)["recent_s"], e["recent_s"] * 2)

    def test_archive_jobs_and_their_results(self):
        e = E.estimate_time(0, archive_days=7, archive_jobs=1)
        self.assertEqual(e["archive_s"], E.archive_minutes(7, 1) * 60)
        self.assertFalse(e["archive_events_known"])
        e2 = E.estimate_time(0, archive_days=7, archive_jobs=1, archive_events=200000)
        self.assertEqual(e2["archive_s"], e["archive_s"] + 100)

    def test_words(self):
        self.assertEqual(E.fmt_duration(20), "under a minute")
        self.assertEqual(E.fmt_duration(600), "about 10 minutes")
        self.assertEqual(E.fmt_duration(3 * 3600 + 300), "about 3 h 05 min")

    def test_time_left_from_progress(self):
        now = time.time()
        p = {"rows_expected": 1000, "rows_written": 250, "export_started": now - 30}
        self.assertAlmostEqual(E.eta_seconds(p, 40), 90, delta=2)
        p = {"stage": "archive job: 10 of 40 files (25%)", "archive_files": 40, "archive_done": 10,
             "archive_started": now - 60, "after_archive_s": 50}
        self.assertAlmostEqual(E.eta_seconds(p, 70), 230, delta=3)
        self.assertEqual(E.eta_seconds({"estimate_s": 100}, 30), 70)
        self.assertIsNone(E.eta_seconds({}, 5))


class ValidateShowsTime(EngineCase):
    def test_recent_data(self):
        spread(self.tenant, T(hours=3), T(hours=1), 120, "fw")
        s = C.Session()
        s.connect("https://api.fake.example", self.tenant.vid, KEY)
        sp = dict(C.empty_spec(), dataset="fw", time={"start": E.iso(T(hours=3)), "end": E.iso(T(hours=1))})
        v = s.validate(sp)
        self.assertTrue(any(c["name"] == "Estimated time" for c in v["checks"]), v["checks"])
        self.assertTrue(v["plan"]["time_words"])

    def test_the_export_log_and_manifest_carry_the_estimate(self):
        spread(self.tenant, T(hours=3), T(hours=1), 120, "fw")
        code, res, run = self.export("--dataset", "fw", start=T(hours=3), end=T(hours=1), name="e")
        self.assertEqual(code, 0)
        self.assertIn("estimate", self.manifest(run))
        self.assertTrue(any("estimated time:" in str(l) for l in self.log), self.log[:12])


class Pickers(unittest.TestCase):
    def test_a_date_alone_is_the_start_of_that_day(self):
        self.assertEqual(W._as_utc("2026-10-05"), "2026-10-05T00:00:00Z")
        self.assertEqual(W._as_utc("2026-10-05T10:00:00Z"), "2026-10-05T10:00:00Z")

    def test_calendar_days_become_a_whole_day_range(self):
        import datetime as dt
        picks = iter([dt.date(2026, 8, 30), dt.date(2026, 8, 28)])
        real_pick, real_select = W.pick_day, W.U.select
        W.pick_day = lambda title, default=None: next(picks)
        W.U.select = lambda *a, **k: "days"
        try:
            sp = C.empty_spec()
            W.choose_time(sp)
        finally:
            W.pick_day, W.U.select = real_pick, real_select
        self.assertEqual(sp["time"], {"start": "2026-08-28T00:00:00Z", "end": "2026-08-31T00:00:00Z"})


if __name__ == "__main__":
    unittest.main()
