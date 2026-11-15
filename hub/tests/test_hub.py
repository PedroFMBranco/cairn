import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
import sys
sys.path.insert(0, os.path.dirname(HERE))

from cairn_hub import config, keychain, nodeops, ssh
from cairn_hub.autostart import AutoStarter
from cairn_hub.server import CairnServer
from cairn_hub.sync import Syncer

FAKE = os.path.join(HERE, "fixtures", "fake-cairn-node")
SECRET = "sk-ant-oat01-SECRETVALUE1234"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cairn-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"CAIRN_HOME": self.tmp, "FAKE_NODE_LOG": os.path.join(self.tmp, "log")})
        env.start()
        self.addCleanup(env.stop)

    def write(self, path, text):
        with open(path, "w") as f:
            f.write(text)

    def log(self):
        p = os.path.join(self.tmp, "log")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(l) for l in f]

    def add_local_node(self, tokens=()):
        os.makedirs(os.path.join(self.tmp, "local-node"))
        shutil.copy(FAKE, os.path.join(self.tmp, "local-node", "cairn-node"))
        config.save_nodes({"lab": {"ssh": "local", "port": None, "allowed_tokens": list(tokens)}})


class ConfigTests(Base):
    def test_round_trip_and_atomic(self):
        config.save_nodes({"a": {"ssh": "h", "port": 2222, "allowed_tokens": ["x"]}})
        self.assertEqual(config.load_nodes()["a"]["port"], 2222)
        self.assertEqual(os.stat(config.path("nodes.json")).st_mode & 0o777, 0o600)
        self.assertEqual([f for f in os.listdir(self.tmp) if f != "nodes.json"], [])
        config.save_tokens({"t": {"kind": "oauth", "last4": "1234"}})
        config.save_projects({"a/p": {"token": "t"}})
        self.assertEqual(config.load_tokens()["t"]["last4"], "1234")
        self.assertEqual(config.token_label_for("a", "p"), "t")
        self.assertEqual(config.token_label_for("a", "other"), "x")

    def test_missing_files_default_empty(self):
        self.assertEqual(config.load_nodes(), {})

    def test_public_tokens_have_no_value(self):
        config.save_tokens({"t": {"kind": "oauth", "last4": "1234", "account": "a", "expires": None}})
        self.assertEqual(config.public_tokens()[0]["last4"], "1234")


class KeychainTests(Base):
    def test_secret_never_in_argv(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            keychain.add("lbl", SECRET)
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["security", "-i"])
        self.assertNotIn(SECRET, " ".join(argv))
        self.assertIn(SECRET, run.call_args.kwargs["input"])

    def test_failure_message_has_no_secret(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stdout="", stderr=SECRET)):
            with self.assertRaises(keychain.KeychainError) as cm:
                keychain.add("lbl", SECRET)
        self.assertNotIn(SECRET, str(cm.exception))

    def test_get_and_delete_argv(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=SECRET + "\n", stderr="")) as run:
            self.assertEqual(keychain.get("lbl"), SECRET)
            keychain.delete("lbl")
        self.assertEqual(run.call_args_list[0].args[0],
                         ["security", "find-generic-password", "-s", "cairn", "-a", "lbl", "-w"])
        self.assertEqual(run.call_args_list[1].args[0][1], "delete-generic-password")


class SshTests(Base):
    def test_remote_argv_options_port_and_stdin(self):
        node = {"ssh": "box", "port": 2222}
        with mock.patch.object(ssh, "run_capped", return_value=(0, b"{}", b"")) as run:
            ssh.run_node(node, ["start", "demo"], stdin=SECRET + "\n")
        argv = run.call_args.args[0]
        self.assertNotIn(SECRET, " ".join(argv))
        self.assertEqual(run.call_args.args[1], SECRET + "\n")
        j = " ".join(argv)
        for opt in ("BatchMode=yes", "ConnectTimeout=5", "ForwardAgent=no", "ControlMaster=auto", "ControlPersist=120",
                    "StrictHostKeyChecking=accept-new", "-p 2222"):
            self.assertIn(opt, j)
        self.assertIn(self.tmp + "/cm-%C", j)
        self.assertTrue(argv[-1].startswith("~/.cairn/cairn-node start demo"))

    def test_error_json_is_surfaced(self):
        self.add_local_node()
        with self.assertRaises(ssh.NodeError) as cm:
            ssh.run_node(config.load_nodes()["lab"], ["bogus"])
        self.assertIn("unknown command", str(cm.exception))

    def test_attach_command(self):
        cmd = ssh.attach_command({"ssh": "box", "port": 2222}, "p")
        self.assertEqual(cmd, "ssh -t -a -o ForwardAgent=no -p 2222 -o UserKnownHostsFile=%s/known_hosts "
                              "-o StrictHostKeyChecking=accept-new box tmux -L cairn attach -t p-review" % self.tmp)


class ServerTests(Base):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.tmp, "keep"))
        config.save_tokens({"oa": {"kind": "oauth", "account": "me", "last4": SECRET[-4:], "expires": None, "added": "x"},
                            "nl": {"kind": "node-login", "account": "", "last4": None, "expires": None, "added": "x"}})
        self.add_local_node(["oa", "nl"])
        self.opened = []
        self.syncer = Syncer(interval=3600)
        self.srv = CairnServer(0, self.syncer, ui_dir=os.path.join(self.tmp, "no-ui"), opener=self.opened.append)
        self.port = self.srv.port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.syncer.sync_now()
        self.addCleanup(self.syncer.stop)

    def req(self, path, method="GET", body=None, headers=None):
        h = {"Origin": "http://127.0.0.1:%d" % self.port, "X-Cairn-Token": self.srv.token,
             "Content-Type": "application/json", "Cookie": "%s=%s" % (self.srv.cookie_name, self.srv.secret)}
        if headers is not None:
            h = {k: v for k, v in {**h, **headers}.items() if v is not None}
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), method=method,
                                   data=json.dumps(body or {}).encode() if method == "POST" else None, headers=h)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_binds_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_index_injects_token_placeholder(self):
        status, body = self.req("/")
        self.assertEqual(status, 200)
        self.assertIn('<meta name="cairn-token" content="%s">' % self.srv.token, body.decode())

    def test_index_injects_token_into_real_ui(self):
        ui = os.path.join(self.tmp, "ui")
        os.makedirs(ui)
        self.write(os.path.join(ui, "index.html"), "<!doctype html><html><head><title>x</title></head><body></body></html>")
        self.write(os.path.join(ui, "app.js"), "1")
        self.srv.ui_dir = ui
        self.assertIn('content="%s"></head>' % self.srv.token, self.req("/")[1].decode())
        self.assertEqual(self.req("/app.js")[0], 200)
        self.assertEqual(self.req("/..%2f..%2fnodes.json")[0], 404)

    def test_state_shape(self):
        status, body = self.req("/api/state")
        self.assertEqual(status, 200)
        st = json.loads(body)
        self.assertEqual(set(st), {"hub_version", "synced_at", "nodes", "tokens", "projects", "chats", "notify"})
        self.assertEqual(st["hub_version"], "0.5.0")
        n = st["nodes"][0]
        self.assertEqual(set(n), {"name", "ssh", "port", "allowed_tokens", "node_version", "update_available",
                                  "sandbox", "ok", "error", "stopped", "synced_at", "snapshot"})
        self.assertTrue(n["ok"])
        self.assertEqual(n["snapshot"]["projects"][0]["name"], "demo")
        self.assertEqual({t["label"] for t in st["tokens"]}, {"oa", "nl"})
        for t in st["tokens"]:
            self.assertEqual(set(t), {"label", "kind", "account", "last4", "expires", "in_use", "limits"})
        self.assertNotIn(SECRET, body.decode())

    def test_failed_sync_keeps_last_snapshot(self):
        os.remove(os.path.join(self.tmp, "local-node", "cairn-node"))
        self.syncer.sync_now()
        n = json.loads(self.req("/api/state")[1])["nodes"][0]
        self.assertFalse(n["ok"])
        self.assertTrue(n["error"])
        self.assertEqual(n["snapshot"]["host"], "fakehost")

    def test_post_auth(self):
        body = {"node": "lab", "project": "demo"}
        self.assertEqual(self.req("/api/stop", "POST", body, {"X-Cairn-Token": None})[0], 403)
        self.assertEqual(self.req("/api/stop", "POST", body, {"X-Cairn-Token": "wrong"})[0], 403)
        self.assertEqual(self.req("/api/stop", "POST", body, {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("/api/stop", "POST", body, {"Origin": None})[0], 403)
        status, out = self.req("/api/stop", "POST", body)
        self.assertEqual((status, json.loads(out)), (200, {"stopped": True}))

    def test_token_on_stdin_for_start_and_review(self):
        with mock.patch.object(keychain, "get", return_value=SECRET) as get:
            status, out = self.req("/api/start", "POST", {"node": "lab", "project": "demo"})
            self.assertEqual(status, 200, out)
            status, out = self.req("/api/review", "POST", {"node": "lab", "project": "demo"})
            self.assertEqual(status, 200, out)
        get.assert_called_with("oa")
        entries = [e for e in self.log() if e["argv"][0] in ("start", "review")]
        self.assertEqual([e["stdin"] for e in entries], [SECRET, SECRET])
        for e in entries:
            self.assertNotIn(SECRET, " ".join(e["argv"]))
        self.assertNotIn(SECRET, out.decode())
        self.assertEqual(json.loads(out), {"attach": "tmux -L cairn attach -t demo-review"})
        self.assertEqual(self.opened, ["tmux -L cairn attach -t demo-review"])

    def test_node_login_sends_empty_line(self):
        projects = {"lab/demo": {"token": "nl"}}
        config.save_projects(projects)
        with mock.patch.object(keychain, "get") as get:
            self.assertEqual(self.req("/api/start", "POST", {"node": "lab", "project": "demo"})[0], 200)
        get.assert_not_called()
        self.assertEqual([e["stdin"] for e in self.log() if e["argv"][0] == "start"], [""])

    def test_validation_errors(self):
        self.assertEqual(self.req("/api/start", "POST", {"node": "nope", "project": "demo"})[0], 404)
        self.assertEqual(self.req("/api/start", "POST", {"node": "lab", "project": "../x"})[0], 400)
        self.assertEqual(self.req("/api/start", "POST", {"node": "lab"})[0], 400)

    def test_file_and_pane_passthrough(self):
        self.assertEqual(json.loads(self.req("/api/file?node=lab&project=demo&path=work/map.md")[1])["content"], "hello")
        self.assertEqual(json.loads(self.req("/api/pane?node=lab&project=demo")[1]), {"lines": ["line1"]})

    def test_sync_post_returns_state(self):
        status, out = self.req("/api/sync", "POST")
        self.assertEqual(status, 200)
        self.assertIn("nodes", json.loads(out))

    def test_sync_post_with_a_node_reads_that_node(self):
        before = len([e for e in self.log() if e.get("argv") == ["snapshot"]])
        status, out = self.req("/api/sync", "POST", {"node": "lab"})
        self.assertEqual(status, 200)
        self.assertIn("nodes", json.loads(out))
        self.assertGreater(len([e for e in self.log() if e.get("argv") == ["snapshot"]]), before)
        self.assertEqual(self.req("/api/sync", "POST", {"node": "nope"})[0], 404)
        self.assertEqual(self.req("/api/sync", "POST", {"node": 7})[0], 404)

    def test_origin_matches_host_for_127_and_localhost(self):
        body = {"node": "lab", "project": "demo"}
        for host in ("127.0.0.1", "localhost"):
            h = {"Host": "%s:%d" % (host, self.port), "Origin": "http://%s:%d" % (host, self.port)}
            self.assertEqual(self.req("/api/stop", "POST", body, h)[0], 200, host)
        crossed = {"Host": "localhost:%d" % self.port, "Origin": "http://127.0.0.1:%d" % self.port}
        self.assertEqual(self.req("/api/stop", "POST", body, crossed)[0], 403)

    def test_bad_host_rejected(self):
        status, _ = self.req("/api/state", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)


def question(qid, answered=True, applied=None):
    return {"id": qid, "ticket": "", "gate": False, "default": "B", "asked_at": "2026-10-17T14:00:00Z",
            "context": "c", "options": [], "answered": answered, "choice": "A" if answered else None,
            "answered_at": "2026-10-17T15:00:00Z" if answered else None, "applied": applied,
            "path": "work/inbox/%s.md" % qid}


class SyncTests(Base):
    def test_node_added_outside_the_hub_gets_synced(self):
        syncer = Syncer(interval=0.1)
        self.addCleanup(syncer.stop)
        syncer.start()
        self.add_local_node()   # what `cairn node add` does while `cairn serve` runs
        for _ in range(100):
            if (syncer.entries.get("lab") or {}).get("ok"):
                break
            threading.Event().wait(0.05)
        self.assertTrue(syncer.entries["lab"]["ok"])


    def test_watcher_survives_a_broken_nodes_file(self):
        syncer = Syncer(interval=0.1)
        self.addCleanup(syncer.stop)
        syncer.start()
        self.write(config.path("nodes.json"), "{not json")
        threading.Event().wait(0.3)
        self.add_local_node()
        for _ in range(100):
            if (syncer.entries.get("lab") or {}).get("ok"):
                break
            threading.Event().wait(0.05)
        self.assertTrue(syncer.entries["lab"]["ok"])


class AutoStartTests(Base):
    def setUp(self):
        super().setUp()
        config.save_tokens({"nl": {"kind": "node-login"}})
        self.add_local_node(["nl"])
        self.projects_file = os.path.join(self.tmp, "projects-snap.json")
        env = mock.patch.dict(os.environ, {"FAKE_NODE_PROJECTS": self.projects_file})
        env.start()
        self.addCleanup(env.stop)
        self.logs = []
        self.syncer = Syncer(interval=3600, autostart=AutoStarter(log=self.logs.append))
        self.addCleanup(self.syncer.stop)

    def project(self, questions, lead="dead", review="none", status="idle"):
        self.write(self.projects_file, json.dumps([{
            "name": "demo", "path": "/tmp/demo", "status": status, "lead": lead, "review": review,
            "map": {"destination": "d", "decisions": []}, "tickets": [], "questions": questions,
            "adrs": [], "commits": []}]))

    def starts(self):
        self.syncer._sync_node("lab")  # no loop thread, so no concurrent sync
        return [e for e in self.log() if e["argv"][0] == "start"]

    def test_starts_once_per_answer_set(self):
        self.project([question("q01"), question("q02", answered=False)])
        self.assertEqual([e["argv"] for e in self.starts()], [["start", "demo"]])
        self.assertEqual(self.logs, ["lab/demo started for answers q01"])
        self.assertEqual(len(self.starts()), 1)
        # the lead died without applying: same set, still no restart
        self.assertEqual(len(self.starts()), 1)
        self.project([question("q01"), question("q02")])
        self.assertEqual(len(self.starts()), 2)
        self.assertEqual(self.logs[-1], "lab/demo started for answers q01, q02")
        self.assertEqual(len(self.starts()), 2)

    def test_unknown_status_starts(self):
        self.project([question("q01")], status="unknown")
        self.assertEqual(len(self.starts()), 1)

    def test_not_when_review_alive(self):
        self.project([question("q01")], review="alive")
        self.assertEqual(self.starts(), [])
        # the review session ended: the same answers now start the lead
        self.project([question("q01")])
        self.assertEqual(len(self.starts()), 1)

    def test_not_when_lead_never_started_or_alive(self):
        for lead in ("none", "alive"):
            self.project([question("q01")], lead=lead)
            self.assertEqual(self.starts(), [], lead)

    def test_not_when_working_or_done(self):
        for status in ("working 03", "done"):
            self.project([question("q01")], status=status)
            self.assertEqual(self.starts(), [], status)

    def test_not_when_nothing_unapplied(self):
        self.project([question("q01", applied="2026-10-17T16:00:00Z"), question("q02", answered=False)])
        self.assertEqual(self.starts(), [])
        self.project([])
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.logs, [])

    def test_unresolvable_token_skipped_and_logged_once(self):
        config.save_projects({"lab/demo": {"token": "ghost"}})
        self.project([question("q01")])
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.logs, ["lab/demo skipped: token ghost is not defined"])
        config.save_projects({})
        self.assertEqual(len(self.starts()), 1)

    def test_oauth_token_on_stdin(self):
        config.save_tokens({"oa": {"kind": "oauth"}})
        config.save_nodes({"lab": {"ssh": "local", "port": None, "allowed_tokens": ["oa"]}})
        self.project([question("q01")])
        with mock.patch.object(keychain, "get", return_value=SECRET):
            self.assertEqual([e["stdin"] for e in self.starts()], [SECRET])
        self.assertNotIn(SECRET, " ".join(self.logs))

    def test_failed_start_logged_not_retried(self):
        self.project([question("q01")])
        with mock.patch.dict(os.environ, {"FAKE_NODE_START_FAIL": "1"}):
            self.assertEqual(len(self.starts()), 1)
            self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.logs, ["lab/demo start failed for answers q01: "
                                     "claude in demo has no usable login: Not logged in"])


class CliTests(Base):
    def run_cli(self, *argv):
        from cairn_hub import cli
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(list(argv))
        return code, err.getvalue()

    def inits(self):
        return [e for e in self.log() if e["argv"][0] == "init"]

    def setUp(self):
        super().setUp()
        config.save_tokens({"oa": {"kind": "oauth", "last4": "1234"}, "other": {"kind": "node-login"}})
        self.add_local_node(["oa"])

    def test_init_validates_token_before_calling_node(self):
        code, err = self.run_cli("init", "lab", "demo", "--destination", "d", "--token", "typo")
        self.assertEqual(code, 1)
        self.assertIn("unknown token typo", err)
        code, err = self.run_cli("init", "lab", "demo", "--destination", "d", "--token", "other")
        self.assertEqual(code, 1)
        self.assertIn("not allowed on node lab", err)
        self.assertEqual(self.inits(), [])
        self.assertEqual(config.load_projects(), {})

    def test_init_with_allowed_token_maps_project(self):
        self.assertEqual(self.run_cli("init", "lab", "demo", "--destination", "d", "--token", "oa")[0], 0)
        self.assertEqual(len(self.inits()), 1)
        self.assertEqual(config.load_projects(), {"lab/demo": {"token": "oa"}})

    def test_init_timeout_covers_node_clone_timeout(self):
        from cairn_hub import cli
        self.assertGreaterEqual(cli.INIT_TIMEOUT, 600 + 30)
        with mock.patch.object(ssh, "run_node", return_value={}) as run:
            self.run_cli("init", "lab", "demo", "--destination", "d")
        self.assertEqual(run.call_args.kwargs["timeout"], cli.INIT_TIMEOUT)

    def test_node_tokens_sets_and_validates(self):
        code, _ = self.run_cli("node", "tokens", "lab", "oa,other")
        self.assertEqual(code, 0)
        self.assertEqual(config.load_nodes()["lab"]["allowed_tokens"], ["oa", "other"])
        code, err = self.run_cli("node", "tokens", "lab", "oa,nope")
        self.assertEqual(code, 1)
        self.assertIn("unknown token(s): nope", err)
        self.assertEqual(config.load_nodes()["lab"]["allowed_tokens"], ["oa", "other"])
        self.assertEqual(self.run_cli("node", "tokens", "ghost", "oa")[0], 1)
        self.assertEqual(self.run_cli("node", "tokens", "lab", "")[0], 0)
        self.assertEqual(config.load_nodes()["lab"]["allowed_tokens"], [])

    def test_version(self):
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "cairn"), "--version"],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "cairn 0.5.0\n", ""))

    def test_node_add_refuses_existing_node(self):
        with mock.patch.object(nodeops, "install") as install:
            code, err = self.run_cli("node", "add", "lab", "--ssh", "box")
        self.assertEqual(code, 1)
        self.assertIn("cairn node tokens lab", err)
        install.assert_not_called()
        self.assertEqual(config.load_nodes()["lab"]["allowed_tokens"], ["oa"])


class InstallTests(Base):
    def test_ssh_install_copies_template_next_to_binary(self):
        node_dir = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(node_dir, "skills", "cairn-review"))
        for rel, text in (("cairn-node", "bin"), ("skills/cairn-review/SKILL.md", "s"), ("LEAD.md.tmpl", "tpl")):
            self.write(os.path.join(node_dir, rel), text)
        calls = []
        with mock.patch.object(nodeops, "NODE_DIR", node_dir), \
                mock.patch.object(ssh, "run_shell", side_effect=lambda n, c, stdin=None, timeout=30: calls.append((c, stdin))), \
                mock.patch.object(ssh, "run_node", return_value={"projects": []}):
            nodeops.install({"ssh": "box", "port": None})
        self.assertIn(("mkdir -p ~/.cairn && cat > ~/.cairn/LEAD.md.tmpl", b"tpl"), calls)

    def test_local_install_copies_files(self):
        node_dir = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(node_dir, "skills", "cairn-review"))
        shutil.copy(FAKE, os.path.join(node_dir, "cairn-node"))
        self.write(os.path.join(node_dir, "skills", "cairn-review", "SKILL.md"), "skill")
        self.write(os.path.join(node_dir, "LEAD.md.tmpl"), "template")
        with mock.patch.object(nodeops, "NODE_DIR", node_dir):
            snap = nodeops.install({"ssh": "local", "port": None, "allowed_tokens": []})
        self.assertEqual(snap["host"], "fakehost")
        self.assertEqual(os.stat(config.path("local-node/cairn-node")).st_mode & 0o777, 0o755)
        with open(config.path("local-node/LEAD.md.tmpl")) as f:
            self.assertEqual(f.read(), "template")
        self.assertTrue(os.path.exists(config.path("local-node/skills/cairn-review/SKILL.md")))


if __name__ == "__main__":
    unittest.main()
