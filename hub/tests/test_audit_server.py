"""Hub fixes from the release audit of 2026-11-14."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from cairn_hub import config, mirror, ssh

import test_hub as th
import test_m2 as m2


class NameRegexTests(unittest.TestCase):
    def test_trailing_newline_is_refused(self):
        for rx, good in ((config.PROJECT_RE, "abc"), (config.NODE_RE, "lab"), (config.LABEL_RE, "oa"),
                         (config.SSH_RE, "user@box")):
            self.assertTrue(rx.match(good), rx.pattern)
            self.assertIsNone(rx.match(good + "\n"), rx.pattern)


class ContentLengthTests(m2.ApiBase):
    def raw_post(self, length):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(s.close)
        s.sendall(("POST /api/sync HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nOrigin: http://127.0.0.1:%d\r\n"
                   "X-Cairn-Token: %s\r\nCookie: %s=%s\r\nContent-Length: %s\r\n\r\n{}"
                   % (self.port, self.port, self.srv.token, self.srv.cookie_name, self.srv.secret, length)).encode())
        return s.recv(4096).split(b"\r\n", 1)[0]

    def test_negative_length_is_a_bad_request(self):
        self.assertIn(b" 400 ", self.raw_post("-1"))


class AttachTests(th.Base):
    def test_review_ssh_never_forwards_the_agent(self):
        argv = ssh.attach_command({"ssh": "box", "port": None}, "p").split()
        self.assertIn("-a", argv)
        self.assertEqual(argv[argv.index("ForwardAgent=no") - 1], "-o")


class OutputCapTests(th.Base):
    def endless_node(self, stream):
        bin_ = os.path.join(self.tmp, "local-node", "cairn-node")
        os.makedirs(os.path.dirname(bin_))
        self.write(bin_, "#!/bin/sh\nyes%s\n" % (" >&2" if stream == "stderr" else ""))
        os.chmod(bin_, 0o755)
        return {"ssh": "local", "port": None}

    def test_endless_output_is_cut_off(self):
        for stream in ("stdout", "stderr"):
            node = self.endless_node(stream)
            with mock.patch.object(ssh, "MAX_OUTPUT", 1024 * 1024):
                started = time.monotonic()
                with self.assertRaises(ssh.NodeError) as cm:
                    ssh.run_node(node, ["snapshot"], timeout=20)
                self.assertIn("more than 1 MB", str(cm.exception))
                self.assertLess(time.monotonic() - started, 10)
                with self.assertRaises(ssh.NodeError):
                    ssh.run_shell(node, "yes", timeout=20)
            shutil.rmtree(os.path.join(self.tmp, "local-node"))

    def test_output_under_the_cap_and_stdin_pass_through(self):
        node = {"ssh": "local", "port": None}
        self.assertEqual(ssh.run_shell(node, "cat; echo err >&2", stdin="x" * 300000), "x" * 300000)
        with self.assertRaises(ssh.NodeError) as cm:
            ssh.run_shell(node, "echo boom >&2; exit 3")
        self.assertEqual(str(cm.exception), "boom")
        with self.assertRaises(ssh.NodeError) as cm:
            ssh.run_shell(node, "sleep 5", timeout=1)
        self.assertEqual(str(cm.exception), "timed out after 1s")


class SnapshotValidationTests(m2.ApiBase):
    def snapshot(self):
        st = self.state()
        return st["nodes"][0]

    def test_malformed_top_level_is_a_failed_sync_and_state_still_answers(self):
        good = self.snapshot()["snapshot"]
        for raw in ([1], {"projects": {"demo": {}}}, {"projects": None}, "x"):
            with mock.patch.object(ssh, "run_node", return_value=raw):
                self.syncer.sync_now()
            status, body = self.req("/api/state")
            self.assertEqual(status, 200, raw)
            n = json.loads(body)["nodes"][0]
            self.assertFalse(n["ok"], raw)
            self.assertIn("malformed snapshot", n["error"])
            self.assertEqual(n["snapshot"], good)

    def test_bad_projects_are_dropped_and_the_rest_normalised(self):
        demo = m2.snap_project("demo", sha="abc1234", path="/srv/demo")
        demo.update({"questions": None, "result_sha": 5, "tickets": [{"id": "01", "blocked_by": "02"}, 7],
                     "map": [], "commits": [{"sha": 123}, {"sha": "--upload-pack=x"}, {"sha": "abc1234", "subject": 9}]})
        demo["questions"] = [{"id": "q01", "options": ["A) yes", 2, None], "answered": "no"}, None]
        self.set_projects([1, {"path": "/srv/x"}, {"name": "a/b", "path": "/srv/x"}, {"name": "rel", "path": "srv/x"},
                           {"name": "num", "path": 5}, {"name": "nl\n", "path": "/srv/x"}, demo,
                           dict(demo, path="/srv/other"), {"name": [], "path": "/srv/x"}, {"name": {}, "path": "/srv/x"},
                           {"name": "long\n" * 5000, "path": "/srv/x"}])
        self.syncer.sync_now()
        status, body = self.req("/api/state")
        self.assertEqual(status, 200)
        snap = json.loads(body)["nodes"][0]["snapshot"]
        self.assertEqual([p["name"] for p in snap["projects"]], ["demo"])
        p = snap["projects"][0]
        self.assertEqual(p["path"], "/srv/demo")
        self.assertEqual(p["tickets"], [{"id": "01", "title": "", "type": "", "status": "", "assignee": "",
                                         "blocked_by": [], "path": "", "state": ""}])
        self.assertEqual(p["questions"][0]["options"], ["A) yes"])
        self.assertIs(p["questions"][0]["answered"], False)
        self.assertEqual(len(p["questions"]), 1)
        self.assertEqual(p["commits"], [{"sha": "abc1234", "date": "", "subject": ""}])
        self.assertIsNone(p["result_sha"])
        self.assertEqual(p["map"], {"destination": "", "decisions": []})
        dropped = [w["warning"] for w in snap["warnings"] if "hub dropped" in w["warning"]]
        self.assertEqual(len(dropped), 10)
        self.assertLessEqual(max(map(len, dropped)), 300)
        self.assertIn("sync: lab: the hub dropped project 3 of the snapshot (no valid name): 'a/b'", self.out.getvalue())
        self.syncer.sync_now()
        self.assertEqual(self.out.getvalue().count("'a/b'"), 1)
        self.assertEqual(list(self.state()["projects"]), ["lab/demo"])

    def test_only_the_hubs_own_notes_reach_the_log(self):
        raw = {"projects": [{"name": "x/y", "path": "/srv/x"}], "warnings": [{"warning": "node says\nforged log line"}]}
        with mock.patch.object(ssh, "run_node", return_value=raw):
            self.syncer.sync_now()
        warnings = [w["warning"] for w in self.snapshot()["snapshot"]["warnings"]]
        self.assertIn("node says\nforged log line", warnings)
        self.assertNotIn("node says", self.out.getvalue())
        self.assertIn("sync: lab: the hub dropped project 1 of the snapshot (no valid name): 'x/y'\n", self.out.getvalue())

    def test_mirror_url_needs_an_absolute_path(self):
        self.assertEqual(mirror.remote_url({"ssh": "box", "port": 2222}, "/srv/demo"), "ssh://box:2222/srv/demo")
        for bad in ("evil.example/x", "", "@evil/x", None):
            with self.assertRaises(ValueError):
                mirror.remote_url({"ssh": "box", "port": None}, bad)


class PythonVersionTests(unittest.TestCase):
    def run_cairn(self, version):
        entry = os.path.join(os.path.dirname(HERE), "cairn")
        code = ("import sys; sys.version_info = %r; sys.argv = ['cairn', '--help']; __file__ = %r; "
                "exec(compile(open(__file__).read(), __file__, 'exec'))" % (version, entry))
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    def test_old_python_is_refused_with_a_clear_message(self):
        r = self.run_cairn((3, 11, 3, "final", 0))
        self.assertEqual(r.returncode, 1)
        self.assertIn("needs Python 3.11.4 or newer", r.stderr)
        self.assertEqual(r.stdout, "")
        r = self.run_cairn((3, 11, 4, "final", 0))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage: cairn", r.stdout)


if __name__ == "__main__":
    unittest.main()
