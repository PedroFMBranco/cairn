"""Milestone 2 backend: mirror, token/node/project/ticket routes, CLI parity."""
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import test_hub as th
from cairn_hub import cli, config, keychain, mirror, nodeops, ssh
from cairn_hub.server import CairnServer
from cairn_hub.sync import Syncer

SECRET = th.SECRET
SECOND = "sk-ant-oat01-SECONDTOKENVALUE5555"

ROUTES = ["/api/nodes", "/api/nodes/tokens", "/api/nodes/remove", "/api/tokens", "/api/tokens/update",
          "/api/tokens/remove", "/api/projects", "/api/projects/token", "/api/tickets", "/api/dialog"]


def snap_project(name="demo", lead="none", review="none", sha="abc1234", path="/srv/demo"):
    return {"name": name, "path": path, "status": "idle", "lead": lead, "review": review,
            "map": {"destination": "d", "decisions": []}, "tickets": [], "questions": [], "adrs": [],
            "commits": [{"sha": sha, "date": "2026-10-17T12:00:00Z", "subject": "s"}] if sha else []}


class GitRecorder:
    """Stands in for subprocess.run inside the mirror."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.fail:
            if hasattr(kw.get("stderr"), "write"):
                kw["stderr"].write(b"fatal: boom\nssh: connect refused\n")
            return mock.Mock(returncode=128, stdout="", stderr="fatal: boom\nssh: connect refused\n")
        if argv[1] == "clone":
            os.makedirs(os.path.join(argv[-1], "objects"))
            open(os.path.join(argv[-1], "HEAD"), "w").close()
        return mock.Mock(returncode=0, stdout="", stderr="")

    def transfers(self):
        """The clone and fetch calls, without the config calls around them."""
        return [argv for argv, _ in self.calls if "clone" in argv or "fetch" in argv]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class KeychainFake:
    def __init__(self):
        self.store = {}

    def add(self, label, value):
        self.store[label] = value

    def get(self, label):
        if label not in self.store:
            raise keychain.KeychainError("no keychain item for token %s" % label)
        return self.store[label]

    def delete(self, label):
        return self.store.pop(label, None) is not None


class M2Base(th.Base):
    """A local fake node "lab", an in-memory Keychain and captured stdout/stderr."""

    def setUp(self):
        super().setUp()
        self.kc = KeychainFake()
        for name in ("add", "get", "delete"):
            p = mock.patch.object(keychain, name, getattr(self.kc, name))
            p.start()
            self.addCleanup(p.stop)
        self.out, self.err = io.StringIO(), io.StringIO()
        for stream, buf in (("stdout", self.out), ("stderr", self.err)):
            p = mock.patch.object(sys, stream, buf)
            p.start()
            self.addCleanup(p.stop)
        self.snap_file = os.path.join(self.tmp, "snap.json")
        env = mock.patch.dict(os.environ, {"FAKE_NODE_PROJECTS": self.snap_file, "HOME": self.tmp})
        env.start()
        self.addCleanup(env.stop)
        self.set_projects([snap_project(sha=None)])
        config.save_tokens({"oa": {"kind": "oauth", "account": "me", "last4": SECRET[-4:], "expires": None, "added": "x"},
                            "nl": {"kind": "node-login", "account": "", "last4": None, "expires": None, "added": "x"}})
        self.kc.store["oa"] = SECRET
        self.add_local_node(["oa", "nl"])

    def set_projects(self, projects):
        self.write(self.snap_file, json.dumps(projects))

    def starts(self):
        return [e for e in self.log() if e["argv"][0] == "start"]

    def assert_no_secret(self, *extra):
        everything = self.out.getvalue() + self.err.getvalue()
        for s in (SECRET, SECOND) + extra:
            self.assertNotIn(s, everything)
        for name in os.listdir(self.tmp):
            p = os.path.join(self.tmp, name)
            if name.endswith(".json") and os.path.isfile(p):
                with open(p) as f:
                    text = f.read()
                for s in (SECRET, SECOND) + extra:
                    self.assertNotIn(s, text, name)


class ApiBase(M2Base):
    def setUp(self):
        super().setUp()
        self.git = GitRecorder()
        self.syncer = Syncer(interval=3600, mirror=mirror.Mirror(run=self.git))
        self.syncer.refresh_nodes = lambda: None  # no background loop threads racing the tests' own syncs
        self.srv = CairnServer(0, self.syncer, ui_dir=os.path.join(self.tmp, "no-ui"), opener=lambda c: None)
        self.port = self.srv.port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.syncer.mirror.wait_idle)  # no git after CAIRN_HOME is restored
        self.addCleanup(self.syncer.stop)
        self.syncer.sync_now()
        self.assertTrue(self.syncer.mirror.wait_idle())

    def req(self, path, method="GET", body=None, headers=None):
        h = {"Origin": "http://127.0.0.1:%d" % self.port, "X-Cairn-Token": self.srv.token,
             "Content-Type": "application/json", "Cookie": "%s=%s" % (self.srv.cookie_name, self.srv.secret)}
        if headers is not None:
            h = {k: v for k, v in {**h, **headers}.items() if v is not None}
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), method=method,
                                   data=json.dumps(body if body is not None else {}).encode() if method == "POST" else None,
                                   headers=h)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            self.addCleanup(e.close)
            return e.code, e.read()

    def post(self, path, body):
        status, raw = self.req(path, "POST", body)
        return status, json.loads(raw)

    def state(self):
        return json.loads(self.req("/api/state")[1])


class AuthTests(ApiBase):
    def test_every_new_post_route_needs_token_and_origin(self):
        for route in ROUTES:
            self.assertEqual(self.req(route, "POST", {}, {"X-Cairn-Token": None})[0], 403, route)
            self.assertEqual(self.req(route, "POST", {}, {"X-Cairn-Token": "wrong"})[0], 403, route)
            self.assertEqual(self.req(route, "POST", {}, {"Origin": "http://evil.example"})[0], 403, route)
            self.assertEqual(self.req(route, "POST", {}, {"Origin": None})[0], 403, route)
            # authenticated but empty: a validation error, never a 403 or a crash
            self.assertIn(self.req(route, "POST", {})[0], (400, 404), route)

    def test_unauthenticated_calls_change_nothing(self):
        before = (config.load_nodes(), config.load_tokens())
        self.req("/api/tokens", "POST", {"label": "x", "kind": "oauth", "secret": SECRET}, {"X-Cairn-Token": None})
        self.req("/api/nodes/remove", "POST", {"name": "lab"}, {"Origin": "http://evil.example"})
        self.assertEqual((config.load_nodes(), config.load_tokens()), before)


class SshHostsTests(ApiBase):
    def test_lists_concrete_hosts_only(self):
        os.makedirs(os.path.join(self.tmp, ".ssh"))
        self.write(os.path.join(self.tmp, ".ssh", "config"),
                   "# comment\nHost alpha beta\n  HostName a.example\nHost *\n  User x\nHost !bad good* gamma # c\n"
                   "host=delta\nHost \"quoted\"\nMatch host zzz\n  HostName ignored\nHost alpha\n")
        status, raw = self.req("/api/ssh-hosts")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"hosts": ["alpha", "beta", "delta", "gamma", "quoted"]})

    def test_follows_include_lines(self):
        ssh_dir = os.path.join(self.tmp, ".ssh")
        os.makedirs(os.path.join(ssh_dir, "config.d"))
        os.makedirs(os.path.join(self.tmp, "orb"))
        self.write(os.path.join(ssh_dir, "config"),
                   "Include config.d/*\nInclude ~/orb/ssh_config\nInclude " + os.path.join(self.tmp, "abs.conf") +
                   "\nInclude missing/*\nHost main\n  Include nested.conf\n")
        self.write(os.path.join(ssh_dir, "config.d", "a"), "Host from-glob-a\n")
        self.write(os.path.join(ssh_dir, "config.d", "b"), "Host from-glob-b *.wild\nInclude config\n")  # a loop
        self.write(os.path.join(self.tmp, "orb", "ssh_config"), "Host orb\n")
        self.write(os.path.join(self.tmp, "abs.conf"), "Host absolute\n")
        self.write(os.path.join(ssh_dir, "nested.conf"), "Host nested\n")
        status, raw = self.req("/api/ssh-hosts")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"hosts": ["absolute", "from-glob-a", "from-glob-b", "main", "nested", "orb"]})

    def test_missing_config_is_empty(self):
        self.assertEqual(json.loads(self.req("/api/ssh-hosts")[1]), {"hosts": []})


class NodeRouteTests(ApiBase):
    CHECK = {"node_version": "0.1.0", "host": "h", "free_ram_mb": 5, "free_disk_mb": 6,
             "deps": {"tmux": "3.3a", "git": None}, "projects": []}

    def test_add_node(self):
        with mock.patch.object(nodeops, "install", return_value=self.CHECK) as install:
            status, out = self.post("/api/nodes", {"name": "box", "ssh": "box.example", "port": 2222,
                                                   "allowed_tokens": ["oa"]})
        self.assertEqual(status, 200, out)
        self.assertEqual(out, {"node": "box", "check": {"deps": self.CHECK["deps"], "host": "h",
                                                        "free_ram_mb": 5, "free_disk_mb": 6}})
        install.assert_called_once_with({"ssh": "box.example", "port": 2222, "allowed_tokens": ["oa"]})
        self.assertEqual(config.load_nodes()["box"], {"ssh": "box.example", "port": 2222, "allowed_tokens": ["oa"]})

    def test_add_node_conflicts_and_validation(self):
        with mock.patch.object(nodeops, "install", return_value=self.CHECK) as install:
            self.assertEqual(self.post("/api/nodes", {"name": "lab", "ssh": "x", "allowed_tokens": []})[0], 409)
            self.assertEqual(self.post("/api/nodes", {"name": "n", "ssh": "x", "allowed_tokens": ["ghost"]})[0], 400)
            self.assertEqual(self.post("/api/nodes", {"name": "bad name", "ssh": "x", "allowed_tokens": []})[0], 400)
            self.assertEqual(self.post("/api/nodes", {"name": "n", "ssh": "-oProxy=x", "allowed_tokens": []})[0], 400)
            self.assertEqual(self.post("/api/nodes", {"name": "n", "ssh": "x", "port": "22", "allowed_tokens": []})[0], 400)
            self.assertEqual(self.post("/api/nodes", {"name": "n", "ssh": "x"})[0], 400)
        install.assert_not_called()
        self.assertEqual(list(config.load_nodes()), ["lab"])

    def test_install_failure_saves_nothing(self):
        with mock.patch.object(nodeops, "install", side_effect=ssh.NodeError("Permission denied")):
            status, out = self.post("/api/nodes", {"name": "n", "ssh": "x", "allowed_tokens": []})
        self.assertEqual((status, out), (502, {"error": "Permission denied"}))
        self.assertEqual(list(config.load_nodes()), ["lab"])

    def test_set_allowed_tokens(self):
        self.assertEqual(self.post("/api/nodes/tokens", {"name": "lab", "allowed_tokens": ["nl"]})[0], 200)
        self.assertEqual(config.load_nodes()["lab"]["allowed_tokens"], ["nl"])
        self.assertEqual(self.post("/api/nodes/tokens", {"name": "lab", "allowed_tokens": ["nope"]})[0], 400)
        self.assertEqual(self.post("/api/nodes/tokens", {"name": "ghost", "allowed_tokens": []})[0], 404)
        self.assertEqual(self.post("/api/nodes/tokens", {"name": "lab"})[0], 400)
        self.assertEqual(config.load_nodes()["lab"]["allowed_tokens"], ["nl"])

    def test_remove_refused_while_session_alive(self):
        self.set_projects([snap_project(lead="alive", sha=None)])
        self.syncer.sync_now()
        status, out = self.post("/api/nodes/remove", {"name": "lab"})
        self.assertEqual(status, 409)
        self.assertIn("lab/demo".split("/")[1], out["error"])
        self.assertIn("lab", config.load_nodes())
        self.set_projects([snap_project(review="alive", sha=None)])
        self.syncer.sync_now()
        self.assertEqual(self.post("/api/nodes/remove", {"name": "lab"})[0], 409)

    def test_remove_forgets_node_and_its_project_entries(self):
        config.save_nodes({**config.load_nodes(), "box": {"ssh": "b", "port": None, "allowed_tokens": ["oa"]}})
        config.save_projects({"lab/demo": {"token": "oa"}, "box/demo": {"token": "oa"}, "lab2/x": {"token": "nl"}})
        status, out = self.post("/api/nodes/remove", {"name": "lab"})
        self.assertEqual((status, out), (200, {"removed": "lab"}))
        self.assertEqual(list(config.load_nodes()), ["box"])
        self.assertEqual(config.load_projects(), {"box/demo": {"token": "oa"}, "lab2/x": {"token": "nl"}})
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "local-node", "cairn-node")))  # machine untouched
        self.assertEqual(self.post("/api/nodes/remove", {"name": "lab"})[0], 404)

    def test_remove_unknown_snapshot_needs_force(self):
        config.save_projects({"lab/demo": {"token": "oa"}})
        os.remove(os.path.join(self.tmp, "local-node", "cairn-node"))  # unreachable from now on
        self.syncer.sync_now()
        self.assertFalse(self.state()["nodes"][0]["ok"])
        for body in ({"name": "lab"}, {"name": "lab", "force": "yes"}, {"name": "lab", "force": False}):
            status, out = self.post("/api/nodes/remove", body)
            self.assertEqual(status, 409, body)
            self.assertIn("unreachable or not synced", out["error"])
        self.assertIn("lab", config.load_nodes())
        self.assertEqual(self.post("/api/nodes/remove", {"name": "lab", "force": True}), (200, {"removed": "lab"}))
        self.assertEqual((config.load_nodes(), config.load_projects()), ({}, {}))

    def test_remove_never_synced_node_needs_force(self):
        config.save_nodes({**config.load_nodes(), "fresh": {"ssh": "f", "port": None, "allowed_tokens": []}})
        self.assertEqual(self.post("/api/nodes/remove", {"name": "fresh"})[0], 409)
        self.assertEqual(self.post("/api/nodes/remove", {"name": "fresh", "force": True})[0], 200)

    def test_force_does_not_override_live_sessions(self):
        self.set_projects([snap_project(lead="alive", sha=None)])
        self.syncer.sync_now()
        status, out = self.post("/api/nodes/remove", {"name": "lab", "force": True})
        self.assertEqual(status, 409)
        self.assertIn("live lead or review", out["error"])
        self.assertIn("lab", config.load_nodes())


class TokenRouteTests(ApiBase):
    def test_add_oauth_token_goes_to_keychain_only(self):
        status, out = self.post("/api/tokens", {"label": "work", "kind": "oauth", "account": "w@x", "secret": SECRET + "  ",
                                                "nodes": ["lab"]})
        self.assertEqual(status, 200, out)
        self.assertEqual((out["label"], out["kind"], out["account"], out["last4"]), ("work", "oauth", "w@x", SECRET[-4:]))
        self.assertEqual(self.kc.store["work"], SECRET)
        self.assertEqual(config.load_nodes()["lab"]["allowed_tokens"], ["oa", "nl", "work"])
        meta = config.load_tokens()["work"]
        self.assertTrue(meta["expires"])
        self.assertNotIn("secret", meta)
        self.assertNotIn(SECRET, json.dumps(out) + json.dumps(self.state()))
        self.assert_no_secret()

    def test_add_validation_and_duplicates(self):
        self.assertEqual(self.post("/api/tokens", {"label": "oa", "kind": "oauth", "secret": SECRET})[0], 409)
        self.assertEqual(self.post("/api/tokens", {"label": "n", "kind": "oauth", "secret": "short"})[0], 400)
        self.assertEqual(self.post("/api/tokens", {"label": "n", "kind": "oauth"})[0], 400)
        self.assertEqual(self.post("/api/tokens", {"label": "n", "kind": "weird"})[0], 400)
        self.assertEqual(self.post("/api/tokens", {"label": "bad label", "kind": "oauth", "secret": SECRET})[0], 400)
        self.assertEqual(self.post("/api/tokens", {"label": "n", "kind": "oauth", "secret": SECRET, "nodes": ["ghost"]})[0], 400)
        self.assertEqual(set(config.load_tokens()), {"oa", "nl"})
        self.assertEqual(set(self.kc.store), {"oa"})
        status, out = self.post("/api/tokens", {"label": "n", "kind": "oauth", "secret": "short1"})
        self.assertNotIn("short1", out["error"])

    def test_add_node_login_has_no_secret(self):
        status, out = self.post("/api/tokens", {"label": "box-login", "kind": "node-login", "secret": SECRET})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["last4"], None)
        self.assertNotIn("box-login", self.kc.store)
        self.assert_no_secret()

    def test_replace_keeps_nodes_and_projects(self):
        config.save_projects({"lab/demo": {"token": "oa"}})
        body = {"label": "oa", "kind": "oauth", "secret": SECOND}
        self.assertEqual(self.post("/api/tokens", body)[0], 409)   # an existing label needs replace
        status, out = self.post("/api/tokens", {**body, "replace": True})
        self.assertEqual(status, 200, out)
        self.assertEqual(self.kc.store["oa"], SECOND)
        self.assertIn("oa", config.load_nodes()["lab"]["allowed_tokens"])
        self.assertEqual(config.load_projects()["lab/demo"]["token"], "oa")
        self.assertNotIn(SECOND, json.dumps(out))

    def test_removed_kind_is_refused_with_the_way_out(self):
        config.save_tokens({**config.load_tokens(), "old": {"kind": "retired-kind", "account": "", "last4": "9f3d"}})
        config.save_projects({"lab/demo": {"token": "old"}})
        nodeops.set_node_tokens("lab", ["oa", "nl", "old"])
        self.kc.store["old"] = "skc_x"
        status, raw = self.req("/api/start", "POST", {"node": "lab", "project": "demo"})
        self.assertEqual(status, 409)
        self.assertIn("cairn token add old --kind oauth", json.loads(raw)["error"])
        self.assertEqual(self.post("/api/tokens", {"label": "s", "kind": "retired-kind", "secret": "x" * 20})[0], 400)
        self.assertEqual(self.post("/api/tokens", {"label": "old", "kind": "oauth", "secret": SECOND, "replace": True})[0], 200)
        self.assertEqual(self.req("/api/start", "POST", {"node": "lab", "project": "demo"})[0], 200)
        self.assertEqual([e["stdin"] for e in self.starts()], [SECOND])

    def test_keychain_failure_is_an_error_without_secret(self):
        with mock.patch.object(keychain, "add", side_effect=keychain.KeychainError("keychain add failed for n (exit 1)")):
            status, out = self.post("/api/tokens", {"label": "n", "kind": "oauth", "secret": SECRET})
        self.assertEqual(status, 500)
        self.assertNotIn("n", config.load_tokens())
        self.assert_no_secret()

    def test_rename_moves_keychain_and_references(self):
        config.save_projects({"lab/demo": {"token": "oa"}, "lab/other": {"token": "nl"}})
        status, out = self.post("/api/tokens/update", {"label": "oa", "new_label": "personal"})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["label"], "personal")
        self.assertEqual(self.kc.store, {"personal": SECRET})  # added under the new label, old one deleted
        self.assertEqual(set(config.load_tokens()), {"personal", "nl"})
        self.assertEqual(config.load_nodes()["lab"]["allowed_tokens"], ["personal", "nl"])
        self.assertEqual(config.load_projects(), {"lab/demo": {"token": "personal"}, "lab/other": {"token": "nl"}})
        self.assert_no_secret()

    def test_rename_adds_new_keychain_entry_before_deleting_old(self):
        order = []
        real_add, real_delete = self.kc.add, self.kc.delete
        with mock.patch.object(keychain, "add", lambda l, v: (order.append(("add", l)), real_add(l, v))[1]), \
                mock.patch.object(keychain, "delete", lambda l: (order.append(("delete", l)), real_delete(l))[1]):
            self.assertEqual(self.post("/api/tokens/update", {"label": "oa", "new_label": "p2"})[0], 200)
        self.assertEqual(order, [("add", "p2"), ("delete", "oa")])

    def test_rename_failures_change_nothing(self):
        self.assertEqual(self.post("/api/tokens/update", {"label": "oa", "new_label": "nl"})[0], 409)
        self.assertEqual(self.post("/api/tokens/update", {"label": "oa", "new_label": "bad label"})[0], 400)
        self.assertEqual(self.post("/api/tokens/update", {"label": "ghost", "new_label": "x"})[0], 404)
        del self.kc.store["oa"]
        status, out = self.post("/api/tokens/update", {"label": "oa", "new_label": "x"})
        self.assertEqual(status, 409)
        self.assertEqual(set(config.load_tokens()), {"oa", "nl"})
        self.assertEqual(config.load_nodes()["lab"]["allowed_tokens"], ["oa", "nl"])

    def test_failed_save_rolls_back_the_new_keychain_entry(self):
        with mock.patch.object(config, "save_tokens", side_effect=OSError("disk full")):
            status, _ = self.post("/api/tokens/update", {"label": "oa", "new_label": "x"})
        self.assertEqual(status, 500)
        self.assertEqual(self.kc.store, {"oa": SECRET})

    def files(self):
        return config.load_tokens(), config.load_nodes(), config.load_projects()

    def assert_usable(self, where):
        """Every label the files name is defined in tokens.json and has its Keychain entry."""
        tokens, nodes, projects = self.files()
        named = {t for n in nodes.values() for t in n.get("allowed_tokens") or []} | \
                {p["token"] for p in projects.values() if p.get("token")}
        self.assertLessEqual(named, set(tokens), where)
        for label, meta in tokens.items():
            if meta.get("kind") != "node-login":
                self.assertEqual(self.kc.store.get(label), SECRET, "%s: %s has no Keychain entry" % (where, label))

    def rename_with_saves(self, fail_from=None, fail_restore=False, after_write=None):
        """Rename oa -> x through the route; save number `fail_from` (1-based) raises, and with `fail_restore`
        every later save does too."""
        real = {f: getattr(config, f) for f in ("save_tokens", "save_nodes", "save_projects")}
        count = [0]

        def wrap(name):
            def save(d):
                count[0] += 1
                if fail_from is not None and (count[0] == fail_from or (fail_restore and count[0] > fail_from)):
                    raise OSError("disk full")
                real[name](d)
                if after_write:
                    after_write("save %d (%s)" % (count[0], name))
            return save

        with mock.patch.multiple(config, **{f: wrap(f) for f in real}):
            return self.post("/api/tokens/update", {"label": "oa", "new_label": "x"})

    def test_rename_is_usable_after_every_write(self):
        config.save_projects({"lab/demo": {"token": "oa"}, "lab/other": {"token": "nl"}})
        states = []
        status, out = self.rename_with_saves(after_write=lambda where: (self.assert_usable(where), states.append(where)))
        self.assertEqual(status, 200, out)
        self.assertEqual(len(states), 4)
        self.assertEqual(self.kc.store, {"x": SECRET})
        self.assertEqual(self.files(), ({"x": {"kind": "oauth", "account": "me", "last4": SECRET[-4:], "expires": None, "added": "x"},
                                         "nl": config.load_tokens()["nl"]},
                                        {"lab": {"ssh": "local", "port": None, "allowed_tokens": ["x", "nl"]}},
                                        {"lab/demo": {"token": "x"}, "lab/other": {"token": "nl"}}))

    def test_rename_failure_at_each_write_restores_everything(self):
        config.save_projects({"lab/demo": {"token": "oa"}})
        before = self.files()
        for step in (1, 2, 3, 4):
            with self.subTest(step=step):
                status, out = self.rename_with_saves(fail_from=step, after_write=self.assert_usable)
                self.assertEqual((status, out), (500, {"error": "OSError: disk full"}))
                self.assertEqual(self.files(), before)
                self.assertEqual(self.kc.store, {"oa": SECRET})  # the new entry is gone, the old one kept

    def test_rename_failed_restore_keeps_both_keychain_entries(self):
        config.save_projects({"lab/demo": {"token": "oa"}})
        for step in (2, 3, 4):
            with self.subTest(step=step):
                status, _ = self.rename_with_saves(fail_from=step, fail_restore=True)
                self.assertEqual(status, 500)
                self.assert_usable("after a failed restore at save %d" % step)
                self.assertEqual(self.kc.store, {"oa": SECRET, "x": SECRET})
                # put things back for the next step
                config.save_tokens({k: v for k, v in config.load_tokens().items() if k != "x"})
                config.save_nodes({"lab": {"ssh": "local", "port": None, "allowed_tokens": ["oa", "nl"]}})
                config.save_projects({"lab/demo": {"token": "oa"}})
                del self.kc.store["x"]

    def test_add_same_label_twice_at_once_keeps_the_first(self):
        entered, release = threading.Event(), threading.Event()
        real_add = self.kc.add

        def slow_add(label, value):
            entered.set()
            release.wait(5)
            real_add(label, value)

        results = {}
        with mock.patch.object(keychain, "add", slow_add):
            t = threading.Thread(target=lambda: results.setdefault("first", self.post(
                "/api/tokens", {"label": "dup", "kind": "oauth", "secret": SECRET})))
            t.start()
            self.assertTrue(entered.wait(5))
            results["second"] = self.post("/api/tokens", {"label": "dup", "kind": "oauth", "secret": "sk-ant-oat01-OTHERSECRET"})
            self.assertEqual(self.post("/api/tokens/update", {"label": "nl", "new_label": "dup"})[0], 409)
            release.set()
            t.join(5)
        self.assertEqual(results["first"][0], 200)
        self.assertEqual(results["second"], (409, {"error": "token dup already exists"}))
        self.assertEqual(self.kc.store["dup"], SECRET)
        self.assertEqual(self.post("/api/tokens", {"label": "dup", "kind": "oauth", "secret": SECRET})[0], 409)
        self.assertNotIn("dup", nodeops.RESERVED)

    def test_failed_add_releases_the_label(self):
        with mock.patch.object(keychain, "add", side_effect=keychain.KeychainError("keychain add failed for z (exit 1)")):
            self.assertEqual(self.post("/api/tokens", {"label": "z", "kind": "oauth", "secret": SECRET})[0], 500)
        self.assertEqual(self.post("/api/tokens", {"label": "z", "kind": "oauth", "secret": SECRET})[0], 200)

    def test_update_account_and_allowed_nodes(self):
        os.makedirs(os.path.join(self.tmp, "x"))
        config.save_nodes({"lab": {"ssh": "local", "port": None, "allowed_tokens": ["oa", "nl"]},
                           "box": {"ssh": "b", "port": None, "allowed_tokens": ["nl"]}})
        status, out = self.post("/api/tokens/update", {"label": "oa", "account": "new@x", "nodes": ["box"]})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["account"], "new@x")
        nodes = config.load_nodes()
        self.assertEqual(nodes["lab"]["allowed_tokens"], ["nl"])
        self.assertEqual(nodes["box"]["allowed_tokens"], ["nl", "oa"])
        self.assertEqual(self.post("/api/tokens/update", {"label": "oa", "nodes": ["ghost"]})[0], 400)
        self.assertEqual(self.post("/api/tokens/update", {"label": "oa", "nodes": "box"})[0], 400)
        self.assertEqual(self.kc.store, {"oa": SECRET})

    def test_remove_refused_while_in_use_then_allowed(self):
        self.set_projects([snap_project(lead="alive", sha=None)])
        self.syncer.sync_now()
        status, out = self.post("/api/tokens/remove", {"label": "oa"})  # demo uses the node's first allowed token
        self.assertEqual(status, 409)
        self.assertIn("lab/demo", out["error"])
        self.assertIn("oa", config.load_tokens())
        self.assertIn("oa", self.kc.store)
        self.assertEqual(self.post("/api/tokens/remove", {"label": "nl"})[0], 200)  # unused token is fine
        self.set_projects([snap_project(lead="dead", review="none", sha=None)])
        self.syncer.sync_now()
        status, out = self.post("/api/tokens/remove", {"label": "oa"})
        self.assertEqual((status, out), (200, {"removed": "oa"}))
        self.assertEqual(self.kc.store, {})
        self.assertEqual(config.load_tokens(), {})
        self.assertEqual(config.load_nodes()["lab"]["allowed_tokens"], [])
        self.assertEqual(self.post("/api/tokens/remove", {"label": "oa"})[0], 404)

    def test_remove_cleans_project_mappings(self):
        config.save_projects({"lab/demo": {"token": "nl"}, "lab/x": {"token": "oa"}})
        self.assertEqual(self.post("/api/tokens/remove", {"label": "nl"})[0], 200)
        self.assertEqual(config.load_projects(), {"lab/x": {"token": "oa"}})


class StateTests(ApiBase):
    def test_in_use_follows_live_sessions_and_project_mapping(self):
        config.save_projects({"lab/b": {"token": "nl"}})
        self.set_projects([snap_project("a", lead="alive", sha=None), snap_project("b", review="alive", sha=None),
                           snap_project("c", sha=None), snap_project("d", lead="dead", sha=None)])
        self.syncer.sync_now()
        toks = {t["label"]: t for t in self.state()["tokens"]}
        self.assertEqual(toks["oa"]["in_use"], ["lab/a"])   # a: no mapping, node's first allowed token
        self.assertEqual(toks["nl"]["in_use"], ["lab/b"])   # b: mapped explicitly
        self.set_projects([snap_project("a", sha=None)])
        self.syncer.sync_now()
        self.assertEqual([t["in_use"] for t in self.state()["tokens"]], [[], []])

    def test_nodes_carry_port(self):
        config.save_nodes({**config.load_nodes(), "box": {"ssh": "b", "port": 2222, "allowed_tokens": []}})
        ports = {n["name"]: n["port"] for n in self.state()["nodes"]}
        self.assertEqual(ports, {"lab": None, "box": 2222})

    def test_in_use_empty_without_snapshot(self):
        os.remove(os.path.join(self.tmp, "local-node", "cairn-node"))
        config.save_nodes({**config.load_nodes(), "new": {"ssh": "local", "port": None, "allowed_tokens": ["oa"]}})
        st = self.state()
        self.assertTrue(all(t["in_use"] == [] for t in st["tokens"]))

    def test_state_has_mirror_per_project(self):
        self.set_projects([snap_project("demo", sha="abc1234", path="/srv/demo"), snap_project("quiet", sha=None)])
        self.syncer.sync_now()
        self.assertTrue(self.syncer.mirror.wait_idle())
        projects = self.state()["projects"]
        m = projects["lab/demo"]["mirror"]
        self.assertEqual(set(m), {"path", "sha", "synced_at", "error"})
        self.assertEqual((m["sha"], m["error"], m["path"]), ("abc1234", None, mirror.mirror_path("lab", "demo")))
        self.assertTrue(m["synced_at"])
        self.assertEqual(projects["lab/demo"]["token"], "oa")
        self.assertIsNone(projects["lab/quiet"]["mirror"])


class ProjectRouteTests(ApiBase):
    BODY = {"node": "lab", "project": "newp", "repo": {"kind": "new"}, "destination": "ship it",
            "notes": "n", "tickets": [{"title": "t1", "type": "build", "question": "q"}], "token": "oa", "start": False}

    def inits(self):
        return [e for e in self.log() if e["argv"][0] == "init"]

    def test_create_maps_token_and_passes_spec_to_init(self):
        status, out = self.post("/api/projects", {**self.BODY, "path": "/srv/newp"})
        self.assertEqual(status, 200, out)
        self.assertEqual(out, {"project": "newp", "path": "/tmp/newp", "started": False, "start_error": None})
        self.assertEqual(config.load_projects(), {"lab/newp": {"token": "oa"}})
        (init,) = self.inits()
        self.assertEqual(init["argv"], ["init", "newp"])
        self.assertEqual(json.loads(init["stdin"]), {"path": "/srv/newp", "repo": {"kind": "new"}, "destination": "ship it",
                                                     "notes": "n", "tickets": self.BODY["tickets"]})
        self.assertEqual(self.starts(), [])

    def test_default_path_is_left_to_the_node(self):
        body = {**self.BODY, "repo": {"kind": "clone", "url": "https://git.example/x.git"}}
        with mock.patch.object(ssh, "run_shell", side_effect=AssertionError("no extra ssh call")):
            status, out = self.post("/api/projects", body)
        self.assertEqual(status, 200, out)
        self.assertEqual(out["path"], "/tmp/newp")  # the node's answer
        spec = json.loads(self.inits()[0]["stdin"])
        self.assertNotIn("path", spec)
        self.assertEqual(spec["repo"], {"kind": "clone", "url": "https://git.example/x.git"})

    def test_start_uses_the_chosen_token_on_stdin(self):
        status, out = self.post("/api/projects", {**self.BODY, "start": True})
        self.assertEqual((status, out["started"], out["start_error"]), (200, True, None))
        self.assertEqual([e["stdin"] for e in self.starts()], [SECRET])
        self.assert_no_secret()

    def test_start_error_is_reported_not_raised(self):
        with mock.patch.dict(os.environ, {"FAKE_NODE_START_FAIL": "1"}):
            status, out = self.post("/api/projects", {**self.BODY, "start": True})
        self.assertEqual(status, 200)
        self.assertEqual(out["started"], False)
        self.assertIn("Not logged in", out["start_error"])
        self.assertEqual(out["project"], "newp")
        self.assertEqual(config.load_projects(), {"lab/newp": {"token": "oa"}})  # the project exists

    def test_token_must_be_allowed_on_the_node(self):
        config.save_tokens({**config.load_tokens(), "other": {"kind": "node-login"}})
        status, out = self.post("/api/projects", {**self.BODY, "token": "other"})
        self.assertEqual(status, 400)
        self.assertIn("not allowed on node lab", out["error"])
        self.assertEqual(self.post("/api/projects", {**self.BODY, "token": "ghost"})[0], 400)
        self.assertEqual(self.inits(), [])
        self.assertEqual(config.load_projects(), {})

    def test_validation(self):
        for patch in ({"project": "Bad Name"}, {"project": "../x"}, {"repo": {"kind": "weird"}}, {"repo": None},
                      {"repo": {"kind": "clone"}}, {"repo": {"kind": "clone", "url": "--upload-pack=x"}},
                      {"destination": ""}, {"destination": None}, {"path": "relative/dir"}, {"tickets": "x"},
                      {"tickets": ["x"]}, {"notes": 5}):
            self.assertEqual(self.post("/api/projects", {**self.BODY, **patch})[0], 400, patch)
        self.assertEqual(self.post("/api/projects", {**self.BODY, "node": "ghost"})[0], 404)
        self.assertEqual(self.inits(), [])

    def test_init_failure_saves_no_mapping(self):
        with mock.patch.object(ssh, "run_node", side_effect=ssh.NodeError("work/map.md already exists")):
            status, out = self.post("/api/projects", self.BODY)
        self.assertEqual((status, out), (502, {"error": "work/map.md already exists"}))
        self.assertEqual(config.load_projects(), {})

    def test_create_syncs_the_node_afterwards(self):
        self.post("/api/projects", self.BODY)
        log = [e["argv"][0] for e in self.log()]
        self.assertEqual(log[log.index("init") + 1:].count("snapshot"), 1)

    def test_project_token_route(self):
        status, out = self.post("/api/projects/token", {"node": "lab", "project": "demo", "token": "nl"})
        self.assertEqual(status, 200, out)
        self.assertEqual(config.load_projects(), {"lab/demo": {"token": "nl"}})
        self.assertEqual(self.starts(), [])  # effective at the next start only
        config.save_tokens({**config.load_tokens(), "other": {"kind": "node-login"}})
        self.assertEqual(self.post("/api/projects/token", {"node": "lab", "project": "demo", "token": "other"})[0], 400)
        self.assertEqual(self.post("/api/projects/token", {"node": "lab", "project": "demo", "token": "ghost"})[0], 400)
        self.assertEqual(self.post("/api/projects/token", {"node": "lab", "project": "../x", "token": "oa"})[0], 400)
        self.assertEqual(self.post("/api/projects/token", {"node": "ghost", "project": "demo", "token": "oa"})[0], 404)
        self.assertEqual(config.load_projects(), {"lab/demo": {"token": "nl"}})
        # the next start uses it
        self.assertEqual(self.req("/api/start", "POST", {"node": "lab", "project": "demo"})[0], 200)
        self.assertEqual([e["stdin"] for e in self.starts()], [""])


class TicketRouteTests(ApiBase):
    def tickets(self):
        return [e for e in self.log() if e["argv"][0] == "ticket"]

    def test_create_passthrough_then_one_sync(self):
        body = {"node": "lab", "project": "demo", "op": "create", "title": "T", "type": "task", "question": "Q?",
                "blocked_by": ["01"]}
        status, out = self.post("/api/tickets", body)
        self.assertEqual((status, out), (200, {"id": "01", "path": "work/tickets/01-x.md", "commit": "abc1234"}))
        (t,) = self.tickets()
        self.assertEqual(t["argv"], ["ticket", "demo"])
        self.assertEqual(json.loads(t["stdin"]), {"op": "create", "title": "T", "type": "task", "question": "Q?",
                                                  "blocked_by": ["01"]})
        log = self.log()
        idx = max(i for i, e in enumerate(log) if e["argv"][0] == "ticket")
        self.assertEqual([e["argv"][0] for e in log[idx + 1:]], ["snapshot"])

    def test_other_ops_pass_through(self):
        for body in ({"op": "close", "id": "01", "resolution": "done"}, {"op": "out-of-scope", "id": "01", "reason": "no"},
                     {"op": "blockers", "id": "01", "blocked_by": []}):
            self.assertEqual(self.post("/api/tickets", {"node": "lab", "project": "demo", **body})[0], 200, body)
        self.assertEqual([json.loads(t["stdin"])["op"] for t in self.tickets()], ["close", "out-of-scope", "blockers"])

    def test_node_refusal_surfaces(self):
        with mock.patch.dict(os.environ, {"FAKE_NODE_TICKET_FAIL": "1"}):
            status, out = self.post("/api/tickets", {"node": "lab", "project": "demo", "op": "close", "id": "01",
                                                     "resolution": "x"})
        self.assertEqual((status, out), (502, {"error": "ticket 01 is claimed by lead"}))

    def test_validation(self):
        self.assertEqual(self.post("/api/tickets", {"node": "lab", "project": "demo", "op": "delete"})[0], 400)
        self.assertEqual(self.post("/api/tickets", {"node": "lab", "project": "demo"})[0], 400)
        self.assertEqual(self.post("/api/tickets", {"node": "lab", "project": "../x", "op": "close"})[0], 400)
        self.assertEqual(self.post("/api/tickets", {"node": "ghost", "project": "demo", "op": "close"})[0], 404)
        self.assertEqual(self.tickets(), [])


class MirrorTests(M2Base):
    def setUp(self):
        super().setUp()
        self.git = GitRecorder()
        self.clock = Clock()
        self.m = mirror.Mirror(run=self.git, clock=self.clock)
        self.dest = os.path.join(self.tmp, "mirror", "lab", "demo.git")

    def sync(self, node=None, **kw):
        node = node or {"ssh": "local", "port": None}
        self.m.sync_node("lab", node, {"projects": [snap_project(**kw)]})

    def test_first_sync_clones_beside_the_mirror_then_moves_it_into_place(self):
        self.sync(sha="aaa1111")
        (argv, kw), *config_calls = self.git.calls
        self.assertEqual(argv, ["git", "clone", "--mirror", "--", "/srv/demo", self.dest + ".partial"])
        self.assertTrue(mirror.is_mirror(self.dest))
        self.assertFalse(os.path.exists(self.dest + ".partial"))
        self.assertEqual(self.m.get("lab", "demo")["sha"], "aaa1111")
        self.assertIsNone(self.m.get("lab", "demo")["error"])

    def test_clone_keeps_history_settings(self):
        self.sync(sha="aaa1111")
        sets = [argv[3:] for argv, _ in self.git.calls if "config" in argv]
        self.assertEqual(sets, [["config", "core.logAllRefUpdates", "always"], ["config", "gc.reflogExpire", "never"],
                                ["config", "gc.reflogExpireUnreachable", "never"]])
        self.assertTrue(all(argv[2] == self.dest + ".partial" for argv, _ in self.git.calls[1:]))  # set before the move

    def test_existing_mirror_gets_missing_history_settings_once(self):
        os.makedirs(os.path.join(self.dest, "objects"))
        open(os.path.join(self.dest, "HEAD"), "w").close()

        def run(argv, **kw):
            if argv[3:5] == ["config", "--get"]:
                self.git.calls.append((argv, kw))
                present = argv[5] == "gc.reflogExpire"  # this one is already set
                return mock.Mock(returncode=0 if present else 1, stdout="never\n" if present else "", stderr="")
            return self.git(argv, **kw)

        self.m.run = run
        self.sync(sha="aaa1111")
        sets = [argv[4:] for argv, _ in self.git.calls if "config" in argv and "--get" not in argv]
        self.assertEqual(sets, [["core.logAllRefUpdates", "always"], ["gc.reflogExpireUnreachable", "never"]])
        self.assertEqual(self.git.transfers()[0][3], "fetch")
        n = len(self.git.calls)
        self.sync(sha="bbb2222")
        self.assertEqual([argv[3] for argv, _ in self.git.calls[n:]], ["fetch"])  # checked once per hub run

    def test_skips_when_sha_unchanged_and_fetches_when_changed(self):
        self.sync(sha="aaa1111")
        self.sync(sha="aaa1111")
        self.assertEqual(len(self.git.transfers()), 1)
        self.sync(sha="bbb2222")
        self.assertEqual(len(self.git.transfers()), 2)
        self.assertEqual(self.git.transfers()[1],
                         ["git", "--git-dir", self.dest, "fetch", "--prune", "--", "/srv/demo", "+refs/*:refs/*"])
        self.assertEqual(self.m.get("lab", "demo")["sha"], "bbb2222")
        self.sync(sha="bbb2222")
        self.assertEqual(len(self.git.transfers()), 2)

    def test_leftover_without_head_or_objects_is_recloned(self):
        os.makedirs(self.dest)
        self.write(os.path.join(self.dest, "junk"), "half a clone")
        os.makedirs(self.dest + ".partial")  # a hub killed mid-clone
        self.sync(sha="aaa1111")
        self.assertEqual(self.git.transfers()[0][1], "clone")
        self.assertTrue(mirror.is_mirror(self.dest))
        self.assertFalse(os.path.exists(os.path.join(self.dest, "junk")))
        self.assertIsNone(self.m.get("lab", "demo")["error"])

    def test_ssh_node_uses_url_and_contract_ssh_options(self):
        self.sync(node={"ssh": "agent@box", "port": 2222}, sha="aaa1111")
        argv, kw = self.git.calls[0]
        self.assertEqual(argv[4], "ssh://agent@box:2222/srv/demo")
        cmd = kw["env"]["GIT_SSH_COMMAND"]
        for opt in ("BatchMode=yes", "ConnectTimeout=5", "ControlMaster=auto", "ControlPersist=120",
                    "StrictHostKeyChecking=accept-new", self.tmp + "/cm-%C", self.tmp + "/known_hosts"):
            self.assertIn(opt, cmd)
        self.assertTrue(cmd.startswith("ssh "))
        self.assertEqual(kw["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_ssh_url_without_port(self):
        self.sync(node={"ssh": "box", "port": None}, sha="aaa1111")
        self.assertEqual(self.git.calls[0][0][4], "ssh://box/srv/demo")

    def test_errors_recorded_per_project_and_retried_after_backoff(self):
        self.m.run = GitRecorder(fail=True)
        self.m.sync_node("lab", {"ssh": "box", "port": None},
                         {"projects": [snap_project("a", sha="aaa1111", path="/srv/a"), snap_project("b", sha="bbb2222", path="/srv/b")]})
        for name in "ab":
            s = self.m.get("lab", name)
            self.assertEqual(s["error"], "ssh: connect refused")
            self.assertIsNone(s["sha"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "mirror", "lab", "a.git")))  # no half clone left
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "mirror", "lab", "a.git.partial")))
        ok = GitRecorder()
        self.m.run = ok
        a = {"projects": [snap_project("a", sha="aaa1111", path="/srv/a")]}
        self.m.sync_node("lab", {"ssh": "box", "port": None}, a)
        self.assertEqual(ok.calls, [])  # still backing off
        self.clock.t += mirror.BACKOFF_MIN
        self.m.sync_node("lab", {"ssh": "box", "port": None}, a)
        self.assertEqual(len(ok.transfers()), 1)
        self.assertIsNone(self.m.get("lab", "a")["error"])
        self.assertEqual(self.m.get("lab", "a")["sha"], "aaa1111")

    def test_backoff_doubles_up_to_the_cap_and_resets_on_success(self):
        fail = GitRecorder(fail=True)
        self.m.run = fail
        self.sync(sha="aaa1111")
        for delay in (30, 60, 120, 240, 480, 960, 1800, 1800):
            n = len(fail.calls)
            self.clock.t += delay - 1
            self.sync(sha="aaa1111")
            self.assertEqual(len(fail.calls), n, "retried before %d s" % delay)
            self.clock.t += 1
            self.sync(sha="aaa1111")
            self.assertEqual(len(fail.calls), n + 1, "not retried after %d s" % delay)
        self.m.run = self.git
        self.clock.t += mirror.BACKOFF_MAX
        self.sync(sha="aaa1111")
        self.assertIsNone(self.m.get("lab", "demo")["error"])
        self.m.run = fail
        self.sync(sha="bbb2222")
        self.clock.t += mirror.BACKOFF_MIN
        n = len(fail.calls)
        self.sync(sha="bbb2222")
        self.assertEqual(len(fail.calls), n + 1)  # back to the first delay

    def test_fetch_error_keeps_last_good_sha(self):
        self.sync(sha="aaa1111")
        good_stamp = self.m.get("lab", "demo")["synced_at"]
        self.m.run = GitRecorder(fail=True)
        self.sync(sha="bbb2222")
        s = self.m.get("lab", "demo")
        self.assertEqual((s["sha"], s["synced_at"]), ("aaa1111", good_stamp))
        self.assertEqual(s["error"], "ssh: connect refused")
        self.assertTrue(mirror.is_mirror(s["path"]))  # a failed fetch leaves the existing mirror alone

    def test_git_missing_or_hanging_never_raises(self):
        self.m.run = mock.Mock(side_effect=FileNotFoundError(2, "No such file", "git"))
        self.sync(sha="aaa1111")
        self.assertIn("cannot run git", self.m.get("lab", "demo")["error"])
        import subprocess
        self.clock.t += mirror.BACKOFF_MAX
        self.m.run = mock.Mock(side_effect=subprocess.TimeoutExpired("git", 1))
        self.sync(sha="aaa1111")
        self.assertIn("timed out", self.m.get("lab", "demo")["error"])
        self.clock.t += mirror.BACKOFF_MAX
        self.m.run = mock.Mock(side_effect=RuntimeError("boom"))
        self.sync(sha="aaa1111")
        self.assertIn("boom", self.m.get("lab", "demo")["error"])

    def test_concurrent_syncs_of_one_project_mirror_once(self):
        gate = threading.Event()
        real = self.git

        def slow(argv, **kw):
            gate.wait(5)
            return real(argv, **kw)

        self.m.run = slow
        ts = [threading.Thread(target=self.sync, kwargs={"sha": "aaa1111"}) for _ in range(2)]
        for t in ts:
            t.start()
        gate.set()
        for t in ts:
            t.join()
        self.assertEqual(len(self.git.transfers()), 1)
        self.assertIsNone(self.m.get("lab", "demo")["error"])

    def test_project_without_commits_is_skipped(self):
        self.sync(sha=None)
        self.assertEqual(self.git.calls, [])
        self.assertIsNone(self.m.get("lab", "demo"))

    def test_failing_mirror_does_not_fail_the_sync(self):
        self.set_projects([snap_project(sha="abc1234")])
        syncer = Syncer(interval=3600, mirror=mirror.Mirror(run=GitRecorder(fail=True)))
        self.addCleanup(syncer.stop)
        syncer.sync_now()
        self.assertTrue(syncer.mirror.wait_idle())
        node = syncer.state()["nodes"][0]
        self.assertTrue(node["ok"])
        self.assertEqual(node["snapshot"]["host"], "fakehost")
        self.assertEqual(syncer.state()["projects"]["lab/demo"]["mirror"]["error"], "ssh: connect refused")

    def test_sync_runs_mirror_only_on_sha_change(self):
        git = GitRecorder()
        syncer = Syncer(interval=3600, mirror=mirror.Mirror(run=git))
        self.addCleanup(syncer.stop)
        self.set_projects([snap_project(sha="abc1234")])
        for sha, transfers in (("abc1234", 1), ("abc1234", 1), ("def5678", 2)):
            self.set_projects([snap_project(sha=sha)])
            syncer.sync_now()
            self.assertTrue(syncer.mirror.wait_idle())
            self.assertEqual(len(git.transfers()), transfers)

    def test_sync_now_retries_a_failing_mirror_at_once_but_the_loop_waits(self):
        fail = GitRecorder(fail=True)
        syncer = Syncer(interval=3600, mirror=mirror.Mirror(run=fail, clock=self.clock))
        self.addCleanup(syncer.stop)
        self.set_projects([snap_project(sha="abc1234")])
        syncer.sync_now()
        self.assertTrue(syncer.mirror.wait_idle())
        syncer._sync_node("lab")  # what the 30 s loop does
        self.assertTrue(syncer.mirror.wait_idle())
        self.assertEqual(len(fail.calls), 1)
        syncer.sync_now()  # what POST /api/sync does
        self.assertTrue(syncer.mirror.wait_idle())
        self.assertEqual(len(fail.calls), 2)


class MirrorWorkerTests(M2Base):
    """Finding 6: syncs and requests only enqueue; one worker runs git."""

    def setUp(self):
        super().setUp()
        self.gate = threading.Event()
        self.addCleanup(lambda: self.syncer.mirror.wait_idle())  # runs last: no git after CAIRN_HOME is restored
        self.addCleanup(self.gate.set)
        self.git = GitRecorder()
        self.ran_on = []
        self.in_git = threading.Event()

        def blocked(argv, **kw):
            self.ran_on.append(threading.current_thread().name)
            self.in_git.set()
            self.gate.wait(10)
            return self.git(argv, **kw)

        self.syncer = Syncer(interval=3600, mirror=mirror.Mirror(run=blocked))
        self.syncer.refresh_nodes = lambda: None
        self.addCleanup(self.syncer.stop)

    def test_sync_stores_the_snapshot_without_waiting_for_git(self):
        self.set_projects([snap_project(sha="aaa1111")])
        t = threading.Thread(target=self.syncer.sync_one, args=("lab",))
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "the sync waited for the mirror")
        self.assertTrue(self.syncer.state()["nodes"][0]["ok"])
        self.assertEqual(self.git.calls, [])
        self.gate.set()
        self.assertTrue(self.syncer.mirror.wait_idle())
        self.assertEqual(len(self.git.transfers()), 1)
        self.assertEqual(set(self.ran_on), {"mirror"})  # never the sync or request thread

    def test_queued_snapshots_of_one_project_coalesce_to_the_newest(self):
        self.set_projects([snap_project(sha="aaa1111")])
        self.syncer.sync_one("lab")
        self.assertTrue(self.in_git.wait(5))
        for sha in ("bbb2222", "ccc3333", "ddd4444"):  # arrive while the first clone runs
            self.set_projects([snap_project(sha=sha)])
            self.syncer.sync_one("lab")
        self.gate.set()
        self.assertTrue(self.syncer.mirror.wait_idle())
        self.assertEqual([argv[1] if argv[1] == "clone" else argv[3] for argv in self.git.transfers()], ["clone", "fetch"])
        self.assertEqual(self.syncer.state()["projects"]["lab/demo"]["mirror"]["sha"], "ddd4444")

    def test_ticket_request_returns_while_a_clone_runs(self):
        self.set_projects([snap_project(sha="aaa1111")])
        srv = CairnServer(0, self.syncer, ui_dir=os.path.join(self.tmp, "no-ui"), opener=lambda c: None)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        r = urllib.request.Request("http://127.0.0.1:%d/api/tickets" % srv.port, method="POST",
                                   data=json.dumps({"node": "lab", "project": "demo", "op": "create", "title": "t",
                                                    "type": "build", "question": "q"}).encode(),
                                   headers={"Origin": "http://127.0.0.1:%d" % srv.port, "X-Cairn-Token": srv.token,
                                            "Cookie": "%s=%s" % (srv.cookie_name, srv.secret)})
        with urllib.request.urlopen(r, timeout=5) as resp:  # the gate is still closed
            self.assertEqual(resp.status, 200)
        self.assertFalse(self.gate.is_set())
        self.assertEqual(self.git.calls, [])

    def test_stop_drops_the_queue_and_ends_the_worker(self):
        self.set_projects([snap_project("a", sha="aaa1111", path="/srv/a"), snap_project("b", sha="bbb2222", path="/srv/b")])
        self.syncer.sync_one("lab")
        self.assertTrue(self.in_git.wait(5))
        worker = self.syncer.mirror.worker
        self.syncer.stop()
        self.gate.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.git.transfers()), 1)  # b was still queued
        self.assertTrue(self.syncer.mirror.wait_idle(1))


class MirrorRealGitTests(M2Base):
    """Finding 11 against real git: a reset on the node does not erase the commit from the mirror."""

    def git(self, *argv, cwd=None):
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example", *argv], cwd=cwd,
                           capture_output=True, text=True, check=True)
        return r.stdout.strip()

    def test_mirror_keeps_commits_a_reset_dropped(self):
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        self.git("init", "-q", "-b", "main", cwd=src)
        self.git("commit", "-q", "--allow-empty", "-m", "one", cwd=src)
        one = self.git("rev-parse", "HEAD", cwd=src)
        self.git("commit", "-q", "--allow-empty", "-m", "two", cwd=src)
        two = self.git("rev-parse", "HEAD", cwd=src)
        dest = mirror.mirror_path("lab", "demo")
        os.makedirs(os.path.dirname(dest))
        self.git("clone", "-q", "--mirror", "--", src, dest)  # a mirror made before the history settings existed
        m = mirror.Mirror()
        node = {"ssh": "local", "port": None}
        self.git("commit", "-q", "--allow-empty", "-m", "three", cwd=src)
        three = self.git("rev-parse", "HEAD", cwd=src)
        m.sync_node("lab", node, {"projects": [snap_project(sha=three, path=src)]})
        self.assertIsNone(m.get("lab", "demo")["error"])
        for k, v in mirror.KEEP_HISTORY:
            self.assertEqual(self.git("--git-dir", dest, "config", "--get", k), v)
        self.git("reset", "-q", "--hard", one, cwd=src)  # the lead throws away two and three
        m.sync_node("lab", node, {"projects": [snap_project(sha=one, path=src)]})
        self.assertEqual(self.git("--git-dir", dest, "rev-parse", "refs/heads/main"), one)
        reflog = self.git("--git-dir", dest, "reflog", "show", "--format=%H", "refs/heads/main")
        self.assertIn(three, reflog.split())
        self.git("--git-dir", dest, "gc", "-q", "--prune=now")
        self.git("--git-dir", dest, "cat-file", "-e", three)
        self.git("--git-dir", dest, "cat-file", "-e", two)

    def test_fresh_clone_gets_the_settings(self):
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        self.git("init", "-q", "-b", "main", cwd=src)
        self.git("commit", "-q", "--allow-empty", "-m", "one", cwd=src)
        m = mirror.Mirror()
        m.sync_node("lab", {"ssh": "local", "port": None},
                    {"projects": [snap_project(sha=self.git("rev-parse", "HEAD", cwd=src), path=src)]})
        dest = mirror.mirror_path("lab", "demo")
        self.assertIsNone(m.get("lab", "demo")["error"])
        for k, v in mirror.KEEP_HISTORY:
            self.assertEqual(self.git("--git-dir", dest, "config", "--get", k), v)


class CliParityTests(M2Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_project_token(self):
        self.assertEqual(self.run_cli("project", "token", "lab", "demo", "nl")[0], 0)
        self.assertEqual(config.load_projects(), {"lab/demo": {"token": "nl"}})
        config.save_tokens({**config.load_tokens(), "other": {"kind": "node-login"}})
        code, _, err = self.run_cli("project", "token", "lab", "demo", "other")
        self.assertEqual(code, 1)
        self.assertIn("not allowed on node lab", err)
        self.assertEqual(self.run_cli("project", "token", "lab", "demo", "ghost")[0], 1)
        self.assertEqual(config.load_projects(), {"lab/demo": {"token": "nl"}})

    def test_ticket_builds_the_same_op_as_the_api(self):
        code, out, _ = self.run_cli("ticket", "lab", "demo", "create", "--title", "T", "--type", "build",
                                    "--question", "Q", "--blocked-by", "01,02")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["id"], "01")
        self.run_cli("ticket", "lab", "demo", "close", "--id", "03", "--resolution", "done")
        self.run_cli("ticket", "lab", "demo", "out-of-scope", "--id", "03", "--reason", "nope")
        self.run_cli("ticket", "lab", "demo", "blockers", "--id", "03", "--blocked-by", "")
        ops = [json.loads(e["stdin"]) for e in self.log() if e["argv"][0] == "ticket"]
        self.assertEqual(ops, [{"op": "create", "title": "T", "type": "build", "question": "Q", "blocked_by": ["01", "02"]},
                               {"op": "close", "id": "03", "resolution": "done"},
                               {"op": "out-of-scope", "id": "03", "reason": "nope"},
                               {"op": "blockers", "id": "03", "blocked_by": []}])

    def test_ticket_refusal_is_an_error_exit(self):
        with mock.patch.dict(os.environ, {"FAKE_NODE_TICKET_FAIL": "1"}):
            code, _, err = self.run_cli("ticket", "lab", "demo", "close", "--id", "01", "--resolution", "x")
        self.assertEqual(code, 1)
        self.assertIn("claimed by lead", err)

    def test_node_rm(self):
        self.set_projects([snap_project(lead="alive", sha=None)])
        code, _, err = self.run_cli("node", "rm", "lab")
        self.assertEqual(code, 1)
        self.assertIn("live lead or review", err)
        self.assertIn("lab", config.load_nodes())
        self.set_projects([snap_project(sha=None)])
        self.assertEqual(self.run_cli("node", "rm", "lab")[0], 0)
        self.assertEqual(config.load_nodes(), {})
        self.assertEqual(self.run_cli("node", "rm", "lab")[0], 1)

    def test_node_rm_unreachable_node_needs_force(self):
        config.save_projects({"lab/demo": {"token": "oa"}})
        os.remove(os.path.join(self.tmp, "local-node", "cairn-node"))
        code, _, err = self.run_cli("node", "rm", "lab")
        self.assertEqual(code, 1)
        self.assertIn("unreachable or not synced", err)
        self.assertIn("lab", config.load_nodes())
        self.assertEqual(self.run_cli("node", "rm", "lab", "--force")[0], 0)
        self.assertEqual((config.load_nodes(), config.load_projects()), ({}, {}))

    def test_token_rm_shares_the_in_use_refusal(self):
        self.set_projects([snap_project(review="alive", sha=None)])
        code, _, err = self.run_cli("token", "rm", "oa")
        self.assertEqual(code, 1)
        self.assertIn("in use by lab/demo", err)
        self.set_projects([snap_project(sha=None)])
        self.assertEqual(self.run_cli("token", "rm", "oa")[0], 0)
        self.assertNotIn("oa", self.kc.store)
        self.assertEqual(config.load_nodes()["lab"]["allowed_tokens"], ["nl"])

    def test_token_add_oauth_uses_hidden_prompt_and_shared_function(self):
        with mock.patch("getpass.getpass", return_value=SECRET):
            code, out, _ = self.run_cli("token", "add", "work", "--account", "w")
        self.assertEqual(code, 0)
        self.assertEqual(self.kc.store["work"], SECRET)
        self.assertNotIn(SECRET, out)
        self.assert_no_secret()

    def test_token_add_replaces_under_the_same_label(self):
        with mock.patch("getpass.getpass", return_value=SECOND):
            code, _, err = self.run_cli("token", "add", "oa", "--kind", "oauth")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.kc.store["oa"], SECOND)
        self.assertIn("oa", config.load_nodes()["lab"]["allowed_tokens"])


class SecretsNeverLoggedTests(ApiBase):
    def test_full_flow_leaves_no_secret_in_output_or_responses(self):
        seen = []
        for path, body in (
                ("/api/tokens", {"label": "w", "kind": "oauth", "secret": SECRET, "nodes": ["lab"]}),
                ("/api/tokens", {"label": "bad", "kind": "oauth", "secret": SECRET}),  # duplicate secret, fresh label
                ("/api/tokens/update", {"label": "w", "new_label": "w2"}),
                ("/api/projects", {**ProjectRouteTests.BODY, "token": "w2", "start": True}),
                ("/api/projects/token", {"node": "lab", "project": "newp", "token": "oa"}),
                ("/api/tokens/remove", {"label": "w2"}),
                ("/api/tokens", {"label": "oa", "kind": "oauth", "secret": SECOND, "replace": True})):
            status, raw = self.req(path, "POST", body)
            seen.append(raw.decode())
        seen.append(self.req("/api/state")[1].decode())
        seen.append(self.req("/")[1].decode())
        for text in seen:
            for s in (SECRET, SECOND):
                self.assertNotIn(s, text)
        self.assert_no_secret()
        # the only place the value travelled outside the Keychain: the start call's stdin
        self.assertIn(SECRET, [e["stdin"] for e in self.starts()])
        for e in self.log():
            self.assertNotIn(SECRET, " ".join(e["argv"]))


if __name__ == "__main__":
    unittest.main()
