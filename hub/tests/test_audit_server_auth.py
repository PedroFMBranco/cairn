"""The hub's sign-in: a secret in ~/.cairn/hub-secret, handed to the browser once by `cairn open` (audit #1, #47)."""
import base64
import hashlib
import http.client
import json
import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from cairn_hub import cli, config, nodeops, server
from cairn_hub.server import CairnServer
from cairn_hub.sync import Syncer

import test_m2 as m2


class RawBase(m2.ApiBase):
    def raw(self, path, method="GET", headers=None, body=None):
        """One request without urllib's redirect following; (status, headers, body)."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(c.close)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.headers, r.read()

    def cookie(self):
        return "%s=%s" % (self.srv.cookie_name, self.srv.secret)


class SignInTests(RawBase):
    def test_secret_file_is_private_long_and_stable(self):
        p = config.path("hub-secret")
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertGreaterEqual(len(self.srv.secret), 32)
        self.assertEqual(config.hub_secret(), self.srv.secret)
        self.assertNotEqual(self.srv.token, self.srv.secret)

    def test_a_short_secret_file_is_refused(self):
        with open(config.path("hub-secret"), "w") as f:
            f.write("\n")
        with self.assertRaises(RuntimeError):
            config.hub_secret()

    def test_nothing_is_served_without_the_cookie(self):
        forged = {"Host": "127.0.0.1:%d" % self.port}   # e.g. a container on host.docker.internal
        for path in ("/", "/index.html", "/app.js", "/fixtures/state.json", "/api/state", "/api/ssh-hosts",
                     "/api/file?node=lab&project=demo&path=work/map.md", "/api/chat/events?node=lab&project=demo",
                     "/api/sandboxes", "/?k=", "/?k=wrong", "/?k=" + self.srv.secret[:-1]):
            for cookie in (None, "%s=wrong" % self.srv.cookie_name, "cairn-1=%s" % self.srv.secret):
                h = dict(forged, **({"Cookie": cookie} if cookie else {}))
                status, headers, body = self.raw(path, headers=h)
                self.assertEqual(status, 401, (path, cookie))
                self.assertIsNone(headers["Set-Cookie"])
                text = body.decode()
                self.assertIn("cairn open", text)
                self.assertNotIn(self.srv.token, text)
                self.assertNotIn(self.srv.secret, text)

    def test_posts_need_the_cookie_as_well_as_the_token(self):
        h = {"Host": "127.0.0.1:%d" % self.port, "Origin": "http://127.0.0.1:%d" % self.port,
             "X-Cairn-Token": self.srv.token, "Content-Type": "application/json"}
        body = json.dumps({"name": "evil", "ssh": "box", "allowed_tokens": []})
        self.assertEqual(self.raw("/api/nodes", "POST", h, body)[0], 401)
        self.assertEqual(list(config.load_nodes()), ["lab"])

    def test_cairn_open_link_sets_the_cookie_and_redirects(self):
        status, headers, _ = self.raw("/?k=" + self.srv.secret)
        self.assertEqual((status, headers["Location"]), (303, "/"))
        cookie = headers["Set-Cookie"]
        self.assertTrue(cookie.startswith(self.cookie() + ";"), cookie)
        for attr in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attr, [a.strip() for a in cookie.split(";")])
        status, _, body = self.raw("/", headers={"Cookie": "other=1; " + self.cookie()})
        self.assertEqual(status, 200)
        self.assertIn('<meta name="cairn-token" content="%s">' % self.srv.token, body.decode())
        self.assertNotIn(self.srv.secret, body.decode())

    def test_post_token_survives_a_restart(self):
        """#47: an open page keeps working after the hub restarts."""
        again = CairnServer(0, Syncer(interval=3600), ui_dir=os.path.join(self.tmp, "no-ui"))
        self.addCleanup(again.server_close)
        self.assertEqual((again.secret, again.token), (self.srv.secret, self.srv.token))

    def test_local_node_refused_through_the_api(self):
        with mock.patch.object(nodeops, "install", return_value=m2.NodeRouteTests.CHECK) as install:
            status, out = self.post("/api/nodes", {"name": "mac", "ssh": "local", "allowed_tokens": []})
            self.assertEqual(status, 403, out)
            self.assertIn("cairn node add", out["error"])
            install.assert_not_called()
            self.assertEqual(list(config.load_nodes()), ["lab"])
            with mock.patch.dict(os.environ, {"CAIRN_ALLOW_LOCAL_NODE": "1"}):
                self.assertEqual(self.post("/api/nodes", {"name": "mac", "ssh": "local", "allowed_tokens": []})[0], 200)
        self.assertEqual(config.load_nodes()["mac"]["ssh"], "local")


class PageBase(RawBase):
    IMPORTMAP = '\n{"imports": {"preact": "./vendor/preact.module.js"}}\n'

    def setUp(self):
        super().setUp()
        ui = os.path.join(self.tmp, "ui")
        os.makedirs(os.path.join(ui, "fixtures"))
        self.write(os.path.join(ui, "index.html"), '<!doctype html><html><head><script type="importmap">%s</script>'
                   '<script type="module" src="./app.js"></script></head><body></body></html>' % self.IMPORTMAP)
        self.write(os.path.join(ui, "app.js"), "1")
        self.write(os.path.join(ui, "fixtures", "state.json"), "{}")
        self.srv.ui_dir = ui

    def get(self, path):
        return self.raw(path, headers={"Cookie": self.cookie()})


class DevModeTests(PageBase):
    def test_fixtures_only_in_dev_mode(self):
        """#7: a link to ?fixture= must not show a stub dashboard on the real hub."""
        for path in ("/fixtures/state.json", "/fixtures/../fixtures/state.json", "/./fixtures/state.json",
                     "/Fixtures/state.json"):
            self.assertEqual(self.get(path)[0], 404, path)
        self.assertNotIn(b"cairn-dev", self.get("/")[2])
        self.srv.dev = True
        self.assertEqual(self.get("/fixtures/state.json")[0], 200)
        self.assertIn(b'<meta name="cairn-dev" content="1">', self.get("/")[2])


class CspTests(PageBase):
    def test_page_csp_allows_only_the_inline_importmap(self):
        digest = base64.b64encode(hashlib.sha256(self.IMPORTMAP.encode()).digest()).decode()
        csp = ("default-src 'self'; script-src 'self'%s; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.assertEqual(self.get("/")[1]["Content-Security-Policy"], csp % (" 'sha256-%s'" % digest))
        self.assertEqual(self.raw("/")[1]["Content-Security-Policy"], csp % "")
        for path in ("/api/state", "/app.js"):
            self.assertEqual(self.get(path)[1]["Content-Security-Policy"], "frame-ancestors 'none'", path)


class RealPageCspTests(RawBase):
    def test_the_dashboard_has_one_policy_the_header_with_its_own_importmap(self):
        """#1, #6: the header is the only CSP on the real page, so no second policy can drift from it."""
        self.srv.ui_dir = server.UI_DIR
        status, headers, body = self.raw("/", headers={"Cookie": self.cookie()})
        self.assertEqual(status, 200)
        self.assertNotIn(b"http-equiv", body.lower())
        importmap = re.search(rb'<script type="importmap">(.*?)</script>', body, re.S).group(1)
        digest = base64.b64encode(hashlib.sha256(importmap).digest()).decode()
        self.assertIn("script-src 'self' 'sha256-%s';" % digest, headers["Content-Security-Policy"])


class ServeCommandTests(m2.M2Base):
    def test_dev_flag_and_environment(self):
        from cairn_hub import server
        for argv, env, dev in ((["serve"], {}, False), (["serve", "--dev"], {}, True), (["serve"], {"CAIRN_DEV": "1"}, True),
                               (["serve"], {"CAIRN_DEV": "0"}, False)):
            with mock.patch.object(server, "serve") as serve, mock.patch.dict(os.environ, env):
                self.assertEqual(cli.main(argv), 0)
            serve.assert_called_once_with(8770, dev=dev)

    def test_serve_makes_an_existing_home_private(self):
        """#11: ~/.cairn made earlier with the default umask (0755) is 0700 once the hub starts."""
        os.chmod(config.home(), 0o755)
        with mock.patch.object(server, "CairnServer", side_effect=OSError(48, "Address already in use")):
            with self.assertRaises(SystemExit):
                server.serve(0)
        self.assertEqual(os.stat(config.home()).st_mode & 0o777, 0o700)


class OpenCommandTests(m2.M2Base):
    def test_open_prints_and_opens_the_sign_in_link(self):
        with mock.patch("subprocess.run") as run:
            self.assertEqual(cli.main(["open", "--port", "8899"]), 0)
        url = "http://127.0.0.1:8899/?k=%s" % config.hub_secret()
        self.assertEqual(run.call_args.args[0], ["open", url])
        self.assertIn(url, self.out.getvalue())
        self.assertEqual(os.stat(config.path("hub-secret")).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
