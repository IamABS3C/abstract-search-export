"""
abx_ops.py — what every filter operator means, in one place.

The semantics are the platform's own, measured operator by operator against the true answer on a production tenant
(2026-10-31, 8,179 events, recent data): text operators ignore case unless case_sensitive is set; CONTAINS_WORD is a
whole word bounded by anything that is not a letter or digit; every operator except IS_EMPTY needs the field to have
a value, so the "not" operators match events that HAVE a value which does not match (an event with no value is not
"not containing" anything — that is what Abstract's search does); ranges include both ends; on a list field an
operator holds when any element holds (CONTAINS_ANY / CONTAINS_ALL compare whole elements).

The tool uses this module to re-check every event the archive returns, and to apply a filter itself where the
platform cannot (text values longer than 256 characters have a value, but most text filters do not see them).
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any, Dict, List, Optional

# "not" operator -> the positive operator it negates (for events that have a value)
NEGATIVE = {"DOES_NOT_EQUAL": "EQUALS", "IS_NOT_IN_LIST": "IS_IN_LIST", "DOES_NOT_CONTAIN": "CONTAINS",
            "DOES_NOT_START_WITH": "STARTS_WITH", "DOES_NOT_END_WITH": "ENDS_WITH",
            "DOES_NOT_CONTAIN_WORD": "CONTAINS_WORD", "DOES_NOT_MATCH_REGEX": "MATCHES_REGEX",
            "IS_OUTSIDE_RANGE": "IS_IN_RANGE", "IS_NOT_IN_SUBNET": "IS_IN_SUBNET", "IS_NOT_RFC1918": "IS_RFC1918"}
POSITIVE_TWIN = {v: k for k, v in NEGATIVE.items()}
# operators that compare text, where a value longer than LONG_TEXT is invisible to the platform
TEXT_OPS = {"EQUALS", "DOES_NOT_EQUAL", "IS_IN_LIST", "IS_NOT_IN_LIST", "CONTAINS", "DOES_NOT_CONTAIN",
            "STARTS_WITH", "DOES_NOT_START_WITH", "ENDS_WITH", "DOES_NOT_END_WITH", "CONTAINS_WORD",
            "DOES_NOT_CONTAIN_WORD", "MATCHES_REGEX", "DOES_NOT_MATCH_REGEX", "IS_EMPTY", "IS_NOT_EMPTY"}
NO_VALUE = {"IS_EMPTY", "IS_NOT_EMPTY", "IS_TRUE", "IS_FALSE", "IS_RFC1918", "IS_NOT_RFC1918"}
LONG_TEXT = 256                      # measured: most text filters do not see a longer value (it still has a value)
_RFC1918 = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


def dig(obj: Any, dotted: str) -> Any:
    if isinstance(obj, dict) and dotted in obj:
        return obj[dotted]
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def present(v: Any) -> List[str]:
    """The field's values that count as a value: a list field gives its elements; empty strings and nulls drop."""
    items = v if isinstance(v, list) else [v]
    return [("true" if x is True else "false" if x is False else str(x)) for x in items
            if x not in (None, "", [], {})]


def has_token(xs: List[str]) -> bool:
    """Measured: "has a value", "is empty" and the "not" operators look for a letter or digit, so a value made only of
    punctuation, such as "/" or "-", counts as empty there (the positive operators still match it: contains "/")."""
    return any(ch.isalnum() for x in xs for ch in x)


def leaf_values(leaf: Dict[str, Any]) -> List[str]:
    v = leaf.get("values")
    if isinstance(v, list) and v:
        return [str(x) for x in v]
    one = leaf.get("value")
    return [] if one in (None, "") else [str(one)]


def _num(x: Any) -> Optional[float]:
    try:
        return float(str(x))
    except (TypeError, ValueError):
        return None


def _word(hay: str, w: str, cs: bool) -> bool:
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])", hay, 0 if cs else re.I) is not None


def _positive(op: str, xs: List[str], leaf: Dict[str, Any]) -> Optional[bool]:
    cs = bool(leaf.get("case_sensitive"))
    n = (lambda s: s) if cs else (lambda s: s.casefold())
    want = leaf_values(leaf)
    ftype = str(leaf.get("field_type") or "String")
    if op in ("EQUALS", "IS_IN_LIST"):
        if ftype == "Float64":
            nums = [_num(w) for w in want]
            return any(_num(x) is not None and _num(x) in nums for x in xs)
        return any(n(x) in {n(w) for w in want} for x in xs)
    if op == "CONTAINS":
        return bool(want) and any(n(want[0]) in n(x) for x in xs)
    if op == "STARTS_WITH":
        return bool(want) and any(n(x).startswith(n(want[0])) for x in xs)
    if op == "ENDS_WITH":
        return bool(want) and any(n(x).endswith(n(want[0])) for x in xs)
    if op == "CONTAINS_WORD":
        return bool(want) and any(_word(x, want[0], cs) for x in xs)
    if op == "MATCHES_REGEX":
        try:
            rx = re.compile(want[0], 0 if cs else re.I)
        except (re.error, IndexError):
            return None
        return any(rx.search(x) for x in xs)
    if op == "CONTAINS_ANY":
        return any(n(x) in {n(w) for w in want} for x in xs)
    if op == "CONTAINS_ALL":
        have = {n(x) for x in xs}
        return bool(want) and all(n(w) in have for w in want)
    if op in ("IS_GREATER_THAN", "IS_GREATER_THAN_OR_EQUAL_TO", "IS_LESS_THAN", "IS_LESS_THAN_OR_EQUAL_TO"):
        y = _num(want[0]) if want else None
        if y is None:
            return None
        cmp = {"IS_GREATER_THAN": lambda a: a > y, "IS_GREATER_THAN_OR_EQUAL_TO": lambda a: a >= y,
               "IS_LESS_THAN": lambda a: a < y, "IS_LESS_THAN_OR_EQUAL_TO": lambda a: a <= y}[op]
        return any(_num(x) is not None and cmp(_num(x)) for x in xs)
    if op == "IS_IN_RANGE":
        lo, hi = _num(leaf.get("from")), _num(leaf.get("to"))
        if lo is None or hi is None:
            return None
        nums = [_num(x) for x in xs]
        return any(n is not None and lo <= n <= hi for n in nums)
    if op == "IS_IN_SUBNET":
        try:
            nets = [ipaddress.ip_network(w, strict=False) for w in want]
        except ValueError:
            return None
        out = False
        for x in xs:
            try:
                out = out or any(ipaddress.ip_address(x) in net for net in nets)
            except ValueError:
                continue
        return out
    if op == "IS_RFC1918":
        out = False
        for x in xs:
            try:
                out = out or any(ipaddress.ip_address(x) in net for net in _RFC1918)
            except ValueError:
                continue
        return out
    if op in ("IS_TRUE", "IS_FALSE"):
        return any(x.lower() == ("true" if op == "IS_TRUE" else "false") for x in xs)
    return None


def leaf_matches(row: Dict[str, Any], leaf: Dict[str, Any]) -> Optional[bool]:
    """True / False for one condition leaf, None when this module does not model the operator."""
    op = str(leaf.get("operation") or leaf.get("field_operation") or "").upper()
    xs = present(dig(row, str(leaf.get("field") or "")))
    if op == "IS_EMPTY":
        return not has_token(xs)
    if op == "IS_NOT_EMPTY":
        return has_token(xs)
    if op in NEGATIVE and not has_token(xs):
        return False                     # measured: a value made only of punctuation counts as empty for "not"
    if not xs:
        return False if _positive(op, ["x"], leaf) is not None else None
    if op in NEGATIVE:
        p = _positive(NEGATIVE[op], xs, leaf)
        return None if p is None else not p
    return _positive(op, xs, leaf)


def condition_matches(row: Dict[str, Any], cond: Optional[Dict[str, Any]], strict: bool = False) -> bool:
    """The whole condition for one row. A leaf this module cannot model counts as True (strict=False: never fail a
    row on an operator we do not know) or False (strict=True)."""
    if not cond:
        return True
    res = []
    for leaf in cond.get("conditions") or []:
        r = leaf_matches(row, leaf)
        res.append((not strict) if r is None else r)
    res += [condition_matches(row, g, strict) for g in cond.get("groups") or []]
    if not res:
        return True
    return any(res) if str(cond.get("operator") or "and").lower() == "or" else all(res)


def caseless_where_meaningless(cond: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Exact case on a value with no letters means nothing, and measured, the platform's exact-case "is one of"
    misses a value made only of punctuation ("/"). Such leaves are sent ignoring case: the same events."""
    if not cond:
        return cond
    out = dict(cond)
    out["conditions"] = [dict(c, case_sensitive=False) if c.get("case_sensitive") and not has_token(leaf_values(c))
                         and leaf_values(c) else c for c in cond.get("conditions") or []]
    out["groups"] = [caseless_where_meaningless(g) for g in cond.get("groups") or []]
    return out


def condition_fields(cond: Optional[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for leaf in (cond or {}).get("conditions") or []:
        if leaf.get("field"):
            out.append(str(leaf["field"]))
    for g in (cond or {}).get("groups") or []:
        out += condition_fields(g)
    return list(dict.fromkeys(out))
