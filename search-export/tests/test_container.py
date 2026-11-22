"""Running the browser app where there is no terminal to press Enter in (a container or a service): the health
check, the address it is reached at, a new link written to the log on request, and the API key from a mounted file."""
import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest

from support import C, E, EngineCase, KEY

import abx_web as W


class _Web(EngineCase):
    def setUp(self):
        super().setUp()
        self.session = C.Session()
        self.auth = W._Auth()
        self.httpd = W._bind(0, W._handler(self.session, self.auth, None))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, method, path, body=None, host=None, origin=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": host or "127.0.0.1:%d" % self.port}
        if origin:
            h["Origin"] = origin
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        text = r.read().decode("utf-8", "replace")
        c.close()
        try:
            return r.status, json.loads(text)
        except ValueError:
            return r.status, text


class Health(_Web):
    def test_health_check_needs_no_sign_in_and_says_only_the_version(self):
        st, j = self.call("GET", "/healthz")
        self.assertEqual((st, j), (200, {"ok": True, "version": C.VERSION}))

    def test_health_check_still_refuses_another_host_name(self):
        st, _ = self.call("GET", "/healthz", host="evil.example")
        self.assertEqual(st, 403)


class NewLink(_Web):
    def test_with_a_terminal_nothing_is_printed(self):
        st, j = self.call("POST", "/api/relink", {})
        self.assertEqual((st, j), (200, {"printed": False}))

    def test_without_a_terminal_the_link_goes_to_the_log_never_to_the_page(self):
        printed = []
        self.auth.on_relink = lambda: printed.append(self.auth.current_code())
        st, j = self.call("POST", "/api/relink", {})
        self.assertEqual((st, j), (200, {"printed": True}))
        self.assertEqual(len(printed), 1)
        self.assertNotIn(printed[0], json.dumps(j))
        self.assertTrue(self.auth.exchange(printed[0]), "the printed link signs the page in")

    def test_asking_again_is_rate_limited_and_never_cancels_an_unused_link(self):
        printed = []
        self.auth.on_relink = lambda: printed.append(self.auth.current_code())
        for _ in range(5):
            self.assertEqual(self.call("POST", "/api/relink", {})[1], {"printed": True})
        self.assertEqual(len(printed), 1, "one link per RELINK_EVERY seconds")
        first = self.auth.current_code()
        self.assertEqual(first, self.auth.current_code())
        self.assertEqual(first, printed[0])

    def test_another_origin_cannot_ask(self):
        st, _ = self.call("POST", "/api/relink", {}, origin="https://evil.example")
        self.assertEqual(st, 403)

    def test_the_headless_message_names_the_page_to_reopen(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            W._print_link("http://127.0.0.1:8765/?code=abc", True, None, True)
        text = out.getvalue()
        self.assertIn("http://127.0.0.1:8765/?code=abc", text)
        self.assertIn("Open http://127.0.0.1:8765/ and a new link is written here", text)
        self.assertNotIn("Press Enter", text)


class PublicAddress(_Web):
    def test_default_is_none(self):
        self.assertIsNone(W.public_address(8765, {}))

    def test_a_remapped_port_on_this_computer(self):
        self.assertEqual(W.public_address(8765, {"ABX_WEB_URL": "http://localhost:9000"}),
                         ("http://localhost:9000/", "http", "localhost"))

    def test_an_https_address_in_front_of_it(self):
        self.assertEqual(W.public_address(8765, {"ABX_WEB_URL": "https://search.example.com/"}),
                         ("https://search.example.com/", "https", "search.example.com"))

    def test_a_codespace_changes_nothing_on_its_own(self):
        env = {"CODESPACES": "true", "CODESPACE_NAME": "calm-space-abc123",
               "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "app.github.dev"}
        self.assertIsNone(W.public_address(8765, env))

    def test_plain_http_to_another_machine_is_refused(self):
        for bad in ("http://10.0.0.5:8765/", "ftp://x/", "https://u:p@x.example/", "https://x.example/path",
                    "https://x.example/?a=1", "nonsense"):
            with self.assertRaises(ValueError, msg=bad):
                W.public_address(8765, {"ABX_WEB_URL": bad})

    def test_only_the_named_address_is_added(self):
        host = "search.example.com"
        self.assertEqual(self.call("GET", "/healthz", host=host)[0], 403)
        self.assertEqual(self.call("POST", "/api/relink", {}, origin="https://" + host)[0], 403)
        self.auth.hosts.add(host)
        self.auth.origins.add(("https", host))
        self.assertEqual(self.call("GET", "/healthz", host=host)[0], 200)
        self.assertEqual(self.call("POST", "/api/relink", {}, origin="https://" + host)[0], 200)
        self.assertEqual(self.call("POST", "/api/relink", {}, origin="http://" + host)[0], 403, "the scheme must match")
        self.assertEqual(self.call("POST", "/api/relink", {}, origin="https://other.example.com")[0], 403)


class KeyFile(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in
                      ("ABSTRACT_API_KEY", "ABSTRACT_API_KEY_FILE", "ABSTRACT_API_KEY_COMMAND")}
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        for f in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, f))
        os.rmdir(self.dir)

    def _file(self, text):
        p = os.path.join(self.dir, "key")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.environ["ABSTRACT_API_KEY_FILE"] = p
        return p

    def test_reads_one_line(self):
        self._file(KEY + "\n")
        self.assertEqual(E.key_from_environment(), KEY)

    def test_the_variable_wins_over_the_file(self):
        self._file("from-the-file-000000\n")
        os.environ["ABSTRACT_API_KEY"] = KEY
        self.assertEqual(E.key_from_environment(), KEY)

    def test_more_than_one_line_is_refused_without_showing_the_content(self):
        self._file("first-line-secret\nsecond-line-secret\n")
        with self.assertRaises(E.UserError) as cm:
            E.key_from_environment()
        self.assertNotIn("secret", str(cm.exception))

    def test_a_missing_file_says_so_without_the_path_content(self):
        os.environ["ABSTRACT_API_KEY_FILE"] = os.path.join(self.dir, "nope")
        with self.assertRaises(E.UserError) as cm:
            E.key_from_environment()
        self.assertIn("ABSTRACT_API_KEY_FILE could not be read", str(cm.exception))

    def test_nothing_set_is_none(self):
        self.assertIsNone(E.key_from_environment())


class KeyFromNetwork(unittest.TestCase):
    """ABSTRACT_API_KEY_FROM_NETWORK=1: a proxy adds the Authorization header after the request leaves, so this
    process holds no key and must send no Authorization header of its own."""

    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in
                      ("ABSTRACT_API_KEY", "ABSTRACT_API_KEY_FILE", "ABSTRACT_API_KEY_COMMAND",
                       "ABSTRACT_API_KEY_FROM_NETWORK")}
        self.creds = E.CREDS

    def tearDown(self):
        E.CREDS = self.creds
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_only_when_asked_for(self):
        self.assertIsNone(E.key_from_environment())
        os.environ["ABSTRACT_API_KEY_FROM_NETWORK"] = "yes"
        self.assertIsNone(E.key_from_environment())
        os.environ["ABSTRACT_API_KEY_FROM_NETWORK"] = "1"
        self.assertEqual(E.key_from_environment(), E.NETWORK_KEY)

    def test_a_real_key_wins(self):
        os.environ["ABSTRACT_API_KEY_FROM_NETWORK"] = "1"
        os.environ["ABSTRACT_API_KEY"] = KEY
        self.assertEqual(E.key_from_environment(), KEY)

    def test_no_authorization_header_is_sent(self):
        c = E.Client("https://api.fake.example", E.NETWORK_KEY, "abcd1234")
        self.assertNotIn("Authorization", c._headers())
        self.assertEqual(c._headers()["X-AS-Vendor-Account-ID"], "abcd1234")
        self.assertEqual(E.Client("https://api.fake.example", KEY, "abcd1234")._headers()["Authorization"], "Bearer " + KEY)

    def test_the_trace_says_where_the_key_comes_from(self):
        E.CREDS = {"api_key": E.NETWORK_KEY}
        self.assertEqual(E.masked_headers("abcd1234", False)["Authorization"], E.NETWORK_NOTE)
        E.CREDS = {"api_key": KEY}
        self.assertEqual(E.masked_headers("abcd1234", False)["Authorization"], E.MASKED)

    def test_a_401_names_the_secret_not_the_key(self):
        E.CREDS = {"api_key": E.NETWORK_KEY}
        with self.assertRaises(E.Fatal) as cm:
            E.classify_error(401, {"detail": "no"}, "/v2/auth/")
        self.assertIn("ABSTRACT_API_KEY_FROM_NETWORK", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
