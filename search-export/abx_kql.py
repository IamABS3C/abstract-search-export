"""
abx_kql — convert a search copied from Abstract (the search box or a saved view) into this tool's own search.

The text is KQL, e.g.
    events | where ['data_stream.dataset'] == "crm - audit events" and user_name == "x" | sort by ['@timestamp'] desc

KQL is NEVER sent to the search API: measured on production tenants, the API silently ignores it and returns every
event. Instead the query is converted, clause by clause, into a dataset, filter rows and a time range, which the
API does apply and which the tool proves before it exports. Anything that cannot be converted faithfully is
refused by name; nothing is dropped or widened silently.

Supported (everything else is refused, by name):
    | where …                          several where stages are ANDed
    and / or / brackets / not(…)       any nesting: not(…) flips each operator (De Morgan), and the result is put
                                       in "every filter, and at least one of each group" form (CNF), which matches
                                       exactly the same events; more than MAX_GROUPS groups is refused
    == =~ != !~ in in~ !in !in~        "is one of" / "is not one of" (== / in exact case, ~ any case)
    contains has startswith endswith   and their ! and _cs forms; hasprefix / hassuffix as a pattern
    matches regex  has_any  has_all
    > >= < <=  between  !between       numbers
    isnotempty isnotnull isempty isnull, ipv4_is_in_range(f, "10.0.0.0/8")
    field == true / false              true/false fields
    ['@timestamp'] > / >= / < / <= datetime(…)   sets the time range
    | sort by … / | order by …          ignored (the export is complete either way)
    | project a, b, c                  becomes the field list
Refused, with the reason: other functions, has on a list field, an empty or space-padded value, a comma inside an
in (…) value, other date fields, | take / | limit (a row limit changes "export everything"), | summarize and any
other stage.
"""
from __future__ import annotations

import datetime as dt
import re

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<br>\[\s*(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')\s*\])
  | (?P<op>==|=~|!=|!~|>=|<=|!in~|!in(?![\w])|in~|\.\.|>|<|\(|\)|,|\|)
  | (?P<ip>\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?(?![\w.]))
  | (?P<num>-?\d+(?:\.\d+)?(?!\w)(?!\.\d))
  | (?P<nid>![A-Za-z_]\w*)
  | (?P<id>[A-Za-z_@][\w.@\-]*)
""", re.X)


class ConvertError(ValueError):
    """The pasted search contains something this tool cannot run exactly as written."""


def _unquote(s):
    q, body = s[0], s[1:-1]
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"', "'": "'"}.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_DATETIME = re.compile(r"datetime\s*\(\s*([^)]*?)\s*\)", re.I)


def tokenize(text):
    toks, pos = [], 0
    while pos < len(text):
        d = _DATETIME.match(text, pos)                 # datetime(2026-09-01T12:00:00Z), quoted or not
        if d:
            toks.append(("dt", d.group(1).strip().strip('"').strip("'")))
            pos = d.end()
            continue
        m = _TOKEN.match(text, pos)
        if not m:
            raise ConvertError(f"cannot read the search near: {text[pos:pos + 30]!r}")
        pos = m.end()
        kind = m.lastgroup
        assert kind is not None                          # every alternative of _TOKEN is a named group
        val = m.group(kind)
        if kind == "ws":
            continue
        if kind == "br":
            inner = val.strip()[1:-1].strip()
            toks.append(("field", _unquote(inner)))
        elif kind == "str":
            toks.append(("str", _unquote(val)))
        elif kind == "num":
            toks.append(("num", val))
        elif kind == "ip":
            toks.append(("ip", val))
        elif kind == "nid":
            toks.append(("id", val))               # !contains, !has, !startswith, !endswith, !between …
        elif kind == "id":
            low = val.lower()
            if low in ("in", "in~"):
                toks.append(("op", low))
            else:
                toks.append(("id", val))
        else:
            toks.append(("op", val))
    return toks


_BARE_KEEP = re.compile(r"^(?:-?\d+(?:\.\d+)?|\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?|true|false)$", re.I)
_BARE_ONE = re.compile(r"""(==|!=|=~|!~|>=|<=|>|<|(?<![\w!])!?(?:contains|has|startswith|endswith|hasprefix|hassuffix)(?:_cs)?(?!\w)
                          |\bmatches\s+regex)(\s+)([^\s"'(),|\x00]+)""", re.I | re.X)
_QUOTED = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_BARE_LIST = re.compile(r"""((?<![\w.])(?:!?in~?|has_any|has_all)\s*\()([^()]*)(\))""", re.I)


def _quote(v):
    return v if _BARE_KEEP.match(v) else '"' + v.replace("\\", "\\\\") + '"'


def quote_bare(text):
    """--where only: put quotes around unquoted values (user_name !contains example.com), so a condition needs no
    inner quotes on any shell. Numbers, IPv4 addresses, CIDRs and true/false stay as they are. Quoted values are set
    aside first, so nothing inside quotes is touched."""
    held = []

    def hold(m):
        held.append(m.group(0))
        return f"\x00{len(held) - 1}\x00"
    text = _QUOTED.sub(hold, text)
    text = _BARE_ONE.sub(lambda m: m.group(1) + m.group(2) + _quote(m.group(3)), text)

    def items(m):
        parts = [p.strip() for p in m.group(2).split(",")]
        return m.group(1) + ", ".join(p if (not p or p[0] == "\x00") else _quote(p) for p in parts) + m.group(3)
    text = _BARE_LIST.sub(items, text)
    return re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], text)


def _split_pipes(toks):
    stages, cur, depth = [], [], 0
    for t in toks:
        if t == ("op", "("):
            depth += 1
        elif t == ("op", ")"):
            depth -= 1
        if t == ("op", "|") and depth == 0:
            stages.append(cur)
            cur = []
        else:
            cur.append(t)
    stages.append(cur)
    return stages


def _iso_utc(text):
    t = text.strip().strip('"').strip("'")
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?$", t)
    if not m:
        raise ConvertError(f"cannot read the time {text!r} (use datetime(2026-09-01T12:00:00Z))")
    day, clock, tz = m.group(1), m.group(2) or "00:00:00", m.group(3) or "Z"
    if len(clock) == 5:
        clock += ":00"
    try:
        d = dt.datetime.fromisoformat(f"{day}T{clock}")
        if tz != "Z":
            sign = 1 if tz[0] == "+" else -1
            hh, mm = int(tz[1:3]), int(tz[-2:])
            d = d - sign * dt.timedelta(hours=hh, minutes=mm)
    except (ValueError, OverflowError):
        raise ConvertError(f"{text!r} is not a real date and time")
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


# KQL text operators (the _cs suffix = exact case) -> the platform operator that does the same, measured exact
TEXT_WORDS = {"contains": "CONTAINS", "!contains": "DOES_NOT_CONTAIN", "has": "CONTAINS_WORD",
              "!has": "DOES_NOT_CONTAIN_WORD", "startswith": "STARTS_WITH", "!startswith": "DOES_NOT_START_WITH",
              "endswith": "ENDS_WITH", "!endswith": "DOES_NOT_END_WITH"}


class _Where:
    """Parses one where-expression into clause dicts: {field, op, values} / {field, fn} / time bounds."""

    def __init__(self, toks, bare=False):
        self.t, self.i, self.bare = toks, 0, bare        # bare: an unquoted word is a value (--where only)

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else (None, "")

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def expect(self, kind, val=None):
        k, v = self.take()
        if k != kind or (val is not None and v != val):
            raise ConvertError(f"expected {val or kind} but found {v!r}")
        return v

    def parse(self):
        """AND items; a top-level `or` becomes one {"or": [alternative, …]} item (and binds tighter than or)."""
        alts = [[self.term()]]
        while self.i < len(self.t):
            k, v = self.take()
            if k == "id" and v.lower() == "and":
                alts[-1].append(self.term())
            elif k == "id" and v.lower() == "or":
                alts.append([self.term()])
            else:
                raise ConvertError(f"cannot read the search near {v!r}")
        alts = [[x for it in a for x in (it["and"] if "and" in it else [it])] for a in alts]
        return alts[0] if len(alts) == 1 else [{"or": alts}]

    def field(self):
        k, v = self.take()
        if k in ("field", "id"):
            return v
        raise ConvertError(f"expected a field name but found {v!r}")

    def value(self):
        k, v = self.take()
        if k == "str":
            return v
        if k == "num":
            return float(v) if "." in v else int(v)
        if k == "id" and v.lower() in ("true", "false"):
            return v.lower() == "true"
        if k == "dt":
            return ("datetime", v)
        if self.bare and k == "id" and v.lower() not in ("and", "or", "not", "datetime"):
            return v
        if k == "ip":
            if self.bare:
                return v
            raise ConvertError(f"put the address {v} in quotes")
        if k == "id" and v.lower() == "datetime":
            self.expect("op", "(")
            parts = []
            while self.peek() != ("op", ")"):
                kk, vv = self.take()
                if kk is None:
                    raise ConvertError("datetime( is not closed")
                parts.append(vv)
            self.expect("op", ")")
            return ("datetime", "".join(str(p) for p in parts))
        raise ConvertError(f"expected a value but found {v!r}")

    def term(self):
        k, v = self.peek()
        if k == "id" and v.lower() == "not" and self.peek(1) == ("op", "("):
            self.take()
            sub = self.term()                   # the bracket that follows
            return {"not": sub}
        if k == "op" and v == "(":
            self.take()
            inner = []
            depth = 1
            while depth:
                kk, vv = self.take()
                if kk is None:
                    raise ConvertError("a '(' is not closed")
                if (kk, vv) == ("op", "("):
                    depth += 1
                elif (kk, vv) == ("op", ")"):
                    depth -= 1
                    if not depth:
                        break
                inner.append((kk, vv))
            sub = _Where(inner, self.bare).parse()
            return sub[0] if len(sub) == 1 else {"and": sub}
        if k == "id" and self.peek(1) == ("op", "("):
            fn = v.lower()
            self.take()
            self.take()
            f = self.field()
            if fn in ("isnotempty", "isnotnull", "isempty", "isnull"):
                self.expect("op", ")")
                return {"field": f, "fn": "IS_NOT_EMPTY" if fn in ("isnotempty", "isnotnull") else "IS_EMPTY"}
            if fn == "ipv4_is_in_range":
                self.expect("op", ",")
                cidr = self.value()
                self.expect("op", ")")
                return {"field": f, "fn": "IS_IN_SUBNET", "values": [str(cidr)]}
            raise ConvertError(f"the function {v}() is not supported")
        f = self.field()
        k, op = self.take()
        if k == "id":
            low = op.lower()
            cs = low.endswith("_cs")
            base = low[:-3] if cs else low
            if base in TEXT_WORDS:
                return {"field": f, "op": TEXT_WORDS[base], "values": [self.value()], "cs": cs}
            if base in ("hasprefix", "!hasprefix", "hassuffix", "!hassuffix"):
                # a word starting (ending) with v: the whole value starts (ends) with it, or it follows (precedes)
                # a character that is not a letter or digit. Measured: Abstract's search refuses ^ / $ inside a group
                v = str(self.value())
                pre = "prefix" in base
                edge = {"field": f, "op": "STARTS_WITH" if pre else "ENDS_WITH", "values": [v], "cs": cs}
                inner = {"field": f, "op": "MATCHES_REGEX", "cs": cs,
                         "values": [("[^A-Za-z0-9]" + re.escape(v)) if pre else (re.escape(v) + "[^A-Za-z0-9]")]}
                if not base.startswith("!"):
                    return {"or": [[edge], [inner]]}
                return {"not": {"or": [[edge], [inner]]}}
            if low == "matches":
                self.expect("id", None)            # "regex"
                return {"field": f, "op": "MATCHES_REGEX", "values": [str(self.value())], "cs": False}
            if low in ("has_any", "has_all"):
                self.expect("op", "(")
                vals = [self.value()]
                while self.peek() == ("op", ","):
                    self.take()
                    vals.append(self.value())
                self.expect("op", ")")
                leaves = [{"field": f, "op": "CONTAINS_WORD", "values": [v], "cs": False} for v in vals]
                return {"or": [[x] for x in leaves]} if low == "has_any" else {"and": leaves}
            if low in ("between", "!between"):
                self.expect("op", "(")
                lo = self.value()
                self.expect("op", "..")
                hi = self.value()
                self.expect("op", ")")
                return {"field": f, "op": "IS_IN_RANGE" if low == "between" else "IS_OUTSIDE_RANGE", "values": [lo, hi]}
            raise ConvertError(f"the operator {op!r} is not supported")
        if k != "op":
            raise ConvertError(f"expected an operator after {f!r} but found {op!r}")
        if op in ("in", "in~", "!in", "!in~"):
            self.expect("op", "(")
            vals = [self.value()]
            while self.peek() == ("op", ","):
                self.take()
                vals.append(self.value())
            self.expect("op", ")")
            return {"field": f, "op": "IS_IN_LIST" if op in ("in", "in~") else "IS_NOT_IN_LIST", "values": vals,
                    "cs": not op.endswith("~")}
        val = self.value()
        # KQL: == / != / in / !in compare EXACT case; =~ / !~ / in~ / !in~ ignore case (measured on a tenant that
        # applies KQL: event.outcome == "success" excluded "Success" events).
        if op in ("==", "=~"):
            return {"field": f, "op": "IS_IN_LIST", "values": [val], "cs": op == "=="}
        if op in ("!=", "!~"):
            return {"field": f, "op": "IS_NOT_IN_LIST", "values": [val], "cs": op == "!="}
        return {"field": f, "op": op, "values": [val]}            # > < >= <= : resolved by field type later


def parse(text, bare=False):
    """KQL text → {"clauses": [...], "project": [...]|None, "notes": [...]} or raise ConvertError."""
    text = (text or "").strip()
    if not text:
        raise ConvertError("paste a search first")
    toks = tokenize(quote_bare(text) if bare else text)
    stages = _split_pipes(toks)
    first = stages[0]
    notes, clauses, project = [], [], None
    if first and first[0][0] == "id" and first[0][1].lower() in ("events", "event"):
        if len(first) > 1:
            raise ConvertError("cannot read the search after 'events'")
        stages = stages[1:]
    elif first:
        stages = [[("id", "where")] + first] + stages[1:]          # a bare expression: treat as "| where …"
    for st in stages:
        if not st:
            continue
        k, v = st[0]
        word = v.lower() if k == "id" else v
        if word == "where":
            clauses += _Where(st[1:], bare).parse()
        elif word in ("sort", "order"):
            notes.append("the sort order is ignored: the export holds every matching event either way")
        elif word == "project":
            names = []
            for kk, vv in st[1:]:
                if (kk, vv) == ("op", ","):
                    continue
                if kk not in ("field", "id"):
                    raise ConvertError("| project can only list field names")
                names.append(vv)
            project = names
        elif word in ("take", "limit", "top", "sample"):
            raise ConvertError(f"| {v} is not supported: this tool exports every matching event, so a row limit "
                               f"would change the result. Remove it")
        else:
            raise ConvertError(f"| {v} is not supported (only where, sort and project can be converted)")
    if not clauses and project is None:
        raise ConvertError("the search has no conditions (add | where …)")
    return {"clauses": clauses, "project": project, "notes": notes}


TIME_FIELDS = ("@timestamp",)
TYPE_WORDS = {"String": "text", "Ipv4": "IP address", "Float64": "number", "Boolean": "true/false"}


def convert(text, field_type, known_fields=None, bare=False):
    """Only ConvertError ever leaves this function (review M2): any other failure while reading an unusual search
    is reported as a search that cannot be converted, never as a traceback. bare=True (command-line --where only)
    reads an unquoted word as a value, so a condition needs no inner quotes on any shell."""
    try:
        return _convert(text, field_type, known_fields, bare)
    except ConvertError:
        raise
    except RecursionError:
        raise ConvertError("the search nests brackets too deeply to read")
    except (ValueError, KeyError, IndexError, TypeError, OverflowError, AttributeError) as ex:
        raise ConvertError(f"cannot read this search ({type(ex).__name__}); simplify it, or build the search here")


NOT_WORDS = {"IS_NOT_IN_LIST": "is not one of", "DOES_NOT_CONTAIN": "does not contain",
             "DOES_NOT_START_WITH": "does not start with", "DOES_NOT_END_WITH": "does not end with",
             "DOES_NOT_CONTAIN_WORD": "does not have the word", "DOES_NOT_MATCH_REGEX": "does not match the pattern",
             "IS_OUTSIDE_RANGE": "is not between"}
POS_WORDS = {"IS_IN_LIST": "is one of", "CONTAINS": "contains", "STARTS_WITH": "starts with", "ENDS_WITH": "ends with",
             "CONTAINS_WORD": "has the word", "MATCHES_REGEX": "matches the pattern", "IS_IN_RANGE": "is between"}


# not(...) flips each operator to its twin. Like every operator except "is empty", the twin needs the field to have
# a value, which is how Abstract's search reads != and !contains too.
FLIP = {"IS_IN_LIST": "IS_NOT_IN_LIST", "CONTAINS": "DOES_NOT_CONTAIN", "STARTS_WITH": "DOES_NOT_START_WITH",
        "ENDS_WITH": "DOES_NOT_END_WITH", "CONTAINS_WORD": "DOES_NOT_CONTAIN_WORD", "MATCHES_REGEX": "DOES_NOT_MATCH_REGEX",
        "IS_IN_RANGE": "IS_OUTSIDE_RANGE", ">": "<=", "<": ">=", "IS_EMPTY": "IS_NOT_EMPTY", "IS_IN_SUBNET": "IS_NOT_IN_SUBNET"}
FLIP.update({v: k for k, v in list(FLIP.items())})
MAX_GROUPS = 64


def _flip(c):
    if c["field"] in TIME_FIELDS:
        raise ConvertError(f"not(...) cannot be used on {c['field']}; it sets the time range")
    key = "fn" if "fn" in c else "op"
    if c[key] not in FLIP:
        raise ConvertError(f"not(...) cannot be used on this condition on {c['field']}")
    out = dict(c, **{key: FLIP[c[key]]})
    out.pop("shown", None)
    return out


def _nnf(node, neg=False):
    """Push not(...) down to the conditions (De Morgan): the result holds only and / or / conditions."""
    if "not" in node:
        return _nnf(node["not"], not neg)
    if "and" in node:
        items = [_nnf(x, neg) for x in node["and"]]
        return {"or": [[x] for x in items]} if neg else {"and": items}
    if "or" in node:
        alts = [_nnf({"and": alt}, neg) for alt in node["or"]]
        return {"and": alts} if neg else {"or": [[x] for x in alts]}
    return _flip(node) if neg else node


def _cnf(node):
    """Any and / or nesting -> a list of OR-clauses that must all hold (exactly the same events). The tool's search
    is 'every filter, and at least one of each group', which is this form."""
    if "and" in node:
        return [cl for x in node["and"] for cl in _cnf(x)]
    if "or" in node:
        out = [[]]
        for alt in node["or"]:
            alt_cnf = _cnf({"and": alt})
            out = [a + b for a in out for b in alt_cnf]
            if len(out) > MAX_GROUPS:
                raise ConvertError("this combination of 'and' and 'or' expands into too many groups to run exactly; "
                                   "simplify it, or split it into several exports")
        return out
    return [[node]]


def _leaves(items):
    for it in items:
        if "not" in it:
            yield from _leaves([it["not"]])
        elif "or" in it:
            for alt in it["or"]:
                yield from _leaves(alt)
        elif "and" in it:
            yield from _leaves(it["and"])
        else:
            yield it


def _convert(text, field_type, known_fields=None, bare=False):
    """Turn KQL into this tool's search pieces.
    field_type(name) -> 'String' | 'List(String)' | 'Ipv4' | 'Float64' | 'Boolean' | 'Date' …; known_fields: a set, or
    None to skip. Returns {"dataset", "filters": [...], "any_groups": [[...], ...], "time", "fields", "notes",
    "readable"}: every filter must match, and in each any-group at least one filter must."""
    p = parse(text, bare)
    st = {"dataset": None, "time_lo": None, "time_hi": None, "notes": list(p["notes"]), "readable": [],
          "not_empty": False}
    unknown = [c["field"] for c in _leaves(p["clauses"]) if known_fields is not None and c["field"] not in known_fields
               and c["field"] not in TIME_FIELDS]
    if unknown:
        raise ConvertError("these fields do not exist on your tenant: " + ", ".join(sorted(set(unknown))))
    filters, groups = [], []
    for clause in _cnf(_nnf({"and": p["clauses"]})):
        uniq = []
        for c in clause:                                    # (a or a) is a
            if c not in uniq:
                uniq.append(c)
        if len(uniq) == 1:
            row = _leaf_row(uniq[0], field_type, st, in_group=False)
            if row is not None:
                filters.append(row)
            continue
        group, shown = [], []
        for c in uniq:
            group.append(_leaf_row(c, field_type, st, in_group=True))
            shown.append(st["readable"].pop())
        groups.append(group)
        st["readable"].append("at least one of: " + "  OR  ".join(shown))
    time = None
    if st["time_lo"] or st["time_hi"]:
        time = {"start": st["time_lo"], "end": st["time_hi"]}
        st["readable"].append("time: " + (st["time_lo"] or "(your start)") + " → " + (st["time_hi"] or "(your end)"))
    notes = st["notes"]
    if any("(exact case)" in r for r in st["readable"]):
        notes.append("== / != / in and the _cs operators compare exact upper/lower case, as in Abstract's search; "
                     "=~ / in~ and the plain text operators ignore it")
    if st["not_empty"]:
        notes.append("a 'not' filter (!contains, !has, !=, …) matches events that HAVE a value which does not match — "
                     "as Abstract's search does. KQL itself would also match events where the field is empty; to "
                     "include those, tick \"or has no value\" on that filter")
    return {"dataset": st["dataset"], "filters": filters, "any_groups": groups, "time": time, "fields": p["project"],
            "notes": notes, "readable": st["readable"]}


def _leaf_row(c, field_type, st, in_group):
    """One parsed condition -> one filter row (or None for the dataset / time, which go elsewhere)."""
    f = c["field"]
    readable = st["readable"]
    if f in TIME_FIELDS:
        if in_group or "fn" in c or len(c.get("values") or []) != 1:
            raise ConvertError(f"{f} can only be compared with > / >= / < / <= datetime(…); it sets the time range")
        op, (val,) = c.get("op"), c["values"]
        if not (isinstance(val, tuple) and val[0] == "datetime") or op not in (">", ">=", "<", "<="):
            raise ConvertError(f"{f} can only be compared with > / >= / < / <= datetime(…); it sets the time range")
        iso = _iso_utc(val[1])
        if op in (">", ">="):
            st["time_lo"] = max(st["time_lo"], iso) if st["time_lo"] else iso
        else:
            st["time_hi"] = min(st["time_hi"], iso) if st["time_hi"] else iso
        return None
    ftype = field_type(f) or "String"
    if ftype == "Date":
        raise ConvertError(f"{f} is a date field; only @timestamp can be used to set the time range")
    if "fn" in c:
        if c["fn"] in ("IS_IN_SUBNET", "IS_NOT_IN_SUBNET"):
            if ftype != "Ipv4":
                raise ConvertError(f"ipv4_is_in_range() needs an IP address field; {f} is {TYPE_WORDS.get(ftype, ftype)}")
            if c["fn"] == "IS_NOT_IN_SUBNET":
                st["not_empty"] = True
            readable.append(f"{f} is {'not ' if c['fn'] == 'IS_NOT_IN_SUBNET' else ''}in subnet {c['values'][0]}")
            return {"field": f, "type": "Ipv4", "op": c["fn"], "value": c["values"][0]}
        readable.append(f"{f} {'has a value' if c['fn'] == 'IS_NOT_EMPTY' else 'is empty'}")
        return {"field": f, "type": ftype if ftype in TYPE_WORDS or ftype.startswith("List") else "String", "op": c["fn"],
                "value": ""}
    op, vals = c["op"], c["values"]
    if any(isinstance(v, tuple) for v in vals):
        raise ConvertError(f"datetime(…) can only be used with @timestamp, not {f}")
    cs = bool(c.get("cs"))
    case = "(exact case)" if cs else "(any case)"
    if op in (">", "<", ">=", "<="):
        if ftype != "Float64":
            raise ConvertError(f"'{op}' needs a number field; {f} is {TYPE_WORDS.get(ftype, ftype)}")
        name = {">": "IS_GREATER_THAN", ">=": "IS_GREATER_THAN_OR_EQUAL_TO", "<": "IS_LESS_THAN",
                "<=": "IS_LESS_THAN_OR_EQUAL_TO"}[op]
        readable.append(f"{f} {({'>': 'greater than', '>=': 'at least', '<': 'less than', '<=': 'at most'})[op]} {vals[0]}")
        return {"field": f, "type": "Float64", "op": name, "value": str(vals[0])}
    if op in ("IS_IN_RANGE", "IS_OUTSIDE_RANGE"):
        if ftype != "Float64":
            raise ConvertError(f"between needs a number field; {f} is {TYPE_WORDS.get(ftype, ftype)}")
        if op == "IS_OUTSIDE_RANGE":
            st["not_empty"] = True
        readable.append(f"{f} {'is between' if op == 'IS_IN_RANGE' else 'is not between'} {vals[0]} and {vals[1]}")
        return {"field": f, "type": "Float64", "op": op, "value": f"{vals[0]},{vals[1]}"}
    if ftype == "Boolean":
        if len(vals) != 1 or not isinstance(vals[0], bool) or op not in ("IS_IN_LIST", "IS_NOT_IN_LIST"):
            raise ConvertError(f"{f} is a true/false field: compare it with == true or == false")
        truth = vals[0] if op == "IS_IN_LIST" else not vals[0]
        readable.append(f"{f} is {'true' if truth else 'false'}")
        return {"field": f, "type": "Boolean", "op": "IS_TRUE" if truth else "IS_FALSE", "value": ""}
    if ftype == "Ipv4":
        if op not in ("IS_IN_LIST", "IS_NOT_IN_LIST"):
            raise ConvertError(f"{f} is an IP address field; use ==, !=, in, !in or ipv4_is_in_range()")
        bad = [v for v in vals if not re.match(r"^(\d{1,3}\.){3}\d{1,3}$", str(v))]
        if bad:
            raise ConvertError(f"{f} is an IP address field; {bad[0]!r} is not an IPv4 address")
        if op == "IS_NOT_IN_LIST":
            st["not_empty"] = True
        readable.append(f"{f} {POS_WORDS.get(op) or NOT_WORDS[op]} {', '.join(str(v) for v in vals)}")
        return {"field": f, "type": "Ipv4", "op": op, "value": ",".join(str(v) for v in vals)}
    if ftype == "Float64":
        if op not in ("IS_IN_LIST", "IS_NOT_IN_LIST"):
            raise ConvertError(f"{f} is a number field; use ==, !=, in, >, <, >=, <= or between")
        if op == "IS_NOT_IN_LIST":
            st["not_empty"] = True
        readable.append(f"{f} {POS_WORDS.get(op) or NOT_WORDS[op]} {', '.join(str(v) for v in vals)}")
        return {"field": f, "type": "Float64", "op": op, "value": ",".join(str(v) for v in vals)}
    strs = [str(v).lower() if isinstance(v, bool) else str(v) for v in vals]
    if any(v.strip() == "" for v in strs):
        raise ConvertError(f"comparing {f} with an empty value is not supported; use isempty({f}) or "
                           f"isnotempty({f}) instead")
    if any(v != v.strip() for v in strs):
        raise ConvertError(f"a value for {f} starts or ends with a space, which would not be kept in the search; "
                           f"remove the spaces or build the filter here")
    list_field = ftype.startswith("List")
    if list_field and op not in ("CONTAINS", "DOES_NOT_CONTAIN", "IS_IN_LIST"):
        raise ConvertError(f"{f} is a list field; use contains, !contains or == (an exact item)")
    if op in ("IS_IN_LIST", "IS_NOT_IN_LIST"):
        if any("," in v for v in strs):
            raise ConvertError(f"a value for {f} contains a comma, which this tool cannot express as a list filter")
        if list_field:
            readable.append(f"{f} has any of these items: " + ", ".join(f'"{v}"' for v in strs))
            return {"field": f, "type": ftype, "op": "CONTAINS_ANY", "value": ",".join(strs), "case_sensitive": cs}
        if (f == "data_stream.dataset" and op == "IS_IN_LIST" and len(strs) == 1 and st["dataset"] is None
                and not in_group):
            st["dataset"] = strs[0]                 # dataset names always match in any case (they are names)
            readable.append(f'dataset is "{st["dataset"]}" (any case)')
            return None
    if op in NOT_WORDS:
        st["not_empty"] = True
    word = c.get("shown") or POS_WORDS.get(op) or NOT_WORDS.get(op) or op.lower()
    if op in ("IS_IN_LIST", "IS_NOT_IN_LIST"):
        readable.append(f"{f} {word} " + ", ".join(f'"{v}"' for v in strs) + " " + case)
    else:
        readable.append(f"{f} {word}" + ("" if c.get("shown") else f' "{strs[0]}"') + " " + case)
    return {"field": f, "type": ftype, "op": op, "value": ",".join(strs) if op in ("IS_IN_LIST", "IS_NOT_IN_LIST")
            else strs[0], "case_sensitive": cs}


KQL_STAGES_OK = {"where", "sort", "order", "project"}


def kql_run_problem(text):
    """Why a search may NOT run as KQL in an export, or None. Only filtering stages are allowed: a stage that limits,
    aggregates, reshapes or joins rows (| take, | summarize, | extend, | join …) would change "every matching
    event", so the search is refused instead of exporting something else."""
    t = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', text or "")   # ignore anything inside quotes
    if not re.match(r"^\s*events\b", t):
        return "the search must start with events"
    for m in re.finditer(r"\|\s*([A-Za-z][\w-]*)", t):
        stage = m.group(1).lower()
        if stage not in KQL_STAGES_OK:
            return (f"'| {stage}' changes which or how many events come back; remove it so the export holds every "
                    f"matching event")
    return None


def to_condition(filters, dataset=None):
    """Filter rows → the API condition object (the same shape abx_core.build_condition produces)."""
    LIST_OPS = {"IS_IN_LIST", "IS_NOT_IN_LIST"}
    NO_VALUE = {"IS_NOT_EMPTY", "IS_EMPTY", "IS_TRUE", "IS_FALSE", "IS_RFC1918", "IS_NOT_RFC1918"}
    conds = []
    if dataset:
        conds.append({"field": "data_stream.dataset", "operation": "IS_IN_LIST", "field_type": "String",
                      "case_sensitive": False, "values": [dataset]})
    for f in filters:
        c = {"field": f["field"], "operation": f["op"], "field_type": f.get("type") or "String",
             "case_sensitive": bool(f.get("case_sensitive"))}
        if f["op"] in LIST_OPS:
            c["values"] = [v.strip() for v in str(f.get("value", "")).split(",") if v.strip()]
        elif f["op"] in NO_VALUE:
            c["value"] = ""
        else:
            c["value"] = str(f.get("value", "")).strip()
        conds.append(c)
    return {"operator": "and", "conditions": conds, "groups": []}


# ───────────────────────────── saved views built with Abstract's filter builder ─────────────────────────────
# A saved view holds its search either as KQL text (kql_query) or, when it was built with the filter builder, as
# structured conditions (query): [{"operator": "and"|"or", "value": [leaf | group, …]}], where a leaf is
# {"field", "fieldType", "field_operation", "value", "caseSensitive"?}. The list of views leaves the structured
# search out; the single view (GET /v1/streamviewer/view/{id}) has it. It is written here as the KQL Abstract's
# search would show, so it goes through exactly the same conversion, checks and proofs as any other search.

_RFC1918_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


def _q(v):
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _vals(leaf):
    v = leaf.get("values") if isinstance(leaf.get("values"), list) and leaf.get("values") else leaf.get("value")
    if isinstance(v, list):
        out = [str(x) for x in v]
    elif v is None:
        out = []
    else:
        out = [x.strip() for x in str(v).split(",")] if "," in str(v) else [str(v)]
    return [x for x in out if x != ""]


def _num(v):
    s = str(v).strip()
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        raise ConvertError(f"{s!r} is not a number")
    return s


def _leaf_kql(leaf):
    f = leaf.get("field")
    if not f:
        raise ConvertError("a condition in this view has no field")
    if str(leaf.get("eventConditionType") or "condition").lower() not in ("condition", ""):
        raise ConvertError(f"the condition on {f} is a {leaf.get('eventConditionType')} condition (such as a model "
                           f"lookup), which this tool cannot run")
    op = str(leaf.get("field_operation") or leaf.get("operation") or "").upper()
    cs = str(leaf.get("caseSensitive", leaf.get("case_sensitive", False))).lower() == "true"
    fld = "['" + str(f).replace("'", "\\'") + "']"
    vals = _vals(leaf)
    one = vals[0] if vals else ""
    t = {"CONTAINS": "contains", "DOES_NOT_CONTAIN": "!contains", "STARTS_WITH": "startswith",
         "DOES_NOT_START_WITH": "!startswith", "ENDS_WITH": "endswith", "DOES_NOT_END_WITH": "!endswith",
         "CONTAINS_WORD": "has", "DOES_NOT_CONTAIN_WORD": "!has"}
    if op in t:
        return f"{fld} {t[op]}{'_cs' if cs else ''} {_q(one)}"
    if op in ("EQUALS", "IS_IN_LIST", "CONTAINS_ANY"):
        if str(leaf.get("fieldType") or "") == "Boolean":
            return f"{fld} == {str(one).lower()}"
        if op == "EQUALS" and len(vals) == 1:
            return f"{fld} {'==' if cs else '=~'} {_q(one)}"
        return f"{fld} {'in' if cs else 'in~'} (" + ", ".join(_q(v) for v in vals) + ")"
    if op in ("DOES_NOT_EQUAL", "NOT_EQUALS", "IS_NOT_IN_LIST"):
        if op != "IS_NOT_IN_LIST" and len(vals) == 1:
            return f"{fld} {'!=' if cs else '!~'} {_q(one)}"
        return f"{fld} {'!in' if cs else '!in~'} (" + ", ".join(_q(v) for v in vals) + ")"
    if op == "CONTAINS_ALL":
        return "(" + " and ".join(f"{fld} {'==' if cs else '=~'} {_q(v)}" for v in vals) + ")"
    if op == "MATCHES_REGEX":
        return f"{fld} matches regex {_q(one)}"
    if op == "DOES_NOT_MATCH_REGEX":
        return f"not({fld} matches regex {_q(one)})"
    if op in ("IS_EMPTY", "IS_NOT_EMPTY"):
        return f"{'isempty' if op == 'IS_EMPTY' else 'isnotempty'}({fld})"
    cmp = {"IS_GREATER_THAN": ">", "IS_GREATER_THAN_OR_EQUAL_TO": ">=", "IS_LESS_THAN": "<",
           "IS_LESS_THAN_OR_EQUAL_TO": "<="}
    if op in cmp:
        return f"{fld} {cmp[op]} {_num(one)}"
    if op in ("IS_IN_RANGE", "IS_OUTSIDE_RANGE"):
        lo, hi = (leaf.get("from"), leaf.get("to")) if leaf.get("from") not in (None, "") else \
            (vals + ["", ""])[:2]
        return f"{fld} {'between' if op == 'IS_IN_RANGE' else '!between'} ({_num(lo)} .. {_num(hi)})"
    if op in ("IS_IN_SUBNET", "IS_NOT_IN_SUBNET"):
        inner = " or ".join(f"ipv4_is_in_range({fld}, {_q(v)})" for v in vals) or "false"
        return f"({inner})" if op == "IS_IN_SUBNET" else f"not({inner})"
    if op in ("IS_RFC1918", "IS_NOT_RFC1918"):
        inner = " or ".join(f"ipv4_is_in_range({fld}, {_q(n)})" for n in _RFC1918_NETS)
        return f"({inner})" if op == "IS_RFC1918" else f"not({inner})"
    if op in ("IS_TRUE", "IS_FALSE"):
        return f"{fld} == {'true' if op == 'IS_TRUE' else 'false'}"
    raise ConvertError(f"the operation {op or '(none)'} on {f} is not supported")


def _group_kql(node):
    """A group {"operator", "value": [...]} or a leaf, as KQL. An empty group is None (it constrains nothing)."""
    if isinstance(node, dict) and "field" in node:
        return _leaf_kql(node)
    items = node.get("value") if isinstance(node, dict) else node
    if not isinstance(items, list):
        raise ConvertError("this view's search has a shape this tool does not recognise")
    parts = [p for p in (_group_kql(x) for x in items) if p]
    if not parts:
        return None
    joiner = " or " if str((node.get("operator") if isinstance(node, dict) else "and") or "and").lower() == "or" \
        else " and "
    return parts[0] if len(parts) == 1 else "(" + joiner.join(parts) + ")"


def condition_to_kql(cond):
    """An API condition ({"operator", "conditions", "groups"}, as a replay or archive job stores it) as KQL, or None
    when it holds no condition."""
    def node(n):
        parts = [_leaf_kql(c) for c in n.get("conditions") or []] + [p for p in (node(g) for g in n.get("groups") or []) if p]
        if not parts:
            return None
        j = " or " if str(n.get("operator") or "and").lower() == "or" else " and "
        return parts[0] if len(parts) == 1 else "(" + j.join(parts) + ")"
    if not isinstance(cond, dict):
        return None
    body = node(cond)
    if not body:
        return None
    return "events | where " + (body[1:-1] if body.startswith("(") and _balanced(body[1:-1]) else body)


def _balanced(s):
    depth = 0
    for ch in s:
        depth += ch == "("
        depth -= ch == ")"
        if depth < 0:
            return False
    return depth == 0


def structured_to_kql(query):
    """The filter builder's conditions as KQL (top-level groups are ANDed), or None when they hold no condition."""
    if not query:
        return None
    parts = [p for p in (_group_kql(g) for g in (query if isinstance(query, list) else [query])) if p]
    if not parts:
        return None
    body = " and ".join(parts)
    if body.startswith("(") and body.endswith(")") and len(parts) == 1:
        depth, inner = 0, True
        for i, ch in enumerate(body):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(body) - 1:
                inner = False
                break
        if inner:
            body = body[1:-1]
    return "events | where " + body


def view_time(ts):
    """A view's time selection as this tool's time range: {"last": "24h"} or {"start", "end"}; None if none."""
    if not isinstance(ts, dict):
        return None
    if str(ts.get("type") or "").lower() == "relative" and ts.get("numeric_value"):
        unit = {"minutes": "m", "hours": "h", "days": "d", "weeks": "d", "months": "d"}.get(
            str(ts.get("option_selected") or "").lower())
        if not unit:
            return None
        n = int(ts["numeric_value"]) * {"weeks": 7, "months": 30}.get(str(ts.get("option_selected")).lower(), 1)
        return {"last": f"{n}{unit}"}
    if ts.get("start_date") and ts.get("end_date"):
        try:
            return {"start": _iso_utc(str(ts["start_date"])), "end": _iso_utc(str(ts["end_date"]))}
        except ConvertError:
            return None
    return None
