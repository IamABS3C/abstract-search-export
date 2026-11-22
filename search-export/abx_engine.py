#!/usr/bin/env python3
"""
abx_engine — lossless, resumable bulk export across the Abstract event-search endpoints.

This is the engine behind the Abstract Search & Export kit (guided terminal, browser app and CLI).
It exists because a naive paging loop loses data on this platform without noticing:

  * several endpoints return errors as HTTP 200 bodies, and one tenant answers every zero-match
    search with HTTP 500 — a loop that treats "no rows" as the end writes a short file;
  * some operators and KQL can be silently ignored, returning every event;
  * the platform can hold two identical stored copies of one event;
  * query windows are truncated to WHOLE SECONDS: every engine answers [floor(start), floor(end)).

What the engine guarantees:
  * every response is classified (data / end / retryable / fatal) — an error is never end-of-data;
  * slice bounds are whole seconds and every row belongs to exactly one slice by its own @timestamp,
    so slices can be split, retried and resumed without gaps or duplicates;
  * each slice is recounted immediately before it is exported and reconciled exactly afterwards
    (written + stored duplicates + rows outside the slice == the server's total), or it is not COMPLETE;
  * before exporting, each filter is proven to filter on this tenant (impossible-value control for
    text filters, leave-one-out comparison for the rest);
  * a range older than hot retention is split: the old part is read from cold storage;
  * rows stream to disk, the manifest makes any run resumable, and the merged file is written atomically.

ENGINES
  v1         POST /v1/streamviewer/search        conditions; cursor paging; also every-field exports
  dataview   POST /v1/dataview/search            conditions (SSE); zero confirmation and cross-checks
  raw        POST /v2/streamviewer/raw-search    DSL; full documents; one request per slice (≤10,000)
  cold       POST /v1/coldstorage/v2/search      conditions; offset paging ≤ 10,000 per slice
  coldquery  POST /v1/coldstorage/query/submit   async job → events (command line, --allow-jobs)
  report     POST /v2/reports/{id}/export        async job (command line, --allow-jobs, experimental)

CREDENTIALS
  ABSTRACT_API_KEY, ABSTRACT_VENDOR_ACCOUNT_ID, ABSTRACT_BASE_URL (default production) and
  ABSTRACT_EXPECT_TENANT (the vendor account id you expect). The key is never written anywhere.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import abx_ops as O

import argparse
import collections
import csv
import datetime as dt
import email.utils
import errno
import gzip
import hashlib
import http.client
import io
import json
import os
import random
import re
import secrets
import shutil
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))


def _read_version():
    try:
        with open(os.path.join(HERE, "VERSION"), encoding="utf-8") as fh:
            return fh.read().strip() or "0"
    except OSError:
        return "0"


VERSION = _read_version()
PROD_HOSTS = {"api.abstractsecurity.app"}
CONTROL_VALUE = "zzz-abx-no-such-value-qqq"
RELATIVE_END_LAG = dt.timedelta(minutes=10)      # relative windows end 10 min ago (late events settle)
SETTLED = dt.timedelta(minutes=10)               # windows ending more recently get a WARN
VENDOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
KEY_RE = re.compile(r"^[\x21-\x7e]{8,4096}$")
PART_RE = re.compile(r"^part-\d{5}\.(ndjson|ndjson\.gz|csv)$")


def default_out_root():
    return os.path.abspath(os.path.expanduser(os.environ.get("ABX_OUT_ROOT") or
                                              os.path.join("~", "AbstractExports")))


DEFAULT_OUT_ROOT = default_out_root()

# Embedding hooks. Everything that varies per caller is thread-local, so a browser session can
# validate while an export runs, and a running export keeps the credentials it started with.
CREDS = None            # process default: {"base_url", "vendor_account_id", "api_key", "expect_tenant"}
LOG_SINK = None         # process default sink: callable(str)
_TL = threading.local()  # .sink .cancel .result .progress .creds


def _set_result(d):
    _TL.result = dict(d)


# ───────────────────────────── API call trace and error log ─────────────────────────────
# Every call to the platform goes through Client.request, which records one entry per attempt here: method, path,
# request body (keys that look like secrets redacted), status, time. Headers and response bodies are never
# recorded: the key is in a header, and responses hold the tenant's events.
API_TRACE: "collections.deque[Dict[str, Any]]" = collections.deque(maxlen=1000)   # the browser's API calls tab
API_SEQ = [0]
SHOW_API = os.environ.get("ABX_SHOW_API") != "0"     # every call is part of the live output (--hide-api: off)
MASKED = "Bearer ******** (masked)"
# Where the network adds the key (ABSTRACT_API_KEY_FROM_NETWORK=1): a proxy outside this computer attaches the
# Authorization header to requests for the API's host, so this process never holds a key and sends no such header.
NETWORK_KEY = "(added-by-the-network)"
NETWORK_NOTE = "added by the network after the request leaves (this tool holds no key)"
_TRACE_LOCK = threading.Lock()
_SECRET_KEY = re.compile(r"(api.?key|token|secret|password|passwd|authori[sz]ation|credential)", re.I)
LOG_KEEP_DAYS = 30


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if _SECRET_KEY.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def log_dir():
    return os.path.join(DEFAULT_OUT_ROOT, "logs")


def log_file():
    """Today's log: the errors the tool showed and the calls that failed. One file per day, kept LOG_KEEP_DAYS."""
    return os.path.join(log_dir(), f"abstract-search-{dt.datetime.now().strftime('%Y-%m-%d')}.log")


_PRUNED = set()


def _append(path, text):
    """Append to a log file (0600). A log that cannot be written never stops the tool."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        pass


def _prune_logs():
    d = log_dir()
    if d in _PRUNED:
        return
    _PRUNED.add(d)
    try:
        cutoff = time.time() - LOG_KEEP_DAYS * 86400
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if name.startswith("abstract-search-") and name.endswith(".log") and os.path.getmtime(p) < cutoff:
                os.remove(p)
    except OSError:
        pass


def log_error(level, msg, exc=None):
    """Write an error the tool showed (and, for a fault in the tool itself, its traceback) to today's log.
    Returns the log path, so the message on screen can name it."""
    import traceback
    path = log_file()
    with _TRACE_LOCK:
        _prune_logs()
        text = f"{dt.datetime.now().isoformat(timespec='seconds')} {level}: {msg}"
        if exc is not None and not isinstance(exc, (UserError, Fatal, Retryable, Cancelled)):
            text += "\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
        _append(path, text)
    return path


def _body_text(body, limit=None):
    if body is None:
        return ""
    t = json.dumps(redact(body), separators=(",", ":"), default=str)
    return t if limit is None or len(t) <= limit else t[:limit] + f"… ({len(t):,} characters)"


def masked_headers(va, with_body):
    """The headers a call carries, with the key masked: for display only."""
    h = {"Authorization": NETWORK_NOTE if (CREDS or {}).get("api_key") == NETWORK_KEY else MASKED,
         "X-AS-Vendor-Account-ID": va, "User-Agent": f"abstract-search-export/{VERSION}"}
    if with_body:
        h["Content-Type"] = "application/json"
    return h


API_HOOKS = []          # functions(method, path, status) told about every platform call (the endpoint list)


def api_record(method, path, body, status, ms, attempt, note="", expected=None, va=None):
    """One call to the platform: kept for the API calls view, printed with --show-api, written to the export's
    run.log, and written to the error log when it failed. expected: a reason, when the tool asked for an error on
    purpose (a 422 names the tenant's field list, or a field's type), so it is not reported as a failure."""
    if expected and status is not None and status >= 400:
        note = "expected: " + expected
    with _TRACE_LOCK:
        API_SEQ[0] += 1
        rec = {"n": API_SEQ[0], "t": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "method": method,
               "path": path, "status": status, "ms": int(ms), "attempt": attempt + 1, "note": note[:300],
               "body": _body_text(body, 20000), "headers": masked_headers(va, body is not None)}
        API_TRACE.append(rec)
        failed = (status is None or status >= 400) and not (expected and status is not None)
        if failed:
            _prune_logs()
            _append(log_file(), f"{rec['t']} API {method} {path} -> {status or 'no response'} ({rec['ms']} ms, "
                                f"attempt {rec['attempt']}) {rec['note']}\n  request: {rec['body'][:4000]}")
    line = f"API {method} {path} -> {status or 'no response'} in {rec['ms']} ms" + \
        (f" (attempt {rec['attempt']})" if attempt else "") + \
        (f" — {note if len(note) <= 160 else note[:160] + '…'}" if note else "")
    _run_log_line(line + (f"\n    request: {rec['body']}" if rec["body"] else ""))
    for hook in API_HOOKS:
        try:
            hook(method, path, status)
        except Exception:                                 # a listener never breaks a platform call
            pass
    if SHOW_API or getattr(_TL, "show_api", False):
        shown = _body_text(body, 2000)
        log(f"  {line}" + (f"\n      {shown}" if shown else ""), quiet=False, trace=False)


def thread_result():
    return getattr(_TL, "result", {})


def _progress(**kw):
    p = getattr(_TL, "progress", None)
    if isinstance(p, dict):
        p.update(kw)


# Positive operators: an impossible value must match nothing, so they get an impossible-value control.
POSITIVE_OPS = {"EQUALS", "CONTAINS", "CONTAINS_WORD", "CONTAINS_ALL", "CONTAINS_ANY", "STARTS_WITH",
                "ENDS_WITH", "IS_IN_LIST", "MATCHES_REGEX"}


def op_words(op):
    return str(op).lower().replace("_", " ")


# `cap` = most rows one slice may hold for the engine to page it losslessly.
ENGINES = {
    "v1":        {"page_max": 10000, "page_default": 5000, "cap": 200_000, "reads": "hot"},
    "dataview":  {"page_max": 10000, "page_default": 5000, "cap": 200_000, "reads": "hot"},
    "raw":       {"page_max": 10000, "page_default": 10000, "cap": 10_000, "reads": "hot"},
    "cold":      {"page_max": 1000,  "page_default": 1000, "cap": 10_000, "reads": "cold"},
    "coldquery": {"page_max": 10000, "page_default": 10000, "cap": None, "reads": "cold"},
    "report":    {"job": True},
}
# Measured: stored copies of an event (same id; some differ only in these ingest bookkeeping fields) sit up to
# ~100 rows apart, so they straddle page boundaries at any page size, and search_after paging across a boundary
# is not repeatable (the same window returned 10,000 then 10,008 rows at page 2,000). So every slice is sized to
# come back in ONE response and is reconciled against the total in that same response.
BOOKKEEPING = ("ingested_time", "insertion_time")
SLICE_FILL = 0.8                                      # plan slices at 80% of one response (measured: ~5,000-row responses are the fastest per row; an overflow only costs one re-split)
FULL_DOC_PAGE = 2000                                  # every-field pages are heavy; 5,000 returned 502s

ENGINE_NOTES = """
How an export works:

time       Time windows use whole seconds, start included and end excluded. Each event belongs to the
           time slice its @timestamp falls in, so slices meet exactly with no gap and no overlap.
slices     The range is split into time slices small enough to come back in one answer. Each slice is
           counted and reconciled against what was written; a slice that does not reconcile is retried
           or split, and the export reports INCOMPLETE rather than finishing short.
filters    Every filter is checked on your tenant before the export starts (see Validate in the Help tab).
           A zero result is checked a second, independent way before it is reported.
copies     If the same event comes back more than once, it is written once and the count is reported.
retention  Before a long export, the tool checks how far back searchable data goes on your tenant and
           reads older events from cold storage where it is available.
methods    v1 (field lists), dataview, raw (DSL, complete events), cold and coldquery (cold storage).
           The tool picks the method; --engine is for advanced use only.
"""


# ───────────────────────────── utilities ─────────────────────────────

def _run_log_line(text):
    """The export's own run.log: every technical log line and API call. Lines from before the export folder exists
    are held and written when it is created."""
    line = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {text}"
    run_log = getattr(_TL, "run_log", None)
    if run_log:
        _append(run_log, line)
    elif getattr(_TL, "pre_log", None) is not None:
        _TL.pre_log.append(line)


def open_run_log(run_dir, header):
    _TL.run_log = os.path.join(run_dir, "run.log")
    _append(_TL.run_log, f"── {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} Abstract Search & Export {VERSION}: "
                         f"{header}")
    for line in getattr(_TL, "pre_log", None) or []:
        _append(_TL.run_log, line)
    _TL.pre_log = None


def log(msg: str, *, quiet=False, trace=True):
    if trace:
        _run_log_line(str(msg))
    sink = getattr(_TL, "sink", None) or LOG_SINK
    if sink is not None:
        try:
            sink(msg)
        except Exception:
            pass
        return
    if not quiet:
        print(_CTRL.sub(lambda m: f"\\x{ord(m.group()):02x}", str(msg)), file=sys.stderr, flush=True)


_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")          # server text must not drive the terminal


class UserError(Exception):
    """A problem with the request itself. Shown to the user verbatim."""


class Cancelled(Exception):
    """The user stopped the run. The manifest is saved; the run can be resumed."""


def _cancel_event():
    return getattr(_TL, "cancel", None)


def check_cancel():
    ev = _cancel_event()
    if ev is not None and ev.is_set():
        raise Cancelled("stopped by user")


def nap(seconds):
    """Sleep that a Stop interrupts."""
    ev = _cancel_event()
    if ev is None:
        time.sleep(max(0.0, seconds))
    elif ev.wait(max(0.0, seconds)):
        raise Cancelled("stopped by user")


_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2})(?::(\d{2}))?(?:[.,](\d+))?\s*"
                    r"([zZ]|[+-]\d{2}:?\d{2})?$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_ts(s) -> dt.datetime:
    """ISO-8601 in the common spellings (T or space, optional seconds, '.' or ',' fraction, Z / +hh:mm / +hhmm,
    or a bare date), identical on Python 3.8–3.14 (raises ValueError).

    The datetime is built from the regex groups and never from fromisoformat, whose accepted forms grew between
    versions (measured: '20260928T000000Z' and 'T24:00:00Z' failed on 3.9 but parsed on 3.14)."""
    s = str(s).strip()
    m = _TS_RE.match(s)
    if m:
        y, mo, d, hh, mi, ss, frac, tz = m.groups()
    else:
        m = _DATE_RE.match(s)
        if not m:
            raise ValueError(f"not an ISO-8601 time: {s!r}")
        (y, mo, d), hh, mi, ss, frac, tz = m.groups(), "0", "0", None, None, None
    us = int(((frac or "") + "000000")[:6])
    if tz is None or tz in ("z", "Z"):
        zone = dt.timezone.utc
    else:
        sign = -1 if tz[0] == "-" else 1
        oh, om = int(tz[1:3]), int(tz[-2:])
        if om > 59:
            raise ValueError(f"not a valid UTC offset: {tz!r}")
        zone = dt.timezone(sign * dt.timedelta(hours=oh, minutes=om))    # ValueError from 24:00 up
    out = dt.datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss or 0), us, tzinfo=zone)  # ValueError
    return out.astimezone(dt.timezone.utc)


def parse_user_ts(s, what="time"):
    try:
        return parse_ts(s)
    except (ValueError, TypeError):
        raise UserError(f"the {what} {s!r} is not a valid UTC time — use a form like 2026-09-28T00:00:00Z")


def floor_s(d: dt.datetime) -> dt.datetime:
    return d.replace(microsecond=0)


def iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def ts_key(v):
    """Whole-second sort key 'YYYY-MM-DDTHH:MM:SS' for an event timestamp (fast path for UTC forms).
    Numbers are epoch time: milliseconds from 1e11 up (after 1973 in ms, year 5138 in seconds), else seconds.
    Raises ValueError when the value is not a time."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("not a time")
        secs = v / 1000.0 if abs(v) >= 1e11 else float(v)
        try:
            return (_EPOCH + dt.timedelta(seconds=int(secs // 1))).isoformat()[:19]
        except OverflowError:
            raise ValueError("not a time")
    s = str(v)
    if len(s) >= 19 and s[10] == "T" and (len(s) == 19 or s.endswith("Z") or s.endswith("+00:00")):
        return s[:19]
    return iso(parse_ts(s))[:19]


def parse_duration(s: str) -> dt.timedelta:
    m = re.fullmatch(r"(\d+)\s*([smhd])", str(s).strip().lower())
    if not m or int(m.group(1)) <= 0:
        raise UserError(f"{s!r} is not a duration — use a form like 30m, 1h, 24h, 7d")
    n, u = int(m.group(1)), m.group(2)
    return dt.timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[u]: n})


def now():
    return dt.datetime.now(dt.timezone.utc)


def dig(obj, dotted: str):
    if isinstance(obj, dict) and dotted in obj:
        return obj[dotted]
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def strip_nulls(obj):
    if isinstance(obj, dict):
        return {k: strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [strip_nulls(i) for i in obj]
    return obj


def read_json_file(path, what="file"):
    """JSON from a file in UTF-8 (with or without BOM) or UTF-16 — never the locale code page."""
    path = os.path.abspath(os.path.expanduser(path))
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as ex:
        raise UserError(f"could not read the {what} {path}: {ex.strerror or ex}")
    try:
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = raw.decode("utf-16")
        else:
            text = raw.decode("utf-8-sig")
        return json.loads(text)
    except (UnicodeDecodeError, ValueError) as ex:
        raise UserError(f"the {what} {path} is not valid JSON ({ex})")


def read_text_file(path, what="file"):
    """Text from a file in UTF-8 (with or without BOM) or UTF-16 — never the locale code page. --kql-file exists
    because Windows PowerShell 5.1 strips the double quotes inside a --kql argument."""
    path = os.path.abspath(os.path.expanduser(path))
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as ex:
        raise UserError(f"could not read the {what} {path}: {ex.strerror or ex}")
    try:
        return raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    except UnicodeDecodeError as ex:
        raise UserError(f"the {what} {path} is not UTF-8 or UTF-16 text ({ex})")


def parse_json_arg(text, what):
    try:
        return json.loads(text)
    except ValueError as ex:
        raise UserError(f"the {what} is not valid JSON ({ex})")


def safe_label(name, fallback="export"):
    """A file-system-safe, collision-resistant label (ASCII fold + short hash; no reserved names)."""
    name = str(name or "").strip()
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", folded).strip("-.")[:40] or fallback
    if base.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                                      *(f"LPT{i}" for i in range(1, 10))}:
        base = "_" + base
    if not name:
        return base
    return f"{base}-{hashlib.sha1(name.casefold().encode('utf-8')).hexdigest()[:6]}"


# ───────────────────────────── errors & transport ─────────────────────────────

class Retryable(Exception):
    """Transient: retry, then (for backend refusals) split the slice. Never end-of-data."""

    def __init__(self, msg, kind="backend"):
        super().__init__(msg)
        self.kind = kind          # backend | backend_exception | mismatch | timeout | transport | shape | loop | rate


class RateLimited(Retryable):
    """HTTP 429 after retries: wait, never split (splitting only adds calls)."""

    def __init__(self, msg):
        super().__init__(msg, kind="rate")


class Fatal(Exception):
    """Stop the run: bad request, missing permission, engine not enabled, proven wrong data."""


class NotEnabled(Fatal):
    """The endpoint exists but is switched off for this tenant."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would forward the Authorization header to any Location,
    including another host or plain http."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# BY DESIGN (PERF-16): no keep-alive. Throughput is bound by the platform's response time, not by connection
# setup: measured 3,000 rows in 2.9 s, 5,000 in 3.9 s and 9,000 in 13.9 s per response, so reusing the TCP/TLS
# connection would save well under 5%, for a persistent-connection layer urllib does not provide.
_OPENER = urllib.request.build_opener(_NoRedirect())


def _decode(raw, path):
    """A success body as strict UTF-8. Replacing bad bytes would silently rewrite event data, so an undecodable
    body is a Retryable shape error (retried, then the slice is split) — never a quietly altered row."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as ex:
        raise Retryable(f"{path}: the answer is not valid UTF-8 (byte {ex.start}) — refusing to alter event data",
                        kind="shape")


def _retry_after(value, attempt):
    if value:
        try:
            return max(0.0, min(120.0, float(value)))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(value)
                return max(0.0, min(120.0, (when - dt.datetime.now(when.tzinfo)).total_seconds()))
            except (TypeError, ValueError):
                pass
    return min(30.0, 2 ** attempt) + random.random()


class Client:
    def __init__(self, base, key, va, timeout=120, retries=4, quiet=False):
        base = str(base or "").strip().rstrip("/")
        u = urllib.parse.urlparse(base)
        if u.scheme != "https" or not u.hostname or u.path not in ("", "/") or u.query:
            raise UserError(f"the API URL must be https://host (got {base!r})")
        if not KEY_RE.match(key or ""):
            raise UserError("the API key contains spaces, line breaks or other characters an API key cannot "
                            "have — paste it again")
        if not VENDOR_ID_RE.match(va or ""):
            raise UserError("the vendor account ID should be 4–64 letters, digits, '-' or '_'")
        self.base, self.key, self.va = base, key, va
        self.host = u.hostname.lower()
        self.timeout, self.retries, self.quiet = timeout, retries, quiet
        self.calls = 0

    @property
    def production(self):
        return self.host in PROD_HOSTS

    def _headers(self, ctype=True):
        h = {"X-AS-Vendor-Account-ID": self.va, "User-Agent": f"abstract-search-export/{VERSION}"}
        if self.key != NETWORK_KEY:
            h["Authorization"] = "Bearer " + self.key
        if ctype:
            h["Content-Type"] = "application/json"
        return h

    def request(self, method, path, body=None, *, raw_text=False, expect=None):
        """One HTTP call with bounded retry/backoff on transport faults. Returns (status, parsed|text, ctype)."""
        expect = expect or getattr(_TL, "expect", None)
        if not getattr(self, "_headers_shown", False) and (SHOW_API or getattr(_TL, "show_api", False)):
            self._headers_shown = True              # once per connection: what every call carries, key masked
            log("  API headers on every call: " + " · ".join(f"{k}: {v}" for k, v in
                                                             masked_headers(self.va, True).items()), quiet=False)
        data = json.dumps(body).encode() if body is not None else None
        last = None
        for attempt in range(self.retries + 1):
            check_cancel()
            self.calls += 1
            req = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers=self._headers(body is not None))
            t0 = time.monotonic()
            try:
                # BY DESIGN (PERF-15): a response is read whole. Every slice is sized to come back in ONE response
                # (at most 10,000 rows), so the buffer is bounded; measured peak memory 44 MB (field list) to 79 MB
                # (every field) for a whole export.
                try:
                    with _OPENER.open(req, timeout=self.timeout) as r:
                        body_bytes, code, ctype = r.read(), r.status, r.headers.get("content-type", "")
                except urllib.error.HTTPError:
                    raise                                       # recorded below, with its message
                except Exception as ex:
                    api_record(method, path, body, None, (time.monotonic() - t0) * 1000, attempt,
                               f"{type(ex).__name__}: {ex}", va=self.va)
                    raise
                api_record(method, path, body, code, (time.monotonic() - t0) * 1000, attempt,
                           f"{len(body_bytes):,} bytes", va=self.va)
            except urllib.error.HTTPError as e:
                try:
                    # Error bodies are only ever read for their message, so a bad byte there is replaced, not fatal:
                    # a strict decode would turn a 401/403 into a retryable shape error.
                    text = e.read().decode("utf-8", "replace")
                finally:
                    e.close()
                code, ctype = e.code, e.headers.get("content-type", "")
                api_record(method, path, body, code, (time.monotonic() - t0) * 1000, attempt,
                           _CTRL.sub(" ", text[:300]), expected=expect, va=self.va)
                if code in (301, 302, 303, 307, 308):
                    raise Fatal(f"the API answered with a redirect (HTTP {code}) for {path}; the tool never follows "
                                f"redirects so your key cannot be sent elsewhere. Check the API URL.")
                if code == 407:
                    raise Fatal("a proxy requires authentication (HTTP 407): set HTTPS_PROXY=http://user:password@host:port "
                                "(NTLM/Kerberos proxies are not supported)")
                if code in (408, 425, 429, 502, 503, 504):
                    wait = _retry_after(e.headers.get("Retry-After"), attempt)
                    last = f"HTTP {code}"
                    if attempt < self.retries:
                        log(f"    busy ({last}) — retrying in {wait:.0f}s", quiet=self.quiet)
                        nap(wait)
                        continue
                    if code == 429:
                        raise RateLimited(f"{method} {path}: the platform is limiting how many searches run at once "
                                          f"(HTTP 429)")
                    raise Retryable(f"{method} {path}: {last} after {self.retries} retries", kind="transport")
                if raw_text or "event-stream" in (ctype or ""):
                    return code, text, ctype
                try:
                    return code, (json.loads(text) if text else None), ctype
                except ValueError:
                    return code, text, ctype
            except urllib.error.URLError as e:
                reason = getattr(e, "reason", e)
                if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
                    raise Fatal("the TLS certificate of the API could not be verified. On macOS with python.org "
                                "Python, run 'Install Certificates.command'; behind a corporate proxy, set "
                                "SSL_CERT_FILE to your company's CA bundle.")
                if "407" in str(reason) and "Proxy" in str(reason):
                    raise Fatal("a proxy requires authentication (HTTP 407): set HTTPS_PROXY=http://user:password@host:port "
                                "(NTLM/Kerberos proxies are not supported)")
                last = f"{type(reason).__name__}: {reason}"
                if "Tunnel connection failed" in str(reason):
                    raise Fatal(f"your proxy refused the connection to {self.base} ({reason}). Ask your IT team to "
                                f"allow {urllib.parse.urlparse(self.base).hostname} through the proxy.")
            except (TimeoutError, ConnectionError, OSError, http.client.HTTPException) as e:
                if "407" in str(e) and "Proxy" in str(e):
                    raise Fatal("a proxy requires authentication (HTTP 407): set HTTPS_PROXY=http://user:password@host:port "
                                "(NTLM/Kerberos proxies are not supported)")
                last = f"{type(e).__name__}: {e}"
                if "Tunnel connection failed" in str(e):
                    raise Fatal(f"your proxy refused the connection to {self.base} ({e}). Ask your IT team to allow "
                                f"{urllib.parse.urlparse(self.base).hostname} through the proxy.")
            except ValueError:
                raise UserError("the API key or account ID contains characters that cannot be sent — paste them again")
            else:
                text = _decode(body_bytes, path)                # outside the try: not a key-character problem
                if raw_text or "event-stream" in (ctype or ""):
                    return code, text, ctype
                try:
                    return code, (json.loads(text) if text else None), ctype
                except ValueError:
                    return code, text, ctype
            if attempt < self.retries:
                wait = min(30.0, 2 ** attempt) + random.random()
                log(f"    network problem ({last}) — retrying in {wait:.0f}s", quiet=self.quiet)
                nap(wait)
        raise Retryable(f"could not reach {self.base} ({last})", kind="transport")

    def post(self, path, body, **kw):
        return self.request("POST", path, body, **kw)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)


def classify_error(code, payload, path):
    """Raise for anything that is not a usable success. Called on EVERY response."""
    text = payload if isinstance(payload, str) else (json.dumps(payload)[:600] if payload is not None else "")
    if code == 401:
        if (_creds() or {}).get("api_key") == NETWORK_KEY:
            raise Fatal(f"{path}: the request arrived without a valid API key (HTTP 401). "
                        f"ABSTRACT_API_KEY_FROM_NETWORK=1 expects the network to add it: check that the secret names "
                        f"this API's host, the Authorization header and the Bearer prefix")
        raise Fatal(f"{path}: the API key was rejected (HTTP 401) — it is wrong, expired or revoked")
    if code == 403:
        raise Fatal(f"{path}: permission denied (HTTP 403) — {text[:300]}")
    if code == 404:
        raise Fatal(f"{path}: not found (HTTP 404) — {text[:200]}")
    if code == 422 and str(path).startswith("/v1/coldstorage/") and "field" not in text.lower():
        raise Fatal(f"archive search is not available on this tenant right now (HTTP {code}). Choose Automatic or "
                    f"Recent data only for this search.")
    if code in (400, 422):
        raise Fatal(f"{path}: the platform rejected the request (HTTP {code}) — {text[:900]}")
    if code >= 500:
        # Measured: one tenant answers every ZERO-match v1 search with HTTP 500 "Exception occurred in get_events"
        # (busy wide windows raise it too). It gets its own kind, so the v1 zero fallback keys on the type, not text.
        # Matched on the whole body (any key, or plain text), as before; a bare 500 without it no longer counts.
        exc = "Exception occurred" in text
        raise Retryable(f"{path}: HTTP {code} — {text[:300]}", kind="backend_exception" if exc else "backend")
    if 400 <= code < 500:
        raise Fatal(f"{path}: HTTP {code} — {text[:300]}")
    if isinstance(payload, dict):
        msg = str(payload.get("message") or "")
        if payload.get("code") == "not_found" or "not enabled" in msg:
            raise NotEnabled(f"{path}: not enabled on this tenant — {text[:300]}")
        if "Exception occurred" in msg or "internal error" in msg.lower():
            raise Retryable(f"{path}: backend exception inside HTTP {code} — {msg[:300]}",
                            kind="backend_exception" if "Exception occurred" in msg else "backend")


# ───────────────────────────── query model ─────────────────────────────

class Query:
    """One logical filter rendered into each engine's dialect.

    Sources: the portable shorthand --dataset / --where (IS_IN_LIST), a condition object, KQL, or DSL.
    Combinations are ANDed explicitly (dataset/where into a condition or DSL). KQL cannot be combined
    with anything else — combine inside the KQL.
    """

    guard_dataset: Optional[str] = None     # the dataset every row is checked against, when the condition was widened

    local_fields: Tuple[str, ...] = ()          # text fields with long values: this tool applies their filters

    def __init__(self, args):
        self.dataset = (getattr(args, "dataset", None) or "").strip() or None
        self.where, self.types = [], {}
        for w in getattr(args, "where", None) or []:
            if "=" not in w:
                raise UserError(f"--where expects field=value[,value…], got {w!r}")
            f, v = w.split("=", 1)
            vals = [x.strip() for x in v.split(",") if x.strip() != ""]
            if not f.strip() or not vals:
                raise UserError(f"--where {w!r} needs a field and at least one value")
            self.where.append((f.strip(), vals))
        # --match any: at least one --where must match (the dataset still always applies)
        self.match = (getattr(args, "match", None) or "all").lower()
        if self.match not in ("all", "any"):
            raise UserError("--match must be all or any")
        self.any = []
        if self.match == "any" and len(self.where) > 1:
            self.any, self.where = self.where, []
        cj, cf = getattr(args, "condition_json", None), getattr(args, "condition_file", None)
        self.condition = parse_json_arg(cj, "condition") if cj else (read_json_file(cf, "condition file") if cf else None)
        if isinstance(self.condition, dict) and "condition" in self.condition and "operator" not in self.condition:
            self.condition = self.condition["condition"]           # a whole request body
        if self.condition is not None and not isinstance(self.condition, dict):
            raise UserError("the condition must be a JSON object")
        kf = getattr(args, "kql_file", None)
        if kf and getattr(args, "kql", None):
            raise UserError("use either --kql or --kql-file, not both")
        self.kql = (read_text_file(kf, "KQL file") if kf else (getattr(args, "kql", None) or "")).strip() or None
        dj, df = getattr(args, "dsl_json", None), getattr(args, "dsl_file", None)
        self.dsl = parse_json_arg(dj, "DSL query") if dj else (read_json_file(df, "DSL file") if df else None)
        if self.kql and (self.condition is not None or self.dsl is not None or self.where or self.dataset):
            raise UserError("KQL cannot be combined with a dataset, filters, a condition or DSL — put everything "
                            "in the KQL (e.g. events | where data_stream.dataset == \"X\" and …)")
        if self.condition is not None and self.dsl is not None:
            raise UserError("use either a condition or a DSL query, not both")

    @property
    def shorthand(self):
        return bool(self.dataset or self.where or getattr(self, "any", None))

    @property
    def portable(self):
        """Expressible identically by every engine."""
        return self.condition is None and self.kql is None and self.dsl is None and not getattr(self, "any", None)

    def leaves(self):
        out = [("data_stream.dataset", [self.dataset])] if self.dataset else []
        return out + self.where

    # Impossible values per field type, for the filter proofs (a text control value is not a valid number or IP).
    TYPED_CONTROL = {"Float64": "-987654321.25", "Ipv4": "255.255.255.254"}

    def _leaf(self, field, vals):
        """--dataset / --where leaf. --where values are typed by the field's real type (resolve_types), so a
        number or IP field is not sent as text (the platform answers 422 'Expected Float64')."""
        ftype = getattr(self, "types", {}).get(field, "String")
        vals = list(vals)
        if ftype == "Boolean":
            truth = [str(v).strip().lower() for v in vals]
            if truth in (["true"], ["false"]):
                return {"field": field, "operation": "IS_TRUE" if truth == ["true"] else "IS_FALSE",
                        "field_type": "Boolean", "value": ""}
            raise UserError(f"{field} is a true/false field: use --where {field}=true or {field}=false")
        if ftype in self.TYPED_CONTROL:
            vals = [self.TYPED_CONTROL[ftype] if v == CONTROL_VALUE else v for v in vals]
        return {"field": field, "operation": "IS_IN_LIST",
                "field_type": ftype if ftype in ("Float64", "Ipv4") else "String", "case_sensitive": False,
                "values": vals}

    def resolve_types(self, client):
        """Look up the real type of every --where field (the dataset field is always text)."""
        self.types = {}
        for f, vals in self.where + list(getattr(self, "any", None) or []):
            t, _ = probe_field_type(client, f)
            self.types[f] = t
            if t == "Float64":
                bad = [v for v in vals if not re.match(r"^-?\d+(\.\d+)?$", v)]
                if bad:
                    raise UserError(f"{f} is a number field; {bad[0]!r} is not a number")
            elif t == "Ipv4":
                bad = [v for v in vals if not re.match(r"^(\d{1,3}\.){3}\d{1,3}$", v)]
                if bad:
                    raise UserError(f"{f} is an IP address field; {bad[0]!r} is not an IPv4 address")
            elif t not in ("String", "Boolean"):
                raise UserError(f"--where cannot filter on {f} (a {t} field). Build this filter in the browser app or "
                                f"the guided terminal, save the search, and run it with --from-saved")
        return self

    def to_condition(self, override=None):
        """Condition for v1 / cold / coldquery / dataview. override replaces the shorthand leaves."""
        leaves = override if override is not None else self.leaves()
        conds = [self._leaf(f, v) for f, v in leaves]
        groups = [] if override == [] else [{"operator": "or", "conditions": [self._leaf(f, v) for f, v in self.any],
                                            "groups": []}] if getattr(self, "any", None) else []
        if self.condition is None or override is not None:
            return {"operator": "and", "conditions": conds, "groups": groups}
        if not conds and not groups:
            return O.caseless_where_meaningless(self.condition)
        return {"operator": "and", "conditions": conds, "groups": groups + [O.caseless_where_meaningless(self.condition)]}

    def with_condition(self, cond):
        q = Query.__new__(Query)
        q.__dict__.update(self.__dict__)
        q.dataset, q.where, q.condition, q.kql, q.dsl, q.any = None, [], cond, None, None, []
        return q

    def to_kql(self, override=None):
        if override is not None:
            leaves = override
            if not leaves:
                return "events"
            parts = []
            for f, vals in leaves:
                q = ", ".join(json.dumps(v) for v in vals)
                parts.append(f"{f} == {json.dumps(vals[0])}" if len(vals) == 1 else f"{f} in ({q})")
            return "events | where " + " and ".join(parts)
        return self.kql

    def to_dsl(self, override=None):
        leaves = override if override is not None else self.leaves()
        must = []
        for f, vals in leaves:
            # match_phrase, never term: dataset values are indexed lowercase and `term` is case-exact.
            should = [{"match_phrase": {f: v}} for v in vals]
            must.append(should[0] if len(should) == 1 else {"bool": {"should": should, "minimum_should_match": 1}})
        if getattr(self, "any", None) and override != []:
            must.append({"bool": {"should": [{"match_phrase": {f: v}} for f, vals in self.any for v in vals],
                                  "minimum_should_match": 1}})
        if self.dsl is not None and override is None:
            must.append(self.dsl.get("query", self.dsl) if isinstance(self.dsl, dict) else self.dsl)
        return {"bool": {"must": must}} if must else {"match_all": {}}

    def has_filter(self):
        return bool(self.shorthand or getattr(self, "any", None) or self.condition is not None or self.kql or self.dsl is not None)

    def describe(self):
        d = {"dataset": self.dataset, "where": self.where, "condition": self.condition, "kql": self.kql,
             "dsl": self.dsl}
        if getattr(self, "any", None):
            d["any"] = self.any
        return d


def condition_leaves(cond, path=()):
    """(path, leaf) for leaves reachable through AND-only chains (safe to test one at a time)."""
    if not isinstance(cond, dict) or str(cond.get("operator", "and")).lower() != "and":
        return
    for i, c in enumerate(cond.get("conditions") or []):
        yield path + ("conditions", i), c
    for i, g in enumerate(cond.get("groups") or []):
        yield from condition_leaves(g, path + ("groups", i))


def has_or_groups(cond):
    if not isinstance(cond, dict):
        return False
    if str(cond.get("operator", "and")).lower() != "and":
        return True
    return any(has_or_groups(g) for g in cond.get("groups") or [])


def without_leaf(cond, path):
    """A copy of cond with the leaf at path removed (for leave-one-out proofs)."""
    c = json.loads(json.dumps(cond))
    node = c
    for k in path[:-2]:
        node = node[k]
    del node[path[-2]][path[-1]]
    return c


def with_extra_leaf(cond, path, leaf):
    """A copy of cond with leaf added next to the leaf at path (the same AND level)."""
    c = json.loads(json.dumps(cond))
    node = c
    for k in path[:-1]:
        node = node[k]
    node.append(leaf)
    return c


def replace_leaf(cond, path, leaf):
    c = json.loads(json.dumps(cond))
    node = c
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = leaf
    return c


def leaf_op(leaf):
    return str(leaf.get("operation") or leaf.get("field_operation") or "").upper()


def leaf_type(leaf):
    return str(leaf.get("field_type") or leaf.get("fieldType") or "String")


# ───────────────────────────── engines ─────────────────────────────

class Engine:
    name = "?"

    def __init__(self, client: Client, query: Query, fields, page_size, quiet=False):
        self.c, self.q, self.fields, self.quiet = client, query, fields, quiet
        self.page_size = self.default_page = page_size
        self.zero_source = None            # provenance of the last count that returned 0

    def count(self, start, end, override=None) -> int:
        raise NotImplementedError

    def pages(self, start, end, expected=None):
        raise NotImplementedError

    single = True                          # can answer a slice (rows + its total) in one response

    def capacity(self):
        """Rows one response can carry."""
        return self.page_size

    def fetch_single(self, start, end):
        """(rows, total) from ONE request; total is the whole window's count from that same response."""
        raise NotImplementedError

    def _guard(self, cursor, prev, n_pages, expected):
        if cursor is not None and cursor == prev:
            raise Retryable(f"{self.name}: the cursor did not advance — the platform returned the same page twice",
                            kind="loop")
        if expected is not None and n_pages > expected // max(1, self.page_size) + 3:
            raise Retryable(f"{self.name}: more pages than the slice's total allows — paging is not advancing",
                            kind="loop")


class V1Engine(Engine):
    name = "v1"
    path = "/v1/streamviewer/search"

    def _body(self, start, end, size, cond):
        return {"page_number": 1, "page_size": size, "start_time": iso(start), "end_time": iso(end),
                "vendor_account_id": self.c.va, "condition": cond, "selected_fields": self.fields,
                "order_by": "@timestamp", "order_type": "DESC", "storage_type": "HOT"}

    def _call(self, body):
        code, j, _ = self.c.post(self.path, body)
        classify_error(code, j, self.path)
        if not isinstance(j, dict) or "events" not in j or not isinstance(j.get("metadata"), dict):
            raise Retryable(f"{self.path}: unexpected answer — {str(j)[:300]}", kind="shape")
        return j

    def count(self, start, end, override=None):
        self.zero_source = None
        cond = self.q.to_condition(override)
        try:
            j = self._call(dict(self._body(start, end, 1, cond), selected_fields=["id"]))
        except Retryable as ex:
            if ex.kind != "backend_exception":
                raise
            # Measured: one tenant answers every ZERO-match v1 search with this exception — but busy wide
            # windows raise the same message. Only an independent engine that answers zeros cleanly
            # (dataview, conditions) may settle it, and only as a zero; anything else re-raises.
            try:
                dv = DataviewEngine(self.c, self.q, ["id"], 1, quiet=True)
                n = dv.count(start, end, override)
            except (Retryable, Fatal):
                raise ex
            if n == 0:
                self.zero_source = "dataview"
                return 0
            raise Retryable(f"{self.path}: v1 refused a window that holds {n:,} events — {ex}", kind="backend")
        # BY DESIGN (DI-28): v1, dataview and cold totals carry no gte/eq relation, so none is checked (raw's is).
        # Measured: a v1 total_events equalled a separate count 4 of 4 times, and totals above 10,000 are exact
        # (10,008), so the total is not capped at the ES default.
        total = j["metadata"].get("total_events")
        if total is None:
            raise Retryable(f"{self.path}: no total in the answer", kind="shape")
        if int(total) == 0:
            self.zero_source = "v1"
        return int(total)

    def pages(self, start, end, expected=None):
        body = self._body(start, end, self.page_size, self.q.to_condition())
        prev, n = None, 0
        while True:
            check_cancel()
            j = self._call(body)
            ev = j["events"]
            n += 1
            if not ev:
                return
            yield ev
            cursor = j["metadata"].get("last_sort")
            if not cursor:
                return
            self._guard(cursor, prev, n, expected)
            prev = cursor
            body = dict(body, search_after=cursor)

    def fetch_single(self, start, end):
        self.zero_source = None
        try:
            j = self._call(self._body(start, end, self.page_size, self.q.to_condition()))
        except Retryable as ex:
            if ex.kind != "backend_exception":
                raise
            n = self.count(start, end)                 # confirms a zero through dataview, else re-raises
            if n == 0:
                return [], 0
            raise
        total = j["metadata"].get("total_events")
        if total is None:
            raise Retryable(f"{self.path}: no total in the answer", kind="shape")
        if int(total) == 0:
            self.zero_source = "v1"
        return j["events"], int(total)


class _JobGone(Exception):
    """A saved archive job id the platform no longer knows (its results expired, or it was deleted)."""


condition_matches = O.condition_matches          # the archive re-check: the platform's measured semantics
condition_fields = O.condition_fields


def all_leaves(cond):
    for leaf in (cond or {}).get("conditions") or []:
        yield leaf
    for g in (cond or {}).get("groups") or []:
        yield from all_leaves(g)


def text_filter_fields(cond):
    """Text fields a leaf compares (the dataset name is never long): candidates for the long-value check."""
    return sorted({str(x["field"]) for x in all_leaves(cond) if x.get("field") and x["field"] != "data_stream.dataset"
                   and str(x.get("field_type") or "String") == "String"
                   and str(x.get("operation") or "").upper() in O.TEXT_OPS})


def long_text_fields(client, q, start, end, quiet):
    """{field: n or None}: text fields of the search that hold values longer than O.LONG_TEXT characters in the range
    (None = could not tell, treated as yes). Measured: such a value counts as having a value (IS_NOT_EMPTY), but
    starts / ends with, patterns, lists and most contains / does not contain filters do not see it (only a
    contains on a plain word does). Read only: one count per field."""
    out = {}
    for f in text_filter_fields(q.to_condition()):
        flt: List[Dict[str, Any]] = [{"range": {"@timestamp": {"gte": iso(start), "lt": iso(end)}}}]
        if q.dataset:
            flt.append({"match_phrase": {"data_stream.dataset": q.dataset}})
        body = {"start_time": iso(start), "end_time": iso(end), "size": 0, "track_total_hits": True,
                "query": {"bool": {"filter": flt, "must_not": [{"exists": {"field": f}}],
                                   "should": [{"prefix": {f: ch}} for ch in "abcdefghijklmnopqrstuvwxyz0123456789"],
                                   "minimum_should_match": 1}}}
        try:
            code, j, _ = client.post("/v2/streamviewer/raw-search", body)
            v = ((j or {}).get("total") or {}).get("value") if code == 200 else None
            n = int(str(v)) if v is not None else None
        except (Retryable, Fatal, TypeError, ValueError):
            n = None
        log(f"  long values in {f}: " + ("could not tell — checking this filter in the tool" if n is None else f"{n:,}"),
            quiet=quiet)
        if n != 0:
            out[f] = n
    return out


def widen_for_fields(cond, fields):
    """The superset the server can answer exactly: a text leaf on a listed field becomes (the leaf OR "has a value").
    "Has a value" is measured to include every long value; the leaf itself still answers every short value,
    including one made only of punctuation, which "has a value" does not count. The tool then keeps only the
    events the real condition matches. "Is empty" and "has a value" need no widening."""
    if not cond:
        return cond
    is_or = str(cond.get("operator") or "and").lower() == "or"
    out = {"operator": cond.get("operator") or "and", "conditions": [], "groups": []}
    for leaf in cond.get("conditions") or []:
        op = str(leaf.get("operation") or "").upper()
        if leaf.get("field") in fields and op in O.TEXT_OPS and op not in ("IS_EMPTY", "IS_NOT_EMPTY"):
            has = {"field": leaf["field"], "operation": "IS_NOT_EMPTY", "field_type": leaf.get("field_type") or "String",
                   "value": ""}
            if is_or:
                out["conditions"] += [leaf, has]
            else:
                out["groups"].append({"operator": "or", "conditions": [leaf, has], "groups": []})
        else:
            out["conditions"].append(leaf)
    out["groups"] += [widen_for_fields(g, fields) for g in cond.get("groups") or []]
    return out


def row_check_needed(cond):
    """A search with a text or list operator: every row is checked by the tool (abx_ops), because the platform reads
    some values differently (punctuation only, long text) and the archive differs again."""
    return any(str(x.get("operation") or "").upper() in O.TEXT_OPS | {"CONTAINS_ANY", "CONTAINS_ALL"}
               and x.get("field") != "data_stream.dataset" for x in all_leaves(cond))     # the dataset has its guard


def archive_condition(cond):
    """What an archive job is asked for. Measured (2026-10-31): the archive matches patterns with exact case even
    when case does not matter, and "contains" on a list field only whole items. Those leaves become "has a value"
    (a superset) and the tool's row check keeps what matches; "not" leaves get their has-a-value pin."""
    if not cond:
        return cond
    out = {"operator": cond.get("operator") or "and", "conditions": [], "groups": []}
    for leaf in cond.get("conditions") or []:
        op = str(leaf.get("operation") or "").upper()
        if (op in ("MATCHES_REGEX", "DOES_NOT_MATCH_REGEX") and not leaf.get("case_sensitive")) or \
                (str(leaf.get("field_type") or "").startswith("List") and op in ("CONTAINS", "DOES_NOT_CONTAIN")):
            leaf = {"field": leaf["field"], "operation": "IS_NOT_EMPTY", "field_type": leaf.get("field_type") or "String",
                    "value": ""}
        out["conditions"].append(leaf)
    out["groups"] = [archive_condition(g) for g in cond.get("groups") or []]
    return pin_negatives(out)


def pin_negatives(cond):
    """The "not" operators match events that HAVE a value (measured on recent data). The archive may count an event
    with no value as "not containing" it, so every "not" leaf sent to the archive carries an explicit has-a-value
    leaf beside it: both stores then answer the same question."""
    if not cond:
        return cond
    out = {"operator": cond.get("operator") or "and", "conditions": [], "groups": []}
    for leaf in cond.get("conditions") or []:
        op = str(leaf.get("operation") or "").upper()
        if op in O.NEGATIVE and str(cond.get("operator") or "and").lower() == "and":
            out["conditions"].append(leaf)
            out["conditions"].append({"field": leaf["field"], "operation": "IS_NOT_EMPTY",
                                      "field_type": leaf.get("field_type") or "String", "value": ""})
        elif op in O.NEGATIVE:
            out["groups"].append({"operator": "and", "groups": [], "conditions": [
                leaf, {"field": leaf["field"], "operation": "IS_NOT_EMPTY",
                       "field_type": leaf.get("field_type") or "String", "value": ""}]})
        else:
            out["conditions"].append(leaf)
    out["groups"] += [pin_negatives(g) for g in cond.get("groups") or []]
    return out


def _drop_field(row, dotted):
    if dotted in row:
        row.pop(dotted, None)
        return
    parts = dotted.split(".")
    cur = row
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        cur = cur[part]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


class ColdQueryEngine(V1Engine):
    """The archive: an asynchronous archive job per slice (at most ARCHIVE_CHUNK of time), then its results paged.
    The job's own total is the slice's reconciliation target; a job is accepted only when every one of its files
    was processed. Job ids are kept in the manifest so a resumed export reads the same job instead of running it
    again. Every row is re-checked against the search (condition_matches), because a filter proof on recent data
    says nothing about how the archive applied it."""
    name = "coldquery"
    single = False

    def __init__(self, *a, job_state=None, poll=10, max_wait=3600, **kw):
        super().__init__(*a, **kw)
        self.job_state = job_state if job_state is not None else {}
        self.poll, self.max_wait = poll, max_wait
        self.on_job: Optional[Callable[[], None]] = None   # saves the manifest the moment a job id changes

    def _saved(self):
        if self.on_job:
            self.on_job()

    def _submit(self, start, end):
        body = {"start_time": iso(start), "end_time": iso(end), "condition": archive_condition(self.q.to_condition()),
                "task_type": "QUERY"}
        code, j, _ = self.c.post("/v1/coldstorage/query/submit", body)
        classify_error(code, j, "/v1/coldstorage/query/submit")
        qid = (j or {}).get("nanoid")
        if not qid:
            raise Retryable(f"the archive job was not accepted — {str(j)[:300]}", kind="shape")
        log(f"    archive job {qid} started for {iso(start)[:16]} → {iso(end)[:16]}", quiet=self.quiet)
        return qid

    def _cancel(self, qid):
        try:
            self.c.post(f"/v1/coldstorage/query/{qid}/cancel", {})
        except (Retryable, Fatal):
            pass

    def _wait(self, qid, key):
        t0 = time.time()
        try:
            while True:
                code, j, _ = self.c.get(f"/v1/coldstorage/query/{qid}")
                if code == 404:
                    raise _JobGone(qid)
                classify_error(code, j, "/v1/coldstorage/query/{id}")
                j = j or {}
                st = str(j.get("status", "")).upper()
                files, done, failed = j.get("total_files"), j.get("completed_count"), j.get("failure_count") or 0
                if st == "COMPLETED":
                    if failed or (files is not None and done is not None and done != files):
                        self.job_state.pop(key, None)
                        raise Retryable(f"archive job {qid} finished with {failed or (files or 0) - (done or 0):,} of "
                                        f"{files or 0:,} files not processed; it will be run again", kind="mismatch")
                    p = getattr(_TL, "progress", None)
                    if isinstance(p, dict):                 # the time-left estimate restarts from reading the results
                        p["export_started"] = time.time()
                        p["rows_at_start"] = (p.get("rows_written") or 0) + (p.get("current_slice_rows") or 0)
                    return qid
                if st in ("FAILED", "CANCELLED", "CANCELED", "ERROR", "PAUSED"):
                    self.job_state.pop(key, None)
                    raise Retryable(f"archive job {qid} ended {st.lower()} ({str(j.get('message') or '')[:160]}); "
                                    f"it will be run again", kind="mismatch")
                if time.time() - t0 > self.max_wait:
                    raise Retryable(f"archive job {qid} is still running after {self.max_wait}s", kind="timeout")
                pct = f"{100 * (done or 0) / files:.0f}%" if files else "starting"
                _progress(stage=f"archive job: {done or 0:,} of {files or 0:,} files ({pct})", archive_files=files,
                          archive_done=done, archive_started=t0)
                nap(self.poll)
        except (Cancelled, KeyboardInterrupt):
            self._cancel(qid)
            self.job_state.pop(key, None)
            raise

    def _job(self, start, end):
        key = f"{iso(start)}|{iso(end)}"
        for _ in range(2):                     # a saved job the platform no longer knows is started again once
            qid = self.job_state.get(key)
            if not qid:
                qid = self._submit(start, end)
                self.job_state[key] = qid
                self._saved()                  # a resume after a kill reads this job instead of starting another
            try:
                return self._wait(qid, key)
            except _JobGone:
                self.job_state.pop(key, None)
        raise Retryable(f"the archive job for {key} could not be found after it was started", kind="shape")

    def _events(self, qid, size, cursor=None, fields=None):
        body = {"page_size": size, "selected_fields": fields or self.fields,
                "order_by": "@timestamp", "order_type": "DESC"}
        if cursor:
            body["search_after"] = cursor
        p = f"/v1/coldstorage/query/{qid}/events"
        code, j, _ = self.c.post(p, body)
        if code == 404:
            for k, v in list(self.job_state.items()):
                if v == qid:
                    self.job_state.pop(k, None)
            raise Retryable(f"the results of archive job {qid} are no longer available; it will be run again",
                            kind="mismatch")
        classify_error(code, j, p)
        if not isinstance(j, dict) or "items" not in j:
            raise Retryable(f"{p}: unexpected answer — {str(j)[:300]}", kind="shape")
        return j

    def count(self, start, end, override=None):
        if override is not None:
            raise UserError("an archive search cannot be counted with a changed condition")
        return int(self._events(self._job(start, end), 1, fields=["id"])["total_count"])

    def pages(self, start, end, expected=None):
        cond = self.q.to_condition()
        extra = [f for f in condition_fields(cond)
                 if not any(f == r or f.startswith(r + ".") for r in self.fields)]
        fields = list(self.fields) + extra
        qid, cursor, prev, n = self._job(start, end), None, None, 0
        while True:
            check_cancel()
            j = self._events(qid, self.page_size, cursor, fields=fields)
            n += 1
            if not j["items"]:
                return
            checked = getattr(_TL, "local_check", None) is not None   # the slice's row check keeps what matches
            for row in j["items"]:
                if not checked and not condition_matches(row, cond):
                    raise Fatal(f"the archive returned an event that does not match the search (id "
                                f"{row.get('id')}), so this archive search cannot be trusted. Narrow the search "
                                f"or search recent data only.")
                for f in extra:
                    _drop_field(row, f)
            yield j["items"]
            cursor = j.get("last_sort")
            if not cursor:
                return
            self._guard(cursor, prev, n, expected)
            prev = cursor


class DataviewEngine(Engine):
    name = "dataview"
    path = "/v1/dataview/search"

    def _body(self, start, end, limit, override=None):
        b = {"start_time": iso(start), "end_time": iso(end), "vendor_account_id": self.c.va,
             "limit": limit, "selected_fields": self.fields, "order_by": "@timestamp", "order_type": "DESC"}
        if self.q.kql is not None:
            b["kql_query"] = self.q.to_kql(override)
        else:
            b["condition"] = self.q.to_condition(override)
        return b

    def _stream(self, body):
        code, text, ctype = self.c.post(self.path, body, raw_text=True)
        if code != 200 or "event-stream" not in (ctype or ""):
            try:
                payload = json.loads(text)
            except ValueError:
                payload = text
            classify_error(code, payload, self.path)             # NotEnabled / Fatal classes first
            raise Retryable(f"{self.path}: expected a stream, got {ctype or 'nothing'} — {str(text)[:200]}",
                            kind="shape")
        events, last_sort, total, done, ev_name = [], None, None, None, None
        for line in text.split("\n"):
            line = line.rstrip("\r")
            if not line:
                ev_name = None
                continue
            if line.startswith("event:"):
                ev_name = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    d = json.loads(line[5:])
                except ValueError:
                    raise Retryable(f"{self.path}: unreadable stream line — {line[:120]}", kind="shape")
                if ev_name and "error" in ev_name.lower():
                    raise Retryable(f"{self.path}: stream error — {str(d)[:300]}")
                if isinstance(d, dict) and ev_name not in ("done", "init") and "message" in d and not d.get("events"):
                    classify_error(200, d, self.path)             # an error carried inside the stream
                if ev_name == "data":
                    events.extend(d.get("events") or [])
                    last_sort = d.get("last_sort") or last_sort
                    total = d.get("total_count", total)
                elif ev_name == "done":
                    done = d
                    total = d.get("total_count", total)
        if done is None:
            raise Retryable(f"{self.path}: the stream ended early (no done event)", kind="shape")
        if done.get("partial"):
            raise Retryable(f"{self.path}: the platform reported a partial result", kind="shape")
        if total is None and not events and done.get("total_events") == 0:
            total = 0                                  # measured: a zero-match stream omits total_count
        return events, last_sort, total

    def count(self, start, end, override=None):
        _, _, total = self._stream(dict(self._body(start, end, 1, override), selected_fields=["id"]))
        if total is None:
            raise Retryable(f"{self.path}: no total in the stream", kind="shape")
        self.zero_source = "dataview" if int(total) == 0 else None
        return int(total)

    def pages(self, start, end, expected=None):
        body = self._body(start, end, self.page_size)
        prev, n = None, 0
        while True:
            check_cancel()
            ev, cursor, _ = self._stream(body)
            n += 1
            if not ev:
                return
            yield ev
            if not cursor:
                return
            self._guard(cursor, prev, n, expected)
            prev = cursor
            body = dict(body, search_after=cursor)

    def fetch_single(self, start, end):
        ev, _, total = self._stream(self._body(start, end, self.page_size))
        if total is None:
            raise Retryable(f"{self.path}: no total in the stream", kind="shape")
        self.zero_source = "dataview" if int(total) == 0 else None
        return ev, int(total)


class RawEngine(Engine):
    """DSL, full documents, one request per slice (hits carry no sort values, so no cursor)."""
    name = "raw"
    path = "/v2/streamviewer/raw-search"

    def _body(self, start, end, size, override=None):
        if self.q.kql is not None:
            raise UserError("full-document (raw) exports take dataset / filters or DSL, not KQL")
        return {"start_time": iso(start), "end_time": iso(end), "size": size, "from": 0, "track_total_hits": True,
                "sort": [{"@timestamp": "desc"}],
                # start/end do NOT bound a DSL query exactly (measured +13 on a closed hour): re-assert it.
                "query": {"bool": {"must": [self.q.to_dsl(override),
                                            {"range": {"@timestamp": {"gte": iso(start), "lt": iso(end)}}}]}}}

    def _call(self, body):
        code, j, _ = self.c.post(self.path, body)
        classify_error(code, j, self.path)
        if not isinstance(j, dict) or "hits" not in j or "total" not in j:
            raise Retryable(f"{self.path}: unexpected answer — {str(j)[:300]}", kind="shape")
        if j.get("timed_out"):
            raise Retryable(f"{self.path}: the search timed out", kind="timeout")
        return j

    def count(self, start, end, override=None):
        t = self._call(self._body(start, end, 0, override=override))["total"]
        if t.get("relation", "eq") != "eq":
            raise Retryable(f"{self.path}: the total is an estimate, not exact", kind="shape")
        self.zero_source = "raw" if int(t["value"]) == 0 else None
        return int(t["value"])

    def pages(self, start, end, expected=None):
        hits = self._call(self._body(start, end, 10000))["hits"]
        # BY DESIGN (DI-27): only a client-side raw projection drops nulls (raw ignores _source, so the projection is
        # ours). Field-list exports from the other engines keep the platform's nulls, so in NDJSON a field that is
        # absent reads differently from one that is empty or null.
        if hits and self.fields and self.fields != ["*"]:
            hits = [strip_nulls(project(h, self.fields)) for h in hits]
        if hits:
            yield hits

    def capacity(self):
        return 10000

    def fetch_single(self, start, end):
        j = self._call(self._body(start, end, 10000))
        t = j["total"]
        if t.get("relation", "eq") != "eq":
            raise Retryable(f"{self.path}: the total is an estimate, not exact", kind="shape")
        hits = j["hits"]
        if hits and self.fields and self.fields != ["*"]:
            hits = [strip_nulls(project(h, self.fields)) for h in hits]
        self.zero_source = "raw" if int(t["value"]) == 0 else None
        return hits, int(t["value"])


def project(event, fields):
    """Client-side selected_fields (raw ignores _source). Longest paths last so prefixes do not clobber."""
    out = {}
    for f in sorted(fields, key=lambda x: x.count(".")):
        v = dig(event, f)
        if v is None:
            continue
        cur, parts = out, f.split(".")
        for p in parts[:-1]:
            nxt = cur.get(p)
            if not isinstance(nxt, dict):
                nxt = cur[p] = {}
            cur = nxt
        if isinstance(cur.get(parts[-1]), dict) and isinstance(v, dict):
            cur[parts[-1]].update(v)
        else:
            cur[parts[-1]] = v
    return out


class ColdEngine(Engine):
    name = "cold"
    path = "/v1/coldstorage/v2/search"

    def _body(self, start, end, size, page=1, override=None):
        return {"start_time": iso(start), "end_time": iso(end), "page_number": page, "page_size": size,
                "selected_fields": self.fields, "condition": self.q.to_condition(override)}

    def _call(self, body):
        code, j, _ = self.c.post(self.path, body)
        classify_error(code, j, self.path)
        if not isinstance(j, dict) or "items" not in j or "total_count" not in j:
            raise Retryable(f"{self.path}: unexpected answer — {str(j)[:300]}", kind="shape")
        return j

    def count(self, start, end, override=None):
        n = int(self._call(dict(self._body(start, end, 1, override=override), selected_fields=["id"]))["total_count"])
        self.zero_source = "cold" if n == 0 else None
        return n

    def pages(self, start, end, expected=None):
        size = min(self.page_size, 1000)
        page, got = 1, 0
        while True:
            check_cancel()
            if (page - 1) * size >= 10000:
                raise Retryable(f"{self.path}: slice exceeds the 10,000-row offset limit", kind="mismatch")
            items = self._call(self._body(start, end, size, page))["items"]
            if not items:
                return
            yield items
            got += len(items)
            if len(items) < size or (expected is not None and got >= expected):
                return
            page += 1
            if page > (expected or 10000) // size + 3:
                raise Retryable(f"{self.path}: paging is not advancing", kind="loop")

    def capacity(self):
        return min(self.page_size, 1000)

    def fetch_single(self, start, end):
        j = self._call(self._body(start, end, self.capacity(), 1))
        n = int(j["total_count"])
        self.zero_source = "cold" if n == 0 else None
        return j["items"], n


ENGINE_CLASSES = {"v1": V1Engine, "dataview": DataviewEngine, "raw": RawEngine,
                  "cold": ColdEngine, "coldquery": ColdQueryEngine}


def make_engine(name, client, q, fields, page_size, quiet, **kw):
    spec = ENGINES[name]
    size = max(1, min(page_size or spec["page_default"], spec["page_max"]))
    return ENGINE_CLASSES[name](client, q, fields, size, quiet=quiet, **kw)


# ───────────────────────────── output ─────────────────────────────

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)          # Windows: os.open defaults to text mode, which would add CRs


def _open_nofollow(path, write, what="file"):
    """A binary file object for a file inside an export folder that is never a symbolic link (SEC-1): a folder
    handed over by someone else cannot redirect the tool's reads or writes to another file."""
    if os.path.islink(path):
        raise Fatal(f"the {what} {os.path.basename(path)} is a symbolic link; the tool does not follow links inside "
                    f"an export folder")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_TRUNC) if write else os.O_RDONLY
    try:
        fd = os.open(path, flags | _NOFOLLOW | _BINARY, 0o600)
    except OSError as ex:
        if ex.errno == errno.ELOOP:
            raise Fatal(f"the {what} {os.path.basename(path)} is a symbolic link; the tool does not follow links "
                        f"inside an export folder")
        raise
    return os.fdopen(fd, "wb" if write else "rb")


class Writer:
    def __init__(self, path, fmt, fields):
        self.path, self.fmt, self.fields = path, fmt, fields
        self.n = 0
        self.raw = _open_nofollow(path, True, "part file")
        if fmt == "ndjson.gz":
            self.fh = gzip.open(self.raw, "wt", encoding="utf-8", newline="", compresslevel=6)
        elif fmt == "csv":
            self.fh = io.TextIOWrapper(self.raw, encoding="utf-8-sig", newline="")   # BOM: Excel reads UTF-8
        else:
            self.fh = io.TextIOWrapper(self.raw, encoding="utf-8", newline="")
        self.csv = None
        if fmt == "csv":
            self.csv = csv.writer(self.fh)
            self.csv.writerow(fields)

    def write(self, row):
        if self.csv is not None:
            self.csv.writerow([_cell(dig(row, f)) for f in self.fields])
        else:
            self.fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.n += 1

    def close(self):
        try:
            self.fh.close()
        finally:
            self.raw.close()                      # a GzipFile does not close the file object it was given


def _cell(v):
    """A CSV cell. Text that a spreadsheet would run as a formula is prefixed with ' (CSV injection).
    Booleans are true/false (as in the JSON); None is an empty cell, so CSV cannot tell a null from an empty
    string by design — NDJSON keeps that difference."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    if isinstance(v, str) and v.startswith(_FORMULA_START):
        return "'" + v
    return v


# ───────────────────────────── planning ─────────────────────────────

_T = TypeVar("_T")


def with_retry(fn: Callable[[], _T], what, quiet, attempts=3) -> _T:
    for i in range(attempts):
        try:
            return fn()
        except RateLimited:
            if i == attempts - 1:
                raise
            log(f"    {what}: the platform is busy (too many searches at once) — waiting 60s", quiet=quiet)
            nap(60)
        except Retryable as ex:
            if i == attempts - 1 or ex.kind in ("loop",):
                raise
            wait = min(30, 3 * 2 ** i) + random.random()
            log(f"    {what}: {ex} — retrying in {wait:.0f}s", quiet=quiet)
            nap(wait)
    raise Retryable(f"{what}: no attempt was made", kind="shape")


def _mid(s, e):
    return floor_s(s + (e - s) / 2)


def bisect_plan(engine: Engine, start, end, cap, quiet, grid=dt.timedelta(hours=6)):
    """Whole-second slices, each with ≤ cap rows. Starts from a grid (wide single counts are exactly
    what busy tenants refuse), then halves. Returns [(s, e, count, zero_source)]."""
    windows, s = [], start
    while s < end:
        e = min(end, s + grid)
        windows.append((s, e))
        s = e
    out, stack, counted, rows = [], list(reversed(windows)), 0, 0
    while stack:
        check_cancel()
        s, e = stack.pop()
        try:
            n = with_retry(lambda: engine.count(s, e), f"count {iso(s)[:19]}", quiet, attempts=2)
        except RateLimited:
            stack.append((s, e))
            nap(60)
            continue
        except Retryable as ex:
            if e - s <= dt.timedelta(seconds=1):
                raise Fatal(f"the platform keeps refusing to count even a one-second window ({iso(s)[:19]}): {ex}")
            m = _mid(s, e)
            if m <= s:
                m = s + dt.timedelta(seconds=1)
            stack.extend([(m, e), (s, m)])
            continue
        counted += 1
        if counted % 10 == 0:
            log(f"  planning: {counted:,} windows counted, {rows + n:,} events so far", quiet=quiet)
        _progress(stage="planning", windows_counted=counted, rows_planned=rows + n)
        if cap is None or n <= cap or e - s <= dt.timedelta(seconds=1):
            if cap is not None and n > cap:
                raise Fatal(f"{n:,} events fall inside one second ({iso(s)[:19]}) — more than can be retrieved for one second "
                            f"({cap:,}). Narrow the search with a filter, or export fewer fields.")
            out.append((s, e, n, engine.zero_source if n == 0 else None))
            rows += n
            continue
        m = _mid(s, e)
        if m <= s:
            m = s + dt.timedelta(seconds=1)
        stack.extend([(m, e), (s, m)])
    return sorted(out, key=lambda x: x[0])


# ───────────────────────────── one slice ─────────────────────────────

class SliceSink:
    """Writes one slice's rows and accounts for every row the platform counted.

    A row is OWNED by the slice its whole-second @timestamp falls in ([start, end) — the platform's own window
    semantics). A repeated id is classified by content:
      * same content apart from ingest bookkeeping -> a stored copy: credited (it is in the server total), not
        written again. Within ONE response every repeat is a stored copy (nothing can be re-served);
      * byte-identical but in a DIFFERENT response of a paged slice -> a paging re-serve, not credited (it can
        hide a skipped row), so the slice fails its reconcile instead of passing wrongly;
      * different content -> a distinct event that shares an id: written, and counted as an id conflict.
    """

    def __init__(self, sl, run_dir, fmt, fields, dataset_guard, authority=None):
        self.sl = sl
        self.authority = authority                 # id -> copies, from a one-response id listing (dense seconds)
        self.copies = {}
        self.sk, self.ek = iso(parse_ts(sl["start"]))[:19], iso(parse_ts(sl["end"]))[:19]
        self.w = Writer(os.path.join(run_dir, sl["file"]), fmt, fields)
        self.guard = dataset_guard
        self.first = {}
        self.stored = self.reingested = self.repeats = self.conflicts = 0
        self.outside = self.wrong_ds = self.bad_ts = 0
        self.checked_out = 0
        lc = getattr(_TL, "local_check", None)       # {"condition", "drop"}: set by _run_slices when it applies
        self.keep_cond, self.drop = (lc["condition"], lc.get("drop") or []) if lc else (None, [])

    @staticmethod
    def _hashes(row):
        full = json.dumps(row, sort_keys=True, default=str)
        core = full if not any(k in row for k in BOOKKEEPING) else \
            json.dumps({k: v for k, v in row.items() if k not in BOOKKEEPING}, sort_keys=True, default=str)
        return hashlib.sha1(full.encode()).digest(), hashlib.sha1(core.encode()).digest()

    def feed(self, rows, response_no):
        for row in rows:
            rid = row.get("id")
            if rid is not None:
                full, core = self._hashes(row)
                prev = self.first.get(rid)
                if prev is not None:
                    self.copies[rid] = self.copies.get(rid, 1) + 1
                    if prev[1] == core:
                        if prev[0] == full and prev[2] != response_no and not (
                                self.authority is not None and self.copies[rid] <= self.authority.get(rid, 0)):
                            self.repeats += 1
                        else:
                            self.stored += 1
                            if prev[0] != full:
                                self.reingested += 1
                        continue
                    self.conflicts += 1                         # distinct content under one id: keep it
                else:
                    self.first[rid] = (full, core, response_no)
            ts = row.get("@timestamp")
            if ts is not None:
                try:
                    k = ts_key(ts)
                except ValueError:
                    self.bad_ts += 1
                    k = None
                if k is not None and not (self.sk <= k < self.ek):
                    self.outside += 1                           # owned by another slice
                    continue
            if self.guard is not None and str(dig(row, "data_stream.dataset") or "").casefold() != self.guard:
                self.wrong_ds += 1
                continue
            if self.keep_cond is not None:
                # The server was asked for a wider set (every event where the field has a value); keep exactly the
                # events the search matches, judged on their real values.
                if not O.condition_matches(row, self.keep_cond):
                    self.checked_out += 1
                    continue
                for f in self.drop:
                    _drop_field(row, f)
            self.w.write(row)
            if self.w.n % 5000 == 0:
                _progress(current_slice_rows=self.w.n)

    def close(self, expected):
        self.w.close()
        sl = self.sl
        sl.update(written=self.w.n, stored_duplicates=self.stored, reingested_copies=self.reingested,
                  repeats=self.repeats, id_conflicts=self.conflicts, outside=self.outside,
                  wrong_dataset=self.wrong_ds, bad_timestamps=self.bad_ts, checked_out=self.checked_out)
        if self.wrong_ds:
            raise Fatal(f"{self.wrong_ds:,} events from another dataset came back, so the dataset filter could not be "
                        f"confirmed. The export was stopped to keep the file correct.")
        if self.authority is not None:
            missing = sum(1 for k in self.authority if k is not None and k not in self.first)
            if missing:
                raise Retryable(f"slice {sl['start'][:19]}: {missing:,} events the platform listed did not come back "
                                f"while paging", kind="mismatch")
        got = self.w.n + self.stored + self.outside + self.checked_out
        if expected is not None and got != expected:
            raise Retryable(f"slice {sl['start'][:19]}→{sl['end'][:19]}: accounted for {got:,} events but the "
                            f"platform counts {expected:,}"
                            + (f" ({self.repeats:,} re-served across pages)" if self.repeats else ""), kind="mismatch")
        return sl


def run_slice(engine: Engine, sl: dict, run_dir, fmt, fields, dataset_guard, quiet, window):
    """Paged export of one slice (only a single second holding more than one response, and cold jobs)."""
    sink = SliceSink(sl, run_dir, fmt, fields, dataset_guard)
    try:
        for n, page in enumerate(engine.pages(parse_ts(sl["start"]), parse_ts(sl["end"]), sl.get("server_total"))):
            check_cancel()
            sink.feed(page, n)
    except BaseException:
        sink.w.close()
        raise
    return sink.close(sl.get("server_total"))


def run_dense(eng, sl, total, run_dir, fmt, fields, dataset_guard, quiet, window):
    """More events inside ONE second than one normal response holds.
    Field lists: one larger response. Every field (heavy rows): an authoritative id listing in one response,
    then paged complete events checked against it, so a stored copy on a page boundary is credited while a
    re-served row (which would hide a skipped one) still fails the slice."""
    cap = ENGINES[eng.name]["page_max"]
    s, e = parse_ts(sl["start"]), parse_ts(sl["end"])
    if eng.name not in ("v1", "dataview") or total > cap:
        return run_slice(eng, sl, run_dir, fmt, fields, dataset_guard, quiet, window)     # strict; fails safe
    if eng.default_page >= ENGINES[eng.name]["page_default"]:
        big = type(eng)(eng.c, eng.q, eng.fields, cap, quiet=True)
        rows, n = with_retry(lambda: big.fetch_single(s, e), "fetch", quiet, attempts=2)
        if n > cap:
            raise Retryable(f"slice {sl['start'][:19]}: grew past {cap:,} events in one second", kind="mismatch")
        sl["server_total"] = n
        return run_single(sl, rows, n, run_dir, fmt, fields, dataset_guard)
    lister = type(eng)(eng.c, eng.q, ["@timestamp", "id"], cap, quiet=True)
    idrows, n = with_retry(lambda: lister.fetch_single(s, e), "fetch", quiet, attempts=2)
    if n > cap or len(idrows) != n:
        raise Retryable(f"slice {sl['start'][:19]}: the id listing returned {len(idrows):,} of {n:,}", kind="mismatch")
    authority = {}
    for r in idrows:
        authority[r.get("id")] = authority.get(r.get("id"), 0) + 1
    sl["server_total"] = n
    sink = SliceSink(sl, run_dir, fmt, fields, dataset_guard, authority=authority)
    try:
        for i, page in enumerate(eng.pages(s, e, n)):
            check_cancel()
            sink.feed(page, i)
    except BaseException:
        sink.w.close()
        raise
    return sink.close(n)


def run_single(sl, rows, total, run_dir, fmt, fields, dataset_guard):
    """One response -> one slice, reconciled against that response's own total."""
    sink = SliceSink(sl, run_dir, fmt, fields, dataset_guard)
    try:
        sink.feed(rows, 0)
    except BaseException:
        sink.w.close()
        raise
    return sink.close(total)


def split_window(s, e, k):
    """k near-equal whole-second parts of [s, e) (at least 2, at most one per second)."""
    secs = int((e - s).total_seconds())
    k = max(2, min(int(k), secs))
    cuts = sorted({s + dt.timedelta(seconds=secs * i // k) for i in range(k)} | {e})
    return list(zip(cuts[:-1], cuts[1:]))


def subdivide(plan, engines):
    """Cut counted windows into slices that fit one response (80% full, so late arrivals usually still fit)."""
    out = []
    for s, e, c, z, n in plan:
        engine = engines[n]
        target = max(1, int(engine.capacity() * SLICE_FILL))
        if not engine.single or c is None or c <= target or e - s <= dt.timedelta(seconds=1):
            out.append((s, e, c, z, n, None))
            continue
        parts = split_window(s, e, -(-c // target))
        out += [(a, b, None, None, n, round(c / len(parts))) for a, b in parts]
    return out


# ───────────────────────────── preflight: prove the filter filters ─────────────────────────────

def _try_count(fn, what, quiet):
    try:
        return with_retry(fn, what, quiet, attempts=2)
    except (Retryable, Fatal) as ex:
        log(f"    {what}: unavailable ({str(ex)[:140]})", quiet=quiet)
        return None


def compare_filter(engine, q, s, e, report, quiet):
    """filtered vs unfiltered on one window → 'PROVEN' | 'EMPTY' | 'ALL' | 'UNKNOWN'."""
    unf = _try_count(lambda: engine.count(s, e, override=[]), "unfiltered count", quiet)
    filt = _try_count(lambda: engine.count(s, e), "filtered count", quiet)
    report.setdefault("comparisons", []).append({"window": [iso(s), iso(e)], "filtered": filt, "unfiltered": unf})
    log(f"  checking the filter on {iso(s)[:16]}→{iso(e)[:16]}: {filt} of {unf} events match", quiet=quiet)
    if filt is None or unf is None or filt > unf:
        return "UNKNOWN"
    if filt == 0:
        return "EMPTY"
    if filt == unf and q.has_filter():
        return "ALL"
    return "PROVEN"


def preflight(engine: Engine, q: Query, start, end, quiet, report, sample_end=None):
    """Prove each part of the filter works on THIS tenant, on a bounded sample window."""
    sample_end = sample_end or min(end, start + dt.timedelta(hours=1))
    ctl = engine if engine.name != "coldquery" else V1Engine(engine.c, q, ["id"], 1, quiet=True)
    report["comparison"] = compare_filter(ctl, q, start, sample_end, report, quiet)
    controls = []

    def impossible(label, fn, extra):
        # A refused control (the backend exception, a type error) is INCONCLUSIVE — never a pass or a crash.
        try:
            n = with_retry(fn, f"check {label}", quiet, attempts=2)
        except (Retryable, Fatal) as ex:
            controls.append(dict(extra, result="INCONCLUSIVE", error=str(ex)[:200]))
            return
        controls.append(dict(extra, impossible_value_total=n, result="PASS" if n == 0 else "FAIL"))
        if n != 0:
            raise Fatal(f"the filter on {label} could not be confirmed: a value that cannot exist still "
                        f"matched {n:,} events. Use 'is one of' instead.")

    def leave_one_out(label, full_fn, minus_fn, extra):
        a = _try_count(full_fn, f"check {label}", quiet)
        b = _try_count(minus_fn, f"check {label}", quiet)
        if a is None or b is None:
            controls.append(dict(extra, result="INCONCLUSIVE"))
        elif a < b:
            controls.append(dict(extra, result="PASS", with_leaf=a, without_leaf=b))
        else:
            controls.append(dict(extra, result="UNPROVEN", with_leaf=a, without_leaf=b,
                                 note="had no effect in the sample window"))

    if q.kql is not None:
        # cmd_export ran kql_applies() first: on this tenant a KQL value that cannot exist matches nothing
        controls.append({"field": "KQL", "result": "PASS",
                         "note": "KQL is applied on this tenant (a value that cannot exist matched 0 events)"})
    elif q.dsl is not None:
        log("    free-form DSL: proven by the filtered-vs-unfiltered comparison", quiet=quiet)
    elif q.condition is None:
        if q.any:
            controls.append({"field": "(any of: " + ", ".join(f for f, _ in q.any) + ")", "result": "INFO",
                             "note": "filters where any one may match cannot be proven one at a time; the filtered "
                                     "vs unfiltered comparison covers them together"})
        leaves = q.leaves()
        for i, (f, _) in enumerate(leaves):
            if (getattr(q, "types", None) or {}).get(f) == "Boolean":
                # A true/false field has no value that cannot exist, so it is proven by leaving it out instead.
                minus = [x for j, x in enumerate(leaves) if j != i]
                leave_one_out(f, lambda: ctl.count(start, sample_end, override=leaves),
                              lambda minus=minus: ctl.count(start, sample_end, override=minus), {"field": f})
                continue
            ov = [(ff, [CONTROL_VALUE] if j == i else vv) for j, (ff, vv) in enumerate(leaves)]
            impossible(f, lambda ov=ov: ctl.count(start, sample_end, override=ov), {"field": f})
    else:
        full = q.to_condition()
        if has_or_groups(full):
            controls.append({"field": "(OR groups)", "result": "INFO",
                             "note": "filters where any one may match cannot be proven one at a time; the filtered "
                                     "vs unfiltered comparison covers them together"})
        for path, leaf in list(condition_leaves(full)):
            f, op, ftype = leaf.get("field"), leaf_op(leaf), leaf_type(leaf)
            label = f"{f} {op}"
            if f in getattr(q, "local_fields", ()):
                # long values make the platform's answer for this field partial; the tool applies it to every event
                controls.append({"field": f, "op": op, "result": "PASS", "note": "values longer than "
                                 f"{O.LONG_TEXT} characters are present, so the tool applies this filter to every "
                                 "event itself"})
                continue
            if op in O.NEGATIVE:          # condition_leaves yields AND-chain leaves only, where a complement holds
                twin = O.NEGATIVE[op]
                pos_leaf = dict(leaf, operation=twin)
                val_leaf = {"field": f, "operation": "IS_NOT_EMPTY", "field_type": ftype, "value": ""}
                probe = json.loads(json.dumps(pos_leaf))
                for k in ("value", "values"):
                    if isinstance(probe.get(k), list):
                        probe[k] = [CONTROL_VALUE]
                    elif isinstance(probe.get(k), str) and probe[k]:
                        probe[k] = CONTROL_VALUE
                qn, qp, qv = (q.with_condition(full), q.with_condition(replace_leaf(full, path, pos_leaf)),
                              q.with_condition(replace_leaf(full, path, val_leaf)))
                cnt = lambda qq: _try_count(lambda: type(ctl)(ctl.c, qq, ["id"], 1, quiet=True).count(start, sample_end),
                                            f"check {label}", quiet)
                # The filter and its opposite must never both hold, and together must cover every event with a value.
                # (Not equality: a value made only of punctuation, such as "/", is seen by both but not by "has a
                # value", measured.) An ignored filter matches everything, so it overlaps its opposite.
                qb = q.with_condition(with_extra_leaf(full, path, pos_leaf))
                n_neg, n_pos, n_val, n_both = cnt(qn), cnt(qp), cnt(qv), cnt(qb)
                n_imp = (cnt(q.with_condition(replace_leaf(full, path, probe))) if twin in POSITIVE_OPS and ftype == "String"
                         else 0)
                rec = {"field": f, "op": op, "not_count": n_neg, "positive_count": n_pos, "with_value": n_val,
                       "both_count": n_both, "impossible_value_total": n_imp}
                if n_neg is None or n_pos is None or n_val is None or n_both is None or n_imp is None:
                    controls.append(dict(rec, result="INCONCLUSIVE"))
                elif n_both == 0 and n_neg + n_pos >= n_val and n_imp == 0 and n_pos == 0:
                    # Nothing in the sample matched the opposite, so an applied and an ignored filter look the same.
                    controls.append(dict(rec, result="UNPROVEN", note="had no effect in the sample window"))
                elif n_both == 0 and n_neg + n_pos >= n_val and n_imp == 0:
                    controls.append(dict(rec, result="PASS", note=f"'{op_words(op)}' ({n_neg:,}) and its opposite "
                                     f"({n_pos:,}) never overlap and together cover every event with a value "
                                     f"({n_val:,}) — this filter is exact"))
                else:
                    why = (f"{n_both:,} events match both" if n_both else
                           f"together they leave out events that have a value ({n_neg:,} + {n_pos:,} < {n_val:,})")
                    controls.append(dict(rec, result="FAIL", note=f"'{op_words(op)}' and its opposite: {why}"))
                    raise Fatal(f"the filter on {f} '{op_words(op)}' could not be confirmed: it and its opposite "
                                f"should never overlap and should cover every event with a value, but {why}.")
                continue
            if op in POSITIVE_OPS and ftype == "String":
                probe = json.loads(json.dumps(leaf))
                for k in ("value", "values"):
                    if isinstance(probe.get(k), list):
                        probe[k] = [CONTROL_VALUE]
                    elif isinstance(probe.get(k), str) and probe[k]:
                        probe[k] = CONTROL_VALUE
                qq = q.with_condition(replace_leaf(full, path, probe))
                impossible(label, lambda qq=qq: type(ctl)(ctl.c, qq, ["id"], 1, quiet=True).count(start, sample_end),
                           {"field": f, "op": op})
            else:
                qa, qb = q.with_condition(full), q.with_condition(without_leaf(full, path))
                leave_one_out(label,
                              lambda qa=qa: type(ctl)(ctl.c, qa, ["id"], 1, quiet=True).count(start, sample_end),
                              lambda qb=qb: type(ctl)(ctl.c, qb, ["id"], 1, quiet=True).count(start, sample_end),
                              {"field": f, "op": op})
    report["controls"] = controls


def needs_reproof(pre):
    """An empty sample proves nothing: every impossible-value control returns 0 there whether or not the
    platform applies the filter."""
    first = (pre.get("comparisons") or [{}])[0]
    return pre.get("comparison") in ("UNKNOWN", "EMPTY") or not first.get("filtered") or not first.get("unfiltered")


def reprove(engine, q, s, e, quiet, pre):
    """Re-run every filter proof on the busiest counted window (the first hour held nothing to test)."""
    log(f"  the first hour had nothing to test the filter on — proving it on {iso(s)[:16]}→{iso(e)[:16]}", quiet=quiet)
    fresh = {}
    preflight(engine, q, s, e, quiet, fresh, sample_end=e)
    fresh["comparisons"] = (pre.get("comparisons") or []) + (fresh.get("comparisons") or [])
    fresh["resampled"] = [iso(s), iso(e)]
    pre.clear()
    pre.update(fresh)


def preflight_gate(report, q):
    """The verdict on the filter proofs. An INCONCLUSIVE or UNPROVEN leaf is surfaced (report["filter_unproven"],
    the result and a warning line) but does not stop the export — BY DESIGN (DI-9): UNPROVEN means the filter removed
    nothing in the sample window, which is also what a correct filter does on a feed where every sampled event
    matches, so blocking would refuse correct searches. A control that shows a filter being IGNORED still stops it."""
    controls = report.get("controls", [])
    inconclusive = any(c.get("result") == "INCONCLUSIVE" for c in controls)
    unproven = [c for c in controls if c.get("result") == "UNPROVEN"]
    cmp_ = report.get("comparison")
    all_pass = controls and all(c.get("result") in ("PASS", "INFO") for c in controls)
    if not q.has_filter():
        report["verdict"] = "UNFILTERED"
    elif cmp_ == "ALL":
        if all_pass:
            report["verdict"] = "PASS"
            report["note"] = "the filter matched every event in the sample window; each part was still proven"
        else:
            raise Fatal("the filter could not be confirmed: it matched every event in the sample and "
                        "not every part of it could be proven. Use 'is one of' filters.")
    elif cmp_ == "PROVEN":
        report["verdict"] = "PASS" if not (inconclusive or unproven) else "PASS-PARTIAL"
    elif inconclusive and cmp_ == "UNKNOWN":
        raise Fatal("the tool could not confirm that your filters are applied: the platform returned errors instead of "
                    "counts. Shorten the time range and try again, or use 'is one of' filters.")
    else:
        report["verdict"] = "PASS" if all_pass or not controls else "PASS-PARTIAL"
    if unproven:
        report["unproven"] = [c["field"] for c in unproven]
    report["filter_unproven"] = list(dict.fromkeys(
        str(c.get("field")) for c in controls if c.get("result") in ("INCONCLUSIVE", "UNPROVEN")))


# ───────────────────────────── retention ─────────────────────────────

ARCHIVE_CHUNK = dt.timedelta(days=7)        # one archive job per slice of at most this much time
EDGE_MARGIN_DAYS = 3                         # recent data is deleted gradually over its last days: stay inside it
ARCHIVE_MIN_PER_DAY = 1.5                    # measured on a busy tenant: a month of archive took 41-54 minutes
ARCHIVE_MIN_PER_JOB = 4                      # and every job, even one hour, about 3-6 minutes


def floor_day(d):
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def tenant_storage(client):
    """{'hot': days, 'cold': days} from the tenant's own record, or None when the key cannot read it."""
    try:
        code, j, _ = client.get(f"/v1/vendor-accounts/{client.va}")
    except (Retryable, Fatal):
        return None
    sr = (j or {}).get("storage_retention") if code == 200 and isinstance(j, dict) else None
    if not isinstance(sr, dict):
        return None
    out = {}
    for k in ("hot", "cold"):
        try:
            out[k] = int(sr[k]) if sr.get(k) is not None else None
        except (TypeError, ValueError):
            out[k] = None
    return out


def archive_jobs_available(client):
    """True when this key can use archive jobs. Read only: lists jobs, never starts one."""
    try:
        code, _, _ = client.get("/v1/coldstorage/query?page_number=1&page_size=1")
    except (Retryable, Fatal):
        return False
    return code == 200


def _hot_count(client, s, e, quiet):
    return with_retry(lambda: DataviewEngine(client, Query(argparse.Namespace()), ["id"], 1, quiet=True)
                      .count(s, e, override=[]), "retention check", quiet, attempts=2)


def recent_edge(client, quiet):
    """(edge, storage, first): events at or after `edge` are all in recent data; older ones only in the archive.
    The tenant's recent-data setting decides, less EDGE_MARGIN_DAYS (the oldest days of recent data are deleted
    gradually, so they are incomplete). Without the setting: the earliest UTC day holding recent events, measured
    tenant-wide, plus the same margin."""
    st = tenant_storage(client)
    today = floor_day(now())
    if st and st.get("hot"):
        return today - dt.timedelta(days=max(1, st["hot"] - EDGE_MARGIN_DAYS)), st, None
    lo, hi = today - dt.timedelta(days=400), today          # earliest day D with recent events: count [lo, D) == 0
    if _hot_count(client, lo, now() - RELATIVE_END_LAG, quiet) == 0:
        return today, st, today
    while (hi - lo) > dt.timedelta(days=1):
        check_cancel()
        mid = floor_day(lo + (hi - lo) / 2)
        if mid <= lo:
            break
        if _hot_count(client, today - dt.timedelta(days=400), mid, quiet) == 0:
            lo = mid
        else:
            hi = mid
    # `first` = the earliest day with recent events: a range starting there or later is wholly in recent data (a
    # young tenant); one starting earlier reaches data that has aged out, so the margin applies.
    return lo + dt.timedelta(days=EDGE_MARGIN_DAYS + 1), st, lo


def archive_minutes(days: float, jobs):
    """Rough archive time: measured per-day and per-job costs (a guide for the user, never a check)."""
    return max(1, round(days * ARCHIVE_MIN_PER_DAY + jobs * ARCHIVE_MIN_PER_JOB))


# Measured on a production tenant (2026-11-01), whole exports including connecting, proofs and combining:
# recent data 711 events/s with a field list (85,347 in 120 s), 231/s with every field (39,444 in 171 s); archive job
# results read at about 2,200 events/s with a field list. Every-field archive reads are assumed 3x slower (not measured).
HOT_EVENTS_PER_S = 700
HOT_FULL_EVENTS_PER_S = 230
ARCHIVE_READ_PER_S = 2000
ARCHIVE_FULL_READ_PER_S = 700
SETUP_S = 30                                 # connect, field list, proofs: before the first event is written


def estimate_time(hot_events, archive_days: float = 0, archive_jobs=0, archive_events=None, every_field=False):
    """How long an export should take, in seconds, by part: a guide shown before and during an export, never a check.
    archive_events, when unknown (the archive is counted only when its job finishes), is not added: the archive part
    is then the job time alone, and the result says so."""
    hot_s = (hot_events or 0) / (HOT_FULL_EVENTS_PER_S if every_field else HOT_EVENTS_PER_S)
    arch_s = archive_minutes(archive_days, archive_jobs) * 60 if archive_jobs else 0
    if archive_jobs and archive_events:
        arch_s += archive_events / (ARCHIVE_FULL_READ_PER_S if every_field else ARCHIVE_READ_PER_S)
    total = SETUP_S + hot_s + arch_s
    return {"total_s": round(total), "recent_s": round(hot_s), "archive_s": round(arch_s),
            "archive_events_known": bool(archive_events) or not archive_jobs}


def fmt_duration(seconds):
    s = max(0, int(seconds or 0))
    if s < 90:
        return "under 2 minutes" if s >= 45 else "under a minute"
    m = round(s / 60)
    if m < 90:
        return f"about {m} minutes"
    h, mm = divmod(m, 60)
    return f"about {h} h {mm:02d} min"


def eta_seconds(p, elapsed):
    """Time left, from what has happened so far: the archive job's files, then events written against the plan;
    before either, the planned estimate minus the time spent. None when there is nothing to go on yet."""
    now_ = time.time()
    if p.get("archive_files") and p.get("archive_done") and p.get("archive_started") and str(p.get("stage", "")).startswith("archive"):
        spent = now_ - p["archive_started"]
        left_job = spent * (p["archive_files"] - p["archive_done"]) / max(1, p["archive_done"])
        return left_job + (p.get("after_archive_s") or 0)
    exp = p.get("rows_expected") or 0
    done = (p.get("rows_written") or 0) + (p.get("current_slice_rows") or 0)
    since = done - (p.get("rows_at_start") or 0)
    if exp and since > 0 and p.get("export_started"):
        spent = now_ - p["export_started"]
        return spent * max(0, exp - done) / since
    if p.get("estimate_s"):
        return max(0, p["estimate_s"] - (elapsed or 0))
    return None


def tenant_capabilities(client, quiet=True):
    """What this tenant supports, checked by BEHAVIOUR, read only. The product's own flags are read too, but a flag
    can say a feature is on while searches still do not apply it, so the measured answer decides. Returns
    {"checked": iso, "items": [{"key", "label", "status": "available"|"not available"|"unknown", "detail"}],
     "kql": bool|None, "archive": bool|None, "full_events": bool|None}"""
    end = floor_s(now()) - RELATIVE_END_LAG
    flags = None
    try:
        code, j, _ = client.get("/v1/feature-flags/flags")
        flags = j if code == 200 and isinstance(j, dict) else None
    except (Retryable, Fatal):
        flags = None
    allq = Query(argparse.Namespace())

    def count(engine_cls, q, s, e, override=None):
        _TL.expect = "checks what this tenant supports (a refusal means: not available)"
        try:
            return with_retry(lambda: engine_cls(client, q, ["id"], 1, quiet=True).count(s, e, override=override),
                              "capability check", True, attempts=2)
        except NotEnabled:
            return "off"
        except Fatal:
            return "fail"                                # the search was refused: it does not work here
        except Retryable:
            return None
        finally:
            _TL.expect = None

    # a window that holds events: the last hour, else the last day
    start, total = end - dt.timedelta(hours=1), None
    for span in (dt.timedelta(hours=1), dt.timedelta(days=1)):
        start = end - span
        total = count(DataviewEngine, allq, start, end, override=[])
        if isinstance(total, int) and total > 0:
            break
    items = []

    kql = _kql_probe(count, start, end, total)
    items.append({"key": "kql", "label": "KQL searches", "status": _cap_status(kql),
                  "detail": "searches copied from Abstract can also run as KQL" if kql else
                  "searches copied from Abstract are converted into filters" if kql is False else
                  "could not be checked now; searches are converted into filters"})

    # archive: the tenant keeps older data (its archive setting) and this key can use archive jobs (listed, never run)
    st = tenant_storage(client)
    jobs = archive_jobs_available(client)
    days = (st or {}).get("cold")
    archive = jobs and days != 0
    hot = (st or {}).get("hot")
    items.append({"key": "archive", "label": "Archive (older data)", "status": _cap_status(archive),
                  "detail": (f"data older than about {hot} days is searched in the archive" if hot else
                             "older data is searched in the archive") +
                            (f" (kept {days} days)" if days else "") + "; that part takes minutes to an hour"
                            if archive else "only recent data can be searched"})

    # complete-event search
    raw = count(RawEngine, allq, end - dt.timedelta(minutes=5), end, override=[])
    full = True if isinstance(raw, int) else (False if raw in ("off", "fail") else None)
    items.append({"key": "full_events", "label": "Complete-event search", "status": _cap_status(full),
                  "detail": "complete events (every field) can be exported" if full else "exports use a field list" if full is False else
                  "could not be checked now"})
    return {"checked": iso(now()), "items": items, "kql": kql, "archive": archive, "full_events": full}


def _kql_probe(count, start, end, total):
    """KQL is applied when a value that cannot exist matches nothing AND a search with no condition matches the
    same total as the unfiltered count. True / False / None (could not tell: no events in the window, or errors)."""
    if not (isinstance(total, int) and total > 0):
        return None
    none = count(DataviewEngine, Query(argparse.Namespace(kql=f'events | where id == "{CONTROL_VALUE}"')), start, end)
    every = count(DataviewEngine, Query(argparse.Namespace(kql="events")), start, end)
    # Late-arriving events make two counts of the same window taken a second apart differ slightly (measured on a
    # busy tenant: the probe read "cannot tell" 1 time in 3). The impossible value matching nothing is the proof;
    # the unfiltered count only has to agree to within 1%.
    if none == 0 and isinstance(every, int) and every > 0 and abs(every - total) <= max(5, total // 100):
        return True
    if (isinstance(none, int) and none > 0) or none in ("off", "fail") or every in ("off", "fail"):
        return False
    return None


def kql_applies(client):
    """The KQL part of tenant_capabilities on its own: run before every KQL export."""
    end = floor_s(now()) - RELATIVE_END_LAG
    allq = Query(argparse.Namespace())

    def count(engine_cls, q, s, e, override=None):
        _TL.expect = "checks whether this tenant applies KQL" if q.kql else None
        try:
            return with_retry(lambda: engine_cls(client, q, ["id"], 1, quiet=True).count(s, e, override=override),
                              "KQL check", True, attempts=2)
        except NotEnabled:
            return "off"
        except Fatal:
            return "fail"                                # the search was refused: it does not work here
        except Retryable:
            return None
        finally:
            _TL.expect = None
    for span in (dt.timedelta(hours=1), dt.timedelta(days=1)):
        total = count(DataviewEngine, allq, end - span, end, override=[])
        if isinstance(total, int) and total > 0:
            return _kql_probe(count, end - span, end, total)
    return None


def _cap_status(v):
    return "available" if v else ("not available" if v is False else "unknown")


def retention_split(client, start, end, primary, quiet):
    """Where each part of the range is read from. Returns (segments, coverage):
    segments = [(seg_start, seg_end, engine_name)], coverage = record for the manifest / UI.
    Events before the recent-data edge are read with archive jobs, one per ARCHIVE_CHUNK; the rest from recent data."""
    name = "Retention coverage"
    if now() - start <= dt.timedelta(days=1):
        return [(start, end, primary)], None
    try:
        edge, st, first = recent_edge(client, quiet)
    except (Retryable, Fatal) as ex:
        return [(start, end, primary)], {"name": name, "status": "WARN", "unproven": True,
                                         "not_searched": [iso(start), None], "detail":
                                         f"could not check how far back recent data goes on this tenant "
                                         f"({str(ex)[:120]}); the export covers recent data only."}
    if start >= edge or (first is not None and start >= first):
        return [(start, end, primary)], {"name": name, "status": "PASS", "detail":
                                         "the whole range is in recent data"}
    when = iso(edge)[:10]
    if primary != "v1":
        return [(start, end, primary)], {"name": name, "status": "FAIL", "gap": True,
                                         "not_searched": [iso(start), iso(min(edge, end))], "detail":
                                         f"events before {when} are older than recent data and only in the archive, "
                                         f"which a KQL search cannot read. Build the search with a dataset and filters "
                                         f"for this range."}
    days = (st or {}).get("cold")
    if days == 0 or not archive_jobs_available(client):
        return [(start, end, primary)], {"name": name, "status": "WARN", "unproven": True,
                                         "not_searched": [iso(start), iso(min(edge, end))], "detail":
                                         f"events before {when} are older than recent data, and the archive cannot be "
                                         f"searched with this key; the export covers recent data only."}
    segs, s = [], start
    a_end = min(edge, end)
    while s < a_end:
        e = min(a_end, s + ARCHIVE_CHUNK)
        segs.append((s, e, "coldquery"))
        s = e
    if edge < end:
        segs.append((edge, end, primary))
    span = (a_end - start).total_seconds() / 86400
    mins = archive_minutes(span, len([x for x in segs if x[2] == "coldquery"]))
    detail = (f"events before {when} are older than recent data and are read from the archive, with "
              f"{len([x for x in segs if x[2] == 'coldquery'])} archive job(s)"
              + (f" (roughly {mins} minutes)" if mins else "") + "; the rest from recent data.")
    cov = {"name": name, "status": "INFO", "switched_to": "archive", "edge": iso(edge), "detail": detail,
           "archive_minutes": mins, "archive_days": round(span, 1)}
    if days and start < now() - dt.timedelta(days=days):
        floor = iso(now() - dt.timedelta(days=days))[:10]
        cov.update(status="WARN", retention_starts=floor,
                   detail=detail + f" The archive keeps {days} days, so nothing before {floor} exists to export (it "
                                   f"is not missing from the export — it is no longer kept).")
    return segs, cov


def field_enum(client: Client):
    """The tenant's selectable fields (a bogus selected_field makes the 422 enumerate them), or None."""
    body = {"vendor_account_id": client.va, "page_size": 1, "start_time": iso(now() - dt.timedelta(minutes=2)),
            "end_time": iso(now() - dt.timedelta(minutes=1)),
            "condition": {"operator": "and", "conditions": [], "groups": []},
            "selected_fields": ["id", "zz_abx_field_probe"]}
    code, j, _ = client.post("/v1/streamviewer/search", body,
                             expect="a field that cannot exist makes the platform list every field")
    try:
        expected = j["detail"][0]["ctx"]["expected"]
    except (TypeError, KeyError, IndexError):
        return None
    return sorted(n for n in set(re.findall(r"'([^']+)'", expected)) if n != "*")


def top_level_fields(enum):
    """Fields with no selectable parent: selecting them returns complete events (measured identical to
    full documents)."""
    names = set(enum)
    return sorted(f for f in names if not any(f.startswith(p + ".") for p in names if p != f))


def probe_field_type(client: Client, field):
    """(type, certain). The API names a field's type when asked with the wrong one ('Expected <type>'); a real
    answer to a text filter means String. Anything else is (String, False): not certain, ask again later."""
    end = floor_s(now()) - dt.timedelta(minutes=1)
    body = {"vendor_account_id": client.va, "page_size": 1, "start_time": iso(end - dt.timedelta(minutes=1)),
            "end_time": iso(end), "selected_fields": ["id"],
            "condition": {"operator": "and", "conditions": [
                {"field": field, "operation": "IS_NOT_EMPTY", "field_type": "String", "value": ""}], "groups": []}}
    try:
        code, j, _ = client.post("/v1/streamviewer/search", body, expect="asking as text makes the platform name "
                                                                          "the field's real type")
    except (Retryable, Fatal):
        return "String", False
    m = re.search(r"Expected '([A-Za-z0-9()]+)'", json.dumps(j) if not isinstance(j, str) else j)
    if code == 422 and m:
        return m.group(1), True
    if code == 200 and isinstance(j, dict) and isinstance(j.get("metadata"), dict) and "events" in j:
        return "String", True
    return "String", False


def list_views(client: Client, counts=None):
    """Every saved view on the tenant, read live and never written to disk, each with the search it holds:
    - origin "kql": the view's KQL text (kql_query);
    - origin "filters": built with Abstract's filter builder; its structured conditions are read from the single
      view (the list leaves them out) and written as the equivalent KQL, so they convert like any other search;
    - origin "none": only columns and a time range, no filter.
    Each entry also carries the view's time range and columns. counts (a dict), when given, receives
    "without_search" (origin none) and "filters" (origin filters)."""
    import abx_kql as K
    from concurrent.futures import ThreadPoolExecutor
    summary, page, seen = [], 1, set()
    while page <= 50:
        code, j, _ = client.get(f"/v1/streamviewer/views?page_size=200&page_number={page}")
        classify_error(code, j, "/v1/streamviewer/views")
        got = (j or {}).get("views") or []
        new = [v for v in got if v.get("id") not in seen]
        if not new:
            break
        seen.update(v.get("id") for v in new)
        summary += new
        if len(got) < 200:
            break
        page += 1

    def detail(v):
        try:
            code, d, _ = client.get(f"/v1/streamviewer/view/{urllib.parse.quote(str(v['id']), safe='')}")
        except (Retryable, Fatal) as ex:
            return v, None, str(ex)
        if code != 200 or not isinstance(d, dict):
            return v, None, f"HTTP {code}"
        if not (d.get("kql_query") or d.get("query")) and d.get("queries"):
            # a replay view keeps its search in the replay (archive) job it ran: read the newest one
            newest = sorted(d["queries"], key=lambda q: str(q.get("created_at") or ""))[-1]
            try:
                c2, job, _ = client.get(f"/v1/coldstorage/query/{urllib.parse.quote(str(newest.get('nanoid')), safe='')}")
                if c2 == 200 and isinstance(job, dict):
                    d = dict(d, _job=job)
            except (Retryable, Fatal):
                pass
        return v, d, None
    ctx = {k: getattr(_TL, k, None) for k in ("sink", "cancel", "creds")}

    def detail_in_ctx(v):
        for k, val in ctx.items():
            setattr(_TL, k, val)
        return detail(v)
    with ThreadPoolExecutor(max_workers=8) as pool:
        details = list(pool.map(detail_in_ctx, summary))
    out = []
    for v, d, err in details:
        d = d or {}
        kql = str(v.get("kql_query") or d.get("kql_query") or "").strip()
        entry = {"id": v["id"], "name": v.get("name") or v["id"], "source_ids": v.get("source_ids") or [],
                 "updated_at": v.get("updated_at"), "time": K.view_time(d.get("time_selection")),
                 "fields": [f for f in d.get("fields") or [] if isinstance(f, str)], "problem": None}
        if kql:
            entry.update(origin="kql", kql=kql)
            try:
                entry["alt_kql"] = K.structured_to_kql(d.get("query"))   # used when the KQL itself cannot be
            except K.ConvertError:
                pass
        elif err:
            entry.update(origin="unreadable", kql=None, problem=f"the view could not be read ({err}) — try again")
        elif d.get("_job") and not d.get("query"):
            job = d["_job"]
            try:
                k = (job.get("unstructured_query") or "").strip() or K.condition_to_kql(job.get("query_metadata"))
                entry.update(origin="replay" if k else "none", kql=k)
                if k and not entry["time"] and job.get("start_time") and job.get("end_time"):
                    entry["time"] = {"start": K._iso_utc(str(job["start_time"])), "end": K._iso_utc(str(job["end_time"]))}
            except K.ConvertError as ex:
                entry.update(origin="replay", kql=None, problem=f"the search this view replayed cannot be converted: {ex}")
        else:
            try:
                k = K.structured_to_kql(d.get("query"))
                entry.update(origin="filters" if k else "none", kql=k)
            except K.ConvertError as ex:
                entry.update(origin="filters", kql=None, problem=f"this view's filters cannot be converted: {ex}")
        out.append(entry)
    if counts is not None:
        counts["without_search"] = sum(1 for e in out if e["origin"] == "none")
        counts["filters"] = sum(1 for e in out if e["origin"] == "filters")
        counts["replay"] = sum(1 for e in out if e["origin"] == "replay")
    return sorted(out, key=lambda e: e["name"].casefold())


def list_dataset_names(client: Client):
    """Every configuration name on the tenant (a dataset's name is its configuration's name, any case). Read live."""
    names, page, seen = {}, 1, set()
    while page <= 50:
        code, j, _ = client.get(f"/v1/configurations/?page_number={page}&page_size=100")
        classify_error(code, j, "/v1/configurations/")
        rows = (j or {}).get("data") or (j or {}).get("items") or (j or {}).get("configurations") or [] \
            if isinstance(j, dict) else []
        new = [r for r in rows if isinstance(r, dict) and (r.get("id") or r.get("name")) not in seen]
        if not new:
            break
        seen.update(r.get("id") or r.get("name") for r in new)
        for r in new:
            if r.get("name"):
                names[r["name"].casefold()] = r["name"]
        page += 1
    return names


def recent_dataset_names(client: Client, days=30):
    """Dataset names that actually hold events in the last N days, or None when this tenant cannot say."""
    end = floor_s(now()) - dt.timedelta(minutes=2)
    try:
        code, j, _ = client.post("/v2/streamviewer/raw-search", {
            "start_time": iso(end - dt.timedelta(days=days)), "end_time": iso(end), "size": 0,
            "aggs": {"d": {"terms": {"field": "data_stream.dataset", "size": 2000}}}})
    except (Retryable, Fatal):
        return None
    if code != 200 or not isinstance(j, dict) or not isinstance(j.get("aggregations"), dict):
        return None
    return {str(b.get("key", "")).casefold(): str(b.get("key", ""))
            for b in (j["aggregations"].get("d") or {}).get("buckets") or [] if b.get("key")}


def check_dataset_name(client: Client, dataset, allow_unlisted=False):
    """A partial or misspelled dataset name matches nothing, and a zero looks like a real answer. So the name must
    be one the tenant knows: a configuration name, or a dataset that holds events. Returns the proper-case name."""
    want = dataset.strip().casefold()
    try:
        configs = list_dataset_names(client)
    except (Retryable, Fatal):
        configs = {}
    if want in configs:
        return configs[want]
    recent = recent_dataset_names(client)
    if recent and want in recent:
        return recent[want]
    if allow_unlisted:
        log(f"  ! {dataset!r} is not a dataset name this tenant lists; exporting it anyway (--allow-unlisted-dataset)",
            quiet=False)
        return dataset
    known = sorted(set(configs.values()) | set((recent or {}).values()), key=str.casefold)
    if not known:
        return dataset                                   # the tenant could not list its datasets: do not guess
    import difflib
    close = [n for n in known if want in n.casefold()] or \
        difflib.get_close_matches(dataset, known, n=3, cutoff=0.6)
    hint = (" Did you mean " + " or ".join(repr(n) for n in close[:3]) + "?") if close else ""
    raise UserError(f"no dataset called {dataset!r} on this tenant — a partial name matches nothing.{hint} Run "
                    f"'datasets' to list them all.")


def check_search_names(client: Client, q, enum, args):
    """Walkthrough B1/B2: a misspelled --where field or dataset name is a silent zero that every later check
    (filter proof, confirmed zero) would wave through, so both are checked against the tenant's own lists first."""
    if (q.where or q.any) and enum:
        bad = validate_fields(client, [f for f, _ in q.where + q.any], enum)
        if bad:
            raise UserError(f"These fields do not exist on your tenant: {', '.join(bad)} (in --where). A misspelled "
                            f"field matches nothing. Check the name with 'fields --dataset NAME'.")
    if q.dataset:
        q.dataset = check_dataset_name(client, q.dataset, getattr(args, "allow_unlisted_dataset", False))


def apply_imported_search(args):
    """--from-file / --from-view: convert a search copied from Abstract into dataset + condition + window + fields.
    KQL is never sent to the API (it ignores KQL on production tenants); each clause becomes a filter it applies."""
    import abx_kql as K
    client = make_client(args)
    tenant_guard(client, expected_tenant(args), True)   # review M4: the guard runs before anything is read
    hits: List[Dict[str, Any]] = []
    if args.from_view:
        views = list_views(client)
        want = args.from_view.strip().casefold()
        hits = [v for v in views if v["name"].casefold() == want or v["id"] == args.from_view.strip()]
        if not hits:
            import difflib
            names = [v["name"] for v in views]
            close = [n for n in names if want in n.casefold()][:3] or \
                difflib.get_close_matches(args.from_view.strip(), names, n=3, cutoff=0.5)
            raise UserError(f"no saved view called {args.from_view!r} on this tenant."
                            + (" Did you mean " + " or ".join(repr(n) for n in close) + "?" if close else "")
                            + " Run 'views' to list them all.")
        if len(hits) > 1:
            raise UserError(f"{len(hits)} saved views are called {args.from_view!r}; pass the view id instead")
        text, label = hits[0]["kql"], f"saved view {hits[0]['name']!r}"
        if hits[0].get("problem"):
            raise UserError(f"the {label}: {hits[0]['problem']}")
        if not text:
            raise UserError(f"the {label} holds no filter, only columns and a time range, so it would export every "
                            f"event. Load it in the browser app or the terminal and add a dataset or a filter, then "
                            f"save it and use export --from-saved")
    else:
        text, label = read_text_file(args.from_file, "search file"), f"file {args.from_file}"
    types = {}

    def ftype(f):
        if f not in types:
            types[f] = probe_field_type(client, f)[0]
        return types[f]
    enum = field_enum(client)

    def gate_and_convert(t):
        kql_gate(client, t)
        return K.convert(t, ftype, (set(enum) | set(re.findall(r"ext\.[\w.@-]+", t))) if enum else None)
    alt = hits[0].get("alt_kql") if args.from_view else None
    try:
        try:
            conv = gate_and_convert(text)
        except (K.ConvertError, Fatal) as first:
            if not alt:
                raise
            log(f"the {label}'s KQL could not be used ({first}); its filter-builder conditions are used instead",
                quiet=args.quiet)
            text = alt
            conv = gate_and_convert(text)
    except K.ConvertError as ex:
        if args.dataset or args.where or args.condition_json or args.condition_file or args.dsl_json or args.dsl_file:
            raise UserError("--from-file / --from-view already hold the whole search; do not add --dataset, --where, "
                            "a condition or DSL")
        applies = kql_applies(client)
        stop = K.kql_run_problem(text)
        if applies is True and stop:
            raise UserError(f"the {label} cannot be converted exactly: {ex}. It cannot run as KQL either: {stop}.")
        if applies is True:                               # this tenant applies KQL: run the search as written
            args.kql = " ".join(text.split())
            log(f"the {label} cannot be converted into filters ({ex}); KQL is available on this tenant, so it runs "
                f"as KQL:", quiet=args.quiet)
            log(f"    {args.kql}", quiet=args.quiet)
            return
        raise UserError(f"the {label} cannot be converted exactly: {ex}"
                        + (". KQL is not available on this tenant, so it cannot run as written either"
                           if applies is False else ""))
    import abx_core as C
    for f in conv["filters"] + [x for g in conv.get("any_groups") or [] for x in g]:
        prob = C.filter_problem(f)                        # review M3: the same checks the browser applies
        if prob:
            raise UserError(f"the {label} cannot be converted exactly: {prob}")
    if args.from_view and len(hits[0].get("source_ids") or []) > 1:
        log("    note: this saved view also searched other sources (such as federated search); only events stored "
            "in Abstract are exported", quiet=args.quiet)
    if args.dataset or args.where or args.condition_json or args.condition_file or args.dsl_json or args.dsl_file:
        raise UserError("--from-file / --from-view already hold the whole search; do not add --dataset, --where, "
                        "a condition or DSL")
    args.dataset = conv["dataset"]
    if conv["filters"] or conv.get("any_groups"):
        # the filters AND every "at least one of" group: the same condition the browser app and Validate run
        cond = C.build_condition(C.normalize_spec(dict(C.empty_spec(), filters=conv["filters"],
                                                       any_groups=conv.get("any_groups") or [])))
        args.condition_json = json.dumps(cond)
    if args.from_view and not conv["time"] and hits[0].get("time") and not (args.start or args.end):
        vt = hits[0]["time"]                              # the view's own time range, unless one is given
        if vt.get("last"):
            end = floor_s(now()) - RELATIVE_END_LAG
            args.start, args.end = iso(end - parse_duration(vt["last"])), iso(end)
        else:
            args.start, args.end = vt["start"], vt["end"]
    if args.from_view and not conv["fields"] and hits[0].get("fields") and not args.fields \
            and not getattr(args, "every_field", False):
        conv["fields"] = hits[0]["fields"]                # the view's own columns
    replaced = bool(conv["time"] and (args.start or args.end))
    if conv["time"] and not (args.start or args.end):
        end = floor_s(now()) - RELATIVE_END_LAG
        args.end = conv["time"]["end"] or iso(end)
        args.start = conv["time"]["start"] or iso(parse_ts(args.end) - dt.timedelta(hours=24))
    if conv["fields"] and not args.fields and not getattr(args, "every_field", False):
        args.fields = ",".join(conv["fields"])
    log(f"converted {label}:", quiet=args.quiet)
    for r in conv["readable"]:
        if replaced and r.startswith("time: "):
            r = f"time: {args.start} → {args.end} (your --last / --start / --end replace the search's own range)"
        log(f"    {r}", quiet=args.quiet)
    for n in conv["notes"]:
        log(f"    note: {n}", quiet=args.quiet)


def validate_fields(client: Client, fields, enum=None):
    enum = enum if enum is not None else field_enum(client)
    if enum is None:
        return None
    valid = set(enum)
    return [f for f in fields if f not in valid and not f.startswith(("ext.", "event.parsed."))]


def kql_validate(client: Client, kql):
    code, j, _ = client.post("/v1/kql/validate", {"kql_query": kql})
    if code == 404:
        return None
    classify_error(code, j, "/v1/kql/validate")
    bad = [v for v in (j or {}).get("violations") or [] if v.get("kind") in ("unknown_field", "unsupported_field")]
    if isinstance(j, dict) and j.get("valid") is False and bad:
        parts = [f"{v.get('kind', '').replace('_', ' ')} '{v.get('field')}'" for v in bad]
        raise Fatal("The KQL is not valid: " + "; ".join(parts) + " — the search would match nothing.")
    return j


def kql_gate(client: Client, kql):
    """A search copied from Abstract is first checked by the platform's own KQL validator (it answers against the
    tenant's real field catalogue). ANY violation refuses the conversion - e.g. '!=' on a free-text field like
    message, which Abstract's KQL engine itself rejects - so the export never runs something the user's search
    would not have run. The KQL is only validated, never executed."""
    try:
        code, j, _ = client.post("/v1/kql/validate", {"kql_query": kql})
    except Retryable:
        return None                                       # the validator is unavailable now: the converter decides
    if code == 404:
        return None                                           # validator not deployed here: the converter decides
    if code in (401, 403, 405) or code >= 500:
        return None                                       # the validator is unavailable here: the converter decides
    classify_error(code, j, "/v1/kql/validate")
    if not isinstance(j, dict):
        return None
    vio = j.get("violations") or []
    vio = [v for v in vio if isinstance(v, dict)] if isinstance(vio, list) else []
    if j.get("valid") is False or vio:
        parts = []
        for v in vio[:5]:
            msg = v.get("message") or v.get("detail") or v.get("kind", "")
            parts.append(f"{v.get('field') or ''}: {msg}".strip(": "))
        raise Fatal("Abstract's search rejects this query - " + ("; ".join(parts) or "it is not valid KQL") + ".")
    return j


def _creds():
    return getattr(_TL, "creds", None) or CREDS or {}


def tenant_guard(client: Client, expect, quiet):
    code, j, _ = client.get("/v2/auth/")
    classify_error(code, j, "/v2/auth/")
    org = (j or {}).get("current_organization") or {}
    name, vid = org.get("name"), org.get("current_vendor_account_id")
    if not vid or vid != client.va:
        raise Fatal(f"this API key belongs to vendor account {vid or 'unknown'}, not {client.va}")
    # The key belongs to the vendor account it was sent for (checked just above). ABSTRACT_EXPECT_TENANT is an
    # optional second pin: when it is set, it must match.
    if expect and expect != vid:
        raise Fatal(f"TENANT MISMATCH: connected as {name} ({vid}), expected {expect}")
    log(f"Connected to {name} ({vid}) · " + ("production" if client.production else client.base.replace("https://", "")),
        quiet=quiet)
    return {"name": name, "vendor_account_id": vid, "base_url": client.base, "production": client.production}


def key_from_environment():
    """The API key from ABSTRACT_API_KEY, from the file named by ABSTRACT_API_KEY_FILE (a mounted secret), or printed
    by ABSTRACT_API_KEY_COMMAND: a command that reads it from a secret
    manager, key vault or keychain at the moment it is needed (for example `op read op://vault/abstract/api-key`).
    The command runs without a shell, its output is never logged or written, and the tool keeps the key only in this
    process's memory. With ABSTRACT_API_KEY_FROM_NETWORK=1 and none of those, the key is added by the network (see
    NETWORK_KEY). Returns None when nothing is set."""
    key = (os.environ.get("ABSTRACT_API_KEY") or "").strip()
    if key:
        return key
    path = (os.environ.get("ABSTRACT_API_KEY_FILE") or "").strip()
    if path:                                       # a mounted secret (Docker, Kubernetes): one line, the key
        try:
            with open(path, encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh.read(8192).splitlines() if ln.strip()]
        except (OSError, UnicodeDecodeError) as ex:
            raise UserError(f"ABSTRACT_API_KEY_FILE could not be read ({type(ex).__name__}); check the path and "
                            f"that this user may read it")
        if len(lines) != 1:
            raise UserError("ABSTRACT_API_KEY_FILE must hold the API key on one line, and nothing else")
        return lines[0]
    cmd = (os.environ.get("ABSTRACT_API_KEY_COMMAND") or "").strip()
    if not cmd:
        return NETWORK_KEY if os.environ.get("ABSTRACT_API_KEY_FROM_NETWORK") == "1" else None
    if _KEY_FROM_COMMAND.get(cmd):                 # asked once per run (a secret manager may prompt for approval)
        return _KEY_FROM_COMMAND[cmd]
    import shlex
    import subprocess
    try:
        # Windows: the command line goes to CreateProcess as written ("C:\\Program Files\\…\\op.exe" read "op://…"),
        # still without a shell. Splitting it first would keep the quotes inside the program path.
        argv = cmd if os.name == "nt" else shlex.split(cmd)
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.SubprocessError) as ex:
        raise UserError(f"ABSTRACT_API_KEY_COMMAND could not run ({type(ex).__name__}); check the command")
    if r.returncode != 0:
        raise UserError(f"ABSTRACT_API_KEY_COMMAND failed (exit {r.returncode}); check the command and that you are "
                        f"signed in to your secret manager")
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    if len(lines) != 1:
        raise UserError("ABSTRACT_API_KEY_COMMAND must print the API key on one line, and nothing else")
    _KEY_FROM_COMMAND.clear()
    _KEY_FROM_COMMAND[cmd] = lines[0]
    return lines[0]


_KEY_FROM_COMMAND = {}                              # this process only; never written anywhere


def make_client(args):
    c = _creds()
    key = c.get("api_key") or key_from_environment()
    va = c.get("vendor_account_id") or getattr(args, "vendor_account", None) or os.environ.get("ABSTRACT_VENDOR_ACCOUNT_ID")
    base = c.get("base_url") or getattr(args, "base_url", None) or os.environ.get("ABSTRACT_BASE_URL") or \
        "https://api.abstractsecurity.app"
    if not key or not va:
        raise UserError("not connected — set ABSTRACT_API_KEY (or ABSTRACT_API_KEY_COMMAND) and "
                        "ABSTRACT_VENDOR_ACCOUNT_ID, or start "
                        "abstract_search.py and choose Connect")
    return Client(base, key, va, timeout=getattr(args, "timeout", 120) or 120, quiet=getattr(args, "quiet", False))


def expected_tenant(args):
    return getattr(args, "expect_tenant", None) or _creds().get("expect_tenant") or os.environ.get("ABSTRACT_EXPECT_TENANT")


def check_window(start, end, quiet):
    if end <= start:
        raise UserError("the end time must be after the start time")
    if end > now():
        raise UserError("the end time is in the future — choose an end time that is now or earlier")
    if now() - end < SETTLED:
        log("  ! the window ends less than 10 minutes ago; late-arriving events can still change the totals",
            quiet=quiet)


# ───────────────────────────── manifest, lock, merge ─────────────────────────────

class ManifestStore:
    """Throttled, atomic manifest writes (compact JSON), retried on Windows sharing violations."""

    def __init__(self, run_dir, man, every=3.0):
        self.run_dir, self.man, self.every, self.last = run_dir, man, every, 0.0

    def save(self, force=False):
        if not force and time.time() - self.last < self.every:
            return
        self.last = time.time()
        save_manifest(self.run_dir, self.man)


def save_manifest(run_dir, man):
    tmp = os.path.join(run_dir, ".manifest.json.tmp")
    data = json.dumps(man, default=str, separators=(",", ":"))
    if os.path.islink(tmp):
        os.remove(tmp)                            # removes the link itself, never its target
    with _open_nofollow(tmp, True, "manifest") as fh:
        fh.write(data.encode("utf-8"))
    for i in range(80):
        try:
            os.replace(tmp, os.path.join(run_dir, "manifest.json"))
            return
        except PermissionError:
            time.sleep(0.025)
    os.replace(tmp, os.path.join(run_dir, "manifest.json"))


def load_manifest(run_dir):
    path = os.path.join(run_dir, "manifest.json")
    if os.path.islink(path):
        raise UserError(f"the manifest in {run_dir} is a symbolic link; refusing to resume it")
    if not os.path.isfile(path):
        raise UserError(f"{run_dir} has no manifest.json — it is not an export folder")
    try:
        with open(path, encoding="utf-8") as fh:
            man = json.load(fh)
    except (OSError, ValueError) as ex:
        raise UserError(f"the manifest in {run_dir} cannot be read ({ex})")
    for sl in man.get("slices") or []:
        if not isinstance(sl.get("file"), str) or not PART_RE.match(sl["file"]):
            raise UserError(f"the manifest in {run_dir} names an unexpected part file {sl.get('file')!r}; "
                            f"refusing to resume it")
        if os.path.islink(os.path.join(run_dir, sl["file"])):
            raise UserError(f"the part file {sl['file']} in {run_dir} is a symbolic link; refusing to resume it")
    return man


def pid_alive(pid):
    """Whether the process holding a .lock may still be running. On Windows os.kill(pid, 0) does not probe: it
    TERMINATES that process (TerminateProcess with exit code 0), which after pid reuse can be any program. So on
    Windows another pid is always treated as alive (a stale .lock is deleted by hand, docs/CLI.md)."""
    if not pid or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                                   # alive, owned by another user
    except OSError:
        return False


class RunLock:
    """One writer per export folder (a second process or a double click cannot interleave)."""

    def __init__(self, run_dir):
        self.path = os.path.join(run_dir, ".lock")
        self.fd = None

    def __enter__(self):
        for _ in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    with open(self.path) as fh:
                        pid = int(fh.read().strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                alive = bool(pid) and pid != os.getpid() and pid_alive(pid)
                if alive:
                    raise UserError("this export is already running in another window")
                try:
                    os.remove(self.path)
                except OSError:
                    pass
        raise UserError("could not lock the export folder")

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.remove(self.path)
        except OSError:
            pass


def _part_rows(path, fmt):
    """Events in one part file: lines for NDJSON (JSON escapes every newline inside a value), a CSV reader for CSV
    (a cell can hold a line break). A truncated or unreadable part raises ValueError."""
    with _open_nofollow(path, False, "part file") as raw:
        try:
            if fmt == "csv":
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as t:
                    return max(0, sum(1 for _ in csv.reader(t)) - 1)          # minus the header row
            src = gzip.GzipFile(fileobj=raw, mode="rb") if fmt == "ndjson.gz" else raw
            n, last = 0, b"\n"
            for chunk in iter(lambda: src.read(1 << 20), b""):
                n += chunk.count(b"\n")
                last = chunk[-1:]
            if last != b"\n":
                raise ValueError("the last line is cut short")
            return n
        except (EOFError, OSError, zlib.error, UnicodeDecodeError, csv.Error) as ex:
            raise ValueError(f"it cannot be read to the end ({ex})")


def part_format(fmt, every_field):
    """The format part files are written in. CSV with every field has no column list until every event is in, so its
    parts are NDJSON and merge_parts builds the CSV from the fields the events actually hold."""
    return "ndjson" if fmt == "csv" and every_field else fmt


def _csv_every_field(paths, dst):
    """CSV of complete events: one column per field found in ANY event (@timestamp and id first, then the ACS fields,
    then the ext.* fields, each alphabetically). Two passes over the NDJSON parts; nothing is held in memory but the
    column names."""
    cols = set()
    for p in paths:
        with _open_nofollow(p, False, "part file") as raw, io.TextIOWrapper(raw, encoding="utf-8", newline="") as t:
            for line in t:
                if line.strip():
                    cols.update(_flat_event(json.loads(line)))
    first = [c for c in ("@timestamp", "id") if c in cols]
    rest = sorted(cols - set(first))
    header = first + [c for c in rest if not c.startswith("ext.")] + [c for c in rest if c.startswith("ext.")]
    out = io.TextIOWrapper(dst, encoding="utf-8-sig", newline="", write_through=True)
    w = csv.writer(out)
    w.writerow(header)
    for p in paths:
        with _open_nofollow(p, False, "part file") as raw, io.TextIOWrapper(raw, encoding="utf-8", newline="") as t:
            for line in t:
                if line.strip():
                    f = _flat_event(json.loads(line))
                    w.writerow([_cell(f.get(c)) for c in header])
    out.flush()
    out.detach()
    return len(header)


def _flat_event(d, prefix=""):
    """Nested objects become dotted columns; a list stays one cell (written as JSON)."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict) and v:
            out.update(_flat_event(v, f"{prefix}{k}."))
        else:
            out[prefix + k] = v
    return out


def merge_parts(run_dir, man, keep_parts=False):
    """Concatenate COMPLETE parts into export.<fmt>, atomically, after a disk-space check. Parts are combined oldest
    slice first; inside a slice the events stay in the platform's order, newest first.
    Each part's event count is checked against its slice's `written` first (DI-23): a part that was cut short or
    changed puts its slice back to INCOMPLETE, so resuming fetches it again."""
    fmt = man["args"]["format"]
    out = os.path.join(run_dir, "export." + fmt)
    pfmt = part_format(fmt, man["args"].get("every_field"))       # what the part files hold
    parts = sorted((x for x in man["slices"] if x["status"] == "COMPLETE"), key=lambda x: x["start"])
    paths = []
    for x in parts:
        p = os.path.join(run_dir, x["file"])
        if not os.path.isfile(p) and not os.path.islink(p):
            raise Fatal(f"part file {x['file']} is missing — the export cannot be combined; resume it")
        try:
            n = _part_rows(p, pfmt)
            why = None if n == x.get("written", 0) else f"holds {n:,} events but {x.get('written', 0):,} were written"
        except ValueError as ex:
            why = str(ex)
        if why:
            x["status"], x["error"] = "INCOMPLETE", f"part file {x['file']} {why}; it will be fetched again"
            save_manifest(run_dir, man)
            raise Retryable(f"part file {x['file']} no longer matches its time slice ({why}) — resume the export to "
                            f"fetch that slice again", kind="mismatch")
        paths.append(p)
    need = sum(os.path.getsize(p) for p in paths)
    free = shutil.disk_usage(run_dir).free
    if need + 64 * 1024 * 1024 > free:
        raise Fatal(f"not enough free disk space to combine the files (needs {need / 1e9:.1f} GB, "
                    f"{free / 1e9:.1f} GB free). The part files are complete; free space and resume, or use them "
                    f"as they are.")
    tmp = out + ".tmp"
    if os.path.islink(tmp):
        os.remove(tmp)                            # removes the link itself, never its target
    with _open_nofollow(tmp, True, "combined file") as dst:
        if pfmt != fmt:
            ncols = _csv_every_field(paths, dst)
            man["csv_columns"] = ncols
        for i, p in enumerate(paths if pfmt == fmt else []):
            with _open_nofollow(p, False, "part file") as src:
                if fmt == "csv" and i > 0:
                    head = src.read(3)
                    if head != b"\xef\xbb\xbf":
                        src.seek(0)
                    src.readline()                               # drop repeated BOM + header
                shutil.copyfileobj(src, dst, 1 << 20)            # concatenated gzip members are valid gzip
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(tmp, out)
    if not keep_parts:
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass
    return out


# ───────────────────────────── export ─────────────────────────────

RESTORABLE = {"storage", "match", "engine", "dataset", "where", "condition_json", "kql", "dsl_json", "fields", "every_field", "format",
              "no_long_check",
              "start", "end", "all_events", "slice", "page_size", "merge", "keep_parts", "accept_hot_only",
              "no_preflight"}
_SWITCHES_ON = {"merge", "keep_parts"}          # on if the original run OR this command line turned them on


def _refuse_resume_changes(args, man):
    """A resume continues the SAME export: its search, fields, format and window come from the manifest. An option
    that would change them is refused rather than silently ignored (walkthrough M4)."""
    saved = man.get("args") or {}
    given = []
    for k, flag in (("storage", "--storage"), ("dataset", "--dataset"), ("where", "--where"), ("condition_json", "--condition-json"),
                    ("condition_file", "--condition-file"), ("dsl_json", "--dsl-json"), ("dsl_file", "--dsl-file"),
                    ("fields", "--fields"), ("start", "--start / --last"), ("end", "--end / --last")):
        v = getattr(args, k, None)
        if v not in (None, [], "") and v != saved.get(k):
            given.append(flag)
    if getattr(args, "every_field", False) and not saved.get("every_field"):
        given.append("--every-field")
    if getattr(args, "format", None) not in (None, saved.get("format")):
        given.append("--format")
    if given:
        raise UserError("--resume continues the same export, so its search, fields, format and time range come from "
                        "its manifest. Leave out " + ", ".join(dict.fromkeys(given)) + ", or start a new export.")


def _restore_args(args, man):
    saved = man.get("args") or {}
    for k in RESTORABLE:
        if k in _SWITCHES_ON:
            setattr(args, k, bool(getattr(args, k, False)) or bool(saved.get(k)))
        elif k in saved:
            setattr(args, k, saved[k])
    args.condition_file = args.dsl_file = args.kql_file = None   # the query is taken from the manifest
    q = man.get("query") or {}
    if q.get("kql"):
        args.kql = q["kql"]                               # checked against the tenant before it runs again
    if q.get("condition") is not None:
        args.condition_json = json.dumps(q["condition"])
    if q.get("dsl") is not None:
        args.dsl_json = json.dumps(q["dsl"])


STORAGE = ("auto", "recent", "archive")


def resolve_engine_name(args, q):
    e = getattr(args, "engine", None) or "auto"
    if e != "auto":
        return e
    if (getattr(args, "storage", None) or "auto") == "archive" and q.kql is None and q.dsl is None:
        return "coldquery"                               # archive jobs for the whole range
    if q.dsl is not None:
        return "raw"
    if q.kql is not None:
        return "dataview"
    return "v1"


def _fields_for(args, client, engine_name, enum):
    if getattr(args, "every_field", False):
        if engine_name == "raw":
            return ["*"]
        if enum is None:
            raise UserError("could not read this tenant's field list, which every-field exports need — try again")
        return ["@timestamp", "id"] + [f for f in top_level_fields(enum) if f not in ("@timestamp", "id")]
    fields = [f.strip() for f in (args.fields or "@timestamp,id,data_stream.dataset,message").split(",") if f.strip()]
    if "*" in fields:
        raise UserError('"*" is not a field — choose fields, or export every field')
    # @timestamp and id first, each field once (a repeated field would be a repeated CSV column)
    fields = list(dict.fromkeys(["@timestamp", "id"] + fields))
    if args.dataset and "data_stream.dataset" not in fields:
        fields.append("data_stream.dataset")                     # needed for the per-row dataset guard
    return fields


def cmd_export(args):
    _TL.run_log, _TL.pre_log = None, []
    try:
        return _cmd_export(args)
    except BaseException as ex:
        _run_log_line(f"STOPPED: {type(ex).__name__}: {ex}")
        raise
    finally:
        _TL.run_log, _TL.pre_log = None, None


def _cmd_export(args):
    man = None
    run_dir = None
    if args.resume:
        run_dir = os.path.abspath(os.path.expanduser(args.resume))
        man = load_manifest(run_dir)
        _TL.planned, _TL.run_dir = True, run_dir
        open_run_log(run_dir, "resume")
        if getattr(_TL, "plan_flag", None) is not None:
            _TL.plan_flag[0] = run_dir
        _refuse_resume_changes(args, man)
        summ = man.get("summary") or {}
        merged = summ.get("merged_file")
        if summ.get("verdict") in ("COMPLETE", "PARTIAL") and \
                all(x["status"] in ("COMPLETE", "SPLIT") for x in man.get("slices") or []) and \
                ((merged and os.path.isfile(os.path.join(run_dir, merged))) or (not merged and not args.merge)):
            # Walkthrough M4: resuming a finished export is not an error; nothing is fetched or rewritten. A PARTIAL
            # export is finished too: it keeps its verdict and exit code 3, so a re-run never reports it complete.
            partial = summ.get("verdict") == "PARTIAL"
            log("this export is already finished" + (" (partial: recent data only)" if partial else "")
                + " — nothing to resume", quiet=False)
            cov_w = summ.get("coverage") if partial else None
            res = {"verdict": summ.get("verdict"), "already_complete": True, "rows_written": summ.get("rows_written"),
                   "rows_expected": summ.get("rows_expected"), "slices": f"{summ.get('complete')}/{summ.get('slices')}",
                   "run_dir": run_dir, "merged_file": merged, "exit_code": 3 if partial else 0,
                   "coverage_warning": cov_w, "coverage_unproven": partial,
                   "warnings": list(summ.get("warnings") or [])}
            _set_result(res)
            _print_result(res)
            return 3 if partial else 0
        _restore_args(args, man)
        args.format = args.format or "ndjson.gz"              # a manifest from before 1.1 may not record it
        merged_there = merged and os.path.isfile(os.path.join(run_dir, merged))
        for x in man["slices"]:
            if x["status"] == "COMPLETE" and not merged_there and not os.path.isfile(os.path.join(run_dir, x["file"])):
                x["status"] = "PENDING"                   # its part file is gone: fetch that slice again
                for k in ("written", "stored_duplicates", "outside", "id_conflicts", "reingested_copies", "checked_out"):
                    x.pop(k, None)
    else:
        args.format = args.format or "ndjson.gz"

    q = Query(args)
    if getattr(args, "kql_file", None):
        args.kql, args.kql_file = q.kql, None                    # the manifest keeps the query text, not a path
    storage = getattr(args, "storage", None) or "auto"
    if storage not in STORAGE:
        raise UserError("--storage must be auto, recent or archive")
    if storage == "archive" and (q.kql is not None or q.dsl is not None):
        raise UserError("archive search runs a dataset and filters, not KQL")
    engine_name = resolve_engine_name(args, q)
    if engine_name not in ENGINES:
        raise UserError(f"--engine must be one of {', '.join(ENGINES)}")
    spec = ENGINES[engine_name]
    if spec.get("job") and not args.allow_jobs:
        raise UserError(f"--engine {engine_name} creates a job in your tenant — pass --allow-jobs to proceed")
    if engine_name == "report":
        return cmd_report(args)
    if engine_name in ("v1", "cold", "coldquery") and q.kql:
        raise UserError("KQL needs the dataview engine — leave --engine out")
    if engine_name == "raw" and q.kql:
        raise UserError("full-document (raw) exports take a DSL query — not KQL")
    if engine_name == "raw" and q.shorthand:
        # Measured semantics differ: raw can only express --dataset / --where as match_phrase, which is not the
        # case-insensitive exact 'is one of' every other engine runs, so the same flags would select other events.
        raise UserError("the raw engine cannot run --dataset / --where with the same meaning as the other search "
                        "methods — put the dataset and filters inside the DSL query, or leave --engine out")
    if engine_name in ("v1", "dataview", "cold", "coldquery") and q.dsl is not None:
        raise UserError("a DSL query needs the raw engine — leave --engine out")
    if not q.has_filter() and not args.all_events:
        raise UserError("add a dataset or at least one filter")

    client = make_client(args)
    tenant = tenant_guard(client, expected_tenant(args), args.quiet)
    if q.kql is not None:
        import abx_kql as K
        stop = K.kql_run_problem(q.kql)
        if stop:
            raise UserError(f"this KQL search cannot be exported: {stop}.")
    if q.kql is not None and kql_applies(client) is not True:
        raise UserError("KQL is not available on this tenant now, so this search cannot run as KQL. Build the search "
                        "with a dataset and filters instead.")
    if q.where or q.any:
        q.resolve_types(client)
    if man is not None:
        mt = man.get("tenant") or {}
        if mt.get("vendor_account_id") != tenant["vendor_account_id"] or \
                str(mt.get("base_url", "")).rstrip("/") != client.base:
            raise Fatal(f"this export belongs to {mt.get('name')} ({mt.get('vendor_account_id')}) but you are "
                        f"connected to {tenant['name']} ({tenant['vendor_account_id']}) — connect to the original "
                        f"tenant to resume it")

    start, end = parse_user_ts(args.start, "start"), parse_user_ts(args.end, "end")
    start, end = floor_s(start), floor_s(end)                    # the platform works in whole seconds
    check_window(start, end, args.quiet)

    enum = field_enum(client)
    if man is None:
        check_search_names(client, q, enum, args)
    fields = _fields_for(args, client, engine_name, enum)
    # CSV columns are exactly the fields the user chose (@timestamp and id first). data_stream.dataset is fetched
    # for the per-row dataset guard; it becomes a column only when the user asked for it.
    chosen = [f.strip() for f in (args.fields or "").split(",") if f.strip()]
    columns = [f for f in fields if f != "data_stream.dataset" or f in chosen or not args.fields
               or getattr(args, "every_field", False)]
    if engine_name != "raw" and not getattr(args, "every_field", False):
        bad = validate_fields(client, fields, enum)
        if bad:
            raise UserError(f"These fields do not exist on your tenant: {', '.join(bad)}. Remove them or pick "
                            f"fields from the field list.")
    if q.kql and engine_name == "dataview":
        kql_validate(client, q.kql)

    # Every row of a search with a text filter is checked by the tool against the search (abx_ops), so an event the
    # platform returns for a value it reads differently (one made only of punctuation, measured) is left out. Text
    # values longer than O.LONG_TEXT characters are missed by most text filters, so where the search filters such a
    # field the server is asked for a wider set as well, and the same check keeps exactly what matches.
    proof_q = q
    local = man.get("local_check") if man is not None else None
    if man is None and q.kql is None and q.dsl is None and row_check_needed(q.to_condition()):
        lf: Dict[str, Any] = {}
        if not getattr(args, "no_long_check", False) and text_filter_fields(q.to_condition()):
            _progress(stage="checking for long text values")
            lf = long_text_fields(client, q, start, end, args.quiet)
        orig = q.to_condition()
        local = {"fields": lf, "condition": orig,
                 "extra_fields": [f for f in condition_fields(orig)
                                  if not any(f == r or f.startswith(r + ".") for r in fields)]}
    if local:
        if local["fields"]:
            proof_q.local_fields = tuple(local["fields"])
            guard = q.dataset
            q = q.with_condition(widen_for_fields(local["condition"], list(local["fields"])))
            q.guard_dataset = guard
        fields = fields + [f for f in local.get("extra_fields") or [] if f not in fields]

    page = FULL_DOC_PAGE if getattr(args, "every_field", False) and engine_name in ("v1", "dataview") and \
        not args.page_size else args.page_size
    fmt = part_format(args.format, getattr(args, "every_field", False))      # the part files' format
    if fmt != args.format and not getattr(args, "merge", False):
        raise UserError("a CSV with every field needs --merge: its columns are worked out when the slices are combined")

    if man is None:
        _progress(stage="checking the tenant's retention")
        coverage: Optional[Dict[str, Any]] = None
        segments = [(start, end, engine_name)]
        if storage == "recent":
            coverage = {"name": "Retention coverage", "status": "INFO", "recent_only": True,
                        "detail": "this export searched recent data only; older events kept in the archive are not "
                                  "included."}
        elif engine_name in ("v1", "dataview", "raw"):
            segments, coverage = retention_split(client, start, end, engine_name, args.quiet)
            if coverage and coverage.get("gap"):
                if not args.accept_hot_only:
                    raise Fatal(coverage["detail"])
                coverage.update(unproven=True, status="WARN", gap=False,
                                detail="--accept-hot-only: " + coverage["detail"] + " Those older events are NOT in "
                                       "this export.")
        jobs: Dict[str, str] = {}                 # archive job ids, saved in the manifest from the start
        engines = {n: make_engine(n, client, q, fields, page if n != "cold" else None, args.quiet,
                                  **({"job_state": jobs, "max_wait": args.max_wait} if n == "coldquery" else {}))
                   for n in {x[2] for x in segments}}
        primary = engines[segments[-1][2]]
        hot_segs = [x for x in segments if x[2] != "coldquery"]
        # The filter is proven on recent data (how the platform applies a filter is not a property of the range);
        # archive rows are each re-checked against the search as they arrive.
        proof = make_engine(hot_segs[-1][2] if hot_segs else "v1", client, proof_q, ["id"], None, args.quiet)
        recent_hour = (floor_s(now()) - RELATIVE_END_LAG - dt.timedelta(hours=1), floor_s(now()) - RELATIVE_END_LAG)
        pre = {}
        if not args.no_preflight:
            _progress(stage="proving the filter")
            if hot_segs:
                preflight(proof, proof_q, max(start, hot_segs[-1][0]), end, args.quiet, pre)
            else:
                preflight(proof, proof_q, recent_hour[0], recent_hour[1], args.quiet, pre)
        if getattr(args, "estimate", False):
            return _estimate(args, client, tenant, proof_q, segments, engines, coverage, pre, proof, local)
        plan = []
        for seg_s, seg_e, n in segments:
            eng = engines[n]
            cap = ENGINES[n]["cap"]
            if args.slice:
                step = parse_duration(args.slice)
                s = seg_s
                while s < seg_e:
                    e = min(seg_e, max(floor_s(s + step), s + dt.timedelta(seconds=1)))
                    plan.append((s, e, None, None, n))
                    s = e
            elif n == "coldquery":
                # One archive job per slice of at most ARCHIVE_CHUNK: bounds a slice's memory (SliceSink keeps one id
                # hash per event) and what a failed or resumed job has to run again.
                s = seg_s
                while s < seg_e:
                    e = min(seg_e, s + ARCHIVE_CHUNK)
                    plan.append((s, e, None, None, n))
                    s = e
            else:
                plan += [(s, e, c, z, n) for s, e, c, z in bisect_plan(eng, seg_s, seg_e, cap, args.quiet)]
        if not args.no_preflight:
            if needs_reproof(pre):
                counted = [p for p in plan if p[2]]
                if counted:
                    best = max(counted, key=lambda p: p[2])
                    reprove(proof, proof_q, best[0], best[1], args.quiet, pre)
                elif not hot_segs:
                    reprove(proof, proof_q, recent_hour[1] - dt.timedelta(days=1), recent_hour[1], args.quiet, pre)
            preflight_gate(pre, q)
            log(f"  filter check: {pre['verdict']}", quiet=args.quiet)
        plan = subdivide(plan, engines)
        if run_dir is None:
            stamp = now().strftime("%Y%m%dT%H%M%SZ")
            label = safe_label(q.dataset or getattr(args, "label", None) or "export")
            if args.out:
                run_dir = os.path.abspath(os.path.expanduser(args.out))
                if os.path.exists(os.path.join(run_dir, "manifest.json")):
                    raise UserError(f"{run_dir} already holds an export. Choose a new, empty folder for --out, or "
                                    f"continue that export with --resume.")
                if os.path.isdir(run_dir) and set(os.listdir(run_dir)) - {"search.json"}:
                    raise UserError(f"{run_dir} is not empty. Choose a new, empty folder for --out.")
            else:
                # Unique per run (walkthrough B3): two exports started in the same second must never share a folder.
                while True:
                    run_dir = os.path.join(DEFAULT_OUT_ROOT, f"{stamp}_{label}-{secrets.token_hex(3)}")
                    if not os.path.exists(run_dir):
                        break
        os.makedirs(run_dir, exist_ok=bool(args.out))
        try:
            os.chmod(run_dir, 0o700)
        except OSError:
            pass
        slices = [{"i": i, "start": iso(s), "end": iso(e), "server_total": c, "zero_source": z, "engine": n,
                   "status": "PENDING", "file": f"part-{i:05d}.{fmt}", "attempts": 0, **({"estimate": est} if est else {})}
                  for i, (s, e, c, z, n, est) in enumerate(plan)]
        saved_args = {k: getattr(args, k, None) for k in RESTORABLE}
        man = {"tool": "abstract-search-export", "version": VERSION, "created": iso(now()), "tenant": tenant,
               "engine": primary.name, "args": saved_args, "fields": fields, "columns": columns,
               "query": proof_q.describe(),
               "window": {"start": iso(start), "end": iso(end)}, "preflight": pre, "slices": slices,
               "jobs": jobs, "coverage": coverage, "local_check": local}
        save_manifest(run_dir, man)
        _TL.planned, _TL.run_dir = True, run_dir
        open_run_log(run_dir, "export of " + json.dumps(man["query"], default=str)[:2000])
        if getattr(_TL, "plan_flag", None) is not None:
            _TL.plan_flag[0] = run_dir
        total = sum(_expect(x) for x in slices)
        n_arch = sum(1 for x in slices if x["engine"] == "coldquery")
        arch_days = sum((parse_ts(x["end"]) - parse_ts(x["start"])).total_seconds() for x in slices
                        if x["engine"] == "coldquery") / 86400
        est = estimate_time(total, arch_days, n_arch, None, bool(getattr(args, "every_field", False)))
        _progress(estimate_s=est["total_s"], after_archive_s=est["recent_s"])
        man["estimate"] = est
        save_manifest(run_dir, man)
        if n_arch:
            hot_n = len(slices) - n_arch
            log((f"plan: {total:,} events from recent data in {hot_n:,} time slice" + ("" if hot_n == 1 else "s") +
                 ", plus " if hot_n else "plan: ") + f"{n_arch:,} archive job(s), each counted when it finishes",
                quiet=args.quiet)
        else:
            log(f"plan: {total:,} events in {len(slices):,} time slice" + ("" if len(slices) == 1 else "s"),
                quiet=args.quiet)
        log(f"estimated time: {fmt_duration(est['total_s'])}"
            + (f" (archive jobs {fmt_duration(est['archive_s'])}, plus reading their results; recent data "
               f"{fmt_duration(est['recent_s'])})" if n_arch else ""), quiet=args.quiet)
        log(f"export folder: {run_dir}", quiet=args.quiet)
        if args.dry_run:
            if total == 0 and not any(x[2] == "coldquery" for x in segments):
                log("  ! Nothing matched in the plan. A zero is a claim: check it with count, or a wider window, "
                    "before reporting an absence.", quiet=False)
            log("plan only — nothing exported", quiet=args.quiet)
            res = {"dry_run": True, "rows_planned": total, "slices": len(slices), "run_dir": run_dir,
                   "coverage_warning": (coverage or {}).get("detail") if (coverage or {}).get("status") != "PASS" else None,
                   "filter_unproven": list(pre.get("filter_unproven") or []), "exit_code": 0}
            _set_result(res)
            _print_result(res)
            return 0
    else:
        _reroute_aged_slices(man, client, args.quiet)
        engines = {n: make_engine(n, client, q, fields, page if n != "cold" else None, args.quiet,
                                  **({"job_state": man.setdefault("jobs", {}), "max_wait": args.max_wait}
                                     if n == "coldquery" else {}))
                   for n in {x.get("engine") or man.get("engine") or engine_name for x in man["slices"]}}
        done_n = sum(1 for x in man["slices"] if x["status"] == "COMPLETE")
        log(f"resuming: {done_n:,}/{len(man['slices']):,} slices already complete", quiet=args.quiet)

    with RunLock(run_dir):
        return _run_slices(args, man, run_dir, engines, fields, q, fmt)


def _reroute_aged_slices(man, client, quiet):
    """On resume: a planned recent-data slice whose time has since aged out of recent data would never reconcile.
    Move such slices to archive jobs (the archive also holds recent days, so an early switch loses nothing)."""
    pending = [x for x in man["slices"] if x["status"] not in ("COMPLETE", "SPLIT")
               and (x.get("engine") or man.get("engine")) == "v1"]
    if not pending or min(parse_ts(x["start"]) for x in pending) > now() - dt.timedelta(days=1):
        return
    try:
        edge, st, first = recent_edge(client, quiet)
    except (Retryable, Fatal):
        return
    if (st or {}).get("cold") == 0 or not archive_jobs_available(client):
        return
    moved = 0
    for x in pending:
        if parse_ts(x["start"]) < edge and (first is None or parse_ts(x["start"]) < first):
            x["engine"] = "coldquery"
            for k in ("server_total", "zero_source", "estimate"):
                x.pop(k, None)
            moved += 1
    if moved:
        man.setdefault("jobs", {})
        log(f"  {moved} slice(s) are now older than recent data and will be read from the archive", quiet=quiet)


def _estimate(args, client, tenant, q, segments, engines, coverage, pre, proof=None, local=None):
    """Validate without planning: prove the filter, count the recent-data part on the grid, estimate slices.
    Archive parts are never run here (an archive job takes minutes to an hour): they are counted during the export."""
    total, windows, counted, zeros = 0, 0, [], set()
    archive = [x for x in segments if x[2] == "coldquery"]
    for seg_s, seg_e, n in segments:
        if n == "coldquery":
            continue
        eng = engines[n]
        s = seg_s
        while s < seg_e:
            e = min(seg_e, s + dt.timedelta(hours=6))
            c = None
            for _ in range(2):
                try:
                    c = with_retry(lambda: eng.count(s, e), f"count {iso(s)[:16]}", args.quiet, attempts=2)
                    break
                except Retryable:
                    pass
            if c is None:
                raise Fatal(f"the platform refused to count {iso(s)[:16]}→{iso(e)[:16]} — try a shorter time range")
            if c == 0 and eng.zero_source:
                zeros.add(eng.zero_source)
            total += c
            windows += 1
            counted.append((s, e, c, n))
            _progress(stage="counting", windows_counted=windows, rows_planned=total)
            s = e
    if not args.no_preflight:
        busy = [w for w in counted if w[2]]
        if needs_reproof(pre) and busy:
            best = max(busy, key=lambda w: w[2])
            _progress(stage="proving the filter")
            reprove(proof or engines[best[3]], q, best[0], best[1], args.quiet, pre)
        elif needs_reproof(pre) and not busy and proof is not None:
            _progress(stage="proving the filter")
            last = floor_s(now()) - RELATIVE_END_LAG
            reprove(proof, q, last - dt.timedelta(days=1), last, args.quiet, pre)
        preflight_gate(pre, q)
    hot_seg = [x for x in segments if x[2] != "coldquery"]
    eng = engines[(hot_seg or segments)[-1][2]]
    per = max(1, int(eng.capacity() * SLICE_FILL)) if eng.single else (ENGINES[eng.name]["cap"] or total or 1)
    est = max(1, -(-total // per)) if total else 0
    # zero_source: the engines that produced the zeros (DI-16) — a cross-check by one of them is not independent.
    if archive:
        est += len(archive)
    hot_days = sum((b - a).total_seconds() for a, b, n in segments if n != "coldquery") / 86400
    arch_days = sum((b - a).total_seconds() for a, b, n in archive) / 86400
    # the archive is counted only when its job runs; a guess from recent data's daily rate, said to be a guess
    guess = round(total / hot_days * arch_days) if archive and total and hot_days >= 1 / 24 else None
    if archive and guess is None and proof is not None:
        # the whole range is in the archive: one count of the same search over the last day of recent data gives a
        # daily rate (read only, and only for the estimate)
        last = floor_s(now()) - RELATIVE_END_LAG
        try:
            per_day = with_retry(lambda: proof.count(last - dt.timedelta(days=1), last), "estimate", True, attempts=2)
            guess = round(per_day * arch_days) if per_day else None
        except (Retryable, Fatal):
            guess = None
    timing = estimate_time(total, arch_days, len(archive), guess, bool(getattr(args, "every_field", False)))
    timing.update(archive_events_guess=guess)
    res = {"estimate": {"total": total, "slices": est, "engine": segments[-1][2], "time": timing,
                        "segments": [[iso(a), iso(b), n] for a, b, n in segments],
                        "zero_source": sorted(zeros) if total == 0 else [],
                        "archive": ({"start": iso(archive[0][0]), "end": iso(archive[-1][1]), "jobs": len(archive),
                                     "minutes": (coverage or {}).get("archive_minutes")} if archive else None)},
           "preflight": pre, "coverage": coverage, "tenant": tenant, "exit_code": 0,
           "local_check": ({"fields": local["fields"]} if local and local["fields"] else None)}
    _set_result(res)
    _print_result(res)
    return 0


def _expect(x):
    return x["server_total"] if x.get("server_total") is not None else (x.get("estimate") or 0)


def _run_slices(args, man, run_dir, engines, fields, q, fmt):
    fields = man.get("columns") or fields          # what each part file is written with (CSV header = these)
    store = ManifestStore(run_dir, man)
    guard = getattr(q, "guard_dataset", None) or q.dataset
    dataset_guard = guard.casefold() if guard else None
    lc = man.get("local_check")
    _TL.local_check = {"condition": lc["condition"], "drop": lc.get("extra_fields") or []} if lc else None
    window = (parse_ts(man["window"]["start"]), parse_ts(man["window"]["end"]))
    queue = sorted((x for x in man["slices"] if x["status"] not in ("COMPLETE", "SPLIT")), key=lambda x: x["start"])

    def report():
        live = [x for x in man["slices"] if x["status"] != "SPLIT"]
        # rows_expected is computed exactly as finish() does (server totals minus stored copies and rows owned by a
        # neighbouring slice), so the percentage reaches exactly 100 on a COMPLETE export.
        p = getattr(_TL, "progress", None)
        if isinstance(p, dict) and not p.get("export_started"):
            p["export_started"] = time.time()
        _progress(stage="exporting", slices=len(live), complete=sum(1 for x in live if x["status"] == "COMPLETE"),
                  rows_written=sum(x.get("written", 0) for x in live if x["status"] == "COMPLETE"),
                  rows_expected=sum(_expect(x) for x in live) - sum(x.get("stored_duplicates", 0) for x in live)
                  - sum(x.get("outside", 0) for x in live) - sum(x.get("checked_out", 0) for x in live),
                  current_slice_rows=0)

    report()
    for e_ in engines.values():
        if isinstance(e_, ColdQueryEngine):
            e_.on_job = lambda: store.save(force=True)
    failed = []
    # BY DESIGN (PERF-17): slices run one at a time. The platform limits concurrent searches per user (HTTP 429),
    # so parallel slices would compete with each other and with the user's own searches; throughput is bound by the
    # platform's response time, which parallelism against one per-user limit does not raise.
    #
    # Worst case per slice (CQ-14): the Client makes up to 5 attempts per request (retries=4, each up to --timeout
    # 120 s, waits up to 31 s or a Retry-After capped at 120 s); with_retry makes 2 attempts; a backend, transport,
    # shape or timeout error splits the slice after its 2nd slice attempt, so that is 2 x 2 x 5 = 20 requests
    # (~40 min at 120 s each) before a split. Rate limits and count mismatches retry up to --slice-retries + 1 = 4
    # slice attempts (4 x 2 x 5 = 40 requests) because splitting would not help them.
    try:
        while queue:
            sl = queue.pop(0)
            eng = engines[sl.get("engine") or man.get("engine")]
            eng.page_size = eng.default_page                          # a previous slice's shrink does not persist
            split_k = 0
            for attempt in range(args.slice_retries + 1):
                sl["attempts"] = sl.get("attempts", 0) + 1
                try:
                    s, e = parse_ts(sl["start"]), parse_ts(sl["end"])
                    _progress(current_slice=sl["i"], current_slice_rows=0)
                    if eng.single:
                        # One request answers the slice: its rows AND its total, from the same response.
                        rows, n = with_retry(lambda: eng.fetch_single(s, e), "fetch", args.quiet, attempts=2)
                        sl["server_total"], sl["zero_source"] = n, (eng.zero_source if n == 0 else None)
                        if n > eng.capacity():
                            if e - s > dt.timedelta(seconds=1):
                                split_k = -(-n // max(1, int(eng.capacity() * SLICE_FILL)))
                                break
                            if eng.name == "raw":
                                raise Fatal(f"{n:,} events fall inside one second ({sl['start'][:19]}), more than one "
                                            f"full-document search can return (10,000). Narrow the search.")
                            # more than one response inside a single second
                            run_dense(eng, sl, n, run_dir, fmt, fields, dataset_guard, args.quiet, window)
                        else:
                            run_single(sl, rows, n, run_dir, fmt, fields, dataset_guard)
                    else:
                        if sl.get("server_total") is None:
                            sl["server_total"] = with_retry(lambda: eng.count(s, e), "count", args.quiet, attempts=2)
                        run_slice(eng, sl, run_dir, fmt, fields, dataset_guard, args.quiet, window)
                    sl["status"] = "COMPLETE"
                    sl.pop("error", None)
                    report()
                    extra = f"  ({sl['stored_duplicates']:,} duplicate copies removed)" if sl.get("stored_duplicates") else ""
                    p = getattr(_TL, "progress", None)
                    left = eta_seconds(p, None) if isinstance(p, dict) else None
                    if left is not None and any(x["status"] != "COMPLETE" for x in man["slices"] if x["status"] != "SPLIT"):
                        extra += f"  · {fmt_duration(left)} left"
                    log(f"  ✓ {sl['start'][:19]} → {sl['end'][:19]}  {sl['written']:>10,} events{extra}", quiet=args.quiet)
                    store.save()
                    break
                except RateLimited as ex:
                    sl["status"], sl["error"] = "INCOMPLETE", str(ex)[:500]
                    log(f"  … {ex} — waiting 60s", quiet=args.quiet)
                    nap(60)
                except Retryable as ex:
                    sl["status"], sl["error"] = "INCOMPLETE", str(ex)[:500]
                    log(f"  ✗ {sl['start'][:19]} attempt {attempt + 1}: {ex}", quiet=args.quiet)
                    if ex.kind == "timeout" and eng.page_size > 500 and eng.name in ("v1", "dataview", "coldquery"):
                        eng.page_size = max(500, eng.page_size // 2)       # a smaller response; the slice splits to fit
                    if ex.kind == "mismatch" and not eng.single and eng.page_size > 500:
                        eng.page_size = max(500, int(eng.page_size * 0.73))   # move page boundaries (paged cold jobs)
                    if ex.kind in ("backend", "backend_exception", "transport", "shape", "timeout") and attempt >= 1:
                        split_k = 2
                        break
                    if attempt < args.slice_retries:
                        nap(min(30, 5 * 2 ** attempt))
                except Fatal as ex:
                    sl["status"], sl["error"] = "FAILED", str(ex)[:500]
                    store.save(force=True)
                    log(f"FATAL: {ex}", quiet=False)
                    return finish(run_dir, man, 2, args, error=str(ex))
                except OSError as ex:                                     # disk full, folder removed, permissions
                    msg = f"could not write the export to disk ({ex.strerror or ex}) — free some space, then resume"
                    sl["status"], sl["error"] = "INCOMPLETE", msg
                    try:
                        store.save(force=True)
                    except OSError:
                        pass
                    log(f"STOPPED: {msg}", quiet=False)
                    return finish(run_dir, man, 2, args, error=msg)
            if sl["status"] == "COMPLETE":
                continue
            s, e = parse_ts(sl["start"]), parse_ts(sl["end"])
            if split_k and eng.name != "coldquery" and e - s > dt.timedelta(seconds=1):
                parts = split_window(s, e, split_k)
                sl["status"] = "SPLIT"
                sl.pop("error", None)
                try:
                    os.remove(os.path.join(run_dir, sl["file"]))
                except OSError:
                    pass
                n0 = max(x["i"] for x in man["slices"]) + 1
                per = round((sl.get("server_total") or 0) / len(parts))
                kids = [{"i": n0 + k, "start": iso(a), "end": iso(b), "server_total": None, "estimate": per,
                         "engine": eng.name, "status": "PENDING", "file": f"part-{n0 + k:05d}.{fmt}", "attempts": 0,
                         "parent": sl["i"]} for k, (a, b) in enumerate(parts)]
                man["slices"].extend(kids)
                queue[:0] = kids
                store.save(force=True)
                report()
                log(f"    {sl['start'][:19]}→{sl['end'][:19]} holds more than one response — split into {len(parts)}",
                    quiet=args.quiet)
                continue
            failed.append(sl)
    except (Cancelled, KeyboardInterrupt):
        try:
            store.save(force=True)                              # every finished slice is kept for the resume
        except OSError:
            pass
        raise
    return finish(run_dir, man, 1 if failed else 0, args, engines=engines, q=q)


def _split_drift(man, quiet):
    """DI-12: once every part of a split slice has an answer, compare the parts' totals with the parent's last total.
    Recorded on the parent as information, never an error: windows are live, so late arrivals move totals."""
    by_parent = {}
    for x in man["slices"]:
        if x.get("parent") is not None:
            by_parent.setdefault(x["parent"], []).append(x)
    memo = {}

    def resolved(x):
        if x["i"] in memo:
            return memo[x["i"]]
        if x["status"] == "COMPLETE":
            v = x.get("server_total")
        elif x["status"] == "SPLIT":
            kids = by_parent.get(x["i"]) or []
            vals = [resolved(k) for k in kids]
            v = sum(vals) if kids and all(t is not None for t in vals) else None
        else:
            v = None
        memo[x["i"]] = v
        return v

    for x in man["slices"]:
        if x["status"] != "SPLIT" or x.get("server_total") is None:
            continue                                   # no measured parent total (an estimate proves nothing)
        kids = by_parent.get(x["i"]) or []
        vals = [resolved(k) for k in kids]
        if not kids or any(t is None for t in vals):
            continue
        x["children_total"] = sum(vals)
        x["split_drift"] = x["children_total"] - x["server_total"]
        if x["split_drift"]:
            log(f"  i {x['start'][:19]}→{x['end'][:19]} was counted {x['server_total']:,} before it was split and "
                f"{x['children_total']:,} across its parts ({x['split_drift']:+,}): the window is live, so late "
                f"events move totals; each part was reconciled on its own", quiet=quiet)


def _confirm_zero(man, engines, q, quiet):
    """DI-4: a zero is a claim. Returns (engine that independently counted 0 over the whole window, or None; why not).
    A slice zero the v1 engine could not count (its measured HTTP 500 on zero matches) was already settled by
    dataview, a second engine, so that is the confirmation. Otherwise ONE count over the whole window by an engine
    that did not make the claim: dataview for a v1 export, v1 for a dataview export with conditions."""
    live = [x for x in man["slices"] if x["status"] != "SPLIT"]
    used = {x.get("engine") or man.get("engine") for x in live}
    if q.kql is not None or q.dsl is not None or not used or not used <= {"v1", "dataview"}:
        return None, "no second search method can count this search the same way"
    by_other = {x.get("zero_source") for x in live if x.get("zero_source") and
                x.get("zero_source") != (x.get("engine") or man.get("engine"))}
    if by_other and all(x.get("zero_source") and x.get("zero_source") != (x.get("engine") or man.get("engine"))
                        for x in live):
        return sorted(by_other)[0], None
    s, e = parse_ts(man["window"]["start"]), parse_ts(man["window"]["end"])
    client = next(iter(engines.values())).c
    for name in ("dataview", "v1"):
        if name in used:
            continue
        eng = ENGINE_CLASSES[name](client, q, ["id"], 1, quiet=True)
        try:
            n = with_retry(lambda: eng.count(s, e), f"{name} confirmation count", quiet, attempts=2)
        except (Retryable, Fatal) as ex:
            return None, f"the second search could not count it ({str(ex)[:120]})"
        if n == 0 and eng.zero_source == name:
            return name, None
        if n:
            return None, (f"a second search ({name}) counted {n:,} events in the same window; if the window ended "
                          f"recently, late events explain it — export an earlier window")
        return None, "the second search's zero was itself settled by another engine"
    return None, "no second search method was available"


def finish(run_dir, man, code, args, error=None, engines=None, q=None):
    sl = [x for x in man["slices"] if x["status"] != "SPLIT"]
    done = [x for x in sl if x["status"] == "COMPLETE"]
    written = sum(x.get("written", 0) for x in done)
    stored = sum(x.get("stored_duplicates", 0) for x in sl)
    outside = sum(x.get("outside", 0) for x in sl) + sum(x.get("checked_out", 0) for x in sl)
    server = sum(x.get("server_total") or 0 for x in sl)
    expected = server - stored - outside
    conflicts = sum(x.get("id_conflicts", 0) for x in sl)
    reingested = sum(x.get("reingested_copies", 0) for x in sl)
    bad_ts = sum(x.get("bad_timestamps", 0) for x in sl)
    complete = code == 0 and len(done) == len(sl) and written == expected
    if code == 0 and not complete:
        code = 1
    first_err = error or next((x.get("error") for x in sl if x["status"] in ("FAILED", "INCOMPLETE") and x.get("error")),
                              None)
    cov = man.get("coverage") or {}
    pre = man.get("preflight") or {}
    unproven = list(pre.get("filter_unproven") or [])
    warnings = []
    zero_result, zero_by, zero_warning = False, None, None
    _split_drift(man, getattr(args, "quiet", False))
    if complete and server == 0:
        # BY DESIGN (DI-4): a zero stays COMPLETE with exit code 0, so scheduled exports of quiet datasets do not
        # fail; the claim is surfaced as zero_result / zero_confirmed_by and a warning line instead.
        zero_result = True
        why = "no second search method is available for this kind of search"
        if engines and q is not None:
            _progress(stage="confirming the zero")
            zero_by, why = _confirm_zero(man, engines, q, getattr(args, "quiet", False))
        zero_warning = (f"Nothing matched. A zero is a claim: confirmed by {zero_by}." if zero_by else
                        f"Nothing matched. A zero is a claim: not independently confirmed ({why}).")
    if bad_ts:
        warnings.append(f"{bad_ts:,} events had a @timestamp the tool could not read as a time, so it could not check "
                        f"which time slice owns them; each was written in the slice that returned it.")
    if unproven:
        warnings.append("Not proven on this tenant: the filter on " + ", ".join(unproven) + " removed nothing in the "
                        "sample window (or its check returned an error), so the tool cannot confirm it narrows the "
                        "results. Check the exported events before relying on that filter.")
    summary = {"slices": len(sl), "complete": len(done), "rows_written": written, "rows_expected": expected,
               "stored_duplicates": stored, "reingested_copies": reingested, "id_conflicts": conflicts,
               "bad_timestamps": bad_ts, "filter_unproven": unproven, "zero_result": zero_result,
               "zero_confirmed_by": zero_by, "finished": iso(now()), "verdict": "INCOMPLETE"}
    if cov.get("unproven"):
        summary["coverage"] = partial_sentence(cov)
    elif cov.get("retention_starts"):
        summary["coverage"] = f"searchable history starts about {cov['retention_starts']}; nothing earlier exists"
    man["summary"] = summary
    if complete:
        _progress(stage="combining the files")
        if getattr(args, "merge", False):
            try:
                merged = merge_parts(run_dir, man, keep_parts=getattr(args, "keep_parts", False))
                summary["merged_file"] = os.path.basename(merged)
            except Retryable as ex:                   # a part file no longer matches its slice: resume refetches it
                code, first_err = 1, str(ex)
                complete = False
            except (Fatal, OSError) as ex:
                code, first_err = 2, str(ex)
                complete = False
        if complete:
            # PARTIAL: every slice finished, but only recent data was searched; older data was not (exit code 3).
            # Scripts that test verdict == "COMPLETE" therefore never mistake a partial export for a complete one.
            summary["verdict"] = "PARTIAL" if cov.get("unproven") else "COMPLETE"
            if cov.get("unproven"):
                code = 3
    if not complete and zero_result:                              # a zero is only a result once the export is complete
        zero_result, zero_by = False, None
        summary["zero_result"], summary["zero_confirmed_by"] = False, None
    if not complete:
        done = [x for x in sl if x["status"] == "COMPLETE"]
        summary["complete"] = len(done)
    if zero_result and zero_warning:                              # added only once the export is final (CQ-20)
        warnings.insert(0, zero_warning)
    cov_warning = partial_sentence(cov) if cov.get("unproven") else \
        ("Retention: " + cov["detail"]) if cov.get("retention_starts") else \
        ("Recent data only: " + cov["detail"]) if cov.get("recent_only") else None
    if cov_warning:
        warnings.append(cov_warning)                              # walkthrough M3: never only in its own key
    summary["warnings"] = warnings
    save_manifest(run_dir, man)
    res = {"verdict": summary["verdict"], "rows_written": written, "rows_expected": expected,
           "stored_duplicates": stored, "id_conflicts": conflicts, "slices": f"{len(done)}/{len(sl)}",
           "run_dir": run_dir,
           "merged_file": summary.get("merged_file"), "exit_code": code,
           "coverage_warning": cov_warning,
           "coverage_unproven": bool(cov.get("unproven")), "error": first_err if not complete else None,
           "zero_result": zero_result, "zero_confirmed_by": zero_by, "bad_timestamps": bad_ts,
           "filter_unproven": unproven, "warnings": warnings}
    for w in warnings:
        log(f"  ! {w}", quiet=False)                  # printed even with --quiet, like errors
    _progress(stage="done")
    _set_result(res)
    _print_result(res)
    if not complete:
        log(f"INCOMPLETE — resume it later (Export history → Resume, or: export --resume \"{run_dir}\")", quiet=False)
    return code


PARTIAL = "Partial: recent data only. Older data was not searched."


def not_searched_words(cov):
    """The part of the range that was not searched, in words: "2026-10-01 00:00 to 2026-10-24 06:00 UTC"."""
    ns = (cov or {}).get("not_searched") or []
    a = ns[0] if ns else None
    b = ns[1] if len(ns) > 1 else None
    fmt = lambda t: str(t)[:16].replace("T", " ")
    if a and b:
        return f"{fmt(a)} to {fmt(b)} UTC"
    if a:
        return f"from {fmt(a)} UTC up to where recent data begins (that date could not be read)"
    return "the part of the range older than recent data"


def partial_sentence(cov):
    """Every human-facing line for exit code 3 starts with this: the verdict, then the range not searched, then why."""
    return f"{PARTIAL} Not searched: {not_searched_words(cov)}. " + str((cov or {}).get("detail") or "")


def _print_result(res):
    if getattr(_TL, "sink", None) is None and LOG_SINK is None:
        print(json.dumps(res, default=str))


# ───────────────────────────── report export (job, command line) ─────────────────────────────

def cmd_report(args):
    if not args.report_id or not re.match(r"^[A-Za-z0-9_-]{1,64}$", args.report_id):
        raise UserError("--engine report needs a valid --report-id")
    client = make_client(args)
    tenant = tenant_guard(client, expected_tenant(args), args.quiet)
    body = {"format": args.report_format}
    if args.start and args.end:
        body["timeRange"] = {"from": args.start, "to": args.end}
    p = f"/v2/reports/{args.report_id}/export"
    code, j, _ = client.post(p, body)
    classify_error(code, j, p)
    task = (j or {}).get("taskId")
    if not task:
        raise Fatal(f"the report export was not accepted — {str(j)[:300]}")
    log(f"report export {task} accepted — waiting", quiet=args.quiet)
    run_dir = os.path.abspath(os.path.expanduser(args.out)) if args.out else \
        os.path.join(DEFAULT_OUT_ROOT, f"{now().strftime('%Y%m%dT%H%M%SZ')}_report-{safe_label(args.report_id)}-"
                                       f"{secrets.token_hex(3)}")
    if os.path.isdir(run_dir) and os.listdir(run_dir):
        raise UserError(f"{run_dir} is not empty. Choose a new, empty folder for --out.")
    os.makedirs(run_dir, exist_ok=True)
    t0 = time.time()
    while True:
        code, rec, _ = client.get(f"/v2/reports/exports/{urllib.parse.quote(str(task))}")
        classify_error(code, rec, "/v2/reports/exports/{id}")
        st = str((rec or {}).get("status", "")).lower()
        if st in ("completed", "complete", "succeeded", "success", "failed", "error", "partial"):
            break
        if time.time() - t0 > args.max_wait:
            raise Fatal(f"report export {task} still {st} after {args.max_wait}s")
        nap(10)
    with _open_nofollow(os.path.join(run_dir, "export-record.json"), True, "report record") as fh:
        fh.write(json.dumps(rec, indent=2).encode("utf-8"))
    urls = sorted({v for v in _walk_strings(rec) if v.startswith("https://")})
    failed = rec.get("failedWidgets") or []
    res = {"status": st, "failedWidgets": failed, "download_urls": urls, "run_dir": run_dir, "tenant": tenant["name"]}
    _set_result(res)
    _print_result(res)
    return 0 if st in ("completed", "complete", "succeeded", "success") and not failed else 1


def _walk_strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _walk_strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_strings(v)


# ───────────────────────────── count (cross-engine control) ─────────────────────────────

def cmd_count(args):
    client = make_client(args)
    tenant_guard(client, expected_tenant(args), args.quiet)
    q = Query(args)
    if q.where or q.any:
        q.resolve_types(client)
    if not q.shorthand or not q.portable:
        raise UserError("count needs --dataset and/or --where (it compares search methods on exactly the same "
                        "search); it does not take --from-view, --from-file or a condition")
    check_search_names(client, q, field_enum(client), args)
    start, end = floor_s(parse_user_ts(args.start, "start")), floor_s(parse_user_ts(args.end, "end"))
    check_window(start, end, args.quiet)
    engines = [e.strip() for e in (args.engines or "v1,dataview,raw,cold").split(",") if e.strip()]
    unknown = [e for e in engines if e not in ENGINE_CLASSES]
    if unknown:
        raise UserError(f"count supports {', '.join(ENGINE_CLASSES)} — not {', '.join(unknown)}")
    if "coldquery" in engines and not args.allow_jobs:
        raise UserError("coldquery submits a job in your tenant — pass --allow-jobs to include it")
    rows = {}
    for name in engines:
        eng = ENGINE_CLASSES[name](client, q, ["@timestamp", "id"], 1, quiet=args.quiet)
        try:
            real = with_retry(lambda: eng.count(start, end), f"{name} count", args.quiet)
            src = eng.zero_source if real == 0 else None
            ctrls = []
            for i in range(len(q.leaves())):
                if (q.types or {}).get(q.leaves()[i][0]) == "Boolean":
                    ctrls.append("n/a")                    # a true/false field has no impossible value
                    continue
                ov = [(f, [CONTROL_VALUE]) if j == i else (f, v) for j, (f, v) in enumerate(q.leaves())]
                ctrls.append(_try_count(lambda ov=ov: eng.count(start, end, override=ov), f"{name} control",
                                        args.quiet) if name != "coldquery" else None)
            checked = [c for c in ctrls if c != "n/a"]
            if any(c not in (0, None) for c in checked):
                ctl = "FAIL — filter not confirmed"
            elif checked and all(c == 0 for c in checked):
                ctl = "PASS"
            else:
                ctl = "INCONCLUSIVE"
            rows[name] = {"total": real, "impossible_value_controls": ctrls, "control": ctl,
                          "zero_confirmed_by": src if src and src != name else None}
        except NotEnabled as ex:
            rows[name] = {"unavailable": "not enabled on this tenant", "detail": str(ex)[:200]}
        except (Fatal, Retryable) as ex:
            rows[name] = {"error": str(ex)[:300]}
        if name == "raw":
            # DI-14: raw can express --dataset / --where only as match_phrase, not the case-insensitive exact
            # 'is one of' of the other engines, so its count is shown for reference and never decides the verdict.
            rows[name]["semantics"] = "phrase match (approximate)"
    exact = {n: r for n, r in rows.items() if n != "raw"}
    independent = {n: r for n, r in exact.items() if "total" in r and not r.get("zero_confirmed_by")}
    totals = {r["total"] for r in exact.values() if "total" in r}
    errored = [n for n, r in exact.items() if "error" in r]
    ok = len(totals) == 1 and not errored and len(independent) >= 1 and \
        all(r.get("control") in ("PASS", "INCONCLUSIVE") for r in exact.values() if "total" in r) and \
        any(r.get("control") == "PASS" for r in exact.values())
    res = {"window": [iso(start), iso(end)], "query": q.describe(), "engines": rows, "agree": len(totals) == 1,
           "errored": errored, "verdict": "PASS" if ok else "FAIL"}
    _set_result(res)
    _print_result(res)
    return 0 if ok else 1


# ───────────────────────────── CLI ─────────────────────────────

def advanced_on():
    """Advanced options always work; they are listed in --help only when ABX_ADVANCED=1 is set."""
    return os.environ.get("ABX_ADVANCED") == "1"


def build_parser():
    adv = (lambda h: h) if advanced_on() else (lambda h: argparse.SUPPRESS)
    p = argparse.ArgumentParser(prog="abstract_search.py",
                                description="Abstract Search & Export — command line (see Start-Here-Guide.html, Part 3)",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--help-engines", action="store_true", help=adv("print how an export works and exit"))
    sub = p.add_subparsers(dest="cmd", metavar="{export,count}" if advanced_on() else "{export}")

    def common(sp):
        sp.add_argument("--start", help="UTC, e.g. 2026-09-28T00:00:00Z (whole seconds)")
        sp.add_argument("--end", help="UTC, must not be in the future")
        sp.add_argument("--last", help="relative window ending 10 minutes ago: 15m, 1h, 24h, 7d, 30d")
        sp.add_argument("--dataset", help="the dataset (integration configuration) name")
        sp.add_argument("--where", action="append", help="field=value[,value…] (is one of), or a condition as in "
                        "Abstract's search, e.g. \"user_name !contains example.com\"; repeatable")
        sp.add_argument("--match", choices=("all", "any"), default=None,
                        help="all (default): every --where must match; any: at least one --where must match")
        sp.add_argument("--condition-file", help=adv("a StreamViewer condition object (JSON file)"))
        sp.add_argument("--condition-json", help=adv("a StreamViewer condition object (inline JSON)"))
        # No --kql / --kql-file (removed 2026-10-01): on production tenants the search API silently ignores KQL
        # and returns every event. A search copied from Abstract is CONVERTED to filters instead:
        sp.add_argument("--dsl-file", help=adv("an Elasticsearch-style DSL query (JSON file) — full documents"))
        sp.add_argument("--dsl-json", help=adv("an Elasticsearch-style DSL query (inline JSON)"))
        sp.add_argument("--expect-tenant", help="the vendor account id you expect (required unless ABSTRACT_EXPECT_TENANT)")
        sp.add_argument("--vendor-account", help=adv("override ABSTRACT_VENDOR_ACCOUNT_ID"))
        sp.add_argument("--base-url", help=adv("override ABSTRACT_BASE_URL (https only)"))
        sp.add_argument("--timeout", type=int, default=120, help=adv("seconds per request (default 120)"))
        sp.add_argument("--quiet", action="store_true", help="no progress lines (the final JSON is still printed)")
        sp.add_argument("--storage", choices=STORAGE, default=None,
                        help="where to search: auto (recent data, plus the archive when the range reaches past it; "
                             "default), recent (recent data only, fastest), archive (archive storage for the whole "
                             "range, slower)")
        sp.add_argument("--allow-unlisted-dataset", action="store_true",
                        help=adv("search a dataset name the tenant does not list (normally refused: a wrong name is a "
                             "silent zero)"))

    e = sub.add_parser("export", help="export events to disk (resumable)")
    common(e)
    e.add_argument("--from-view", help="one of your saved views in Abstract (name or id); converted to a dataset "
                                        "and filters, or run as KQL where this tenant supports it")
    e.add_argument("--from-file", help="a search copied from Abstract's search box, in a text file; converted the "
                                        "same way")
    e.add_argument("--from-saved", help="one of your saved searches (see: searches)")
    e.add_argument("--kql", help=argparse.SUPPRESS)       # used by the app when this tenant applies KQL
    e.add_argument("--fields", help="comma-separated fields (default @timestamp,id,data_stream.dataset,message)")
    e.add_argument("--every-field", action="store_true", help="export complete events instead of a field list")
    e.add_argument("--format", default=None, choices=["ndjson", "ndjson.gz", "csv"], help="default ndjson.gz")
    e.add_argument("--merge", action="store_true", help="combine the slice files into export.<format> when complete")
    e.add_argument("--keep-parts", action="store_true", help=adv("keep the slice files after combining"))
    e.add_argument("--out", help="export folder (default: a new folder under ABX_OUT_ROOT)")
    e.add_argument("--resume", help="resume an export folder")
    e.add_argument("--dry-run", action="store_true", help=adv("plan only: check, count and write the manifest"))
    e.add_argument("--estimate", action="store_true", help=argparse.SUPPRESS)
    e.add_argument("--all-events", action="store_true", help=adv("allow an export with no filter at all"))
    e.add_argument("--no-preflight", action="store_true", help=adv("skip the filter proof (not recommended)"))
    e.add_argument("--no-long-check", action="store_true",
                   help=adv("skip the check for text values too long for the platform to filter (not recommended)"))
    e.add_argument("--accept-hot-only", action="store_true",
                   help=adv("export hot storage only even if older events exist only in cold storage"))
    e.add_argument("--slice", help=adv("advanced: fixed slice width (e.g. 1h)"))
    e.add_argument("--page-size", type=int, help=adv("advanced: events per request"))
    e.add_argument("--slice-retries", type=int, default=3, help=argparse.SUPPRESS)
    e.add_argument("--engine", default="auto", help=adv("advanced: v1 | dataview | raw | cold | coldquery | report"))
    e.add_argument("--allow-jobs", action="store_true", help=adv("advanced: permit coldquery / report jobs"))
    e.add_argument("--report-id", help=argparse.SUPPRESS)
    e.add_argument("--report-format", default="csv_zip", choices=["csv_zip", "json_definition"], help=argparse.SUPPRESS)
    e.add_argument("--max-wait", type=int, default=3600, help=argparse.SUPPRESS)
    e.set_defaults(func=cmd_export)

    c = sub.add_parser("count", help="count one search several independent ways, with filter proofs")
    common(c)
    c.add_argument("--engines", help="advanced: comma list (default v1,dataview,raw,cold)")
    c.add_argument("--allow-jobs", action="store_true", help=argparse.SUPPRESS)
    c.set_defaults(func=cmd_count)
    return p


def secure_umask():
    try:
        os.umask(0o077)                                         # exports hold sensitive event data
    except (AttributeError, OSError):
        pass


_TL_KEYS = ("sink", "cancel", "progress", "creds", "result", "planned")    # progress: time left, on the worker too


def _run_on_worker(fn, args):
    """Run the command on a worker thread while the main thread waits in 0.5 s steps.

    On Windows a blocking socket read (up to --timeout, 120 s) cannot be interrupted, so Ctrl+C would wait for it.
    The main thread's short joins let Ctrl+C land at once: the first one sets the shared cancel Event (naps and
    request loops stop at once, an in-flight read at its end, so the lock and manifest are saved as usual); a second
    one stops waiting and quits. Exceptions are re-raised here, so the exit codes are exactly the in-thread ones."""
    ctx = {k: getattr(_TL, k, None) for k in _TL_KEYS}
    ev = ctx["cancel"] if ctx["cancel"] is not None else threading.Event()
    ctx["cancel"] = ev
    box = {}

    def work():
        for k, v in ctx.items():
            setattr(_TL, k, v)
        try:
            box["code"] = fn(args)
        except BaseException as ex:                     # handed to the main thread's except ladder
            box["error"] = ex
        finally:
            box["result"], box["planned"] = getattr(_TL, "result", {}), getattr(_TL, "planned", False)
            box["run_dir"] = getattr(_TL, "run_dir", None)
            done.set()

    # The worker says when it has finished. Thread.is_alive() is not used for that: a KeyboardInterrupt that lands
    # inside Thread.join() can leave is_alive() reporting False while the worker is still running (seen on Linux
    # with Python 3.12), which returned before the worker had stored its exit code.
    done = threading.Event()
    plan_flag = [False]
    ctx["plan_flag"] = plan_flag
    t = threading.Thread(target=work, name="abx-cli", daemon=True)
    t.start()
    interrupted = False
    try:
        while not done.is_set():
            try:
                done.wait(0.5)
            except KeyboardInterrupt:
                if interrupted:
                    raise                               # second Ctrl+C: stop waiting
                interrupted = True
                ev.set()
                log("stopping after the current request — press Ctrl+C again to quit now", quiet=False)
    finally:
        if "result" in box:
            _TL.result, _TL.planned, _TL.run_dir = box["result"], box["planned"], box.get("run_dir")
        else:
            _TL.planned = bool(plan_flag[0])            # second Ctrl+C: the worker is still running
            _TL.run_dir = plan_flag[0] if isinstance(plan_flag[0], str) else None
    if "error" in box:
        raise box["error"]
    return box["code"]


def main(argv=None):
    secure_umask()
    _TL.planned, _TL.run_dir = False, None
    if getattr(_TL, "progress", None) is None:
        _TL.progress = {}                              # the command line: the time-left estimate needs it too

    def fail(code, level, msg, hint="", exc=None):
        log(f"{level}: {msg}{hint}", quiet=False)
        path = log_error(level, f"{msg}{hint}", exc)
        log(f"  (kept in the log: {path})", quiet=False)
        res = {"error": str(msg), "exit_code": code, "log_file": path}
        rd = getattr(_TL, "run_dir", None) if _TL.planned else None
        if rd:
            res["run_dir"] = rd                         # walkthrough M2: always say which folder to resume
            if code in (1, 130):
                log(f"  resume it with: export --resume \"{rd}\" --merge", quiet=False)
        _set_result(res)
        _print_result(res)                              # the CLI always ends with one JSON result line
        return code
    try:
        p = build_parser()
        args = p.parse_args(argv)
        if args.help_engines:
            print(ENGINE_NOTES)
            return 0
        if not args.cmd:
            p.print_help()
            return 2
        if getattr(args, "last", None) and not (args.start or args.end):
            end = floor_s(now()) - RELATIVE_END_LAG
            args.start, args.end = iso(end - parse_duration(args.last)), iso(end)
        if getattr(args, "from_view", None) or getattr(args, "from_file", None):
            if getattr(args, "resume", None):
                raise UserError("--from-view / --from-file start a new export; they cannot be used with --resume")
            apply_imported_search(args)
        needs_window = args.cmd == "count" or (args.cmd == "export" and not args.resume and
                                               getattr(args, "engine", "") != "report")
        if needs_window and (not args.start or not args.end):
            raise UserError("give a time range: --last 24h, or --start and --end")
        if getattr(_TL, "sink", None) is None and threading.current_thread() is threading.main_thread():
            return _run_on_worker(args.func, args)      # the real CLI: Ctrl+C must land promptly (Windows)
        return args.func(args)
    except SystemExit as ex:
        return ex.code if isinstance(ex.code, int) else 2
    except Fatal as ex:
        return fail(2, "STOPPED", ex)
    except UserError as ex:
        return fail(2, "ERROR", ex)
    except Cancelled:
        return fail(130, "STOPPED", "cancelled", " — resume the export to continue" if _TL.planned else "")
    except KeyboardInterrupt:
        return fail(130, "STOPPED", "cancelled", " — resume the export to continue" if _TL.planned else "")
    except Retryable as ex:
        if _TL.planned:
            return fail(1, "FAILED", ex, "\n  (resume the export later to continue)")
        return fail(2, "FAILED", ex, "\n  (nothing was planned yet — run the command again)")
    except OSError as ex:
        return fail(2, "STOPPED", f"could not read or write a file ({ex})")
    except Exception as ex:                             # a fault in the tool itself: details to the log, not a traceback
        return fail(2, "FAILED", f"something went wrong in the tool itself ({type(ex).__name__}: {ex}); please send "
                                 f"the log to your Abstract contact", exc=ex)


if __name__ == "__main__":
    sys.exit(main())
