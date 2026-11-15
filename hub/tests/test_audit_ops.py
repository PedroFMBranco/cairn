"""Fixes from the release audit of 2026-11-14: attention, rotation, tokens, notifications, Keychain, sandboxes, chat."""
import io
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_hub as th
import test_m2 as m2
import test_m8 as m8
import test_m12 as m12
import test_m13 as m13
from cairn_hub import attention as att
from cairn_hub import autostart, config, keychain, nodeops, notify
from cairn_hub import chat as chatmod

LIMIT = "⎿  You've hit your session limit · resets 11pm (UTC)"


def stopped_lead():
    """What the node reports after `stop` worked and `start` failed: the lead is gone and the marker says stopped."""
    return {"name": "strand", "lead": "dead", "status": "working 42", "lead_stopped": True}


class FailedRestartTests(m13.RotationBase):
    """#19: a restart whose stop worked and whose start failed is retried, and the operator hears about it."""

    def setUp(self):
        super().setUp()
        kc = m2.KeychainFake()
        for name in ("add", "get", "delete"):
            p = mock.patch.object(keychain, name, getattr(kc, name))
            p.start()
            self.addCleanup(p.stop)
        notify._topic.clear()
        self.addCleanup(notify._topic.clear)
        self.post = m8.Recorder()
        self.n = notify.Notifier(post=self.post)
        self.n.setup(server="http://ntfy.example.com")
        self.n.check("strand", {"projects": []}, {}, {})

        def restart(n, p):
            self.restarts.append(config.token_label_for(n, p))
            raise nodeops.OpError("start failed: not logged in", 502)
        self.a.restart = restart

    def sync(self, p):
        snap = {"time": att.iso(self.now), "projects": [p]}
        self.a.check("strand", snap)
        self.n.check("strand", snap, {}, self.a.current(), self.a.open_episodes())
        return self.a.current().get("strand/strand")

    def limited(self):
        return {"name": "strand", "lead": "alive", "status": "working 42", "lead_state": "limit", "lead_note": LIMIT}

    def test_a_stopped_lead_after_a_failed_start_is_retried_three_times_then_pushed(self):
        self.sync(self.limited())
        self.assertEqual(self.restarts, ["researcher-03"])
        for hhmm in ("21:08", "21:12", "21:13", "21:18", "21:30", "22:00"):
            self.at(hhmm)
            e = self.sync(stopped_lead())
            self.assertIsNotNone(e, hhmm)
        self.assertEqual(self.restarts, ["researcher-03"] * 3)
        self.assertEqual((e["resume_attempts"], e["resume_at"]), (3, None))
        self.assertIn("not logged in", e["resume_error"])
        bodies = [body for _, body, _ in self.post.sent]
        self.assertEqual(len(bodies), 2, bodies)
        self.assertIn("researcher-D hit its limit", bodies[0])
        self.assertEqual(bodies[1], "Automatic restart failed after 3 attempts: start failed: not logged in")
        self.sync(stopped_lead())
        self.assertEqual(len(self.post.sent), 2)   # once per episode

    def test_a_lead_stopped_by_the_operator_during_a_wait_is_left_alone(self):
        self.a.restart = lambda n, p: self.restarts.append(p)
        config.save_limits({"researcher-03": {"since": "x", "until": "2026-10-20T22:30:00Z", "known": True, "note": ""},
                            "researcher-B": {"since": "x", "until": "2026-10-20T22:00:00Z", "known": True, "note": ""}})
        self.sync(self.limited())
        self.at("21:30")
        self.assertIsNone(self.sync(stopped_lead()))
        self.at("22:05")
        self.sync(stopped_lead())
        self.assertEqual(self.restarts, [])

    def test_a_working_lead_ends_the_kept_episode(self):
        self.sync(self.limited())
        self.at("21:08")
        self.assertIsNotNone(self.sync(stopped_lead()))
        self.assertIsNone(self.sync({"name": "strand", "lead": "alive", "status": "working 42", "lead_state": "working"}))


class ResetClampTests(m13.RotationBase):
    """#21: a garbled reset time cannot pin a token for years or abort the attention step, and a limit can be cleared."""

    def test_huge_reset_times_are_clamped(self):
        cap = self.now + att.timedelta(days=8)
        for note in ("resets in 99999h", "resets in 999999999999999999999h", "resets in %sh" % ("9" * 5000),
                     "resets in 1h %sm" % ("9" * 5000)):
            self.assertEqual(att.parse_reset(note, self.now), cap, note[:40])
        self.assertEqual(att.iso(att.parse_reset("resets in 2h", self.now)), "2026-10-20T23:07:00Z")

    def test_a_huge_reset_is_recorded_clamped_and_can_be_cleared(self):
        e = self.check(note="limit reached ∙ resets in 99999h")
        self.assertEqual(config.load_limits()["researcher-D"]["until"], "2026-10-28T21:07:00Z")
        self.assertEqual(e["switched_to"], "researcher-03")
        self.assertTrue(att.clear_limit("researcher-D"))
        self.assertNotIn("researcher-D", config.load_limits())
        self.assertFalse(att.clear_limit("researcher-D"))


class NodeLoginLimitTests(m13.RotationBase):
    """#22: a node-login limit belongs to that node's login, which every node-login label on the node shares."""
    LOGINS = ["login-1", "login-2", "researcher-B"]

    def setUp(self):
        super().setUp()
        config.save_tokens({"login-1": {"kind": "node-login"}, "login-2": {"kind": "node-login"},
                            "researcher-B": {"kind": "oauth"}})
        config.save_nodes({n: {"ssh": n, "port": None, "allowed_tokens": list(self.LOGINS)} for n in ("strand", "other")})
        config.save_projects({"strand/strand": {"token": "login-1", "tokens": self.LOGINS}})

    def test_the_other_node_login_label_is_the_same_exhausted_login(self):
        self.check()
        self.assertEqual(self.restarts, [("strand", "strand", "researcher-B")])
        limits = config.load_limits()
        self.assertEqual(set(limits), {"node-login@strand"})
        self.assertEqual(set(att.limits_for("strand", self.LOGINS, limits)), {"login-1", "login-2"})
        self.assertEqual(att.limits_for("other", self.LOGINS, limits), {})   # another node, another login
        self.assertTrue(att.clear_limit("login-2", "strand"))
        self.assertEqual(config.load_limits(), {})


class AllowedAtUseTests(m2.M2Base):
    """#2: a token taken off a node's allowed list is never sent there, whatever the project still names."""

    def setUp(self):
        super().setUp()
        config.save_tokens({**config.load_tokens(), "o2": {"kind": "oauth", "account": "", "last4": "5555"}})
        self.kc.store["o2"] = m2.SECOND
        config.save_projects({"lab/demo": {"token": "o2"}})   # o2 was allowed when it was chosen

    def test_start_restart_and_review_refuse_with_409(self):
        for call in (lambda: nodeops.start("lab", "demo"), lambda: nodeops.restart("lab", "demo"),
                     lambda: nodeops.review("lab", "demo", terminal=False)):
            with self.assertRaises(nodeops.OpError) as cm:
                call()
            self.assertEqual(cm.exception.status, 409)
            self.assertIn("not allowed on node lab", str(cm.exception))
        self.assertEqual(self.log(), [])
        self.assert_no_secret()

    def test_a_node_record_without_an_allowed_list_refuses_with_409(self):
        config.save_nodes({"lab": {"ssh": "local", "port": None}})
        config.save_projects({"lab/demo": {"token": "oa"}})
        with self.assertRaises(nodeops.OpError) as cm:
            nodeops.token_line("lab", "demo")
        self.assertEqual(cm.exception.status, 409)

    def test_an_allowed_token_still_starts(self):
        nodeops.set_node_tokens("lab", ["oa", "nl", "o2"])
        nodeops.start("lab", "demo")
        self.assertEqual(len(self.starts()), 1)


class DisallowedActiveTests(m13.RotationBase):
    """#2: rotation never waits for, or restarts on, an active token the node no longer allows."""

    def test_the_wait_skips_the_disallowed_active_token(self):
        config.save_limits({"researcher-03": {"since": "x", "until": "2026-10-20T22:30:00Z", "known": True, "note": ""},
                            "researcher-B": {"since": "x", "until": "2026-10-20T22:00:00Z", "known": True, "note": ""}})
        config.save_nodes({"strand": {"ssh": "x", "port": None, "allowed_tokens": ["researcher-03", "researcher-B"]}})
        e = self.check(note="limit reached ∙ resets in 10m")   # D resets first, but strand no longer runs it
        self.assertEqual((e["switch_to"], e["resume_at"]), ("researcher-B", "2026-10-20T22:02:00Z"))
        self.at("22:02")
        self.check(note="limit reached ∙ resets in 10m")
        self.assertEqual(self.restarts, [("strand", "strand", "researcher-B")])

    def test_nothing_left_to_run_is_an_error_not_a_restart(self):
        nodeops.set_project_token("strand", "strand", pool=[])
        config.save_nodes({"strand": {"ssh": "x", "port": None, "allowed_tokens": ["researcher-03"]}})
        e = self.check(note="limit reached ∙ resets in 1h")
        self.assertEqual((e["switch_to"], e["resume_at"]), (None, "2026-10-20T22:09:00Z"))
        self.at("22:09")
        e = self.check(note="limit reached ∙ resets in 1h")
        self.assertEqual(self.restarts, [])
        self.assertIn("no token in the pool of strand/strand is allowed on node strand", e["resume_error"])


HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from cairn_hub import config, nodeops
with nodeops.LOCK:
    projects = config.load_projects()
    open(sys.argv[2], "w").close()
    time.sleep(1)
    projects["lab/other"] = {"model": "haiku"}
    config.save_projects(projects)
"""


class CrossProcessLockTests(m2.M2Base):
    """#24: the CLI and a running hub never lose each other's writes to projects.json."""

    def test_a_write_waits_for_the_other_process(self):
        held = os.path.join(self.tmp, "held")
        child = subprocess.Popen([sys.executable, "-c", HOLDER, os.path.dirname(HERE), held])
        self.addCleanup(child.wait)
        deadline = time.monotonic() + 20
        while not os.path.exists(held):
            self.assertLess(time.monotonic(), deadline, "the other process never took the lock")
            time.sleep(0.02)
        nodeops.set_project_models("lab", "demo", "opus", None)   # while the other process is mid-write
        self.assertEqual(child.wait(20), 0)
        self.assertEqual(config.load_projects(), {"lab/other": {"model": "haiku"}, "lab/demo": {"model": "opus"}})

    def test_reentrant_in_one_thread(self):
        with nodeops.LOCK:
            nodeops.set_project_token("lab", "demo", "oa", ["oa", "nl"])   # takes LOCK again inside
        self.assertEqual(config.load_projects()["lab/demo"], {"token": "oa", "tokens": ["oa", "nl"]})


class KeychainTimeoutTests(th.Base):
    """#23: a Keychain waiting on a prompt never hangs its caller."""

    def test_security_calls_time_out_as_keychain_errors(self):
        stall = subprocess.TimeoutExpired(["security"], keychain.TIMEOUT)
        with mock.patch.object(keychain.subprocess, "run", side_effect=stall) as run:
            with self.assertRaises(keychain.KeychainError) as cm:
                keychain.get("oa")
            self.assertIn("timed out", str(cm.exception))
            with self.assertRaises(keychain.KeychainError) as cm:
                keychain.add("oa", m2.SECRET)
            self.assertNotIn(m2.SECRET, str(cm.exception))
            self.assertFalse(keychain.delete("oa"))
        self.assertEqual([c.kwargs.get("timeout") for c in run.call_args_list], [keychain.TIMEOUT] * 3)


class KeychainOutsideLockTests(m2.M2Base):
    """#23: one node's slow Keychain read does not hold up the other nodes' syncs."""

    def test_autostart_reads_the_token_outside_its_lock(self):
        starter = autostart.AutoStarter(log=lambda m: None)
        held = []

        def token_line(node, project):
            free = starter.lock.acquire(blocking=False)
            if free:
                starter.lock.release()
            held.append(not free)
            raise nodeops.OpError("keychain read for oa timed out", 409)
        proj = dict(m2.snap_project(), status="idle", lead="dead",
                    questions=[{"id": "q01", "answered": True, "applied": None}])
        with mock.patch.object(nodeops, "token_line", token_line):
            starter.check("lab", {"projects": [proj]})
            starter.check("lab", {"projects": [proj]})
        self.assertEqual(held, [False, False])   # and a failed read is tried again next sync

    def test_an_unexpected_error_releases_the_claim(self):
        starter = autostart.AutoStarter(log=lambda m: None)
        proj = dict(m2.snap_project(), status="idle", lead="dead",
                    questions=[{"id": "q01", "answered": True, "applied": None}])
        with mock.patch.object(nodeops, "token_line", side_effect=OSError("tokens.json unreadable")):
            with self.assertRaises(OSError):
                starter.check("lab", {"projects": [proj]})
        self.assertEqual(starter.started, {})


CLI_ADD = """
import sys
sys.path.insert(0, sys.argv[1])
from cairn_hub import keychain, nodeops
def add(label, value):
    with open(sys.argv[2], "a") as f:
        f.write("cli add %s\\n" % label)
keychain.add = add
keychain.get = keychain.delete = None   # never the real Keychain
nodeops.add_token("oa", "oauth", secret="sk-ant-oat01-FROMTHECLI9999", overwrite=True)
"""


class TokenKeychainOutsideStateLockTests(m2.M2Base):
    """#23: a token change waiting on the Keychain does not hold up state writes, and loses none of them."""

    def stalled(self, op, label, change):
        """Run change() in a thread while keychain.<op>(label) waits, as `security` does on a locked Keychain, and
        record a limit meanwhile. Returns what change() raised, or None."""
        entered, release = threading.Event(), threading.Event()
        real = getattr(keychain, op)

        def stall(l, *a):
            if l == label:
                entered.set()
                release.wait(10)
            return real(l, *a)
        raised = []

        def run():
            try:
                change()
            except BaseException as e:
                raised.append(e)
        now = att.datetime.now(att.timezone.utc)
        writer = threading.Thread(target=att.record_limit, args=("other", now, now + att.timedelta(hours=1), True, ""))
        with mock.patch.object(keychain, op, stall):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(5), "keychain.%s(%s) was never called" % (op, label))
                writer.start()
                writer.join(3)
                self.assertFalse(writer.is_alive(), "a state write waited for keychain.%s(%s)" % (op, label))
                self.assertTrue(worker.is_alive())
            finally:
                release.set()
                worker.join(10)
                if writer.ident:
                    writer.join(10)
        self.assertIn("other", config.load_limits())   # the token change did not write over it
        return raised[0] if raised else None

    def test_rename_reading_and_adding_the_secret(self):
        limit = {"since": "2026-10-20T20:00:00Z", "until": "2099-01-01T00:00:00Z", "known": True, "note": ""}
        for op, label in (("get", "oa"), ("add", "x")):
            with self.subTest(op=op):
                config.save_projects({"lab/demo": {"token": "oa"}})
                config.save_limits({"oa": limit})   # so the rename writes limits.json too
                self.assertIsNone(self.stalled(op, label, lambda: nodeops.update_token("oa", new_label="x")))
                self.assertEqual(self.kc.store, {"x": m2.SECRET})
                self.assertEqual(config.load_projects(), {"lab/demo": {"token": "x"}})
                self.assertEqual(set(config.load_limits()), {"x", "other"})
                nodeops.update_token("x", new_label="oa")

    def test_rename_deleting_the_old_secret(self):
        self.assertIsNone(self.stalled("delete", "oa", lambda: nodeops.update_token("oa", new_label="x")))
        self.assertEqual(self.kc.store, {"x": m2.SECRET})
        self.assertEqual(set(config.load_tokens()), {"x", "nl"})

    def test_failed_rename_deleting_the_new_secret(self):
        with mock.patch.object(config, "save_projects", side_effect=OSError("disk full")):
            e = self.stalled("delete", "x", lambda: nodeops.update_token("oa", new_label="x"))
        self.assertIsInstance(e, OSError)
        self.assertEqual(self.kc.store, {"oa": m2.SECRET})
        self.assertEqual(set(config.load_tokens()), {"oa", "nl"})

    def test_remove_deleting_the_secret(self):
        self.assertIsNone(self.stalled("delete", "oa", lambda: nodeops.remove_token("oa", {"lab": {"projects": []}})))
        self.assertEqual(self.kc.store, {})
        self.assertEqual(set(config.load_tokens()), {"nl"})

    def test_node_login_replace_deleting_the_old_secret(self):
        self.assertIsNone(self.stalled("delete", "oa", lambda: nodeops.add_token("oa", "node-login", overwrite=True)))
        self.assertEqual(self.kc.store, {})
        self.assertEqual(config.load_tokens()["oa"]["kind"], "node-login")

    def test_add_writing_the_secret(self):
        self.assertIsNone(self.stalled("add", "new", lambda: nodeops.add_token("new", "oauth", secret=m2.SECOND)))
        self.assertEqual(self.kc.store["new"], m2.SECOND)

    def test_an_add_of_the_freed_label_waits_for_the_delete(self):
        """The CLI adds oa again while the hub removes it: the new secret is written after the old one is deleted."""
        events = os.path.join(self.tmp, "events")
        entered, release = threading.Event(), threading.Event()
        real = self.kc.delete

        def delete(label):
            entered.set()
            release.wait(10)
            with open(events, "a") as f:
                f.write("hub delete %s\n" % label)
            return real(label)
        with mock.patch.object(keychain, "delete", delete):
            remover = threading.Thread(target=nodeops.remove_token, args=("oa", {"lab": {"projects": []}}))
            remover.start()
            try:
                self.assertTrue(entered.wait(5))
                cli = subprocess.Popen([sys.executable, "-c", CLI_ADD, os.path.dirname(HERE), events])
                self.addCleanup(cli.wait)
                with self.assertRaises(subprocess.TimeoutExpired):
                    cli.wait(2)   # waiting for the hub's removal to finish
            finally:
                release.set()
                remover.join(10)
            self.assertEqual(cli.wait(20), 0)
        with open(events) as f:
            self.assertEqual(f.read().splitlines(), ["hub delete oa", "cli add oa"])
        self.assertEqual(config.load_tokens()["oa"]["last4"], "9999")


class TokenReplaceTests(m2.M2Base):
    def test_node_login_over_oauth_deletes_the_old_secret(self):
        """#27"""
        nodeops.add_token("oa", "node-login", overwrite=True)
        self.assertEqual(config.load_tokens()["oa"]["kind"], "node-login")
        self.assertNotIn("oa", self.kc.store)


class TokenRemoveTests(m2.M2Base):
    """#28: a token is not removed while a node that may run it cannot be checked, and its secret goes last."""

    def test_an_unchecked_node_refuses_unless_forced(self):
        with self.assertRaises(nodeops.OpError) as cm:
            nodeops.remove_token("oa", {"lab": None})
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("cannot check lab", str(cm.exception))
        self.assertIn("oa", config.load_tokens())
        self.assertIn("oa", self.kc.store)
        nodeops.remove_token("oa", {"lab": None}, force=True)
        self.assertNotIn("oa", config.load_tokens())
        self.assertNotIn("oa", self.kc.store)

    def test_a_node_naming_it_for_a_project_counts_too(self):
        config.save_nodes({**config.load_nodes(), "far": {"ssh": "far", "port": None, "allowed_tokens": ["nl"]}})
        config.save_projects({"far/demo": {"token": "oa"}})
        with self.assertRaises(nodeops.OpError):
            nodeops.remove_token("oa", {"lab": {"projects": []}, "far": None})
        nodeops.remove_token("oa", {"lab": {"projects": []}, "far": {"projects": []}})
        self.assertNotIn("oa", self.kc.store)

    def test_a_failed_write_keeps_the_secret(self):
        with mock.patch.object(config, "save_projects", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                nodeops.remove_token("oa", {"lab": {"projects": []}})
        self.assertEqual(self.kc.store.get("oa"), m2.SECRET)


class TokenLimitFollowsTests(m2.M2Base):
    """#30: a token's recorded limit follows a rename and goes with a removal."""
    LIMIT = {"since": "2026-10-20T20:00:00Z", "until": "2099-01-01T00:00:00Z", "known": True, "note": ""}

    def test_rename_moves_and_remove_drops_the_limit(self):
        config.save_limits({"oa": self.LIMIT, "node-login@lab": self.LIMIT})
        nodeops.update_token("oa", new_label="main")
        self.assertEqual(config.load_limits(), {"main": self.LIMIT, "node-login@lab": self.LIMIT})
        nodeops.remove_token("main", {"lab": {"projects": []}})
        self.assertEqual(config.load_limits(), {"node-login@lab": self.LIMIT})
        nodeops.add_token("main", "oauth", secret=m2.SECOND)
        self.assertEqual(att.limits_for("lab", ["main"], config.load_limits()), {})   # a new label starts clean


QUESTION = dict(m8.QUESTION)


class NotifyBase(m2.M2Base):
    def setUp(self):
        super().setUp()
        notify._topic.clear()
        self.addCleanup(notify._topic.clear)
        self.status = 200
        self.sent = []
        self.n = notify.Notifier(post=self.post)
        self.n.setup(server="http://ntfy.example.com")
        self.n.check("lab", {"projects": []}, {}, {})

    def post(self, url, body, headers):
        if self.status != 200:
            return self.status, b""
        self.sent.append(headers["Title"])
        return 200, b'{"id":"x1","event":"message"}'

    def check(self, projects):
        self.n.check("lab", {"projects": projects}, {}, {})


class NtfyStatusTests(NotifyBase):
    """#32: a rate-limited or unauthorised push is tried again, while other refusals are skipped."""

    def test_401_403_429_retry_next_sync(self):
        for status in (401, 403, 429):
            self.status = status
            self.check([m8.project(questions=[dict(QUESTION, id="s%d" % status)])])
            self.assertNotIn("q:lab/demo/s%d" % status, config.read_json("notified.json", {}), status)
            self.assertIn("(%d)" % status, self.n.view()["last_error"])
        self.status = 200
        self.check([m8.project(questions=[dict(QUESTION, id="s%d" % s) for s in (401, 403, 429)])])
        self.assertEqual(self.sent, ["demo: question s401", "demo: question s403", "demo: question s429"])

    def test_400_is_skipped(self):
        self.status = 400
        self.check([m8.project(questions=[QUESTION])])
        self.assertIn("q:lab/demo/q01", config.read_json("notified.json", {}))


class NotifyVolumeTests(NotifyBase):
    """#26: pushes per sync are capped and sent outside the lock, and keys of gone questions are pruned."""

    def questions(self, n):
        return [m8.project(questions=[dict(QUESTION, id="q%03d" % i) for i in range(n)])]

    def test_at_most_ten_pushes_per_sync_and_the_rest_follow(self):
        for want in (10, 20, 25, 25):
            self.check(self.questions(25))
            self.assertEqual(len(self.sent), want)
        self.assertEqual(len(set(self.sent)), 25)

    def test_pushes_go_out_with_the_lock_free_and_an_overlapping_sync_does_not_repeat_them(self):
        seen = []

        def post(url, body, headers):
            if not seen:
                seen.append("first")
                other = threading.Thread(target=self.check, args=(self.questions(1),))
                other.start()
                other.join(5)
                seen.append("free" if not other.is_alive() else "blocked")
            self.sent.append(headers["Title"])
            return 200, b'{"id":"x1","event":"message"}'
        self.n.post = post
        self.check(self.questions(1))
        self.assertEqual(seen, ["first", "free"])
        self.assertEqual(self.sent, ["demo: question q000"])
        self.assertIn("q:lab/demo/q000", config.read_json("notified.json", {}))

    def test_a_project_created_again_under_the_same_name_pushes_again(self):
        self.check(self.questions(1))
        self.check([])   # the project was deleted
        self.assertNotIn("q:lab/demo/q000", config.read_json("notified.json", {}))
        self.check(self.questions(1))
        self.assertEqual(self.sent, ["demo: question q000"] * 2)

    def test_answered_questions_keep_their_keys_and_gone_ones_lose_them(self):
        self.check(self.questions(2))
        answered = [m8.project(questions=[dict(QUESTION, id="q000", answered=True, choice="A")])]
        self.check(answered)
        sent = config.read_json("notified.json", {})
        self.assertIn("q:lab/demo/q000", sent)
        self.assertNotIn("q:lab/demo/q001", sent)


class ChatRedactTests(unittest.TestCase):
    def setUp(self):
        self.mgr = chatmod.ChatManager(None, reap_every=3600)
        self.addCleanup(self.mgr.stop)
        self.chat = chatmod.Chat("lab", "demo")
        self.chat.secret, self.chat.state, self.chat.proc = th.SECRET, "working", mock.Mock()

    def test_an_error_reason_is_redacted_before_it_is_cut(self):
        """#13: a secret straddling the 300-character cut leaves no part of itself behind."""
        reason = "x" * 290 + th.SECRET + " and more"
        self.mgr._handle(self.chat, {"type": "result", "subtype": "error_during_execution", "is_error": True,
                                     "result": reason})
        self.assertEqual(self.chat.state, "error")
        self.assertEqual(self.chat.detail, "x" * 290 + "*** and mo")
        self.assertNotIn(th.SECRET[:10], str(self.chat.view()))


    def test_an_overlong_stream_line_ends_the_chat_without_holding_it(self):
        """#12: a node streaming one endless line cannot grow the hub's memory."""
        line = b'{"type": "cairn", "session_id": "s1"}\n'
        self.chat.proc.stdout = io.BytesIO(b"x" * 250 + b"\n" + line)
        self.chat.proc.stderr = io.BytesIO(b"")
        self.chat.proc.wait.return_value = 0
        reads = []
        readline = self.chat.proc.stdout.readline
        self.chat.proc.stdout.readline = lambda n=-1: reads.append(n) or readline(n)
        with mock.patch.object(chatmod, "MAX_LINE", 100):
            self.mgr._read(self.chat)
        self.assertEqual(self.chat.state, "error")
        self.assertIn("the chat stream sent a line over", self.chat.detail)
        self.assertIsNone(self.chat.session_id)   # nothing after it is relayed
        self.assertTrue(reads and all(0 < n <= 100 for n in reads))

    def test_an_overlong_stderr_line_is_dropped_whole(self):
        """#12: stderr lines are capped too, and a cut never shows part of a secret."""
        self.chat.proc.stderr = io.BytesIO(b"e" * 4090 + th.SECRET.encode() + b"\nclient_loop: send disconnect")
        with mock.patch.object(chatmod, "MAX_ERR_LINE", 4096):
            self.mgr._read_err(self.chat)
        self.assertEqual(self.chat.stderr_tail, ["client_loop: send disconnect"])


class SandboxForceTests(m12.SandboxBase):
    """#25: a running sandbox whose snapshot fails can still be stopped or deleted, on purpose."""

    def setUp(self):
        super().setUp()
        self.docker.images.add("cairn-slim:0.1")
        self.sb.create("box", "slim", wait=True)
        self.set_snapshot("box", None)   # its own cairn-node is broken: the sync fails

    def test_stop_needs_force(self):
        with self.assertRaises(nodeops.OpError) as cm:
            self.sb.stop("box", None)
        self.assertIn("with force", str(cm.exception))
        self.assertEqual(self.sb.stop("box", None, force=True)["state"], "exited")

    def test_remove_needs_force(self):
        with self.assertRaises(nodeops.OpError):
            self.sb.remove("box", "box", None)
        self.sb.remove("box", "box", None, force=True)
        self.assertNotIn("cairn-box", self.docker.containers)
        self.assertNotIn("box", config.load_nodes())

    def test_force_never_overrides_a_live_lead(self):
        alive = {"projects": [m2.snap_project("demo", lead="alive")]}
        with self.assertRaises(nodeops.OpError):
            self.sb.stop("box", alive, force=True)
        self.assertIn("cairn-box", self.docker.containers)


class SandboxRegistryTests(m12.SandboxBase):
    """#31: a sandbox made again after its node and container went keeps no stale registry entry."""

    def test_a_new_container_replaces_the_old_entry(self):
        self.docker.images.add("cairn-slim:0.1")
        self.register("box", port=2290)   # the node was removed, then its container by hand
        self.bound.add(2222)
        job = self.sb.create("box", "slim", wait=True)
        self.assertEqual(job["phase"], "done", job)
        entry = config.load_sandboxes()["box"]
        self.assertEqual((entry["port"], entry["image"]), (2223, "slim"))
        self.assertEqual(config.load_nodes()["box"]["port"], 2223)

    def test_a_reused_container_refreshes_the_port(self):
        self.docker.images.add("cairn-slim:0.1")
        self.docker.add("cairn-box", 2240, labels={"cairn.sandbox": "box"})
        self.register("box", port=2290)
        self.sb.create("box", "slim", wait=True)
        self.assertEqual(config.load_sandboxes()["box"]["port"], 2240)


class RemovedNodeTests(m2.M2Base):
    """#29: what the hub keeps per node goes with the node, so a node added again under its name starts clean."""

    def test_versions_and_episodes_are_forgotten(self):
        a = att.Attention(log=lambda m: None)
        dialog = {"projects": [m8.project(lead="alive", lead_state="dialog", lead_note="Proceed?")]}
        a.check("lab", dialog)
        a.check("other", dialog)
        nodeops.VERSIONS["lab"] = "0.1.0"
        limit = {"since": "x", "until": "2099-01-01T00:00:00Z", "known": True, "note": ""}
        config.save_limits({"node-login@lab": limit, "oa": limit})
        nodeops.remove_node("lab", {"projects": []})
        a.forget_node("lab")
        self.assertNotIn("lab", nodeops.VERSIONS)
        self.assertEqual(config.load_limits(), {"oa": limit})   # the node's login went with it; OAuth stays
        self.assertEqual(set(a.current()), {"other/demo"})


class SandboxHardeningTests(m12.SandboxBase):
    """#10: containers get a process limit, and the slim image no-new-privileges."""

    def run_argv(self, name, image):
        self.docker.images.update({"cairn-slim:0.1", "cairn-dev:0.1"})
        self.sb.create(name, image, wait=True)
        return next(c for c in self.docker.calls if c[0] == "run" and "cairn-" + name in c)

    def test_flags(self):
        slim, dev = self.run_argv("a", "slim"), self.run_argv("b", "dev")
        for argv in (slim, dev):
            self.assertEqual(argv[argv.index("--pids-limit") + 1], "4096")
        self.assertEqual(slim[slim.index("--security-opt") + 1], "no-new-privileges")
        self.assertNotIn("--security-opt", dev)   # dev's sudo needs setuid


if __name__ == "__main__":
    unittest.main()
