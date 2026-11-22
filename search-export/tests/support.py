"""Shared test plumbing: import the kit from the parent folder, install the fake transport, freeze the clock,
make waits instant, and run the engine exactly as the front ends do (abx_engine.main)."""
import csv
import datetime as dt
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if KIT not in sys.path:
    sys.path.insert(0, KIT)

for _k in [k for k in os.environ if k.startswith("ABSTRACT_")]:
    del os.environ[_k]                                   # never pick up a real key from the shell
_BOOT_HOME = tempfile.mkdtemp(prefix="abx-test-boot-")
os.environ["ABX_OUT_ROOT"] = _BOOT_HOME                  # DEFAULT_OUT_ROOT is fixed at import time
os.environ["NO_COLOR"] = "1"

import abx_engine as E  # noqa: E402
import abx_core as C  # noqa: E402

from fake_tenant import FakeTenant, FakeTransport, iso_ms  # noqa: E402,F401

UTC = dt.timezone.utc
FIXED_NOW = dt.datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
KEY = "test-key-0123456789abcdef"


def finding(test):
    """A test that reproduces a known engine defect. It runs as an expected failure so the build stays green
    while the defect is open; set ABX_FINDINGS_FAIL=1 to see the real assertion. When the engine is fixed the
    test reports an UNEXPECTED SUCCESS and the build goes red on purpose: remove the decorator then."""
    if os.environ.get("ABX_FINDINGS_FAIL") == "1":
        return test
    return unittest.expectedFailure(test)


def T(**kw):
    """FIXED_NOW minus a duration."""
    return FIXED_NOW - dt.timedelta(**kw)


class EngineCase(unittest.TestCase):
    """Each test gets a fresh exports home, a fake tenant, a frozen clock and instant waits."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="abx-test-")
        self._saved = {"out": E.DEFAULT_OUT_ROOT, "nap": E.nap, "now": E.now, "opener": E._OPENER,
                       "creds": E.CREDS, "sink": E.LOG_SINK}
        E.DEFAULT_OUT_ROOT = self.home
        self.naps = []
        check_cancel = E.check_cancel

        def fast_nap(seconds):
            self.naps.append(float(seconds))
            check_cancel()

        E.nap = fast_nap
        E.now = lambda: FIXED_NOW
        FakeTenant.clock = staticmethod(lambda: FIXED_NOW)
        self.log = []
        E.LOG_SINK = self.log.append
        self.tenant = FakeTenant()
        self.use_tenant(self.tenant)
        self._reset_tl()
        self._show_api = E.SHOW_API
        E.SHOW_API = False                                 # tests read the technical log; the live API feed has its own tests

    def tearDown(self):
        unhandled = list(self.tenant.unhandled)
        self._restore()
        self.assertEqual(unhandled, [], "the engine called endpoints the fake does not model")

    def _restore(self):
        E.SHOW_API = self._show_api
        E.DEFAULT_OUT_ROOT = self._saved["out"]
        E.nap, E.now, E._OPENER = self._saved["nap"], self._saved["now"], self._saved["opener"]
        E.CREDS, E.LOG_SINK = self._saved["creds"], self._saved["sink"]
        self._reset_tl()
        shutil.rmtree(self.home, ignore_errors=True)

    @staticmethod
    def _reset_tl():
        for k in ("sink", "cancel", "progress", "creds"):
            setattr(E._TL, k, None)
        E._TL.result = {}

    def use_tenant(self, tenant):
        E._OPENER = FakeTransport(tenant)
        E.CREDS = {"base_url": "https://api.fake.example", "vendor_account_id": tenant.vid, "api_key": KEY,
                   "expect_tenant": tenant.vid}

    # ── running ──
    def run_engine(self, argv):
        E._TL.result = {}
        code = E.main(list(argv))
        return code, dict(E.thread_result())

    def export(self, *extra, start, end, name="run", fmt="ndjson", merge=True):
        run_dir = os.path.join(self.home, name)
        argv = ["export", "--start", E.iso(start), "--end", E.iso(end), "--out", run_dir, "--format", fmt,
                "--quiet"] + (["--merge"] if merge else []) + list(extra)
        code, res = self.run_engine(argv)
        return code, res, run_dir

    # ── reading results ──
    @staticmethod
    def manifest(run_dir):
        with open(os.path.join(run_dir, "manifest.json"), encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def read_rows(path):
        if path.endswith(".gz"):
            fh = gzip.open(path, "rt", encoding="utf-8")
        else:
            fh = open(path, encoding="utf-8")
        with fh:
            return [json.loads(line) for line in fh if line.strip()]

    @staticmethod
    def read_csv(path):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))

    def merged_rows(self, run_dir, fmt="ndjson"):
        return self.read_rows(os.path.join(run_dir, "export." + fmt))

    def assertNoUnhandled(self):
        self.assertEqual(self.tenant.unhandled, [], "the engine called endpoints the fake does not model")

    def cancel_after(self, n_fetches):
        """Stop the run (like the Stop button) after n export fetches (v1 calls with page_size > 1)."""
        ev = threading.Event()
        E._TL.cancel = ev
        seen = {"n": 0}

        def hook(tenant, method, path, body):
            if path == "/v1/streamviewer/search" and body and (body.get("page_size") or 0) > 1 \
                    and "zz_abx_field_probe" not in (body.get("selected_fields") or []):
                seen["n"] += 1
                if seen["n"] >= n_fetches:
                    ev.set()

        self.tenant.on_request = hook
        return ev


def spread(tenant, start, end, n, dataset="fw", **fields):
    """n events evenly spaced (by milliseconds) over [start, end)."""
    span = (end - start).total_seconds()
    out = []
    for i in range(n):
        ts = start + dt.timedelta(seconds=span * i / n)
        out.append(tenant.add(ts, dataset=dataset, **fields))
    return out


def ids_of(rows):
    return [r.get("id") for r in rows]


__all__ = ["E", "C", "EngineCase", "FakeTenant", "FakeTransport", "FIXED_NOW", "T", "KEY", "spread", "ids_of",
           "iso_ms", "io", "finding"]
