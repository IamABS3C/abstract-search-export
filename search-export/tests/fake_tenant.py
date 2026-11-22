"""
fake_tenant — an in-memory Abstract tenant behind a fake HTTP transport, for the offline test suite.

The seam is abx_engine._OPENER (the one object every Client request goes through). Replacing it keeps the real
Client logic under test: retries, Retry-After, 5xx classification and the event-stream handling.

The fake answers:
  GET  /v2/auth/                        tenant org name and vendor account id
  POST /v1/streamviewer/search          whole-second [floor(start), floor(end)) windows, conditions, page_size,
                                        metadata.total_events, last_sort / search_after, and the selected_fields
                                        422 that enumerates the tenant's fields
  POST /v1/dataview/search              text/event-stream with data and done events carrying total_count
  POST /v2/streamviewer/raw-search      hits and total.value / relation (a small DSL subset, and terms aggs)
  POST /v1/coldstorage/v2/search        items and total_count (cold storage holds every event)
  GET  /v1/configurations/              the tenant's datasets
  POST /v1/kql/validate                 404 by default (validation not available), like tenants without it
Anything else is answered 404 and recorded in `unhandled`, so a test can assert the engine called nothing new.

Injectable faults: stored duplicate copies (identical, or differing only in ingested_time / insertion_time, placed
N rows apart), two different events sharing an id, HTTP 500 for zero-match v1 searches, 500 for v1 windows wider
than N seconds, 429 with Retry-After, a 502 on the first call only, filters that are silently ignored (all, or per
field), KQL ignored, and hot retention (nothing before a date in hot; cold holds everything).
"""
import copy
import datetime as dt
import email.message
import io
import ipaddress
import json
import math
import re
import urllib.error
import urllib.parse

import abx_ops as O

UTC = dt.timezone.utc

DEFAULT_ENUM = ["@timestamp", "id", "message", "data_stream", "data_stream.dataset", "ingested_time",
                "insertion_time", "user_name", "source_ipv4", "dest_port", "action"]
DEFAULT_TYPES = {"source_ipv4": "Ipv4", "dest_port": "Float64"}


def parse(s):
    s = str(s).strip().replace("Z", "+00:00")
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def iso_ms(d):
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)


def floor_sec(d):
    return math.floor(d.timestamp())


def dig(obj, dotted):
    if isinstance(obj, dict) and dotted in obj:
        return obj[dotted]
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def project(row, fields):
    """Nested selected_fields projection, like the platform (an absent field is simply left out)."""
    if not fields or fields == ["*"]:
        return copy.deepcopy(row)
    out = {}
    for f in sorted(fields, key=lambda x: x.count(".")):
        v = dig(row, f)
        if v is None:
            continue
        parts, cur = f.split("."), out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = copy.deepcopy(v)
    return out


class Reject(Exception):
    def __init__(self, code, payload, headers=None):
        super().__init__(code)
        self.code, self.payload, self.headers = code, payload, headers or {}


class FakeTenant:
    def __init__(self, vid="VAtest0001", name="Test Tenant Alpha", enum=None, types=None):
        self.vid, self.name = vid, name
        self.enum = list(enum or DEFAULT_ENUM)
        self.types = dict(DEFAULT_TYPES if types is None else types)
        self.rows = []                     # stored rows, copies included; internal keys start with "_"
        self._n = 0
        # fault knobs
        self.v1_500_on_zero = False
        self.v1_500_wider_than = None      # seconds: v1 refuses (HTTP 500) windows wider than this
        self.rate_limit_429 = 0            # answer this many search calls with 429 first
        self.retry_after = "7"
        self.fail_first_502 = False
        self.ignore_filter = set()         # {"*"} = every condition ignored; or a set of field names
        self.ignore_kql = False
        self.kql_validate = False          # True: /v1/kql/validate answers {"valid": true}
        self.kql_violations = []           # violations /v1/kql/validate reports (implies kql_validate)
        self.views = []                    # saved views: [{"id", "name", "kql_query", "source_ids"}]
        self.hot_since = None              # datetime: hot storage holds nothing older
        self.cold_broken = False           # cold storage answers 500
        self.cold_filter_broken = False    # cold answers unfiltered searches but 422s every filtered one
        # archive jobs (/v1/coldstorage/query/*) and the tenant record (/v1/vendor-accounts/{id})
        self.hot_days, self.cold_days = None, 365  # storage_retention (hot None = no limit, reported as 3650)
        self.vendor_readable = True        # False: the key cannot read the tenant record (403)
        self.jobs_enabled = True           # False: archive jobs answer 404 (not available to this key)
        self.jobs = {}                     # id -> {"start", "end", "condition", "polls", "final", "cancelled"}
        self.job_polls = 1                 # GETs that answer IN_PROGRESS before the job finishes
        self.job_failed_files = 0          # the next N jobs finish COMPLETED with failed files
        self.job_pause = 0                 # the next N jobs end PAUSED
        self.job_forget = 0                # the next N saved job ids answer 404 on their first GET (expired)
        self.job_ignore_filter = False     # job results ignore the condition (an archive that drops a filter)
        self.raw_enabled = True            # False: raw search answers "not enabled" (as tenants without it do)
        self.flags = {"enable_kql_search_mode": True, "enable_cold_storage": True}   # UI flags (informational)
        self.on_request = None             # hook(tenant, method, path, body)
        self.v1_500_message = "Exception occurred in get_events operation."   # the measured zero-match 500 body
        self.v1_500_body = None            # a whole 500 body instead of {"message": v1_500_message}
        self.v1_error_body = 0             # answer this many v1 searches with HTTP 200 {"message": ...} (no events)
        self.raw_extra = 0                 # raw totals are off by this many (models match_phrase != IS_IN_LIST)
        self.bad_utf8 = 0                  # corrupt this many v1 answers with an invalid UTF-8 byte in a value
        self.config_page_cap = None        # /v1/configurations/ pages for real, at most this many rows per page
        self.extra_datasets = []           # dataset (configuration) names with no events
        self.fieldsets = {}                # id -> fieldset (field-set CRUD)
        self.nl_answers = {}               # plain English -> {"kql", "explanation"} for /v1/ase/kql
        self.nl_enabled = True             # False: /v1/ase/kql answers 404
        self.to_conditions_enabled = True  # False: /v1/kql/to-conditions answers 404
        self.suggest = {}                  # field -> values for field-suggestions
        self._ids = 0
        self.query = {}                    # the query string of the current request
        self.calls = []                    # (method, path, body)
        self.unhandled = []
        self._first_done = False

    # ── data ──
    def add(self, ts, eid=None, dataset="fw", **fields):
        if isinstance(ts, dt.datetime):
            ts = iso_ms(ts)
        self._n += 1
        row = {"@timestamp": ts, "id": eid or "ev-%06d" % self._n, "data_stream": {"dataset": dataset},
               "message": fields.pop("message", "event %d" % self._n),
               "ingested_time": fields.pop("ingested_time", ts), "insertion_time": fields.pop("insertion_time", ts)}
        row.update(fields)
        row["_k"] = float(self._n * 1000)
        self.rows.append(row)
        return row

    def add_copy(self, row, reingested=False, distance=0):
        """A stored copy of `row` (same id and @timestamp) sitting about `distance` rows away from it in the
        platform's sort order. reingested=True: differs only in ingested_time / insertion_time."""
        c = copy.deepcopy(row)
        if reingested:
            later = iso_ms(parse(row["@timestamp"]) + dt.timedelta(minutes=3))
            c["ingested_time"] = c["insertion_time"] = later
        same = sorted((r for r in self.rows if r["@timestamp"] == row["@timestamp"]), key=lambda r: -r["_k"])
        pos = next(i for i, r in enumerate(same) if r is row)
        if distance <= 0:
            c["_k"] = row["_k"] - 0.25                      # right next to the original
        elif pos + distance < len(same):
            c["_k"] = same[pos + distance]["_k"] - 0.5 / (1 + len(self.rows))   # just after the row `distance` below
        else:
            tgt = max(0, pos - distance)
            c["_k"] = same[tgt]["_k"] + 0.5 / (1 + len(self.rows))              # just before the row `distance` above
        self.rows.append(c)
        return c

    def order_distance(self, rid):
        """Largest gap (in rows, in the platform's sort order) between stored copies of one id."""
        order = sorted(self.rows, key=lambda r: (parse(r["@timestamp"]), r["_k"]), reverse=True)
        pos = [i for i, r in enumerate(order) if r["id"] == rid]
        return max(pos) - min(pos) if len(pos) > 1 else 0

    def unique_ids(self, start, end, dataset=None):
        a, b = floor_sec(start), floor_sec(end)
        return {r["id"] for r in self.rows if a <= floor_sec(parse(r["@timestamp"])) < b
                and (dataset is None or dig(r, "data_stream.dataset") == dataset)}

    def distinct_events(self, start, end, dataset=None):
        a, b = floor_sec(start), floor_sec(end)
        seen = {}
        for r in self.rows:
            if a <= floor_sec(parse(r["@timestamp"])) < b and (dataset is None or
                                                               dig(r, "data_stream.dataset") == dataset):
                core = {k: v for k, v in r.items() if not k.startswith("_") and k not in ("ingested_time",
                                                                                          "insertion_time")}
                seen[json.dumps(core, sort_keys=True)] = r["id"]
        return seen

    # ── matching ──
    def _ignored(self, field):
        return "*" in self.ignore_filter or field in self.ignore_filter

    def _leaf(self, row, c):
        f = c.get("field")
        op = str(c.get("operation") or c.get("field_operation") or "").upper()
        ftype = c.get("field_type") or "String"
        want = self.types.get(f, "String")
        if ftype != want:
            raise Reject(422, {"detail": "Invalid field type for field '%s'. Expected '%s'" % (f, want)})
        if self._ignored(f):
            return True
        if op in ("CONTAINS_ANY", "CONTAINS_ALL") and not ftype.startswith("List"):
            raise Reject(422, {"detail": "Invalid operation '%s' for given type '%s'." % (op, ftype)})
        if op in ("IS_IN_LIST", "IS_NOT_IN_LIST") and ftype.startswith("List"):
            raise Reject(422, {"detail": "Invalid operation '%s' for given type '%s'." % (op, ftype)})
        v = dig(row, f)
        if getattr(self, "_archive", False):
            # measured: archive jobs match patterns with exact case always, and "contains" on a list whole items
            if op in ("MATCHES_REGEX", "DOES_NOT_MATCH_REGEX"):
                c = dict(c, case_sensitive=True)
            if ftype.startswith("List") and op in ("CONTAINS", "DOES_NOT_CONTAIN"):
                c = dict(c, operation="CONTAINS_ANY" if op == "CONTAINS" else "DOES_NOT_CONTAIN",
                         values=O.leaf_values(c))
                if op == "DOES_NOT_CONTAIN":
                    return bool(O.present(v)) and not O.leaf_matches({"_": v}, dict(c, field="_", operation="CONTAINS_ANY"))
        if isinstance(v, str) and (len(v) > O.LONG_TEXT or (v and not any(ch.isalnum() for ch in v))):
            return self._measured_quirk(v, op, c)
        r = O.leaf_matches({"_": v}, dict(c, field="_"))
        if r is None:
            raise Reject(422, {"detail": "unsupported operation %s" % op})
        return r

    @staticmethod
    def _plain(v, op, c):
        """The operator's plain meaning on one value, ignoring any rule about empty values."""
        twin = O.NEGATIVE.get(op)
        p = O._positive(twin or op, [v], c)
        return None if p is None else (not p if twin else p)

    def _measured_quirk(self, v, op, c):
        """How a production tenant answered, measured value by value (2026-10-31), written out here
        independently of abx_ops so the tool's model can be wrong against it."""
        vals = O.leaf_values(c)
        alnum = all(x.isalnum() for x in vals)
        if len(v) > O.LONG_TEXT:                   # 16 long url values
            if op in ("IS_EMPTY", "IS_NOT_EMPTY"):
                return op == "IS_NOT_EMPTY"
            if op in ("CONTAINS", "DOES_NOT_CONTAIN", "CONTAINS_WORD") and alnum:
                return self._plain(v, op, c)
            if op in ("IS_IN_LIST", "IS_NOT_IN_LIST") and all(len(x) <= O.LONG_TEXT for x in vals):
                return self._plain(v, op, c)
            return False                           # starts / ends with, patterns, contains with punctuation: missed
        # 15 values of exactly "/"
        if op in ("IS_EMPTY", "IS_NOT_EMPTY"):
            return op == "IS_EMPTY"
        if op == "IS_IN_LIST":
            return False if c.get("case_sensitive") else bool(self._plain(v, op, c))
        if op in ("IS_NOT_IN_LIST", "DOES_NOT_CONTAIN_WORD"):
            return False
        if op == "DOES_NOT_CONTAIN":
            return False if alnum else bool(self._plain(v, op, c))
        return bool(self._plain(v, op, c))         # contains / starts / ends / regex and their "not": as a value

    def _check_types(self, cond):
        """The platform validates a condition's field types up front (a 422 even when nothing is in the window)."""
        if not isinstance(cond, dict):
            return
        for c in cond.get("conditions") or []:
            f, ftype = c.get("field"), c.get("field_type") or "String"
            want = self.types.get(f, "String")
            if ftype != want:
                raise Reject(422, {"detail": "Invalid field type for field '%s'. Expected '%s'" % (f, want)})
        for g in cond.get("groups") or []:
            self._check_types(g)

    def _cond(self, row, cond):
        if not cond:
            return True
        op = str(cond.get("operator") or "and").lower()
        res = [self._leaf(row, c) for c in cond.get("conditions") or []]
        res += [self._cond(row, g) for g in cond.get("groups") or []]
        if not res:
            return True
        return all(res) if op == "and" else any(res)

    _KQL_TERM = re.compile(r'^\s*\[?\'?([\w.@]+)\'?\]?\s*(==|in|contains)\s*(.+?)\s*$', re.S)

    def _kql(self, row, kql):
        """A small KQL subset: `events | where` terms joined by and / or; ==, in (…) and contains."""
        if self.ignore_kql:
            return True
        m = re.match(r"^\s*events\s*(?:\|\s*where\s+(.*))?$", kql or "", re.S)
        if not m:
            raise Reject(400, {"detail": "could not parse KQL"})
        if not m.group(1):
            return True
        try:
            return any(all(self._kql_term(row, part) for part in re.split(r"\s+and\s+", alt))
                       for alt in re.split(r"\s+or\s+", m.group(1)))
        except ValueError:
            raise Reject(400, {"detail": "unsupported KQL"})

    def _kql_term(self, row, part):
        t = self._KQL_TERM.match(part)
        if not t:
            raise Reject(400, {"detail": "unsupported KQL %r" % part})
        f, op, rhs = t.groups()
        v = dig(row, f)
        if op == "contains":
            return v is not None and json.loads(rhs).casefold() in str(v).casefold()
        vals = [json.loads(rhs)] if op == "==" else json.loads("[" + rhs.strip()[1:-1] + "]")
        return v is not None and str(v).casefold() in {str(x).casefold() for x in vals}

    def _dsl(self, row, q):
        if not q or "match_all" in q:
            return True
        if "bool" in q:
            b = q["bool"]
            if not all(self._dsl(row, x) for x in b.get("must") or []):
                return False
            if not all(self._dsl(row, x) for x in b.get("filter") or []):
                return False
            if any(self._dsl(row, x) for x in b.get("must_not") or []):
                return False
            sh = b.get("should") or []
            if sh and sum(1 for x in sh if self._dsl(row, x)) < int(b.get("minimum_should_match", 1)):
                return False
            return True
        for k in ("match_phrase", "term", "match"):
            if k in q:
                (f, want), = q[k].items()
                if isinstance(want, dict):
                    want = want.get("query", want.get("value"))
                if self._ignored(f):
                    return True
                v = dig(row, f)
                return v is not None and str(v).casefold() == str(want).casefold()
        if "exists" in q:                          # the keyword index: a value longer than LONG_TEXT is not in it
            v = dig(row, q["exists"]["field"])
            return v not in (None, "", [], {}) and not (isinstance(v, str) and len(v) > O.LONG_TEXT)
        if "prefix" in q:                          # the text index: any word of the value starting with the prefix
            (f, want), = q["prefix"].items()
            v = dig(row, f)
            return v is not None and any(w.startswith(str(want).lower())
                                         for w in re.findall(r"[a-z0-9]+", str(v).lower()))
        if "terms" in q:
            (f, wants), = q["terms"].items()
            v = dig(row, f)
            return v is not None and str(v).casefold() in {str(x).casefold() for x in wants}
        if "range" in q:
            (f, r), = q["range"].items()
            t = parse(dig(row, f)) if f == "@timestamp" else dig(row, f)
            for op, lim in r.items():
                lim = parse(lim) if f == "@timestamp" else lim
                if (op == "gte" and not t >= lim) or (op == "gt" and not t > lim) or \
                        (op == "lt" and not t < lim) or (op == "lte" and not t <= lim):
                    return False
            return True
        raise Reject(400, {"detail": "unsupported DSL %s" % list(q)})

    # ── storage views ──
    def _window(self, body, storage):
        a, b = floor_sec(parse(body["start_time"])), floor_sec(parse(body["end_time"]))
        out = []
        for r in self.rows:
            t = parse(r["@timestamp"])
            if not (a <= floor_sec(t) < b):
                continue
            if storage == "hot" and self.hot_since is not None and t < self.hot_since:
                continue
            if storage == "hot" and self.hot_days is not None and t < self._hot_floor():
                continue
            out.append(r)
        return sorted(out, key=lambda r: (parse(r["@timestamp"]), r["_k"]), reverse=True)

    clock = staticmethod(lambda: dt.datetime.now(dt.timezone.utc))   # support.EngineCase sets the tests' clock

    def _hot_floor(self):
        n = self.clock()
        return n.replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=self.hot_days)

    # ── archive jobs ──
    def vendor_account(self, method, body):
        if not self.vendor_readable:
            raise Reject(403, {"detail": "Not enough permissions"})
        return 200, {"nanoid": self.vid, "storage_retention": {"hot": self.hot_days or 3650, "warm": 0,
                                                               "cold": self.cold_days}}

    def jobs_list(self, method, body):
        if not self.jobs_enabled:
            raise Reject(404, {"detail": "Not Found"})
        return 200, {"total_count": len(self.jobs), "items": list(self.jobs.values())[:1]}

    def job_submit(self, method, body):
        if not self.jobs_enabled:
            raise Reject(404, {"detail": "Not Found"})
        qid = "job%04d" % (len(self.jobs) + 1)
        final = "COMPLETED"
        if self.job_failed_files > 0:
            self.job_failed_files -= 1
            final = "FAILED_FILES"
        elif self.job_pause > 0:
            self.job_pause -= 1
            final = "PAUSED"
        self.jobs[qid] = {"nanoid": qid, "start": body["start_time"], "end": body["end_time"],
                          "condition": body.get("condition"), "polls": 0, "final": final, "cancelled": False}
        return 200, {"nanoid": qid}

    def job_get(self, qid):
        j = self.jobs.get(qid)
        if j is None:
            raise Reject(404, {"detail": "Not Found"})
        if self.job_forget > 0 and not j.get("forgot"):
            self.job_forget -= 1
            j["forgot"] = True
            del self.jobs[qid]
            raise Reject(404, {"detail": "Not Found"})
        j["polls"] += 1
        if j["cancelled"]:
            return 200, {"nanoid": qid, "status": "CANCELLED", "total_files": 4, "completed_count": 1, "failure_count": 0}
        if j["polls"] <= self.job_polls:
            return 200, {"nanoid": qid, "status": "IN_PROGRESS", "total_files": 4, "completed_count": 1, "failure_count": 0}
        if j["final"] == "FAILED_FILES":
            return 200, {"nanoid": qid, "status": "COMPLETED", "total_files": 4, "completed_count": 3, "failure_count": 1,
                         "message": "query completed with failures: failed processing 1 of 4 files"}
        if j["final"] == "PAUSED":
            return 200, {"nanoid": qid, "status": "PAUSED", "total_files": 4, "completed_count": 2, "failure_count": 0}
        return 200, {"nanoid": qid, "status": "COMPLETED", "total_files": 4, "completed_count": 4, "failure_count": 0,
                     "query_metadata": j.get("condition"), "unstructured_query": None, "start_time": j["start"],
                     "end_time": j["end"]}

    def job_events(self, qid, body):
        j = self.jobs.get(qid)
        if j is None:
            raise Reject(404, {"detail": "Not Found"})
        self._check_fields(body.get("selected_fields"))
        self._archive = True                       # archive semantics while the job's rows are chosen
        try:
            rows = [r for r in self._window({"start_time": j["start"], "end_time": j["end"]}, "cold")
                    if self.job_ignore_filter or self._cond(r, j["condition"])]
        finally:
            self._archive = False
        total = len(rows)
        sa = body.get("search_after")
        if sa:
            rows = [r for r in rows if self._sort(r) < list(sa)]
        page = rows[:int(body.get("page_size") or 10)]
        return 200, {"total_count": total, "items": [project(self._public(r), body.get("selected_fields")) for r in page],
                     "last_sort": self._sort(page[-1]) if page else None}

    def job_cancel(self, qid):
        if qid in self.jobs:
            self.jobs[qid]["cancelled"] = True
        return 200, {"nanoid": qid}

    def dispatch_dynamic(self, method, path, body):
        m = re.fullmatch(r"/v1/coldstorage/query/([A-Za-z0-9_-]+)(/events|/cancel)?", path)
        if m and m.group(1) != "submit":
            qid, tail = m.group(1), m.group(2)
            if method == "GET" and not tail:
                return self.job_get(qid)
            if method == "POST" and tail == "/events":
                return self.job_events(qid, body or {})
            if method == "POST" and tail == "/cancel":
                return self.job_cancel(qid)
        m = re.fullmatch(r"/v1/streamviewer/field-set/([A-Za-z0-9_-]+)", path)
        if m:
            f = self.fieldsets.get(m.group(1))
            if f is None:
                raise Reject(404, {"detail": "Fieldset not found"})
            if method == "GET":
                return 200, dict(f)
            if method == "PATCH":
                f.update(body or {})
                self._ids += 1
                f["updated_at"] = "2026-11-20T01:00:%02dZ" % (self._ids % 60)
                return 200, dict(f)
            if method == "DELETE":
                del self.fieldsets[m.group(1)]
                return 200, {"success": True}
        m = re.fullmatch(r"/v1/streamviewer/view/([A-Za-z0-9_-]+)", path)
        if m and method in ("PATCH", "DELETE"):
            v = next((x for x in self.views if x["id"] == m.group(1)), None)
            if v is None:
                raise Reject(404, {"detail": "View not found"})
            if method == "DELETE":
                self.views.remove(v)
                return 200, {"success": True}
            if (body or {}).get("order_by", "@timestamp") is None:      # the platform refuses a null order_by
                raise Reject(422, {"detail": [{"msg": "Input should be '*', 'id', … (order_by)"}]})
            v.update(body or {})
            v["updated_at"] = "2026-11-20T02:00:00Z"
            return 200, dict(v)
        if m and method == "GET":
            v = next((x for x in self.views if x["id"] == m.group(1)), None)
            if v is None:
                raise Reject(404, {"detail": "View not found"})
            return 200, dict({"query": None, "fields": [], "time_selection": None, "kql_query": None,
                              "unstructured_query": None, "storage_type": "HOT", "source_ids": ["src-default"],
                              "order_by": "@timestamp", "order_type": "DESC"}, **v)
        m = re.fullmatch(r"/v1/vendor-accounts/([A-Za-z0-9_-]+)", path)
        if m and method == "GET":
            if m.group(1) != self.vid:
                raise Reject(403, {"detail": "forbidden"})
            return self.vendor_account(method, body)
        return None

    @staticmethod
    def _sort(r):
        t = parse(r["@timestamp"])
        return [int(t.timestamp() * 1000), r["_k"]]

    @staticmethod
    def _public(r):
        out = {k: v for k, v in r.items() if not k.startswith("_")}
        if "_ts_out" in r:
            out["@timestamp"] = r["_ts_out"]        # how the platform returns this row's @timestamp (epoch, junk…)
        return out

    def _check_fields(self, fields):
        # ext.* sub-fields are accepted when "ext" is selectable (measured on a production tenant, 2026-10-28)
        bad = [f for f in fields or [] if f != "*" and f not in self.enum
               and not (f.startswith("ext.") and "ext" in self.enum)]
        if bad:
            expected = ", ".join("'%s'" % f for f in self.enum + ["*"])
            raise Reject(422, {"detail": [{"type": "literal_error", "loc": ["body", "selected_fields", 1],
                                           "msg": "Input should be " + expected, "input": bad[0],
                                           "ctx": {"expected": expected}}]})

    # ── endpoints ──
    def auth(self, method, body):
        return 200, {"current_organization": {"name": self.name, "current_vendor_account_id": self.vid}}

    def v1_search(self, method, body):
        self._check_fields(body.get("selected_fields"))
        if body.get("kql_query"):
            raise Reject(422, {"detail": "kql_query is not accepted here"})
        width = floor_sec(parse(body["end_time"])) - floor_sec(parse(body["start_time"]))
        if self.v1_500_wider_than is not None and width > self.v1_500_wider_than:
            raise Reject(500, {"message": self.v1_500_message})
        if self.v1_error_body > 0:
            self.v1_error_body -= 1
            return 200, {"message": "internal error while searching"}
        self._check_types(body.get("condition"))
        rows = [r for r in self._window(body, "hot") if self._cond(r, body.get("condition"))]
        total = len(rows)
        if total == 0 and self.v1_500_on_zero:
            raise Reject(500, self.v1_500_body if self.v1_500_body is not None else {"message": self.v1_500_message})
        sa = body.get("search_after")
        if sa:
            rows = [r for r in rows if self._sort(r) < list(sa)]
        page = rows[:int(body.get("page_size") or 10)]
        return 200, {"events": [project(self._public(r), body.get("selected_fields")) for r in page],
                     "metadata": {"total_events": total, "page_size": body.get("page_size"),
                                  "last_sort": self._sort(page[-1]) if page else None}}

    def dataview(self, method, body):
        self._check_fields(body.get("selected_fields"))
        rows = self._window(body, "hot")
        kq = body.get("kql_query")
        m = re.match(r"^(.*?)\|\s*summarize\s+(\w+)\s*=\s*count\(\)\s+by\s+([\w.]+)\s*$", kq or "", re.S)
        if m and not self.ignore_kql:              # a result table: one row per group, like the platform's
            rows = [r for r in rows if self._kql(r, m.group(1).strip())]
            counts = {}
            for r in rows:
                k = dig(r, m.group(3))
                counts[k] = counts.get(k, 0) + 1
            out_rows = []
            for k, n in sorted(counts.items(), key=lambda x: -x[1]):
                row = {}
                cur = row
                parts = m.group(3).split(".")
                for part in parts[:-1]:
                    cur = cur.setdefault(part, {})
                cur[parts[-1]] = k
                row[m.group(2)] = n
                out_rows.append(row)
            out = ["event: init", "data: {}", "", "event: data", "data: " + json.dumps({"events": out_rows}), "",
                   "event: done", "data: " + json.dumps({"total_events": len(out_rows), "partial": False}), ""]
            return 200, "\n".join(out) + "\n", "text/event-stream"
        if kq is not None:
            rows = [r for r in rows if self._kql(r, kq)]
        else:
            rows = [r for r in rows if self._cond(r, body.get("condition"))]
        total = len(rows)
        sa = body.get("search_after")
        if sa:
            rows = [r for r in rows if self._sort(r) < list(sa)]
        page = rows[:int(body.get("limit") or 10)]
        out = ["event: init", "data: {}", ""]
        for i in range(0, len(page), 1000):
            chunk = page[i:i + 1000]
            out += ["event: data", "data: " + json.dumps({
                "events": [project(self._public(r), body.get("selected_fields")) for r in chunk],
                "last_sort": self._sort(chunk[-1]), "total_count": total}), ""]
        done = {"total_count": total, "partial": False} if total else {"total_events": 0, "partial": False}
        out += ["event: done", "data: " + json.dumps(done), ""]
        return 200, "\n".join(out) + "\n", "text/event-stream"

    def raw(self, method, body):
        if not self.raw_enabled:
            return 200, {"message": "this endpoint is not enabled for this tenant"}
        rows = [r for r in self._window(body, "hot") if self._dsl(r, body.get("query"))]
        res = {"hits": [self._public(r) for r in rows[int(body.get("from") or 0):][:int(body.get("size") or 0)]],
               "total": {"value": len(rows) + self.raw_extra, "relation": "eq"}, "timed_out": False}
        aggs = body.get("aggs") or {}
        if aggs:
            res["aggregations"] = {}
            for name, a in aggs.items():
                f = a["terms"]["field"]
                counts = {}
                for r in rows:
                    counts[dig(r, f)] = counts.get(dig(r, f), 0) + 1
                res["aggregations"][name] = {"buckets": [{"key": k, "doc_count": n} for k, n in
                                                         sorted(counts.items(), key=lambda x: -x[1])]}
        return 200, res

    def cold(self, method, body):
        if self.cold_broken:
            raise Reject(500, {"message": "cold storage is unavailable"})
        if self.cold_filter_broken and ((body.get("condition") or {}).get("conditions") or
                                        (body.get("condition") or {}).get("groups")):
            raise Reject(422, {"detail": [{"loc": ["body", "quickwit_search_url"], "type": "value_error",
                                           "msg": "quickwit_search_url must point to a configured Quickwit host."}]})
        size, page = int(body.get("page_size") or 10), int(body.get("page_number") or 1)
        if size > 1000 or (page - 1) * size >= 10000:
            raise Reject(400, {"detail": "page_size must be <= 1000 and offsets <= 10000"})
        rows = [r for r in self._window(body, "cold") if self._cond(r, body.get("condition"))]
        items = rows[(page - 1) * size:page * size]
        return 200, {"items": [project(self._public(r), body.get("selected_fields")) for r in items],
                     "total_count": len(rows)}

    def configurations(self, method, body):
        names = sorted({dig(r, "data_stream.dataset") for r in self.rows} | set(self.extra_datasets))
        data = [{"id": "cfg-%d" % i, "name": n, "type": "Event Source", "active": True} for i, n in enumerate(names)]
        if self.config_page_cap:
            size = min(int(self.query.get("page_size", 100)), self.config_page_cap)
            page = int(self.query.get("page_number", 1))
            data = data[(page - 1) * size:page * size]
        return 200, {"data": data}

    def kql_validate_ep(self, method, body):
        if self.kql_violations:
            return 200, {"valid": False, "violations": list(self.kql_violations)}
        if not self.kql_validate:
            raise Reject(404, {"detail": "Not Found"})
        return 200, {"valid": True, "violations": []}

    def to_conditions_ep(self, method, body):
        if not self.to_conditions_enabled:
            raise Reject(404, {"detail": "Not Found"})
        k = body.get("kql_query") or ""
        if re.search(r"\|\s*(summarize|extend|project|take|top)\b", k):
            stage = re.search(r"\|\s*(summarize|extend|project|take|top)\b", k).group(1)
            return 200, {"kql_query": k, "condition": None, "refusal": {
                "code": "pipeline_stage", "construct": stage,
                "message": "The condition builder holds only the filter; the '%s' stage would be lost." % stage}}
        return 200, {"kql_query": k, "condition": {"operator": "and", "conditions": [], "groups": []},
                     "refusal": None}

    def from_conditions_ep(self, method, body):
        # the fake has no condition-to-KQL translator of its own: it answers the way the platform does for a
        # condition with no exact KQL, so a saved view keeps only its filter-builder query
        return 200, {"kql_query": None, "refusal": {"code": "no_exact_kql", "construct": None,
                                                    "message": "this condition has no exact KQL"}}

    def ase_kql_ep(self, method, body):
        if not self.nl_enabled:
            raise Reject(404, {"detail": "Not Found"})
        a = self.nl_answers.get(body.get("query"))
        if a is None:
            return 200, {"kql": "", "explanation": "I could not turn that into a filter."}
        return 200, a

    def suggest_ep(self, method, body):
        vals = list(self.suggest.get(body.get("field"), []))
        pre = (body.get("prefix") or "").casefold()
        return 200, {"field": body.get("field"), "values": [v for v in vals if str(v).casefold().startswith(pre)]
                     [:int(body.get("limit") or 10)], "available": True, "unavailable_reason": None, "message": None}

    def _new_id(self, kind):
        self._ids += 1
        return "%s%06d" % (kind, self._ids)

    def fieldsets_ep(self, method, body):
        size, page = int(self.query.get("page_size", 20)), int(self.query.get("page_number", 1))
        fs = list(self.fieldsets.values())
        return 200, {"fieldsets": fs[(page - 1) * size:page * size]}

    def fieldset_create(self, method, body):
        bad = [f for f in body.get("fields") or [] if f not in self.enum and not re.match(r"^(ext\.|event\.parsed\.).+", f)]
        if bad or not body.get("name"):
            raise Reject(422, {"detail": [{"msg": "invalid field %s" % (bad[:1] or ["name"])[0]}]})
        fid = self._new_id("fs")
        now = "2026-11-20T00:00:%02dZ" % (self._ids % 60)
        self.fieldsets[fid] = dict({"id": fid, "description": None, "tags": None, "share_level": "private",
                                    "created_at": now, "updated_at": now, "created_by": "tester@example.com",
                                    "updated_by": None}, **body)
        return 201, self.fieldsets[fid]

    def views_create(self, method, body):
        if not body.get("name"):
            raise Reject(422, {"detail": [{"msg": "name is required"}]})
        vid = self._new_id("vw")
        v = dict({"id": vid, "created_at": "2026-11-20T00:00:00Z", "updated_at": "2026-11-20T00:00:00Z"}, **body)
        ts = v.get("time_selection")
        if isinstance(ts, dict):                       # like the platform: the unused parts come back as null
            v["time_selection"] = dict({"start_date": None, "end_date": None, "numeric_value": None,
                                        "option_selected": None, "end_numeric_value": None,
                                        "end_option_selected": None}, **ts)
        v.setdefault("kql_verdict", {"status": "accepted", "message": None})
        self.views.append(v)
        return 201, v

    def flags_ep(self, method, body):
        return 200, dict(self.flags)

    def views_ep(self, method, body):
        size, page = int(self.query.get("page_size", 20)), int(self.query.get("page_number", 1))
        # like the platform, the list leaves out a view's filter-builder conditions, columns and time range
        vs = [dict({"created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z", "queries": [],
                    "storage_type": "HOT", "source_ids": ["src-default"], "last_executed_query": None},
                   **{k: x for k, x in v.items() if k not in ("query", "fields", "time_selection")})
              for v in self.views]
        return 200, {"views": vs[(page - 1) * size:page * size], "orphan_queries": [], "metadata": {}}

    ROUTES = {("GET", "/v2/auth/"): "auth", ("POST", "/v1/streamviewer/search"): "v1_search",
              ("POST", "/v1/dataview/search"): "dataview", ("POST", "/v2/streamviewer/raw-search"): "raw",
              ("POST", "/v1/coldstorage/v2/search"): "cold", ("GET", "/v1/configurations/"): "configurations",
              ("POST", "/v1/kql/validate"): "kql_validate_ep", ("GET", "/v1/streamviewer/views"): "views_ep",
              ("GET", "/v1/feature-flags/flags"): "flags_ep", ("GET", "/v1/coldstorage/query"): "jobs_list",
              ("POST", "/v1/coldstorage/query/submit"): "job_submit",
              ("POST", "/v1/kql/to-conditions"): "to_conditions_ep",
              ("POST", "/v1/kql/from-conditions"): "from_conditions_ep", ("POST", "/v1/ase/kql"): "ase_kql_ep",
              ("POST", "/v1/streamviewer/field-suggestions"): "suggest_ep",
              ("GET", "/v1/streamviewer/field-sets"): "fieldsets_ep",
              ("POST", "/v1/streamviewer/field-set"): "fieldset_create", ("POST", "/v1/streamviewer/view"): "views_create"}
    SEARCHES = {"/v1/streamviewer/search", "/v1/dataview/search", "/v2/streamviewer/raw-search",
                "/v1/coldstorage/v2/search"}

    def handle(self, method, path, headers, body):
        """-> (status, payload, content_type, extra_headers)."""
        self.calls.append((method, path, body))
        if self.on_request:
            try:
                self.on_request(self, method, path, body)
            except Reject as ex:                       # a hook may refuse the request like the platform would
                return ex.code, ex.payload, "application/json", ex.headers
        if not str(headers.get("Authorization", "")).startswith("Bearer "):
            return 401, {"detail": "Not authenticated"}, "application/json", {}
        if headers.get("X-as-vendor-account-id") not in (None, self.vid):
            return 403, {"detail": "forbidden for this vendor account"}, "application/json", {}
        if path in self.SEARCHES and self.rate_limit_429 > 0:
            self.rate_limit_429 -= 1
            return 429, {"detail": "Too Many Requests"}, "application/json", {"Retry-After": self.retry_after}
        if self.fail_first_502 and not self._first_done:
            self._first_done = True
            return 502, "<html>Bad Gateway</html>", "text/html", {}
        name = self.ROUTES.get((method, path))
        if name is None:
            try:
                r = self.dispatch_dynamic(method, path, body)
            except Reject as ex:
                return ex.code, ex.payload, "application/json", ex.headers
            if r is not None:
                return r[0], r[1], "application/json", {}
            self.unhandled.append((method, path))
            return 404, {"detail": "Not Found"}, "application/json", {}
        try:
            r = getattr(self, name)(method, body)
        except Reject as ex:
            return ex.code, ex.payload, "application/json", ex.headers
        code, payload = r[0], r[1]
        ctype = r[2] if len(r) > 2 else "application/json"
        return code, payload, ctype, {}


class _Resp:
    def __init__(self, status, data, headers):
        self.status, self._data, self.headers = status, data, headers

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeTransport:
    """Stands in for abx_engine._OPENER: .open(req, timeout) → a response, or raises HTTPError like urllib."""

    def __init__(self, tenant):
        self.tenant = tenant

    def open(self, req, timeout=None):
        u = urllib.parse.urlparse(req.full_url)
        path = u.path + (("?" + u.query) if u.query else "")
        route = u.path
        self.tenant.query = dict(urllib.parse.parse_qsl(u.query))
        body = json.loads(req.data.decode()) if req.data else None
        code, payload, ctype, extra = self.tenant.handle(req.get_method(), route, dict(req.header_items()), body)
        data = payload if isinstance(payload, (bytes, bytearray)) else \
            (payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())
        hdrs = email.message.Message()
        hdrs["Content-Type"] = ctype
        for k, v in extra.items():
            hdrs[k] = v
        if code >= 400:
            raise urllib.error.HTTPError(req.full_url, code, "fake %d" % code, hdrs, io.BytesIO(data))
        if self.tenant.bad_utf8 > 0 and route == "/v1/streamviewer/search" and b'"message": "' in data:
            self.tenant.bad_utf8 -= 1
            data = data.replace(b'"message": "', b'"message": "\xff', 1)     # decodes with "replace" as U+FFFD
        del path
        return _Resp(code, data, hdrs)
