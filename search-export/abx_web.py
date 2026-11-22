"""
abx_web — the local browser app for Abstract Search & Export.

A small web server on 127.0.0.1. It serves one page and a JSON API that drives the same abx_core
session as the terminal menu. Security model (see docs/SECURITY.md):
  * binds to the loopback interface only, so nothing on the network can reach it. A container is the one
    exception: there it listens on the container's own interface (--bind 0.0.0.0) and the container's port is
    published to the computer's loopback address, which gives the same result;
  * each launch prints a link carrying a one-time launch code. The page exchanges it once (POST) for a
    session token, keeps that token in sessionStorage and sends it as the X-ABX-Token header. There is
    no cookie, and no long-lived secret ever appears in a URL;
  * downloads use five-minute tickets minted on click (abx_core enforces the exports folder). A ticket is
    redeemed once for a whole-file GET; after that it answers only Range continuations of an INTERRUPTED
    download, until it expires or the last byte has been delivered, whichever comes first;
  * at most MAX_HANDLERS requests are handled at once; a request over the cap gets a fast 503;
  * the Host header must name 127.0.0.1 or localhost (any port), which blocks DNS rebinding; a POST
    from another origin is refused. ABX_WEB_URL adds exactly one more address, the one the app is reached at;
  * no CORS headers, a strict Content-Security-Policy with no inline script, and no framing;
  * the API key is held in this process's memory and is never sent back to the page.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import email.utils
import errno
import json
import os
import re
import secrets
import select
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import abx_core as C
import abx_engine as E

WEB_DIR = os.path.join(C.HERE, "web")
MAX_BODY = 1024 * 1024                       # JSON requests are small; anything bigger is refused (413)
SOCKET_TIMEOUT = 30                          # seconds a client may stall one read or write
ALLOWED_HOSTS = ("127.0.0.1", "localhost")
PORT_TRIES = 10                              # an explicit --port that is taken: try the next few
# SEC-14: ThreadingMixIn starts one thread per connection with no limit. A browser opens at most ~6
# connections per origin, so 16 is generous for the page while bounding a flood (or 16 stalled clients,
# each dropped by SOCKET_TIMEOUT). Over the cap the accept thread answers 503 itself, without a thread.
MAX_HANDLERS = 16
SLOT_WAIT = 0.2                              # a burst gets this long for a slot to free up before the 503
TICKET_TTL = 300                             # matches abx_core's five-minute download tickets
RELINK_EVERY = 5                             # seconds between launch links printed on request (no terminal)
LOOPBACK = ("127.0.0.1", "localhost", "::1")

# A fixed map, never the mimetypes module: on Windows the registry can map .js to text/plain, and with
# nosniff the browser would then refuse to run the app.
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".json": "application/json; charset=utf-8", ".md": "text/markdown; charset=utf-8",
        ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}
STATIC = {"app.js", "app.css", "workbench.js", "logo-white.svg", "logo-mark.svg",
          "rocket-a.js", "rocket-a.css", "rocket-a.svg", "launch.js", "launch.css"}   # launch mode (hidden)
# The brand fonts (served as /static/fonts/<name>; exact names only, so no other path under fonts/ is reachable)
FONT_FILES = ("Barlow-Regular", "Barlow-Italic", "Barlow-Medium", "Barlow-SemiBold", "Barlow-Bold",
              "BarlowSemiCondensed-SemiBold", "BarlowSemiCondensed-Bold", "BarlowSemiCondensed-ExtraBold",
              "JetBrainsMonoNL-Regular", "JetBrainsMonoNL-Medium")
STATIC |= {f"fonts/{n}.woff2" for n in FONT_FILES} | {f"fonts/OFL-{n}.txt" for n in ("Barlow", "BarlowSemiCondensed",
                                                                                      "JetBrainsMono")}

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'")

# A client that goes away mid-response must never take the server down.
DISCONNECT = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout)
# Core's ticket store is a plain dict rebuilt on every mint; handler threads run concurrently, so serialise.
_TICKETS = threading.Lock()
_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")


class _Continuations:
    """PERF-1: core's redeem_ticket burns a ticket on first use, but a browser resuming an interrupted
    download asks for the SAME URL again with a Range header. This remembers each redeemed ticket's file
    until the ticket's own five minutes run out OR the file's last byte has been delivered, and answers only
    Range requests with it; a second whole-file GET is always refused, and once a download has completed the
    link is finished (SEC-3), so only an INTERRUPTED download can resume. "The same session" is this app process:
    a download started by an <a> element carries no X-ABX-Token header, so the ticket itself is the binding,
    and a restarted app has an empty store."""

    def __init__(self):
        self.minted = {}                                  # ticket -> expiry (monotonic clock)
        self.redeemed = {}                                # ticket -> (path, expiry)

    def _prune(self):
        now = time.monotonic()
        self.minted = {k: v for k, v in self.minted.items() if v > now}
        self.redeemed = {k: v for k, v in self.redeemed.items() if v[1] > now}

    def mint(self, ticket):
        self._prune()
        self.minted[ticket] = time.monotonic() + TICKET_TTL

    def first_use(self, ticket, path):
        exp = self.minted.pop(ticket, None) or (time.monotonic() + TICKET_TTL)
        self.redeemed[ticket] = (path, exp)

    def done(self, ticket):
        """The file's last byte has been sent: the link is finished, so no range may fetch it again (SEC-3)."""
        self.redeemed.pop(ticket, None)

    def continuation(self, ticket):
        self._prune()
        v = self.redeemed.get(ticket)
        return v[0] if v else None


def _download_name(path):
    """PERF-1: offer '<run folder>.<full suffix>' (20260930T101500Z_Okta-3f2a.ndjson.gz), not the generic
    export.ndjson.gz that every run shares. splitext would keep only '.gz'."""
    base = os.path.basename(path)
    folder = os.path.basename(os.path.dirname(path))
    suffix = base.split(".", 1)[1] if "." in base else ""
    return (folder + "." + suffix) if (folder and suffix) else (folder or base)


def _one_range(header, size):
    """A single 'bytes=N-' or 'bytes=N-M' range as (start, end inclusive); None = ignore it and send the whole
    file (multi-range, suffix '-N' and anything else); 'unsatisfiable' when N is past the end."""
    m = _RANGE_RE.match((header or "").strip())
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size:
        return "unsatisfiable"
    if end < start:
        return None                                        # RFC 9110: an invalid range is ignored
    return start, min(end, size - 1)


def _hostname(host):
    """The host part of a Host header, lower-cased, without the port ('' when malformed)."""
    h = (host or "").strip().lower()
    if not h:
        return ""
    if h.startswith("["):                               # [::1]:port — not served, but parse it properly
        end = h.find("]")
        return h[1:end] if end > 0 else ""
    name, sep, port = h.rpartition(":")
    if not sep:
        return h
    return name if port.isdigit() or port == "" else ""


class _Auth:
    """The launch code (single use) and the session tokens it was exchanged for."""

    def __init__(self):
        self.lock = threading.Lock()
        self.code = None
        self.tokens = []                                # newest last; a few tabs at most
        self.hosts = set()                              # the one extra host name the app is reached at, if any
        self.origins = set()                            # and its (scheme, host)
        self.on_relink: Optional[Callable[[], None]] = None   # set when there is no terminal to press Enter in
        self._relinked = 0.0

    def current_code(self):
        """The unused launch code, or a new one. Asking again never cancels a link that was just printed."""
        with self.lock:
            if not self.code:
                self.code = secrets.token_urlsafe(24)
            return self.code

    def relink(self):
        """Print the launch link to the app's own output again, at most once every RELINK_EVERY seconds. True when
        this app has no terminal and the link is in its log; False when the person presses Enter instead."""
        if self.on_relink is None:
            return False
        with self.lock:
            now = time.monotonic()
            due = now - self._relinked >= RELINK_EVERY
            if due:
                self._relinked = now
        if due:
            self.on_relink()
        return True

    def new_code(self):
        with self.lock:
            self.code = secrets.token_urlsafe(24)
            return self.code

    def exchange(self, code):
        with self.lock:
            if not (self.code and isinstance(code, str) and secrets.compare_digest(code, self.code)):
                return None
            self.code = None                            # burn it: the link works once
            tok = secrets.token_urlsafe(32)
            self.tokens = (self.tokens + [tok])[-16:]
            return tok

    def valid(self, tok):
        if not isinstance(tok, str) or not tok:
            return False
        with self.lock:
            return any(secrets.compare_digest(tok, t) for t in self.tokens)


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    # SO_REUSEADDR on Windows lets a second program bind a port that is already in use, silently.
    allow_reuse_address = os.name != "nt"

    def __init__(self, *a, **kw):
        self._slots = threading.BoundedSemaphore(MAX_HANDLERS)
        HTTPServer.__init__(self, *a, **kw)

    def process_request(self, request, client_address):
        """SEC-14: a handler thread only when a slot is free; otherwise a fast 503 from this thread."""
        if not self._slots.acquire(True, SLOT_WAIT):
            return self._refuse(request)
        try:
            ThreadingMixIn.process_request(self, request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            ThreadingMixIn.process_request_thread(self, request, client_address)
        finally:
            self._slots.release()

    def _refuse(self, request):
        body = json.dumps({"error": "The app is busy with other requests. Try again in a moment.",
                           "kind": "overloaded"}).encode("utf-8")
        head = ("HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json; charset=utf-8\r\n"
                "Content-Length: %d\r\nRetry-After: 1\r\nCache-Control: no-store\r\n"
                "X-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n" % len(body)).encode("ascii")
        try:
            request.settimeout(1.0)
            request.sendall(head + body)
            request.shutdown(socket.SHUT_WR)
            request.setblocking(False)
            try:                                          # drain what already arrived, so close() sends FIN, not RST
                while request.recv(65536):
                    pass
            except (BlockingIOError, OSError):
                pass
        except OSError:
            pass
        finally:
            try:
                request.close()
            except OSError:
                pass

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        if self.allow_reuse_address and hasattr(socket, "SO_REUSEADDR"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name, self.server_port = str(self.server_address[0]), int(self.server_address[1])   # no DNS lookup

    def handle_error(self, request, client_address):
        ex = sys.exc_info()[1]
        if isinstance(ex, DISCONNECT):
            return                                      # the browser went away; nothing to report
        if os.environ.get("ABX_WEB_DEBUG"):
            traceback.print_exc()
        else:
            sys.stderr.write(f"  web: a request failed ({type(ex).__name__}); the app is still running\n")


_NO_ROUTE = object()


def _text(b, key, limit=20000):
    v = b.get(key)
    if not isinstance(v, str) or not v.strip():
        raise E.UserError({"kql": "type or paste a KQL query first", "text": "describe what you are looking for first"}
                          .get(key, f"{key} is required"))
    if len(v) > limit:
        raise E.UserError(f"that is too long (more than {limit:,} characters)")
    return v


def _time(b):
    t = b.get("time")
    if not isinstance(t, dict):
        return {"last": "1h"}
    if t.get("start") or t.get("end"):
        return {"start": str(t.get("start") or ""), "end": str(t.get("end") or "")}
    return {"last": str(t.get("last") or "1h")}


def _rows(v, default):
    try:
        return max(1, min(int(v), 10000))
    except (TypeError, ValueError):
        return default


def _op(b):
    op = b.get("op")
    if op not in ("create", "update", "delete"):
        raise E.UserError("choose create, update or delete")
    return op


def _spec_kql(s, spec):
    """The builder's search written as KQL: Abstract's own translation where the tenant offers it, else the tool's."""
    import abx_kql as K
    import abx_platform as P
    s.require()
    if spec["mode"] == "kql":
        return {"kql": spec.get("kql") or "", "source": "kql"}
    if spec["mode"] != "simple" or not (spec["dataset"] or spec["filters"] or spec["any_groups"]):
        return {"kql": "", "source": None}
    cond = C.build_condition(spec)
    r = P.server_from_conditions(s.client, cond)
    if r and r.get("kql"):
        return {"kql": r["kql"], "source": "abstract"}
    try:
        return {"kql": K.condition_to_kql(cond), "source": "tool"}
    except Exception as ex:                            # a condition with no exact KQL: say so, never guess
        return {"kql": "", "source": None, "reason": str(ex) or (r or {}).get("refusal")}


class _BadRequest(Exception):
    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code = code


def _decorate_exports(rows):
    for x in rows:
        x["merged_path"] = os.path.join(x["run_dir"], x["merged_file"]) if x.get("merged_file") else None
    return rows


def _job(session):
    """job_status() plus merged_path. A finished job's run_dir is reported only if the folder still exists: core
    deletes a run folder that ended before any plan was written (for example a filter that failed its proof)."""
    j = session.job_status()
    if j and j.get("result"):
        res = dict(j["result"])
        rd = res.get("run_dir") or j.get("run_dir")
        rd = rd if rd and os.path.isdir(rd) else None
        mf = res.get("merged_file")
        mp = os.path.join(rd, mf) if (rd and mf) else None
        res["run_dir"], res["merged_path"] = rd, (mp if mp and os.path.isfile(mp) else None)
        j = dict(j, result=res)
    return j


STILL_RUNNING = "That export is still running. Stop it first, then delete it."
# On Windows core cannot probe a .lock's pid, so a stale lock also lists as RUNNING: say how to clear it.
STALE_LOCK_NT = ("This export looks like it is still running. If no export is running, delete the .lock file in "
                 "its folder and try again.")


def _export_delete(session, run_dir, windows=None):
    """UX-44: delete one finished, failed or planned export folder. Core's _inside_home realpath rule, plus:
    never the exports folder itself, only its direct children, only folders that hold a manifest (so
    saved-searches/ and anything else in the folder cannot be removed this way), and never a RUNNING export
    (list_exports' own definition: this session's job, or a live .lock from another process). The .lock is
    never removed here: on Windows a RUNNING that is not this session's job may be a stale lock, and the
    message says how to clear it by hand."""
    windows = (os.name == "nt") if windows is None else windows
    p = C._inside_home(run_dir, want_dir=True)
    root = os.path.realpath(C.home_dir())
    if p == root or os.path.dirname(p) != root:
        raise E.UserError("only an export folder directly inside your exports folder can be deleted")
    lock = getattr(session, "_job_lock", None) or threading.Lock()
    with lock:                                          # no export can start in this folder while we check
        job = session.job if session.busy() else None
        if job and job.get("run_dir") and os.path.realpath(job["run_dir"]) == p:
            raise C.Busy(STILL_RUNNING)                 # this session's export, even before its manifest exists
        lock_alive = getattr(C, "_lock_alive", lambda d: False)
        if lock_alive(p):
            raise C.Busy(STALE_LOCK_NT if windows else STILL_RUNNING)
        if not os.path.isfile(os.path.join(p, "manifest.json")):
            raise E.UserError("that folder is not an export (it has no manifest.json)")
        row = next((x for x in C.list_exports(session) if os.path.realpath(x["run_dir"]) == p), None)
        if row is None:
            raise E.UserError("that folder is not in the exports list")
        if row.get("status") == "RUNNING":
            raise C.Busy(STALE_LOCK_NT if windows else STILL_RUNNING)
        shutil.rmtree(p)
    return p


def _handler(session, auth, initial_spec):
    conts = _Continuations()

    class H(BaseHTTPRequestHandler):
        server_version = "abx"
        sys_version = ""
        timeout = SOCKET_TIMEOUT                         # StreamRequestHandler applies it to the socket

        def log_message(self, format, *args):           # quiet by default; never logs query strings
            if os.environ.get("ABX_WEB_DEBUG"):
                sys.stderr.write("web: " + format % args + "\n")

        def handle(self):
            try:
                BaseHTTPRequestHandler.handle(self)
            except DISCONNECT:
                self.close_connection = True

        def finish(self):
            try:
                BaseHTTPRequestHandler.finish(self)
            except DISCONNECT + (OSError,):
                pass

        # -- responses --
        def _headers(self, code, ctype, length, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            if isinstance(body, bytes):
                data = body
            elif isinstance(body, str):
                data = body.encode("utf-8")
            else:
                data = json.dumps(body, default=str).encode("utf-8")
            try:
                self._headers(code, ctype, len(data), extra)
                if self.command != "HEAD":
                    self.wfile.write(data)
            except DISCONNECT:
                self.close_connection = True

        def _err(self, code, msg, kind="user", **more):
            d = {"error": msg, "kind": kind}
            d.update(more)
            self._send(code, d)

        def _send_file(self, path, allow_full=True):
            """Stream a file in 1 MB chunks: memory stays flat whatever the export size (measured: reading a
            1.5 GB file whole took the server to 1.24 GB RSS). PERF-1: one 'bytes=N-' / 'bytes=N-M' range is
            answered with 206, so an interrupted browser download resumes instead of restarting."""
            try:
                fh = open(path, "rb")
            except OSError:
                return self._send(404, "That file is no longer in the exports folder.", "text/plain; charset=utf-8")
            with fh:
                st = os.fstat(fh.fileno())
                size = st.st_size
                etag = '"%x-%x"' % (size, int(st.st_mtime))
                name = _download_name(path)
                ascii_name = "".join(ch if (32 < ord(ch) < 127 and ch not in '"\\;') else "_" for ch in name) or "export"
                disp = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{urllib.parse.quote(name)}'
                extra = {"Content-Disposition": disp, "Accept-Ranges": "bytes", "ETag": etag,
                         "Last-Modified": email.utils.formatdate(st.st_mtime, usegmt=True)}
                rng = _one_range(self.headers.get("Range"), size) if self.headers.get("Range") else None
                if_range = self.headers.get("If-Range")
                if rng and if_range and if_range.strip() not in (etag, extra["Last-Modified"]):
                    rng = None                              # the file changed since the first part: send it whole
                if rng == "unsatisfiable":
                    return self._send(416, b"", "application/octet-stream", {"Content-Range": f"bytes */{size}"})
                if rng is None and not allow_full:
                    return self._send(410, "This download link has already been used. Choose Download again.",
                                      "text/plain; charset=utf-8")
                start, end = rng if rng else (0, size - 1)
                length = max(0, end - start + 1)
                if rng:
                    extra["Content-Range"] = f"bytes {start}-{end}/{size}"
                try:
                    self.connection.settimeout(300)      # a paused browser download must not die at the 30s request timeout
                    self._headers(206 if rng else 200, "application/octet-stream", length, extra)
                    if self.command == "HEAD":
                        return
                    fh.seek(start)
                    left = length
                    while left > 0:
                        chunk = fh.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                    return left == 0 and end == size - 1  # True: the whole file has now been delivered
                except DISCONNECT:
                    self.close_connection = True         # the browser cancelled the download
                    return False

        # -- guards --
        def _host_ok(self):
            h = _hostname(self.headers.get("Host"))
            return h in ALLOWED_HOSTS or h in auth.hosts

        def _origin_ok(self):
            o = self.headers.get("Origin")
            if o is None:
                return True
            u = urllib.parse.urlparse(o)
            host = (u.hostname or "").lower()
            return (u.scheme == "http" and host in ALLOWED_HOSTS) or (u.scheme, host) in auth.origins

        def _authed(self):
            return auth.valid(self.headers.get("X-ABX-Token"))

        def _body(self, limit=None):
            if self.headers.get("Transfer-Encoding"):
                raise _BadRequest(400, "chunked request bodies are not accepted")
            raw_len = self.headers.get("Content-Length")
            if raw_len is None or raw_len.strip() == "":
                n = 0
            else:
                s = raw_len.strip()
                if not s.isdigit():                      # rejects negatives, signs, spaces and non-numbers
                    raise _BadRequest(400, "invalid Content-Length")
                n = int(s)
                if n > (limit or MAX_BODY):
                    raise _BadRequest(413, "request too large")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if n and ctype != "application/json":
                raise _BadRequest(415, "requests must be JSON")
            raw = self.rfile.read(n) if n else b""
            if len(raw) != n:
                raise _BadRequest(400, "the request body was cut short")
            if not raw:
                return {}
            try:
                b = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise _BadRequest(400, "request body is not JSON")
            if not isinstance(b, dict):
                raise _BadRequest(400, "request body must be a JSON object")
            return b

        # -- routing --
        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

        def _route(self, method):
            try:
                if not self._host_ok():
                    self.close_connection = True
                    return self._err(403, "bad host", "host")
                if method != "GET" and not self._origin_ok():
                    self.close_connection = True
                    return self._err(403, "cross-origin request refused", "origin")
                u = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(u.query)
                path = u.path
                if method == "GET" and path == "/healthz":   # is the app up? for a container platform: says nothing else
                    return self._send(200, {"ok": True, "version": C.VERSION})
                if method == "GET" and not path.startswith("/api/"):
                    return self._static(path)
                if not path.startswith("/api/"):
                    return self._err(404, "not found", "route")
                name = path[5:]
                if name == "session" and method == "POST":
                    tok = auth.exchange(self._body().get("code"))
                    if not tok:
                        return self._err(403, "This link has already been used or is out of date." if auth.on_relink
                                         else "This link has already been used or is out of date. In the terminal "
                                              "running the app, press Enter to get a new link.", "code")
                    return self._send(200, {"token": tok})
                if name == "relink" and method == "POST":    # no secret in the answer: the link goes to the app's log
                    self._body()
                    return self._send(200, {"printed": auth.relink()})
                if name == "download" and method == "GET":
                    return self._download(qs)                # the single-use ticket is the authorisation
                if not self._authed():
                    return self._err(403, "This page is not signed in to the app, or the app was restarted. Open "
                                          "the link printed in the terminal.", "token")
                return self._api(method, name, qs)
            except _BadRequest as ex:
                self.close_connection = True                 # an unread body must not become the next request
                return self._err(ex.code, str(ex), "request")
            except C.Busy as ex:
                return self._err(409, C.humanize(ex), "busy")
            except C.SavedExists as ex:
                return self._err(409, C.humanize(ex), "exists")
            except E.UserError as ex:
                E.log_error("ERROR", f"browser app: {C.humanize(ex)}")
                return self._err(400, C.humanize(ex), "user")
            except E.Cancelled as ex:
                return self._err(400, C.humanize(ex) or "stopped", "user")
            except (E.Fatal, E.Retryable) as ex:
                E.log_error("STOPPED", f"browser app: {C.humanize(ex)}")
                return self._err(502, C.humanize(ex), "platform")
            except DISCONNECT:
                self.close_connection = True
            except OSError as ex:
                E.log_error("ERROR", f"browser app: a file could not be read or written: {C.humanize(ex)}")
                return self._err(500, "A file could not be read or written: " + C.humanize(ex), "os")
            except ValueError as ex:
                E.log_error("ERROR", f"browser app: {C.humanize(ex)}")
                return self._err(400, C.humanize(ex), "user")
            except Exception as ex:                          # never leak a traceback to the page
                traceback.print_exc()
                path = E.log_error("FAILED", f"browser app: {type(ex).__name__}: {ex}", ex)
                return self._err(500, f"Something went wrong in the tool itself ({type(ex).__name__}). The details "
                                      f"are in the log: {path}", "internal")

        def _static(self, path):
            if path in ("/", "/index.html"):
                fn = "index.html"
            elif path == "/favicon.ico":
                fn = "logo-mark.svg"
            elif path.startswith("/static/") and path[len("/static/"):] in STATIC:
                fn = path[len("/static/"):]
            else:
                return self._send(404, "not found", "text/plain; charset=utf-8")
            with open(os.path.join(WEB_DIR, fn), "rb") as fh:
                data = fh.read()
            return self._send(200, data, MIME.get(os.path.splitext(fn)[1].lower(), "application/octet-stream"))

        def _download(self, qs):
            ticket = (qs.get("ticket") or [""])[0]
            p: Any = None
            with _TICKETS:
                cont = conts.continuation(ticket) if ticket else None
                if cont is None:
                    try:
                        p = session.redeem_ticket(ticket)
                    except E.UserError as ex:
                        return self._send(410, C.humanize(ex), "text/plain; charset=utf-8")
                    conts.first_use(ticket, p)
            if cont is not None:
                try:                                     # a continuation re-checks the exports-folder rule
                    p = C._inside_home(cont, want_file=True)
                except E.UserError as ex:
                    return self._send(410, C.humanize(ex), "text/plain; charset=utf-8")
            # Range only on a continuation: a used link never serves the whole file again. Once the last byte has
            # gone out, the link is finished and even a range is refused (only an interrupted download resumes).
            finished = self._send_file(p, allow_full=cont is None)
            if finished is True and self.command != "HEAD":
                with _TICKETS:
                    conts.done(ticket)
            return None

        def _spec(self, b):
            spec = C.normalize_spec(b.get("spec"))
            if spec["mode"] == "advanced":
                raise E.UserError("This saved search uses an advanced condition, which runs only from the command "
                                  "line (see Help, Command line and automation).")
            return spec

        def _api(self, method, name, qs):
            s = session
            if name == "state" and method == "GET":
                return self._send(200, {
                    "app": C.APP_NAME, "version": C.VERSION, "tenant": s.tenant, "settings": C.load_settings(),
                    "home": C.home_dir(), "sep": os.sep, "prod_url": C.PROD_URL,
                    "operators": {k: [{"op": o, "label": l, "list": x} for o, l, x in v] for k, v in C.OPERATORS.items()},
                    "case_ops": sorted(C.CASE_OPS), "not_ops": sorted(C.NOT_OPS),
                    "type_labels": C.TYPE_LABELS, "presets": C.TIME_PRESETS, "default_fields": C.DEFAULT_FIELDS,
                    "empty_spec": C.empty_spec(),
                    "initial_spec": C.normalize_spec(initial_spec) if initial_spec else None,
                    "help": [{"path": p, "title": t} for p, t in C.HELP_TOPICS], "job": _job(s),
                    "log_file": E.log_file()})
            if name == "job" and method == "GET":
                return self._send(200, {"job": _job(s)})
            if name == "api-calls" and method == "GET":
                # The platform calls this app has made (never headers or response bodies; secrets redacted), for the
                # API calls tab. The page builds the matching curl command with the key as a variable, never a value.
                try:
                    since = int((qs.get("since") or ["0"])[0])
                except ValueError:
                    since = 0
                with E._TRACE_LOCK:
                    calls = [c for c in E.API_TRACE if c["n"] > since]
                t = s.tenant or {}
                return self._send(200, {"calls": calls[-500:], "base_url": t.get("base_url") or C.default_base_url(),
                                        "vendor_account_id": t.get("vendor_account_id"),
                                        "log_file": E.log_file(), "last": E.API_SEQ[0]})
            if name == "problems" and method == "POST":
                b = self._body()
                spec = C.normalize_spec(b.get("spec"))
                if b.get("format"):
                    spec["format"] = b["format"]
                return self._send(200, {"problems": C.spec_problems(spec), "describe": C.describe_spec(spec)})
            if name == "command" and method == "POST":
                b = self._body()
                spec = C.normalize_spec(b.get("spec"))
                if b.get("format"):
                    spec["format"] = b["format"]
                return self._send(200, C.command_line(spec, (s.tenant or {}).get("vendor_account_id")))
            if name == "capabilities" and method == "GET":
                return self._send(200, s.capabilities(refresh=bool(qs.get("refresh"))))
            if name == "views" and method == "GET":
                vs = s.views(refresh=True)
                return self._send(200, {"views": [{"id": v["id"], "name": v["name"], "origin": v.get("origin", "kql"),
                                                   "problem": v.get("problem"), "time": v.get("time"),
                                                   "columns": len(v.get("fields") or [])} for v in vs],
                                        "without_search": getattr(s, "views_without_search", 0),
                                        "from_filters": getattr(s, "views_from_filters", 0)})
            if name == "views-sync" and method == "POST":
                saved = C.sync_views(s)
                return self._send(200, {"saved": saved, "count": len(saved)})
            if name == "import" and method == "POST":
                b = self._body()
                r = s.import_search(text=b.get("text"), view_id=b.get("view_id"))
                return self._send(200, r)
            if name == "connect" and method == "POST":
                b = self._body()
                t = s.connect(b.get("base_url"), b.get("vendor_account_id"), b.get("api_key"))
                return self._send(200, {"tenant": t})
            if name == "disconnect" and method == "POST":
                s.disconnect()
                return self._send(200, {"ok": True})
            if name == "datasets" and method == "GET":
                refresh = bool(qs.get("refresh"))
                if refresh and s.busy():                     # PERF-14: a refresh searches; the cached list does not
                    raise C.Busy("An export is running. The dataset list refreshes after it finishes.")
                return self._send(200, {"datasets": s.datasets(refresh=refresh)})
            if name == "fields" and method == "GET":
                return self._send(200, {"fields": s.fields()})
            if name == "dataset-fields" and method == "GET":
                return self._send(200, s.dataset_fields((qs.get("dataset") or [""])[0], refresh=bool(qs.get("refresh"))))
            if name == "field-type" and method == "GET":
                f = (qs.get("field") or [""])[0]
                if f not in s.fields() and not (f.startswith("ext.") and len(f) > 4):    # ext.* is not in the catalog
                    raise E.UserError(f"'{f}' is not a field on your tenant")
                # PERF-14: core answers a cached type even during an export and raises Busy (-> 409) only
                # when it would have to search. Called once: a failed lookup is not cached (CQ-27).
                t = s.field_type(f)
                return self._send(200, {"field": f, "type": t, "label": C.type_label(t)})
            if name == "preview" and method == "POST":
                return self._send(200, s.preview(self._spec(self._body()), 50))
            if name == "validate" and method == "POST":
                b = self._body()
                spec = self._spec(b)
                if b.get("format"):
                    spec["format"] = b["format"]
                try:
                    return self._send(200, s.validate(spec))
                except E.Cancelled:                          # PERF-8 / UX-16: Stop in the page
                    return self._send(200, {"cancelled": True, "ok": False, "ready": False, "checks": [],
                                            "plan": None, "message": "Validation stopped"})
            if name == "validate-cancel" and method == "POST":
                if not hasattr(s, "cancel_validate"):        # an older core: nothing to stop with
                    return self._send(200, {"cancelled": False, "supported": False})
                stopped = s.cancel_validate()                # core: True when a validate was running
                return self._send(200, {"cancelled": True if stopped is None else bool(stopped), "supported": True})
            if name == "export-delete" and method == "POST":
                return self._send(200, {"deleted": _export_delete(s, self._body().get("run_dir"))})
            if name == "export" and method == "POST":
                b = self._body()
                raw = b.get("options")
                opts = raw if isinstance(raw, dict) else {}
                opts = {"format": opts.get("format"), "merge": bool(opts.get("merge", True)), "keep_parts": False}
                s.start_export(self._spec(b), opts)
                return self._send(200, {"job": _job(s)})
            if name == "resume" and method == "POST":
                s.resume(self._body().get("run_dir"))
                return self._send(200, {"job": _job(s)})
            if name == "cancel" and method == "POST":
                return self._send(200, {"cancelled": s.cancel()})
            if name == "exports" and method == "GET":
                return self._send(200, {"exports": _decorate_exports(C.list_exports(s)), "home": C.home_dir()})
            if name == "open" and method == "POST":
                try:
                    p = C.open_folder(self._body().get("path") or C.home_dir())
                except C.Headless as ex:                           # no file manager: say where it is, as information
                    return self._send(200, {"ok": True, "info": C.sanitize(str(ex)), "path": C.sanitize(ex.path)})
                return self._send(200, {"ok": True, "path": p})
            if name == "ticket" and method == "POST":
                path = self._body().get("path")
                with _TICKETS:
                    t = s.download_ticket(path)
                    conts.mint(t)
                return self._send(200, {"url": "/api/download?ticket=" + urllib.parse.quote(t)})
            if name == "saved":
                if method == "GET":
                    return self._send(200, {"saved": C.saved_list()})
                if method == "POST":
                    b = self._body()
                    if b.get("load"):
                        return self._send(200, {"spec": C.saved_load(b["load"])})
                    return self._send(200, {"file": C.saved_save(b.get("spec"), overwrite=bool(b.get("overwrite")))})
                if method == "DELETE":
                    return self._send(200, {"deleted": C.saved_delete((qs.get("file") or [""])[0])})
            if name == "help" and method == "GET":
                return self._send(200, {"text": C.help_text((qs.get("path") or [""])[0])})
            r = self._api_platform(s, method, name, qs)
            if r is not _NO_ROUTE:
                return r
            return self._err(404, "no such API", "route")

        def _api_platform(self, s, method, name, qs):
            """KQL, plain English, field values, fieldsets, saved-view changes and the endpoint list. Every change to
            the tenant is two calls: a plan (read only, returns the exact change and a single-use token) and an
            apply with that token."""
            q1 = lambda k: (qs.get(k) or [""])[0]
            if name == "kql-check" and method == "POST":
                return self._send(200, s.kql_check(_text(self._body(), "kql")))
            if name == "nl" and method == "POST":
                return self._send(200, s.nl_to_kql(_text(self._body(), "text")))
            if name == "kql-preview" and method == "POST":
                b = self._body()
                return self._send(200, s.kql_preview(_text(b, "kql"), _time(b), _rows(b.get("rows"), 50)))
            if name == "kql-save" and method == "POST":
                b = self._body()
                fmt_ = b.get("format") if b.get("format") in ("csv", "json") else "csv"
                return self._send(200, s.kql_result_export(_text(b, "kql"), _time(b), fmt_, str(b.get("name") or "")[:80]))
            if name == "spec-kql" and method == "POST":
                return self._send(200, _spec_kql(s, C.normalize_spec(self._body().get("spec"))))
            if name == "field-catalog" and method == "GET":
                return self._send(200, s.field_catalog(q1("dataset")))
            if name == "field-values" and method == "GET":
                f = q1("field").strip()
                if not f:
                    raise E.UserError("name the field first")
                return self._send(200, s.field_values(f, q1("prefix"), q1("dataset")))
            if name == "fieldsets" and method == "GET":
                return self._send(200, {"fieldsets": s.fieldsets()})
            if name == "fieldset" and method == "GET":
                return self._send(200, s.fieldset(q1("id")))
            if name == "fieldset-plan" and method == "POST":
                b = self._body()
                data = b.get("data") if isinstance(b.get("data"), dict) else None
                return self._send(200, s.fieldset_plan(_op(b), data, b.get("id") or None))
            if name == "view-plan" and method == "POST":
                b = self._body()
                spec = b.get("spec") if isinstance(b.get("spec"), dict) else None
                fs = b.get("fieldset_ids")
                fs = [str(x) for x in fs] if isinstance(fs, list) else None
                return self._send(200, s.view_plan(_op(b), spec, b.get("id") or None, b.get("name") or None, fs))
            if name in ("fieldset-apply", "view-apply") and method == "POST":
                tok = self._body().get("token")
                if not tok:
                    raise E.UserError("review the change first: there is nothing to confirm")
                want = "fieldset" if name == "fieldset-apply" else "view"
                pending = getattr(getattr(s, "_plans", None), "_p", {}).get(str(tok))
                if pending is not None and pending.get("kind") != want:     # checked before the token is used up
                    raise E.UserError(f"that review is for a {pending.get('kind')}, not a {want}")
                return self._send(200, s.apply_plan(tok))
            if name == "endpoints" and method == "GET":
                return self._send(200, {"endpoints": s.endpoints()})
            r = self._api_content(s, method, name, qs)
            if r is not _NO_ROUTE:
                return r
            return _NO_ROUTE

        def _api_content(self, s, method, name, qs):
            """Saved views and fieldsets as code, and automation recipes. A bundle comes and goes as one JSON document
            (abstract.content/v1); applying it is a plan with one single-use token, like any other change."""
            import abx_content as CT
            import abx_automate as AU
            q1 = lambda k: (qs.get(k) or [""])[0]
            names = lambda k: [x.strip() for x in q1(k).split(",") if x.strip()] if q1(k) else None
            if name == "content-export" and method == "GET":
                views, fsets = names("views"), names("fieldsets")
                kind = q1("kind") or None
                if views is not None and fsets is None and kind is None:
                    fsets = []
                doc = s.content_document(views, fsets, kind)
                w = CT.write_folder(doc, CT.default_dir(s))
                return self._send(200, {"bundle": doc, "dir": w["dir"], "counts": w["counts"]})
            if name in ("content-diff", "content-plan") and method == "POST":
                b = self._body(limit=CT.MAX_BUNDLE_BYTES + 65536)
                doc = b.get("bundle")
                if not isinstance(doc, dict):
                    raise E.UserError("send the bundle as {\"bundle\": {...}}")
                CT.validate(doc)
                if name == "content-diff":
                    return self._send(200, {"diff": s.content_diff(doc)})
                return self._send(200, s.content_plan(doc, bool(b.get("prune"))))
            if name == "content-apply" and method == "POST":
                tok = self._body().get("token")
                if not tok:
                    raise E.UserError("review the bundle first: there is nothing to confirm")
                pending = getattr(getattr(s, "_plans", None), "_p", {}).get(str(tok))
                if pending is not None and pending.get("kind") != "bundle":
                    raise E.UserError(f"that review is for a {pending.get('kind')}, not a bundle")
                return self._send(200, s.apply_plan(tok))
            if name == "automation" and method == "POST":
                b = self._body()
                spec = b.get("spec") if isinstance(b.get("spec"), dict) else None
                if spec is None:
                    raise E.UserError("send the search to schedule as {\"spec\": {...}}")
                o = b.get("options")
                o = o if isinstance(o, dict) else {}
                vid = o.get("vendor_account_id") or (s.tenant or {}).get("vendor_account_id")
                r = AU.recipe(spec, str(b.get("target") or ""), vid, (s.tenant or {}).get("base_url"),
                              str(o.get("every") or "1d"), str(o.get("at") or "00:20"), o.get("job") or None,
                              o.get("app") or None, o.get("folder") or None)
                if o.get("save"):
                    import abx_core as C2
                    d = os.path.join(C2.home_dir(), "automation", f"{r['job']}-{r['target']}")
                    r["written"] = AU.write(r, d)
                    r["dir"] = d
                return self._send(200, r)
            return _NO_ROUTE

    return H


def _no_browser_reason():
    """Why a browser cannot be opened here (None = try it)."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return "this is an SSH session"
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "no desktop display was found"
    return None


def public_address(port, env=None):
    """Where a browser reaches the app when that is not http://127.0.0.1:PORT/: ABX_WEB_URL says so (a container
    whose port is published on another number, or an address in front of it). Returns (base url ending in /, scheme,
    host) or None. https only, except this computer."""
    env = os.environ if env is None else env
    url = (env.get("ABX_WEB_URL") or "").strip()
    if not url:
        return None
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host or u.username or u.password or u.query or u.fragment or \
            u.path not in ("", "/"):
        raise ValueError("ABX_WEB_URL must be an address like https://name.example/ or http://localhost:9000/")
    if u.scheme == "http" and host not in LOOPBACK:
        raise ValueError("ABX_WEB_URL must start with https:// unless it is this computer (http://localhost:PORT/)")
    return f"{u.scheme}://{u.netloc}/", u.scheme, host


def _bind(port, handler, bind="127.0.0.1"):
    """A server on 127.0.0.1 (or the bind address a container asks for). port 0 = any free port; an explicit port
    that is taken tries the next few."""
    given = port
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        port = -1
    if port < 0 or port > 65535:
        print(f"\n  {given} is not a valid port. Use a number from 0 to 65535 (0 picks any free port), or leave --port out.\n")
        return None
    tries = [0] if port == 0 else [p for p in range(port, port + PORT_TRIES) if p <= 65535]
    for p in tries:
        try:
            srv = _Server((bind, p), handler)
        except OSError as ex:
            if ex.errno in (errno.EADDRINUSE, errno.EACCES, getattr(errno, "WSAEADDRINUSE", -1),
                            getattr(errno, "WSAEACCES", -1), 10048, 10013):
                continue
            print(f"\n  The browser app could not start: {ex}\n")
            return None
        if port and p != port:
            print(f"\n  Port {port} is in use, so the app is using port {p} instead.")
        return srv
    print(f"\n  Ports {tries[0]} to {tries[-1]} are all in use by other programs. Close the program using them, or "
          f"start with another port, e.g. --port 8765 (or leave --port out to pick a free one).\n")
    return None


def _print_link(url, first, reason, headless=False):
    if first:
        print("\n  Abstract Search & Export is running. Open this link in your browser:\n")
    elif headless:
        print("\n  Your link:\n")
    else:
        print("\n  New link (the previous one no longer works):\n")
    print(f"    {url}\n")
    if headless:
        base = url.split("?", 1)[0]
        print(f"  The link works once. Closed the page? Open {base} and a new link is written here.")
        print("  The app keeps running until it is stopped.\n")
        sys.stdout.flush()
        return
    print("  The link works once. Keep this window open while you use the app, and while an export runs.")
    if sys.stdin is not None and _isatty(sys.stdin):
        print("  Closed the page? Press Enter here for a new link. Press Ctrl+C to stop the app.\n")
    else:
        print("  Press Ctrl+C to stop the app.\n")
    if reason and first:
        port = urllib.parse.urlparse(url).port
        print(f"  The browser was not opened because {reason}. Open the link in a browser on this computer.")
        if "SSH" in reason:
            print(f"  From your own computer, forward the port first:  ssh -L {port}:127.0.0.1:{port} <this machine>")
            print("  then open the link there. (Any local port works.)")
        print()
    sys.stdout.flush()


def _isatty(f):
    try:
        return f.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _open_browser(url):
    try:
        ok = webbrowser.open(url)
    except Exception:                                      # webbrowser raises many things on odd systems
        ok = False
    if not ok:
        print("  Could not open a browser automatically. Open the link above yourself.\n")
        sys.stdout.flush()


def _wait_keys(on_enter):
    """Block until Ctrl+C. Enter prints a fresh launch link. Reads keys on THIS thread only, so nothing is
    left reading the terminal after the app stops (the main menu gets its keys back)."""
    stdin = sys.stdin
    if stdin is None or not _isatty(stdin):
        while True:
            time.sleep(1)
    if os.name == "nt":
        import msvcrt
        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    on_enter()
            time.sleep(0.1)
    fd = stdin.fileno()
    while True:
        r, _, _ = select.select([fd], [], [], 0.5)
        if not r:
            continue
        data = os.read(fd, 1024)
        if not data:                                       # end of input: just keep serving
            while True:
                time.sleep(1)
        if b"\n" in data or b"\r" in data:
            on_enter()


def serve(session=None, spec=None, port=0, open_browser=True, bind=None):
    session = session or C.Session()
    if session.tenant is None:
        try:
            session.connect_from_env()
        except (E.UserError, E.Fatal, E.Retryable, OSError, ValueError) as ex:
            print(f"  The environment credentials did not work: {C.humanize(ex)}")
    auth = _Auth()
    bind = (bind or os.environ.get("ABX_WEB_BIND") or "127.0.0.1").strip()
    httpd = _bind(port, _handler(session, auth, spec), bind)
    if httpd is None:
        return None
    base = f"http://127.0.0.1:{httpd.server_address[1]}/"
    try:
        public = public_address(httpd.server_address[1])
    except ValueError as ex:
        httpd.server_close()
        print(f"\n  {ex}\n")
        return None
    if public:
        base = public[0]
        auth.hosts.add(public[2])
        auth.origins.add((public[1], public[2]))
    headless = sys.stdin is None or not _isatty(sys.stdin)

    def link():
        return base + "?code=" + auth.new_code()

    if headless:                                           # no Enter key: the page asks, the link goes to the log
        auth.on_relink = lambda: _print_link(base + "?code=" + auth.current_code(), False, None, True)
    url = link()
    reason = _no_browser_reason()
    if bind not in LOOPBACK:
        print(f"\n  Listening on {bind}, for a container. Publish the port to this computer only "
              f"(for example -p 127.0.0.1:{httpd.server_address[1]}:{httpd.server_address[1]}).")
        reason = None
        open_browser = False
    _print_link(url, True, reason, headless)
    if open_browser and not reason:
        threading.Timer(0.6, _open_browser, args=(url,)).start()
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True, name="abx-web")
    t.start()
    try:
        _wait_keys(lambda: _print_link(link(), False, None))
    finally:
        httpd.shutdown()
        httpd.server_close()
    return base
