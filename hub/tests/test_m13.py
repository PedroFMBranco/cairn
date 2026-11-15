"""Milestone 13: token rotation on usage limits."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_hub as th
import test_m2 as m2
import test_m8 as m8
from cairn_hub import attention as att
from cairn_hub import cli, config, keychain, nodeops, notify, rotation

NOW = datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc)


def limits(untils):
    return {label: {"until": "2026-10-20T%s:00Z" % at} for label, at in untils.items()}


class ChooseTests(unittest.TestCase):
    def test_table(self):
        pool = ["D", "03", "B"]
        cases = [
            ("D", limits({"D": "23:00"}), ("switch", "03")),
            ("D", limits({"D": "23:00", "03": "22:00"}), ("switch", "B")),
            ("03", limits({"03": "23:00"}), ("switch", "B")),
            ("B", limits({"B": "23:00"}), ("switch", "D")),   # wraps
            ("D", limits({"D": "23:00", "03": "21:00"}), ("switch", "03")),   # a limit that ended at `now` is over
            ("D", {}, ("switch", "03")),
            ("D", limits({"D": "23:00", "B": "22:30", "03": "22:00"}), ("wait", "03", "22:00")),
            ("D", limits({"D": "22:00", "B": "22:00", "03": "23:00"}), ("wait", "D", "22:00")),   # tie: pool order
            ("03", limits({"D": "22:00", "B": "22:00", "03": "23:00"}), ("wait", "D", "22:00")),
        ]
        for active, lim, want in cases:
            got = rotation.choose(pool, active, lim, NOW)
            if got[0] == "wait":
                got = (got[0], got[1], got[2].strftime("%H:%M"))
            self.assertEqual(got, want, (active, lim))

    def test_one_token_and_active_outside_the_pool(self):
        until = datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc)
        self.assertEqual(rotation.choose(["D"], "D", limits({"D": "23:00"}), NOW), ("wait", "D", until))
        self.assertEqual(rotation.choose(["D"], "D", {}, NOW), ("switch", "D"))
        self.assertEqual(rotation.choose(["03", "B"], "D", limits({"D": "23:00"}), NOW), ("switch", "03"))
        self.assertIsNone(rotation.choose([], "D", limits({"D": "23:00"}), NOW))   # D left the node's allowed list


class PoolTests(m2.ApiBase):
    def setUp(self):
        super().setUp()
        config.save_tokens({**config.load_tokens(), "o2": {"kind": "oauth", "account": "", "last4": "5555"}})
        self.kc.store["o2"] = m2.SECOND
        nodeops.set_node_tokens("lab", ["oa", "nl", "o2"])

    def entry(self):
        return config.load_projects().get("lab/demo")

    def test_route_sets_the_pool_and_keeps_the_active_token_in_it(self):
        status, out = self.post("/api/projects/token", {"node": "lab", "project": "demo", "token": "oa",
                                                        "tokens": ["nl", "oa"]})
        self.assertEqual((status, out["token"], out["tokens"]), (200, "oa", ["nl", "oa"]))
        self.assertEqual(self.entry(), {"token": "oa", "tokens": ["nl", "oa"]})
        self.assertEqual(self.post("/api/projects/token", {"node": "lab", "project": "demo", "tokens": ["o2"]})[0], 200)
        self.assertEqual(self.entry(), {"token": "oa", "tokens": ["oa", "o2"]})   # the active token is prepended
        self.assertEqual(self.post("/api/projects/token", {"node": "lab", "project": "demo", "token": "nl"})[0], 200)
        self.assertEqual(self.entry(), {"token": "nl", "tokens": ["nl", "oa", "o2"]})
        for bad in ({"tokens": ["oa", "oa"]}, {"tokens": ["ghost"]}, {"tokens": "oa"}, {"tokens": ["oa"], "token": ""}):
            self.assertEqual(self.post("/api/projects/token", {"node": "lab", "project": "demo", **bad})[0], 400, bad)
        nodeops.set_node_tokens("lab", ["oa", "nl"])
        self.assertEqual(self.post("/api/projects/token", {"node": "lab", "project": "demo", "tokens": ["o2"]})[0], 400)
        self.assertEqual(self.entry(), {"token": "nl", "tokens": ["nl", "oa", "o2"]})
        self.assertEqual(self.post("/api/projects/token", {"node": "lab", "project": "demo", "tokens": []})[0], 200)
        self.assertEqual(self.entry(), {"token": "nl"})

    def test_cli_mirrors_node_tokens(self):
        self.assertEqual(cli.main(["project", "tokens", "lab", "demo", "o2,nl"]), 0)
        self.assertEqual(self.entry(), {"token": "oa", "tokens": ["oa", "o2", "nl"]})
        self.assertIn("token pool: oa, o2, nl (active oa)", self.out.getvalue())
        self.assertEqual(cli.main(["project", "tokens", "lab", "demo", "o2,ghost"]), 1)
        self.assertEqual(cli.main(["project", "tokens", "lab", "demo", ""]), 0)
        self.assertEqual(self.entry(), {"token": "oa"})

    def test_rename_and_remove_follow_into_the_pool(self):
        nodeops.set_project_token("lab", "demo", "oa", ["oa", "nl", "o2"])
        self.assertEqual(self.post("/api/tokens/update", {"label": "o2", "new_label": "spare"})[0], 200)
        self.assertEqual(self.entry(), {"token": "oa", "tokens": ["oa", "nl", "spare"]})
        self.assertEqual(self.post("/api/tokens/remove", {"label": "oa"})[0], 200)
        self.assertEqual(self.entry(), {"tokens": ["nl", "spare"]})
        self.assertEqual(config.token_pool("lab", "demo"), ["nl", "spare"])   # the node's first allowed is active

    def test_state_shows_the_pool_and_each_limit(self):
        nodeops.set_project_token("lab", "demo", "oa", ["oa", "nl"])
        config.save_limits({"oa": {"since": "2026-10-20T20:00:00Z", "until": "2099-01-01T00:00:00Z", "known": False,
                                   "note": "usage limit reached"},
                            "nl": {"since": "2026-10-20T20:00:00Z", "until": "2026-10-20T21:00:00Z", "known": True,
                                   "note": "resets 9pm"}})
        p = self.state()["projects"]["lab/demo"]
        self.assertEqual((p["token"], p["tokens"]), ("oa", ["oa", "nl"]))
        self.assertEqual(p["limits"], {"oa": {"until": "2099-01-01T00:00:00Z", "known": False}})   # nl's has ended


class RotationBase(th.Base):
    """A lead on strand whose token hits its limit, with fake snapshots and a recorded restart."""
    POOL = ["researcher-D", "researcher-03", "researcher-B"]

    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 10, 20, 21, 7, tzinfo=timezone.utc)
        self.restarts, self.logs = [], []
        self.a = att.Attention(restart=lambda n, p: self.restarts.append((n, p, config.token_label_for(n, p))),
                               clock=lambda: 0.0, now=lambda: self.now, log=self.logs.append)
        config.save_tokens({label: {"kind": "oauth"} for label in self.POOL})
        config.save_nodes({"strand": {"ssh": "x", "port": None, "allowed_tokens": list(self.POOL)}})
        nodeops.set_project_token("strand", "strand", "researcher-D", self.POOL)
        tz = mock.patch.dict(os.environ, {"TZ": "UTC"})   # the menu's clock times carry no zone
        tz.start()
        self.addCleanup(tz.stop)

    def check(self, state="limit", note="⎿  You've hit your session limit · resets 11pm (UTC)", taken=None, login=None):
        p = {"name": "strand", "lead": "alive", "status": "working 42", "lead_state": state,
             "lead_note": note if state == "limit" else None}
        snap = {"time": att.iso(taken or self.now), "projects": [p]}
        if login is not None:
            snap["login"] = login
        self.a.check("strand", snap)
        return self.a.current().get("strand/strand")

    def at(self, hhmm):
        h, m = map(int, hhmm.split(":"))
        self.now = datetime(2026, 10, 20, h, m, tzinfo=timezone.utc)

    def later(self, seconds=att.RESUME_DELAY + 30):
        self.now += timedelta(seconds=seconds)


class RotationTests(RotationBase):
    def test_a_limit_switches_to_the_next_token_and_restarts_once(self):
        e = self.check()
        self.assertEqual(self.restarts, [("strand", "strand", "researcher-03")])
        self.assertEqual(config.load_projects()["strand/strand"], {"token": "researcher-03", "tokens": self.POOL})
        self.assertEqual(config.load_limits()["researcher-D"]["until"], "2026-10-20T23:00:00Z")
        self.assertTrue(config.load_limits()["researcher-D"]["known"])
        self.assertEqual((e["token"], e["switch_to"], e["switched_to"], e["resume_at"]),
                         ("researcher-D", "researcher-03", "researcher-03", None))
        self.assertIn("strand/strand switched from researcher-D to researcher-03 after a usage limit", self.logs)
        self.assertIsNone(self.check("working"))
        self.assertEqual(len(self.restarts), 1)

    def test_the_next_token_hitting_its_limit_moves_on_again(self):
        self.check()
        self.later()
        self.check()   # the restarted lead on researcher-03 is at a limit too
        self.assertEqual([r[2] for r in self.restarts], ["researcher-03", "researcher-B"])
        self.later()
        e = self.check()   # and on researcher-B: all three reset at 23:00, so the first in the pool
        self.assertEqual(len(self.restarts), 2)
        self.assertEqual((e["token"], e["switch_to"], e["resume_at"]),
                         ("researcher-B", "researcher-D", "2026-10-20T23:02:00Z"))

    def test_a_limit_read_in_the_wrong_zone_never_counts_as_reset(self):
        """An older node prints "11pm" on its UTC clock; the Mac in Lisbon reads 22:00Z, already past at 22:05Z."""
        os.environ["TZ"] = "Europe/Lisbon"
        self.at("22:05")
        for _ in range(12):   # six minutes of syncs, the pane showing the limit throughout
            self.check(note="2. Wait here, then continue automatically at 11pm")
            self.later(30)
        self.assertEqual([r[2] for r in self.restarts], ["researcher-03", "researcher-B", "researcher-D"])
        for label, lim in config.load_limits().items():   # each limit lasts at least RESUME_RETRY from when it was seen
            self.assertGreaterEqual(datetime.fromisoformat(lim["until"]) - datetime.fromisoformat(lim["since"]),
                                    timedelta(seconds=att.RESUME_RETRY), label)

    def test_a_node_that_names_its_zone_waits_for_the_real_reset(self):
        os.environ["TZ"] = "Europe/Lisbon"
        self.at("22:05")
        for _ in range(12):
            e = self.check(note="2. Wait here, then continue automatically at 11pm (UTC)")
            self.later(30)
        self.assertEqual([r[2] for r in self.restarts], ["researcher-03", "researcher-B"])
        self.assertEqual((e["token"], e["switch_to"], e["resume_at"]),
                         ("researcher-B", "researcher-D", "2026-10-20T23:02:00Z"))

    def test_a_limit_after_a_restart_on_the_same_token_opens_a_new_episode(self):
        """Every token limited; the active one resets first, so the lead restarts on it and is limited again."""
        config.save_limits({"researcher-03": {"since": "x", "until": "2026-10-20T23:30:00Z", "known": True, "note": ""},
                            "researcher-B": {"since": "x", "until": "2026-10-20T23:45:00Z", "known": True, "note": ""}})
        e = self.check()
        self.assertEqual((e["switch_to"], e["resume_at"]), (None, "2026-10-20T23:02:00Z"))
        self.at("23:02")
        self.check()
        self.assertEqual(self.restarts, [("strand", "strand", "researcher-D")])
        weekly = "⎿  You've hit your weekly limit · resets Oct 24"
        self.at("23:03")
        e = self.check(note=weekly)   # inside the grace: still the old episode
        self.assertEqual((e["since"], len(self.restarts)), ("2026-10-20T21:07:00Z", 1))
        self.at("23:05")
        e = self.check(note=weekly)
        self.assertEqual((e["since"], e["token"], e["known"], e["switch_to"], e["resume_at"]),
                         ("2026-10-20T23:05:00Z", "researcher-D", False, "researcher-03", "2026-10-20T23:32:00Z"))
        self.at("23:32")
        e = self.check(note=weekly)
        self.assertEqual([r[2] for r in self.restarts], ["researcher-D", "researcher-03"])
        self.assertEqual((e["switched_to"], config.token_label_for("strand", "strand")), ("researcher-03", "researcher-03"))

    def test_a_snapshot_taken_before_the_restart_does_not_rotate_again(self):
        """A second sync thread (POST /api/sync) captured the pane before the switch and reports after it."""
        self.check()
        before = self.now
        self.later()
        self.check(taken=before)
        self.check(taken=before + timedelta(seconds=att.RESUME_DELAY))   # the resumed pane still shows the old text
        self.assertEqual([r[2] for r in self.restarts], ["researcher-03"])
        self.assertEqual(set(config.load_limits()), {"researcher-D"})

    def test_the_resume_token_is_chosen_again_when_the_wait_ends(self):
        config.save_limits({"researcher-03": {"since": "x", "until": "2026-10-20T22:30:00Z", "known": True, "note": ""},
                            "researcher-B": {"since": "x", "until": "2026-10-20T22:00:00Z", "known": True, "note": ""}})
        e = self.check()
        self.assertEqual((e["switch_to"], e["resume_at"]), ("researcher-B", "2026-10-20T22:02:00Z"))
        nodeops.set_node_tokens("strand", ["researcher-D", "researcher-03"])   # B is no longer allowed on strand
        self.at("22:02")
        e = self.check()
        self.assertEqual((self.restarts, e["switch_to"], e["resume_at"]), ([], "researcher-03", "2026-10-20T22:32:00Z"))
        self.at("22:32")
        e = self.check()
        self.assertEqual(self.restarts, [("strand", "strand", "researcher-03")])
        self.assertEqual((e["switch_to"], e["switched_to"]), ("researcher-03", "researcher-03"))

    def test_failed_switch_restart_retries_on_the_new_token(self):
        def restart(n, p):
            self.restarts.append(config.token_label_for(n, p))
            raise nodeops.OpError("node unreachable", 502)
        self.a.restart = restart
        self.check()
        for hhmm in ("21:13", "21:19", "21:30"):
            self.at(hhmm)
            e = self.check()
        self.assertEqual(self.restarts, ["researcher-03"] * 3)
        self.assertEqual((e["resume_attempts"], e["resume_at"], e["switched_to"]), (3, None, "researcher-03"))
        self.assertEqual(set(config.load_limits()), {"researcher-D"})   # 03 never ran, so it is not marked limited

    def test_b_every_token_limited_waits_for_the_first_reset_then_switches(self):
        config.save_limits({"researcher-03": {"since": "x", "until": "2026-10-20T22:30:00Z", "known": True, "note": ""},
                            "researcher-B": {"since": "x", "until": "2026-10-20T22:00:00Z", "known": True, "note": ""}})
        e = self.check()
        self.assertEqual((e["switch_to"], e["resume_at"]), ("researcher-B", "2026-10-20T22:02:00Z"))
        self.assertEqual((self.restarts, config.token_label_for("strand", "strand")), ([], "researcher-D"))
        self.at("22:01")
        self.check()
        self.assertEqual(self.restarts, [])
        self.at("22:02")
        e = self.check()
        self.assertEqual(self.restarts, [("strand", "strand", "researcher-B")])
        self.assertEqual((e["switched_to"], e["resume_at"]), ("researcher-B", None))

    def test_c_menu_note_and_a_note_without_a_time(self):
        nodeops.set_project_token("strand", "strand", pool=[])
        e = self.check(note="2. Wait here, then continue automatically at 11pm")
        self.assertEqual((e["resume_at"], e["switch_to"], self.restarts), ("2026-10-20T23:02:00Z", None, []))
        self.check("working")
        e = self.check(note="❯ 1. Stop and wait for limit to reset")
        self.assertEqual(e["resume_at"], "2026-10-21T02:09:00Z")   # 21:07 + the 5-hour window + 2 minutes
        self.assertFalse(config.load_limits()["researcher-D"]["known"])

    def test_d_one_token_pool_restarts_after_the_reset_on_the_same_token(self):
        nodeops.set_project_token("strand", "strand", pool=[])
        e = self.check(note="limit reached ∙ resets in 1h")
        self.assertEqual((e["resume_at"], e["switch_to"]), ("2026-10-20T22:09:00Z", None))
        self.at("22:09")
        e = self.check(note="limit reached ∙ resets in 1h")
        self.assertEqual(self.restarts, [("strand", "strand", "researcher-D")])
        self.assertEqual((e["switched_to"], e["resume_at"]), (None, None))
        self.check(note="limit reached ∙ resets in 1h")   # still at its limit: no second restart, as before
        self.assertEqual(len(self.restarts), 1)

    def test_e_tokens_the_node_cannot_run_are_skipped(self):
        config.save_tokens({**config.load_tokens(), "seat": {"kind": "retired-kind"}, "spare": {"kind": "node-login"}})
        config.save_nodes({"strand": {"ssh": "x", "port": None,
                                      "allowed_tokens": ["researcher-D", "researcher-B", "seat", "spare"]}})
        config.save_projects({"strand/strand": {"token": "researcher-D",
                                                "tokens": ["researcher-D", "researcher-03", "gone", "seat", "spare"]}})
        self.check()
        self.assertEqual(self.restarts, [("strand", "strand", "spare")])


class SnapshotLoginTests(unittest.TestCase):
    def test_the_hub_reads_only_yes_and_no(self):
        from cairn_hub import ssh
        for raw, want in (("yes", "yes"), ("no", "no"), ("maybe", "unknown"), (False, "unknown"), (None, "unknown")):
            snap = {"projects": []} if raw is None else {"projects": [], "login": raw}
            self.assertEqual(ssh.parse_snapshot(snap)["login"], want, raw)


class NodeLoginPreflightTests(RotationBase):
    """Rotation never switches a project to a node-login token on a node whose snapshot says it is not signed in."""
    POOL = ["researcher-D", "own", "researcher-B"]

    def setUp(self):
        super().setUp()
        config.save_tokens({**config.load_tokens(), "own": {"kind": "node-login"}})

    def test_a_signed_out_node_skips_its_node_login_token(self):
        e = self.check(login="no")
        self.assertEqual(self.restarts, [("strand", "strand", "researcher-B")])
        self.assertEqual((e["switch_to"], e["switched_to"]), ("researcher-B", "researcher-B"))
        self.assertIn("strand/strand: node strand is not signed in to Claude, so own cannot run there; "
                      "not switching to it", self.logs)

    def test_signed_in_or_unknown_switches_as_before(self):
        for login in ("yes", "unknown", None):   # None: a cairn-node older than 0.4.6 sends no login field
            with self.subTest(login=login):
                nodeops.set_project_token("strand", "strand", "researcher-D", self.POOL)
                config.save_limits({})
                self.a.episodes.clear()
                self.restarts.clear()
                self.check(login=login)
                self.assertEqual(self.restarts, [("strand", "strand", "own")])

    def test_signed_out_waits_for_the_limited_token_instead(self):
        nodeops.set_project_token("strand", "strand", "researcher-D", ["researcher-D", "own"])
        e = self.check(login="no")
        self.assertEqual((self.restarts, e["switch_to"], e["resume_at"]), ([], None, "2026-10-20T23:02:00Z"))
        self.assertEqual(config.token_label_for("strand", "strand"), "researcher-D")

    def test_signed_out_with_no_other_token_fails_the_restart_and_keeps_the_token(self):
        nodeops.set_project_token("strand", "strand", "researcher-D", ["researcher-D", "own"])
        config.save_nodes({"strand": {"ssh": "x", "port": None, "allowed_tokens": ["own"]}})   # D left the node since
        self.assertEqual(self.check(login="no")["resume_at"], "2026-10-20T23:02:00Z")   # nothing to run: the reset
        self.at("23:02")
        e = self.check(login="no")
        self.assertEqual(self.restarts, [])
        self.assertEqual(config.token_label_for("strand", "strand"), "researcher-D")
        self.assertIn("node strand is not signed in to Claude", e["resume_error"])
        self.assertEqual((e["resume_attempts"], e["switched_to"]), (1, None))
        self.at("23:07")
        e = self.check(login="yes")   # signed in meanwhile: the next attempt switches
        self.assertEqual(self.restarts, [("strand", "strand", "own")])
        self.assertEqual((e["resume_error"], e["switched_to"]), (None, "own"))

    def test_the_active_node_login_token_restarts_as_before(self):
        nodeops.set_project_token("strand", "strand", "own", [])   # only a switch is checked, never the same token
        e = self.check(login="no")
        self.assertEqual((e["switch_to"], e["resume_at"]), (None, "2026-10-20T23:02:00Z"))
        self.at("23:02")
        e = self.check(login="no")
        self.assertEqual(self.restarts, [("strand", "strand", "own")])
        self.assertIsNone(e["resume_error"])

    def test_a_switch_to_node_login_whose_start_fails_is_retried_then_given_up(self):
        def restart(n, p):
            self.restarts.append(config.token_label_for(n, p))
            raise nodeops.OpError("claude in strand has no usable login: Not logged in · Please run /login", 502)
        self.a.restart = restart
        self.check(login="unknown")
        for hhmm in ("21:13", "21:19", "21:30"):
            self.at(hhmm)
            e = self.check(login="unknown")
        self.assertEqual(self.restarts, ["own"] * 3)
        self.assertEqual((e["resume_attempts"], e["resume_at"], e["switched_to"]), (3, None, "own"))
        self.assertIn("no usable login", e["resume_error"])


class SwitchPushTests(RotationBase):
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

    def sync(self, note):
        self.check(note=note)
        p = {"name": "strand", "lead": "alive", "status": "working 42", "lead_state": "limit", "lead_note": note}
        self.n.check("strand", {"projects": [p]}, {}, self.a.current(), self.a.open_episodes())
        return [body for _, body, _ in self.post.sent]

    def test_the_push_names_the_switch(self):
        self.assertEqual(self.sync("⎿  You've hit your session limit · resets 11pm (UTC)"),
                         ["strand switched to researcher-03: researcher-D hit its limit (resets 23:00)"])

    def test_each_switch_of_a_cascade_and_the_wait_push(self):
        note = "⎿  You've hit your session limit · resets 11pm (UTC)"
        for _ in range(3):
            self.sync(note)
            self.later()
        self.assertEqual(self.sync(note), ["strand switched to researcher-03: researcher-D hit its limit (resets 23:00)",
                                           "strand switched to researcher-B: researcher-03 hit its limit (resets 23:00)",
                                           "Hit a usage limit: " + note])

    def test_a_limit_with_nothing_to_switch_to_pushes_as_before(self):
        nodeops.set_project_token("strand", "strand", pool=[])
        self.assertEqual(self.sync("usage limit reached"), ["Hit a usage limit: usage limit reached"])


if __name__ == "__main__":
    unittest.main()
