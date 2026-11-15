"""Where the release-audit fixes meet: node states the hub reads, functions wired to the API, CLI and dashboard."""
import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_m2 as m2
import test_m8 as m8
import test_m12 as m12
from cairn_hub import attention as att
from cairn_hub import autostart, cli, config, mirror, nodeops, sandbox, ssh

UNKNOWN = m8.project(lead="unknown", status="working 03", lead_state=None, lead_note=None)


class UnknownLeadTests(m2.M2Base):
    """A lead whose tmux did not answer (cairn-node 0.4.3 reports lead "unknown") proves neither alive nor dead."""

    def test_attention_keeps_an_open_episode_and_opens_none(self):
        a = att.Attention(log=lambda m: None)
        a.check("lab", {"projects": [m8.project(lead="alive", lead_state="dialog", lead_note="Proceed?")]})
        a.check("lab", {"projects": [UNKNOWN]})
        self.assertEqual(a.current()["lab/demo"]["kind"], "dialog")
        b = att.Attention(log=lambda m: None)
        b.check("lab", {"projects": [UNKNOWN]})   # working, not alive: "dead" here would read as a crash
        self.assertEqual(b.open_episodes(), set())

    def test_autostart_does_not_restart_it(self):
        answered = dict(UNKNOWN, status="idle", questions=[{"id": "q01", "answered": True, "applied": None}])
        self.assertIsNone(autostart.pending(answered))
        self.assertEqual(autostart.pending(dict(answered, lead="dead")), frozenset({"q01"}))

    def test_node_removal_counts_it_as_unchecked(self):
        with self.assertRaises(nodeops.OpError) as cm:
            nodeops.remove_node("lab", {"projects": [UNKNOWN]})
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(str(cm.exception), "tmux on node lab did not answer for demo, so live sessions cannot be "
                                            "checked; remove it anyway with force")
        self.assertEqual(cm.exception.extra, {"unchecked": ["lab"]})
        nodeops.remove_node("lab", {"projects": [UNKNOWN]}, force=True)
        self.assertNotIn("lab", config.load_nodes())

    def test_token_removal_counts_it_as_unchecked(self):
        with self.assertRaises(nodeops.OpError) as cm:
            nodeops.remove_token("oa", {"lab": {"projects": [UNKNOWN]}})
        self.assertIn("cannot check lab", str(cm.exception))
        nodeops.remove_token("oa", {"lab": {"projects": [UNKNOWN]}}, force=True)
        self.assertNotIn("oa", config.load_tokens())


class ReviewSuffixTests(m2.ApiBase):
    """#38: project x's review runs in tmux session x-review, so no new project may take that name."""

    def test_a_new_name_ending_in_review_is_a_400_before_the_node(self):
        status, res = self.post("/api/projects", {"node": "lab", "project": "demo-review", "repo": {"kind": "new"},
                                                  "destination": "d"})
        self.assertEqual(status, 400, res)
        self.assertIn("-review", res["error"])
        self.assertEqual([e for e in self.log() if e["argv"][0] == "init"], [])

    def test_an_existing_one_still_works(self):
        nodeops.start("lab", "old-review")
        self.assertEqual(self.starts()[-1]["argv"][:2], ["start", "old-review"])


class TokenRemoveWiringTests(m2.ApiBase):
    """#28: the API and CLI check every node as it is now, and force overrides only a node that cannot be checked."""

    def setUp(self):
        super().setUp()
        self.syncer.sync_now()   # a good snapshot that goes stale below

    def break_node(self):
        self.write(self.snap_file, "not json")   # the fake node's snapshot fails from now on

    def test_a_node_good_at_its_last_sync_but_unreachable_now_refuses(self):
        self.break_node()
        status, res = self.post("/api/tokens/remove", {"label": "oa"})
        self.assertEqual(status, 409, res)
        self.assertEqual(res["unchecked"], ["lab"])
        self.assertIn("oa", self.kc.store)
        self.assertEqual(self.post("/api/tokens/remove", {"label": "oa", "force": "yes"})[0], 409)
        self.assertEqual(self.post("/api/tokens/remove", {"label": "oa", "force": True}), (200, {"removed": "oa"}))
        self.assertNotIn("oa", self.kc.store)

    def test_a_lead_started_since_the_last_sync_is_seen(self):
        self.set_projects([m2.snap_project(lead="alive", sha=None)])
        status, res = self.post("/api/tokens/remove", {"label": "oa", "force": True})
        self.assertEqual(status, 409, res)
        self.assertIn("in use by lab/demo", res["error"])

    def test_a_stopped_sandbox_runs_nothing(self):
        self.break_node()
        self.syncer.sandboxes = mock.Mock(stopped=lambda name: True)
        self.assertEqual(self.post("/api/tokens/remove", {"label": "oa"})[0], 200)

    def test_cli_force(self):
        self.break_node()
        self.assertEqual(cli.main(["token", "rm", "oa"]), 1)
        self.assertIn("remove it anyway with force", self.err.getvalue())
        self.assertEqual(cli.main(["token", "rm", "oa", "--force"]), 0)
        self.assertNotIn("oa", config.load_tokens())


class SandboxForceWiringTests(m12.SandboxBase):
    """#25: stop and delete take force from the API and the CLI; it overrides only "cannot be checked"."""

    def setUp(self):
        super().setUp()
        self.docker.images.add("cairn-slim:0.1")
        self.sb.create("box", "slim", wait=True)
        self.set_snapshot("box", None)   # its own cairn-node is broken: the sync fails

    def test_api_stop(self):
        status, res = self.post("/api/sandboxes/stop", {"node": "box"})
        self.assertEqual((status, res["unchecked"]), (409, ["box"]), res)
        self.assertEqual(self.post("/api/sandboxes/stop", {"node": "box", "force": 1})[0], 409)
        status, res = self.post("/api/sandboxes/stop", {"node": "box", "force": True})
        self.assertEqual((status, res["state"]), (200, "exited"), res)

    def test_api_remove(self):
        self.assertEqual(self.post("/api/sandboxes/remove", {"node": "box", "confirm": "box"})[0], 409)
        status, res = self.post("/api/sandboxes/remove", {"node": "box", "confirm": "box", "force": True})
        self.assertEqual((status, res["removed"]), (200, "box"), res)
        self.assertNotIn("cairn-box", self.docker.containers)

    def test_api_force_never_overrides_a_live_lead(self):
        self.set_snapshot("box", {"projects": [m2.snap_project("demo", lead="alive")]})
        status, res = self.post("/api/sandboxes/stop", {"node": "box", "force": True})
        self.assertEqual(status, 409)
        self.assertNotIn("unchecked", res)

    def test_cli(self):
        self.assertEqual(cli.main(["sandbox", "stop", "box"]), 1)
        self.assertIn("with force", self.err.getvalue())
        self.assertEqual(cli.main(["sandbox", "stop", "box", "--force"]), 0)
        self.assertEqual(cli.main(["sandbox", "start", "box"]), 0)
        self.assertEqual(cli.main(["sandbox", "rm", "box", "--yes"]), 1)
        self.assertEqual(cli.main(["sandbox", "rm", "box", "--yes", "--force"]), 0)
        self.assertNotIn("box", config.load_sandboxes())


FUTURE = {"since": "2026-10-20T20:00:00Z", "until": "2099-01-01T00:00:00Z", "known": True, "note": ""}
PAST = dict(FUTURE, until="2020-01-01T00:00:00Z")


class LimitViewTests(m2.ApiBase):
    """#22: the dashboard reads a node-login limit under its node's key; #21: a recorded limit can be cleared."""

    def setUp(self):
        super().setUp()
        nodeops.set_project_token("lab", "demo", "oa", ["oa", "nl"])

    def test_a_project_shows_its_node_login_limit(self):
        config.save_limits({"node-login@lab": FUTURE, "node-login@far": FUTURE, "oa": PAST})
        limits = self.state()["projects"]["lab/demo"]["limits"]
        self.assertEqual(limits, {"nl": {"until": FUTURE["until"], "known": True}})

    def test_tokens_show_their_limits_with_the_node_for_a_node_login(self):
        config.save_nodes({**config.load_nodes(), "far": {"ssh": "far", "port": None, "allowed_tokens": ["oa"]}})
        config.save_limits({"node-login@lab": FUTURE, "node-login@far": FUTURE, "oa": FUTURE})
        tokens = {t["label"]: t["limits"] for t in self.state()["tokens"]}
        self.assertEqual(tokens, {"oa": [{"node": None, "until": FUTURE["until"], "known": True}],
                                  "nl": [{"node": "lab", "until": FUTURE["until"], "known": True}]})
        config.save_limits({"oa": PAST})
        self.assertEqual({t["label"]: t["limits"] for t in self.state()["tokens"]}, {"oa": [], "nl": []})

    def test_clear_limit_api(self):
        config.save_limits({"node-login@lab": FUTURE, "oa": FUTURE})
        self.assertEqual(self.post("/api/tokens/clear-limit", {"label": "oa"}),
                         (200, {"label": "oa", "node": None, "cleared": True}))
        self.assertEqual(self.post("/api/tokens/clear-limit", {"label": "oa"})[1]["cleared"], False)
        status, res = self.post("/api/tokens/clear-limit", {"label": "nl"})
        self.assertEqual(status, 400, res)
        self.assertIn("node", res["error"])
        self.assertEqual(self.post("/api/tokens/clear-limit", {"label": "nl", "node": "ghost"})[0], 404)
        self.assertEqual(self.post("/api/tokens/clear-limit", {"label": "ghost"})[0], 404)
        self.assertEqual(self.post("/api/tokens/clear-limit", {"label": "nl", "node": "lab"})[1]["cleared"], True)
        self.assertEqual(config.load_limits(), {})

    def test_clear_limit_cli(self):
        config.save_limits({"node-login@lab": FUTURE})
        self.assertEqual(cli.main(["token", "clear-limit", "nl"]), 1)
        self.assertEqual(cli.main(["token", "clear-limit", "nl", "--node", "lab"]), 0)
        self.assertIn("limit of nl on lab cleared", self.out.getvalue())
        self.assertEqual(cli.main(["token", "clear-limit", "nl", "--node", "lab"]), 0)
        self.assertIn("has no recorded limit", self.out.getvalue())
        self.assertEqual(config.load_limits(), {})


class ForgetNodeTests(m2.ApiBase):
    """#29: a removed node's snapshot, episodes and autostart claims go with it, so a node added again starts clean."""
    DIALOG = [m8.project(lead="alive", lead_state="dialog", lead_note="Proceed?")]

    def setUp(self):
        super().setUp()
        self.set_projects(self.DIALOG)
        self.syncer.sync_now()
        self.syncer.autostart.started[("lab", "demo")] = frozenset({"q01"})
        self.assertIn("lab/demo", self.syncer.attention.current())

    def assert_forgotten(self):
        self.assertNotIn("lab", self.syncer.entries)
        self.assertEqual(self.syncer.attention.open_episodes(), set())
        self.assertEqual(self.syncer.autostart.started, {})

    def test_api_remove(self):
        self.set_projects([m2.snap_project(sha=None)])
        self.syncer.sync_now()
        self.syncer.autostart.started[("lab", "demo")] = frozenset({"q01"})
        self.syncer.attention.check("lab", {"projects": self.DIALOG})
        self.assertEqual(self.post("/api/nodes/remove", {"name": "lab"})[0], 200)
        self.assert_forgotten()

    def test_a_node_removed_outside_the_hub_is_forgotten_at_its_next_sync(self):
        nodeops.remove_node("lab", None, force=True)   # `cairn node rm` in a terminal
        self.syncer.sync_one("lab")
        self.assert_forgotten()

    def test_a_sync_in_flight_does_not_bring_it_back(self):
        snapshot = ssh.snapshot

        def removed_meanwhile(node, timeout=25):
            snap = snapshot(node, timeout)
            nodeops.remove_node("lab", None, force=True)
            self.syncer.forget("lab")
            return snap
        with mock.patch.object(ssh, "snapshot", removed_meanwhile):
            self.syncer.sync_one("lab")
        self.assert_forgotten()


class ForgetSandboxTests(m12.SandboxBase):
    def test_api_remove(self):
        self.docker.images.add("cairn-slim:0.1")
        self.sb.create("box", "slim", wait=True)
        self.set_snapshot("box", {"projects": []})
        self.syncer.attention.check("box", {"projects": [m8.project(lead="alive", lead_state="dialog", lead_note="?")]})
        self.assertEqual(self.post("/api/sandboxes/remove", {"node": "box", "confirm": "box"})[0], 200)
        self.assertNotIn("box", self.syncer.entries)
        self.assertEqual(self.syncer.attention.open_episodes(), set())


class ParsedSnapshotTests(m2.M2Base):
    """#3: every snapshot the hub reads goes through ssh.snapshot, the CLI's and the installer's too."""

    def test_install_and_the_cli_refuse_a_reply_that_is_no_snapshot(self):
        with mock.patch.object(ssh, "run_node", return_value=[1]):
            with self.assertRaises(ssh.NodeError):
                nodeops.install({"ssh": "local", "port": None})
            self.assertEqual(nodeops.live_snapshots(), {"lab": None})

    def test_the_cli_reads_coerced_snapshots(self):
        self.set_projects([dict(m2.snap_project(lead="alive"), questions=None), {"name": "../x", "path": "/x"}])
        snap = nodeops.live_snapshots()["lab"]
        self.assertEqual([(p["name"], p["questions"]) for p in snap["projects"]], [("demo", [])])
        self.assertIn("no valid name", snap["warnings"][0]["warning"])


class MirrorStderrTests(m2.M2Base):
    def test_a_flood_on_git_stderr_ends_in_one_error_line(self):
        """#12: git's stderr (where a node's git server can print without end) goes to a file read from its end."""
        flood = "import sys; sys.stderr.write('x' * 20000000 + '\\nfatal: the last line\\n'); sys.exit(128)"
        err = mirror.Mirror()._git([sys.executable, "-c", flood], timeout=60)
        self.assertEqual(err, "fatal: the last line")


class HardeningFlagsTests(m12.SandboxBase):
    """#10: the hub runs a sandbox with exactly the hardening flags nodes/slim/run.sh uses."""

    def run_sh_flags(self, image):
        with open(os.path.join(sandbox.REPO, "nodes", "slim", "run.sh")) as f:
            text = f.read()
        flags = re.search(r"HARDEN=\((.*?)\)", text).group(1).split()
        if not image.startswith("cairn-dev:"):
            flags += re.search(r"HARDEN\+=\((.*?)\)", text).group(1).split()
        return flags

    def hub_flags(self, name, image):
        self.docker.images.update({"cairn-slim:0.1", "cairn-dev:0.1"})
        self.sb.create(name, image, wait=True)
        argv = next(c for c in self.docker.calls if c[0] == "run" and "cairn-" + name in c)
        return [a for i, a in enumerate(argv) if a in ("--cap-drop", "--pids-limit", "--security-opt")
                or argv[i - 1] in ("--cap-drop", "--pids-limit", "--security-opt")]

    def test_same_flags(self):
        self.assertEqual(self.hub_flags("a", "slim"), self.run_sh_flags("cairn-slim:0.1"))
        self.assertEqual(self.hub_flags("b", "dev"), self.run_sh_flags("cairn-dev:0.1"))
        self.assertIn("no-new-privileges", self.run_sh_flags("cairn-slim:0.1"))


class UnknownLeadSandboxTests(m12.SandboxBase):
    def test_stop_counts_it_as_unchecked(self):
        self.docker.images.add("cairn-slim:0.1")
        self.sb.create("box", "slim", wait=True)
        with self.assertRaises(nodeops.OpError) as cm:
            self.sb.stop("box", {"projects": [UNKNOWN]})
        self.assertIn("with force", str(cm.exception))
        self.assertEqual(self.sb.stop("box", {"projects": [UNKNOWN]}, force=True)["state"], "exited")


if __name__ == "__main__":
    unittest.main()
