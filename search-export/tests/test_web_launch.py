"""The browser app's 0.4.0 look: the launch-mode assets are served under the same strict CSP, the page loads them,
nothing inline is added, the official mark is shipped unedited, and the terminal's launch mode stays silent where it
cannot show."""
import http.client
import io
import os
import re
import threading
import unittest
from contextlib import redirect_stdout

from support import C, EngineCase, KEY

import abx_ui as U
import abx_web as W

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'")
LAUNCH = ["rocket-a.js", "rocket-a.css", "rocket-a.svg", "launch.js", "launch.css"]


class Served(EngineCase):
    def setUp(self):
        super().setUp()
        self.session = C.Session()
        self.session.connect("https://api.fake.example", self.tenant.vid, KEY)
        self.httpd = W._bind(0, W._handler(self.session, W._Auth(), None))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request("GET", path, headers={"Host": "127.0.0.1:%d" % self.port})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict((k.lower(), v) for k, v in r.getheaders()), body

    def test_launch_assets_are_served_with_the_strict_csp(self):
        types = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
        for name in LAUNCH:
            st, h, body = self.get("/static/" + name)
            self.assertEqual(st, 200, name)
            self.assertTrue(h["content-type"].startswith(types[os.path.splitext(name)[1]]), (name, h["content-type"]))
            self.assertEqual(h["content-security-policy"], CSP, name)
            self.assertNotIn(KEY.encode(), body)

    def test_the_brand_fonts_ship_and_are_served(self):
        css = open(os.path.join(WEB, "app.css"), encoding="utf-8").read()
        urls = re.findall(r'url\("fonts/([^"]+)"\)', css)
        self.assertGreaterEqual(len(urls), 8)
        for n in urls:
            st, h, body = self.get("/static/fonts/" + n)
            self.assertEqual(st, 200, n)
            self.assertEqual(h["content-type"], "font/woff2", n)
            self.assertEqual(body[:4], b"wOF2", n)
        for fam in ("Barlow", "Barlow Semi Condensed", "JetBrains Mono"):
            self.assertIn('font-family:"%s"' % fam, css)
        for lic in ("OFL-Barlow.txt", "OFL-BarlowSemiCondensed.txt", "OFL-JetBrainsMono.txt"):
            self.assertEqual(self.get("/static/fonts/" + lic)[0], 200, lic)
        # only the exact font names are reachable under fonts/
        self.assertEqual(self.get("/static/fonts/../app.css")[0], 404)
        self.assertEqual(self.get("/static/fonts/Other.woff2")[0], 404)

    def test_the_page_loads_them_and_keeps_the_csp(self):
        st, h, body = self.get("/")
        self.assertEqual(st, 200)
        self.assertEqual(h["content-security-policy"], CSP)
        html = body.decode("utf-8")
        for name in LAUNCH:
            if name.endswith(".svg"):
                continue                                   # fetched by launch.js only when launch mode is on
            self.assertIn("/static/" + name, html)


class PageRules(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(WEB, name), encoding="utf-8") as fh:
            return fh.read()

    def test_no_inline_script_style_or_handler(self):
        html = self.read("index.html")
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", html), "an inline <script>")
        self.assertNotIn("<style", html)
        self.assertIsNone(re.search(r'\sstyle="', html), "an inline style attribute")
        self.assertIsNone(re.search(r"\son[a-z]+=", html), "an inline event handler")

    def test_launch_mode_is_hidden_by_default_and_scoped(self):
        html, css = self.read("index.html"), self.read("launch.css")
        self.assertRegex(html, r'<button id="launch-chip"[^>]*\shidden')
        for rule in re.findall(r"(?m)^([^\s@/}{][^{]*)\{", css):
            sel = rule.strip()
            if sel.startswith((":root[data-launch", "#launch-", ".ra-sprite", "#rtabs", "#rhead", "@keyframes")) or \
                    sel.startswith(("0%", "50%", "100%")):
                continue
            self.fail("launch.css has a rule that applies outside launch mode: " + sel)

    def test_the_official_mark_is_shipped_unedited(self):
        svg = self.read("rocket-a.svg")
        self.assertIn('id="rocket-a-mark"', svg)
        self.assertEqual(svg.count("<path "), 4)
        src = os.environ.get("ABX_BRAND_MARK") or ""      # the brand source file, on a maintainer's machine
        if os.path.isfile(src):
            with open(src, encoding="utf-8") as fh:
                self.assertEqual(svg, fh.read())

    def test_launch_js_writes_no_markup_from_strings(self):
        js = self.read("launch.js")
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("setAttribute(\"style\"", js)

    def test_every_new_control_has_a_label(self):
        html = self.read("index.html")
        for cid in ("au-target", "au-when", "au-at", "au-job", "au-save", "cc-names", "cc-file", "cc-prune"):
            self.assertRegex(html, r'(for="%s"|aria-label[^>]*id="%s"|<label[^>]*>\s*<input[^>]*id="%s")' % (cid, cid, cid),
                             cid)


class TerminalLaunch(unittest.TestCase):
    def test_silent_where_it_cannot_show(self):
        was = U.launch_mode()
        try:
            U.launch_mode(True)
            if not U.launch_possible():                   # tests run with output captured: never a terminal
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertFalse(U.rocket_launch(0.05))
                self.assertEqual(out.getvalue(), "")
                self.assertFalse(U.launch_mode())
        finally:
            U.launch_mode(was)


if __name__ == "__main__":
    unittest.main()
