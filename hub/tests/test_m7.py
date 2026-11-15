"""Milestone 7: model choice per project and result handoff (copy to ~/Downloads)."""
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_hub as th
import test_m2 as m2
from cairn_hub import chat as chatmod
from unittest import mock

from cairn_hub import config, mirror, nodeops, ssh
from cairn_hub.server import CairnServer
from cairn_hub.sync import Syncer


class ModelTests(m2.ApiBase):
    BODY = dict(m2.ProjectRouteTests.BODY)

    def test_create_saves_models_and_the_first_start_uses_them(self):
        status, out = self.post("/api/projects", {**self.BODY, "model": "fable", "subagent_model": "haiku", "start": True})
        self.assertEqual((status, out["started"]), (200, True), out)
        self.assertEqual(config.load_projects()["lab/newp"], {"token": "oa", "model": "fable", "subagent_model": "haiku"})
        self.assertEqual([e["argv"] for e in self.starts()], [["start", "newp", "--model", "fable", "--subagent-model", "haiku"]])

    def test_create_without_models_saves_none(self):
        self.assertEqual(self.post("/api/projects", {**self.BODY, "model": None})[0], 200)
        self.assertEqual(config.load_projects()["lab/newp"], {"token": "oa"})

    def test_create_refuses_unknown_models(self):
        for patch in ({"model": "gpt-5"}, {"subagent_model": "claude-opus-5-5"}, {"model": 3}, {"model": ""}):
            self.assertEqual(self.post("/api/projects", {**self.BODY, **patch})[0], 400, patch)
        self.assertEqual([e for e in self.log() if e["argv"][0] == "init"], [])
        self.assertEqual(config.load_projects(), {})

    def test_models_route(self):
        status, out = self.post("/api/projects/models", {"node": "lab", "project": "demo", "model": "opus", "subagent_model": None})
        self.assertEqual((status, out), (200, {"model": "opus", "subagent_model": None}))
        self.assertEqual(self.starts(), [])   # used at the next start only
        self.assertEqual(self.req("/api/start", "POST", {"node": "lab", "project": "demo"})[0], 200)
        self.assertEqual(self.starts()[-1]["argv"], ["start", "demo", "--model", "opus"])
        # the token route keeps the models, the models route keeps the token
        self.assertEqual(self.post("/api/projects/token", {"node": "lab", "project": "demo", "token": "nl"})[0], 200)
        self.post("/api/projects/models", {"node": "lab", "project": "demo", "model": None, "subagent_model": "sonnet"})
        self.assertEqual(config.load_projects()["lab/demo"], {"token": "nl", "subagent_model": "sonnet"})
        self.assertEqual(self.req("/api/start", "POST", {"node": "lab", "project": "demo"})[0], 200)
        self.assertEqual(self.starts()[-1]["argv"], ["start", "demo", "--subagent-model", "sonnet"])
        entry = self.state()["projects"]["lab/demo"]
        self.assertEqual((entry["model"], entry["subagent_model"], entry["copied_sha"], entry["copied_to"]),
                         (None, "sonnet", None, None))

    def test_models_route_validation(self):
        base = {"node": "lab", "project": "demo", "model": None, "subagent_model": None}
        for patch in ({"model": "gpt"}, {"subagent_model": "Opus"}, {"project": "../x"}):
            self.assertEqual(self.post("/api/projects/models", {**base, **patch})[0], 400, patch)
        self.assertEqual(self.post("/api/projects/models", {**base, "node": "ghost"})[0], 404)
        self.assertEqual(config.load_projects(), {})

    def test_auto_start_passes_models(self):
        nodeops.set_project_models("lab", "demo", "haiku", "haiku")
        nodeops.start("lab", "demo")
        self.assertEqual(self.starts()[-1]["argv"], ["start", "demo", "--model", "haiku", "--subagent-model", "haiku"])

    def test_writers_keep_the_other_keys(self):
        nodeops.set_project_models("lab", "demo", "opus", "haiku")
        nodeops.update_project("lab", "demo", copied_sha="a" * 40, copied_to="/x")
        nodeops.set_project_token("lab", "demo", "nl")
        nodeops.update_token("nl", new_label="nl2")
        self.assertEqual(config.load_projects()["lab/demo"],
                         {"model": "opus", "subagent_model": "haiku", "copied_sha": "a" * 40, "copied_to": "/x", "token": "nl2"})
        nodeops.remove_token("nl2", {"lab": {"projects": []}})
        self.assertEqual(config.load_projects()["lab/demo"],
                         {"model": "opus", "subagent_model": "haiku", "copied_sha": "a" * 40, "copied_to": "/x"})
        # the state still names the token the project will use
        self.assertEqual(self.state()["projects"]["lab/demo"]["token"], "oa")

    def test_recreating_a_project_starts_from_a_fresh_entry(self):
        nodeops.update_project("lab", "newp", model="opus", copied_sha="a" * 40)
        self.assertEqual(self.post("/api/projects", self.BODY)[0], 200)
        self.assertEqual(config.load_projects()["lab/newp"], {"token": "oa"})

    def test_old_node_needs_an_update_before_models(self):
        with mock.patch.dict(os.environ, {"FAKE_NODE_VERSION": "0.1.0"}):
            self.syncer.sync_now()
            node = self.state()["nodes"][0]
            self.assertEqual((node["node_version"], node["update_available"]), ("0.1.0", True))
            self.assertEqual(self.req("/api/start", "POST", {"node": "lab", "project": "demo"})[0], 200)  # no models: fine
            nodeops.set_project_models("lab", "demo", "opus", None)
            status, raw = self.req("/api/start", "POST", {"node": "lab", "project": "demo"})
            self.assertEqual(status, 409)
            self.assertIn("update cairn-node", json.loads(raw)["error"])
            self.assertEqual(len(self.starts()), 1)
        status, out = self.post("/api/nodes/update", {"name": "lab"})
        self.assertEqual((status, out), (200, {"node": "lab", "node_version": nodeops.bundled_version()}))
        with open(config.path("local-node/cairn-node")) as f:
            self.assertIn('VERSION = "%s"' % nodeops.bundled_version(), f.read())   # the real script was installed
        shutil.copy(th.FAKE, config.path("local-node/cairn-node"))   # back to the fake for the rest
        self.assertFalse(self.state()["nodes"][0]["update_available"])
        self.assertEqual(self.req("/api/start", "POST", {"node": "lab", "project": "demo"})[0], 200)
        self.assertEqual(self.post("/api/nodes/update", {"name": "ghost"})[0], 404)

    def test_chat_passes_models(self):
        nodeops.set_project_models("lab", "demo", "sonnet", "haiku")
        seen = []

        def popen(argv, **kw):
            seen.append(argv)
            raise OSError(2, "stop here")
        chats = chatmod.ChatManager(popen=popen, reap_every=3600)
        self.addCleanup(chats.stop)
        with self.assertRaises(nodeops.OpError):
            chats.start("lab", "demo")
        self.assertEqual(seen[0][-6:], ["chat", "demo", "--model", "sonnet", "--subagent-model", "haiku"])


class CopyTests(m2.M2Base):
    """Get a copy against real git: the local node's repo is mirrored, then exported to ~/Downloads."""

    def git(self, *argv, cwd=None):
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example", *argv], cwd=cwd,
                           capture_output=True, text=True, check=True)
        return r.stdout.strip()

    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(self.src, "work"))
        self.git("init", "-q", "-b", "main", cwd=self.src)
        self.write(os.path.join(self.src, "index.html"), "<h1>game</h1>\n")
        self.write(os.path.join(self.src, "work", "result.md"), "A game.\n")
        self.git("add", "-A", cwd=self.src)
        self.git("commit", "-q", "-m", "one", cwd=self.src)
        self.sha = self.git("rev-parse", "HEAD", cwd=self.src)
        self.set_projects([m2.snap_project(sha=self.sha, path=self.src)])
        self.revealed = []
        self.syncer = Syncer(interval=3600, mirror=mirror.Mirror())
        self.syncer.refresh_nodes = lambda: None
        self.srv = CairnServer(0, self.syncer, ui_dir=os.path.join(self.tmp, "no-ui"), opener=lambda c: None,
                               reveal=self.revealed.append)
        self.port = self.srv.port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.syncer.mirror.wait_idle)
        self.addCleanup(self.syncer.stop)

    req = m2.ApiBase.req
    post = m2.ApiBase.post

    def copy(self):
        return self.post("/api/projects/copy", {"node": "lab", "project": "demo"})

    def test_copy_exports_the_mirrored_commit_and_never_overwrites(self):
        status, out = self.copy()
        downloads = os.path.join(self.tmp, "Downloads")
        first = os.path.join(downloads, "demo")
        self.assertEqual((status, out), (200, {"path": first, "sha": self.sha, "skipped": []}))
        with open(os.path.join(first, "index.html")) as f:
            self.assertEqual(f.read(), "<h1>game</h1>\n")
        self.assertTrue(os.path.isfile(os.path.join(first, "work", "result.md")))
        self.assertFalse(os.path.exists(os.path.join(first, ".git")))
        self.assertEqual(self.revealed, [first])
        entry = config.load_projects()["lab/demo"]
        self.assertEqual((entry["copied_sha"], entry["copied_to"]), (self.sha, first))
        today = datetime.date.today().isoformat()
        self.assertEqual(self.copy()[1]["path"], os.path.join(downloads, "demo-" + today))
        self.assertEqual(self.copy()[1]["path"], os.path.join(downloads, "demo-%s-2" % today))
        with open(os.path.join(first, "index.html")) as f:
            self.assertEqual(f.read(), "<h1>game</h1>\n")
        # the token route keeps the copy record
        self.assertEqual(self.post("/api/projects/token", {"node": "lab", "project": "demo", "token": "nl"})[0], 200)
        self.assertEqual(config.load_projects()["lab/demo"]["copied_sha"], self.sha)

    def test_copy_takes_the_newest_commit(self):
        self.write(os.path.join(self.src, "index.html"), "<h1>v2</h1>\n")
        self.git("commit", "-q", "-am", "two", cwd=self.src)
        two = self.git("rev-parse", "HEAD", cwd=self.src)
        self.set_projects([m2.snap_project(sha=two, path=self.src)])
        status, out = self.copy()
        self.assertEqual((status, out["sha"]), (200, two))
        with open(os.path.join(out["path"], "index.html")) as f:
            self.assertEqual(f.read(), "<h1>v2</h1>\n")

    def snap(self, sha, path=None, **over):
        p = m2.snap_project(sha=sha, path=path or self.src)
        p.update(over)
        self.set_projects([p])

    def short(self, rev="HEAD"):
        return self.git("rev-parse", "--short", rev, cwd=self.src)

    def test_short_shas_and_result_ready(self):
        self.snap(self.short(), status="done", result="A game.\n", result_sha=self.short())
        self.syncer.sync_now()
        self.assertTrue(self.syncer.mirror.wait_idle())
        def state():
            self.syncer.sync_one("lab")
            return self.syncer.state()["projects"]["lab/demo"]
        self.assertTrue(state()["result_ready"])
        status, out = self.copy()
        self.assertEqual((status, out["sha"]), (200, self.sha))   # stored in full
        self.assertEqual(config.load_projects()["lab/demo"]["copied_sha"], self.sha)
        self.assertFalse(state()["result_ready"])
        self.assertEqual(config.load_projects()["lab/demo"]["copied_result_sha"], self.short())
        # a dashboard commit after the copy (a new ticket, Update LEAD.md) does not make it ready again
        self.write(os.path.join(self.src, "work", "ticket.md"), "t\n")
        self.git("add", "-A", cwd=self.src)
        self.git("commit", "-q", "-m", "ticket", cwd=self.src)
        self.snap(self.short(), status="done", result="A game.\n", result_sha=self.short("HEAD~1"))
        self.assertFalse(state()["result_ready"])
        # the lead rewrites the result: ready again
        self.snap(self.short(), status="done", result="A better game.\n", result_sha=self.short())
        self.assertTrue(state()["result_ready"])
        self.snap(self.short(), status="idle", result="A game.\n", result_sha=self.short())
        self.assertFalse(state()["result_ready"])   # not done

    def test_copy_from_before_copied_result_sha_still_counts(self):
        result_sha = self.short()
        self.write(os.path.join(self.src, "work", "status"), "done\n")
        self.git("add", "-A", cwd=self.src)
        self.git("commit", "-q", "-m", "status done", cwd=self.src)
        head = self.git("rev-parse", "HEAD", cwd=self.src)
        self.snap(self.short(), status="done", result="A game.\n", result_sha=result_sha)
        self.syncer.sync_now()
        self.assertTrue(self.syncer.mirror.wait_idle())
        nodeops.update_project("lab", "demo", copied_sha=head, copied_to="/x")   # a milestone 7 copy of the head
        self.assertFalse(self.syncer.state()["projects"]["lab/demo"]["result_ready"])

    def test_no_result_ready_without_commits(self):
        self.snap(None, status="done", result="A game.\n", result_sha=None)
        self.syncer.sync_now()
        self.assertFalse(self.syncer.state()["projects"]["lab/demo"]["result_ready"])

    def test_refused_when_the_mirror_cannot_fetch_the_newest_commit(self):
        self.assertEqual(self.copy()[0], 200)
        self.write(os.path.join(self.src, "index.html"), "<h1>v2</h1>\n")
        self.git("commit", "-q", "-am", "two", cwd=self.src)
        two = self.git("rev-parse", "HEAD", cwd=self.src)
        self.snap(two, path=os.path.join(self.tmp, "gone"))
        status, out = self.copy()
        self.assertIn(status, (409, 502), out)
        self.assertIn("nothing was copied", out["error"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.tmp, "Downloads"))), ["demo"])
        self.assertEqual(config.load_projects()["lab/demo"]["copied_sha"], self.sha)

    def test_refused_when_the_node_is_unreachable(self):
        with mock.patch.object(ssh, "run_node", side_effect=ssh.NodeError("ssh: connect timed out")):
            status, out = self.copy()
        self.assertEqual(status, 502, out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "Downloads", "demo")))

    def test_export_failure_keeps_gits_message_and_leaves_no_folder(self):
        self.assertEqual(self.copy()[0], 200)
        with self.assertRaises(nodeops.OpError) as cm:
            nodeops.copy_result("lab", "demo", "deadbeef" * 5, reveal=self.revealed.append)
        self.assertIn("deadbeef", str(cm.exception))
        self.assertEqual(sorted(os.listdir(os.path.join(self.tmp, "Downloads"))), ["demo"])

    def test_links_outside_the_folder_are_left_out(self):
        os.symlink("/etc/hosts", os.path.join(self.src, "hosts-link"))
        os.symlink("../../outside", os.path.join(self.src, "work", "up-link"))
        os.symlink("index.html", os.path.join(self.src, "ok-link"))
        self.git("add", "-A", cwd=self.src)
        self.git("commit", "-q", "-m", "links", cwd=self.src)
        self.snap(self.git("rev-parse", "HEAD", cwd=self.src))
        status, out = self.copy()
        self.assertEqual(status, 200, out)
        self.assertEqual(sorted(out["skipped"]), ["hosts-link", "work/up-link"])
        self.assertTrue(os.path.islink(os.path.join(out["path"], "ok-link")))
        self.assertFalse(os.path.lexists(os.path.join(out["path"], "hosts-link")))

    def test_no_commit_yet_is_409(self):
        self.set_projects([m2.snap_project(sha=None, path=self.src)])
        status, out = self.copy()
        self.assertEqual(status, 409, out)
        self.assertEqual(self.revealed, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "Downloads", "demo")))

    def test_validation(self):
        self.assertEqual(self.post("/api/projects/copy", {"node": "lab", "project": "../x"})[0], 400)
        self.assertEqual(self.post("/api/projects/copy", {"node": "ghost", "project": "demo"})[0], 404)


if __name__ == "__main__":
    unittest.main()
