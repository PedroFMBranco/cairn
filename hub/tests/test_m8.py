"""Milestones 8 to 11: notifications, attention, run it, Update LEAD.md, usage, the LaunchAgent."""
import contextlib
import http.server
import io
import json
import os
import sys
import threading
import unittest
import urllib.request
from datetime import datetime, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_m2 as m2
import test_m7 as m7
from cairn_hub import attention as att
from cairn_hub import cli, config, keychain, nodeops, notify
from cairn_hub.server import CairnServer
from cairn_hub.sync import Syncer

QUESTION = {"id": "q01", "ticket": "", "gate": False, "default": "B", "asked_at": "2026-10-17T12:00:00Z",
            "context": "Which way?\nmore", "options": ["A. left", "B. right"], "answered": False, "choice": None}


def project(**over):
    p = m2.snap_project()
    p.update(over)
    return p


class Recorder:
    def __init__(self):
        self.sent, self.fail = [], False

    def __call__(self, url, body, headers):
        if self.fail:
            raise OSError("no route to host")
        self.sent.append((url, body, headers))
        return 200, b'{"id":"x1","event":"message","topic":"t"}'


class NotifyTests(m2.M2Base):
    def setUp(self):
        super().setUp()
        notify._topic.clear()
        self.addCleanup(notify._topic.clear)
        self.post = Recorder()
        self.n = notify.Notifier(post=self.post)

    def check(self, projects, entries=None, attention=None):
        self.n.check("lab", {"projects": projects}, entries or {}, attention or {})

    def titles(self):
        return [h["Title"] for _, _, h in self.post.sent]

    def test_nothing_before_setup_and_no_keychain_read(self):
        with mock.patch.object(notify.keychain, "get", side_effect=AssertionError("keychain read")):
            self.check([project(questions=[QUESTION])])
            self.assertEqual(self.n.view(with_topic=False), {"configured": False, "enabled": True, "last_error": None})
        self.assertEqual(self.post.sent, [])

    def test_setup_then_once_only(self):
        with self.assertRaises(ValueError):
            self.n.setup()   # no default server
        view = self.n.setup(server="http://ntfy.example.com")
        self.assertTrue(view["configured"])
        self.assertTrue(view["topic"].startswith("cairn-"))
        self.assertEqual(self.kc.store[notify.TOPIC_ACCOUNT], view["topic"])
        self.assertEqual(view["server"], "http://ntfy.example.com")
        self.check([project(questions=[QUESTION])])   # first sync: recorded, not pushed
        self.assertEqual(self.post.sent, [])
        q2 = dict(QUESTION, id="q02", gate=True, context="Delete the old tiles?")
        self.check([project(questions=[QUESTION, q2])])
        self.check([project(questions=[QUESTION, q2])])
        self.assertEqual(len(self.post.sent), 1)
        url, body, headers = self.post.sent[0]
        self.assertEqual(url, "http://ntfy.example.com/" + view["topic"])
        self.assertEqual((headers["Title"], headers["Priority"], body), ("demo: question q02", "high", "Delete the old tiles?"))
        # a fresh Notifier (hub restart) sends nothing again
        notify.Notifier(post=self.post).check("lab", {"projects": [project(questions=[QUESTION, q2])]}, {}, {})
        self.assertEqual(len(self.post.sent), 1)

    def test_failure_retries_next_sync(self):
        self.n.setup(server="http://ntfy.example.com")
        self.check([])
        self.post.fail = True
        self.check([project(questions=[QUESTION])])
        self.assertIn("no route to host", self.n.view()["last_error"])
        self.post.fail = False
        self.check([project(questions=[QUESTION])])
        self.assertEqual(self.titles(), ["demo: question q01"])
        self.assertIsNone(self.n.view()["last_error"])
        self.assertIsNotNone(self.n.view()["last_sent_at"])

    def test_result_and_attention(self):
        self.n.setup(server="http://ntfy.example.com")
        self.check([])
        done = project(status="done", result="# A game.\nmore", result_sha="abc1234")
        self.check([done], entries={"lab/demo": {"result_ready": True}},
                   attention={"lab/demo": {"kind": "limit", "since": "2026-10-18T10:00:00Z",
                                           "note": "5-hour limit reached ∙ resets 3pm", "resume_at": None}})
        self.assertEqual(self.titles(), ["demo: result ready", "demo: needs attention"])
        self.assertEqual(self.post.sent[0][1], "A game.")
        self.assertEqual(self.post.sent[1][1], "Hit a usage limit: 5-hour limit reached ∙ resets 3pm")
        self.assertEqual(self.post.sent[1][2]["Priority"], "high")

    def test_prompt_episode_under_ten_minutes_keeps_its_push(self):
        self.n.setup(server="http://ntfy.example.com")
        self.check([])
        prompt = {"lab/demo": {"kind": "prompt", "since": "x", "note": None, "resume_at": None}}
        self.n.check("lab", {"projects": [project()]}, {}, prompt, {"lab/demo/prompt"})
        # hub restart: the new episode is not counted for 10 minutes, but it is open, so the key stays
        notify.Notifier(post=self.post).check("lab", {"projects": [project()]}, {}, {}, {"lab/demo/prompt"})
        notify.Notifier(post=self.post).check("lab", {"projects": [project()]}, {}, prompt, {"lab/demo/prompt"})
        self.assertEqual(self.titles(), ["demo: needs attention"])

    def test_attention_once_per_episode_across_hub_restarts(self):
        self.n.setup(server="http://ntfy.example.com")
        self.check([])
        crash = lambda since: {"lab/demo": {"kind": "crashed", "since": since, "note": None, "resume_at": None}}
        self.check([project()], attention=crash("2026-10-18T10:00:00Z"))
        notify.Notifier(post=self.post).check("lab", {"projects": [project()]}, {}, crash("2026-10-18T10:05:00Z"))
        self.assertEqual(self.titles(), ["demo: needs attention"])   # the restarted hub saw a new "since"
        self.check([project()])                                      # the episode ended
        self.check([project()], attention=crash("2026-10-18T11:00:00Z"))
        self.assertEqual(self.titles(), ["demo: needs attention"] * 2)

    def test_disabled_and_reenabled_does_not_flood(self):
        self.n.setup(server="http://ntfy.example.com")
        self.check([])
        self.n.setup(enabled=False)
        self.check([project(questions=[QUESTION])])
        self.assertEqual(self.post.sent, [])
        self.n.setup(enabled=True)
        self.check([project(questions=[QUESTION])])   # piled up while off: recorded, not pushed
        self.check([project(questions=[QUESTION, dict(QUESTION, id="q02")])])
        self.assertEqual(self.titles(), ["demo: question q02"])

    def test_bad_server_refused(self):
        with self.assertRaises(ValueError):
            self.n.setup(server="ntfy.example.com")


class FakeNtfy(http.server.BaseHTTPRequestHandler):
    mode = "ntfy"
    seen = []

    def do_POST(self):
        self.seen.append(("POST", self.path))
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.mode == "redirect":
            self.send_response(301)
            self.send_header("Location", "/login")
            self.end_headers()
            return
        body = b'{"id":"a1","event":"message"}' if self.mode == "ntfy" else b"<html>portal</html>"
        status = 400 if self.mode == "refuse" else 200
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.seen.append(("GET", self.path))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


class DeliveryTests(m2.M2Base):
    """Against a real local HTTP server: only a genuine ntfy answer counts as delivered."""

    def setUp(self):
        super().setUp()
        notify._topic.clear()
        self.addCleanup(notify._topic.clear)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeNtfy)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        FakeNtfy.seen = []
        self.n = notify.Notifier()
        self.n.setup(server="http://127.0.0.1:%d" % self.srv.server_address[1])

    def test_genuine_answer(self):
        FakeNtfy.mode = "ntfy"
        self.n.test()
        self.assertIsNone(self.n.view()["last_error"])

    def test_redirect_and_portal_are_failures_and_retry(self):
        for mode, words in (("redirect", "redirected"), ("portal", "not ntfy")):
            FakeNtfy.mode, FakeNtfy.seen = mode, []
            with self.assertRaises(RuntimeError):
                self.n.test()
            self.assertIn(words, self.n.view()["last_error"])
            self.assertEqual([m for m, _ in FakeNtfy.seen], ["POST"])   # never followed by a GET
        self.n.check("lab", {"projects": []}, {}, {})
        FakeNtfy.mode = "portal"
        self.n.check("lab", {"projects": [project(questions=[QUESTION])]}, {}, {})
        self.assertNotIn("q:lab/demo/q01", config.read_json("notified.json", {}))   # not recorded: retried later
        topic = self.n.view()["topic"]
        self.assertNotIn(topic, self.n.view()["last_error"])

    def test_refused_test_push_is_502(self):
        FakeNtfy.mode = "refuse"
        with self.assertRaises(notify.Undeliverable):
            self.n.test()

    def test_refused_item_is_skipped_not_blocking(self):
        self.n.check("lab", {"projects": []}, {}, {})
        FakeNtfy.mode = "refuse"
        self.n.check("lab", {"projects": [project(questions=[QUESTION, dict(QUESTION, id="q02")])]}, {}, {})
        sent = config.read_json("notified.json", {})
        self.assertIn("q:lab/demo/q01", sent)
        self.assertIn("q:lab/demo/q02", sent)
        self.assertEqual(len(FakeNtfy.seen), 2)

    def test_bad_server_urls_refused(self):
        for bad in ("ntfy.example.com", "http://ntfy.example.com/a b", "http://", "http://ntfy.example.com/?x=1", "ftp://ntfy.example.com"):
            with self.assertRaises(ValueError, msg=bad):
                self.n.setup(server=bad)

    def test_unreadable_keychain_never_replaces_the_topic(self):
        topic = self.n.view()["topic"]
        notify._topic.clear()
        with mock.patch.object(keychain, "get", side_effect=keychain.KeychainError("locked")):
            with self.assertRaises(keychain.KeychainError):
                self.n.setup()
            v = self.n.view(with_topic=False)
            self.assertTrue(v["configured"])
            self.assertIn("locked", v["last_error"])
        self.assertEqual(self.kc.store[notify.TOPIC_ACCOUNT], topic)


class AttentionTests(m2.th.Base):   # a limit is recorded in limits.json, so CAIRN_HOME is a temp dir
    def setUp(self):
        super().setUp()
        self.t = 1000.0
        self.now = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
        self.restarts, self.busy = [], set()
        self.a = att.Attention(restart=lambda n, p: self.restarts.append((n, p)), busy=lambda n, p: (n, p) in self.busy,
                               clock=lambda: self.t, now=lambda: self.now, log=lambda m: None)

    def snap(self, **over):
        return {"projects": [project(lead="alive", **over)]}

    def test_kinds(self):
        self.a.check("lab", self.snap(lead_state="dialog", lead_note="Do you want to proceed?"))
        self.assertEqual(self.a.current()["lab/demo"]["kind"], "dialog")
        self.a.check("lab", self.snap(lead_state="working"))
        self.assertEqual(self.a.current(), {})
        self.a.check("lab", {"projects": [project(lead="dead", status="working 03")]})
        self.assertEqual(self.a.current()["lab/demo"]["kind"], "crashed")
        self.busy.add(("lab", "demo"))   # a review chat is open: not a crash
        self.a.check("lab", {"projects": [project(lead="dead", status="working 03")]})
        self.assertEqual(self.a.current(), {})
        self.a.check("lab", {"projects": []})
        self.assertEqual(self.a.episodes, {})

    def test_deliberate_stop_and_failed_capture(self):
        self.a.check("lab", {"projects": [project(lead="dead", status="working 03", lead_stopped=True)]})
        self.assertEqual(self.a.current(), {})
        self.a.check("lab", self.snap(lead_state="limit", lead_note="usage limit reached"))
        since = self.a.current()["lab/demo"]["since"]
        self.now = datetime(2026, 10, 18, 10, 5, tzinfo=timezone.utc)
        self.a.check("lab", self.snap(lead_state=None))   # the capture failed: the episode stays
        self.a.check("lab", self.snap(lead_state="limit", lead_note="usage limit reached"))
        self.assertEqual(self.a.current()["lab/demo"]["since"], since)

    def test_failed_resume_retries_then_gives_up(self):
        calls = []

        def restart(n, p):
            calls.append(self.now)
            raise nodeops.OpError("node unreachable", 502)
        self.a.restart = restart
        note = "x resets in 1h"
        self.a.check("lab", self.snap(lead_state="limit", lead_note=note))
        for minutes in (63, 64, 69, 75, 90, 200):
            self.now = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc) + att.timedelta(minutes=minutes)
            self.a.check("lab", self.snap(lead_state="limit", lead_note=note))
        self.assertEqual(len(calls), 3)
        e = self.a.current()["lab/demo"]
        self.assertEqual((e["resume_attempts"], e["resume_at"]), (3, None))
        self.assertIn("node unreachable", e["resume_error"])

    def test_successful_resume_clears_resume_at(self):
        self.a.check("lab", self.snap(lead_state="limit", lead_note="x resets in 1h"))
        self.now = datetime(2026, 10, 18, 11, 3, tzinfo=timezone.utc)
        self.a.check("lab", self.snap(lead_state="limit", lead_note="x resets in 1h"))
        e = self.a.current()["lab/demo"]
        self.assertEqual((self.restarts, e["resume_at"], e["resume_error"]), ([("lab", "demo")], None, None))

    def test_prompt_counts_after_ten_minutes(self):
        self.a.check("lab", self.snap(lead_state="prompt"))
        self.assertEqual(self.a.current(), {})
        self.t += 599
        self.a.check("lab", self.snap(lead_state="prompt"))
        self.assertEqual(self.a.current(), {})
        self.t += 2
        self.assertEqual(self.a.current()["lab/demo"]["kind"], "prompt")

    def test_limit_resumes_once_after_the_reset(self):
        self.a.check("lab", self.snap(lead_state="limit", lead_note="limit reached ∙ resets in 1h 30m"))
        e = self.a.current()["lab/demo"]
        self.assertEqual((e["since"], e["resume_at"]), ("2026-10-18T10:00:00Z", "2026-10-18T11:32:00Z"))
        self.now = datetime(2026, 10, 18, 11, 31, tzinfo=timezone.utc)
        self.a.check("lab", self.snap(lead_state="limit", lead_note="limit reached ∙ resets in 1h 30m"))
        self.assertEqual(self.restarts, [])
        self.now = datetime(2026, 10, 18, 11, 33, tzinfo=timezone.utc)
        self.busy.add(("lab", "demo"))
        self.a.check("lab", self.snap(lead_state="limit", lead_note="x resets in 1h 30m"))
        self.assertEqual(self.restarts, [])   # waits while a review chat is open
        self.busy.clear()
        self.a.check("lab", self.snap(lead_state="limit", lead_note="x resets in 1h 30m"))
        self.a.check("lab", self.snap(lead_state="limit", lead_note="x resets in 1h 30m"))
        self.assertEqual(self.restarts, [("lab", "demo")])

    def test_parse_reset(self):
        now = datetime(2026, 10, 18, 14, 0, tzinfo=timezone.utc)   # 15:00 in Lisbon (UTC+1)
        cases = {
            "5-hour limit reached ∙ resets 3pm (Europe/Lisbon)": "2026-10-18T14:00:00Z",   # just passed: reset already
            "resets 8am (Europe/Lisbon)": "2026-10-19T07:00:00Z",   # 7 h ago: tomorrow
            "resets 3pm (UTC)": "2026-10-18T15:00:00Z",
            "resets 4:30pm (Europe/Lisbon)": "2026-10-18T15:30:00Z",
            "resets at 09:00 (Europe/Lisbon)": "2026-10-19T08:00:00Z",
            "resets in 2h": "2026-10-18T16:00:00Z",
            "resets in 45m": "2026-10-18T14:45:00Z",
            "resets in 1h 20m": "2026-10-18T15:20:00Z",
            "Continuing automatically at 6:30am (UTC) · esc to cancel": "2026-10-19T06:30:00Z",
        }
        for note, want in cases.items():
            self.assertEqual(att.iso(att.parse_reset(note, now)), want, note)
        with mock.patch.dict(os.environ, {"TZ": "Europe/Lisbon"}):
            self.assertEqual(att.iso(att.parse_reset("2. Wait here, then continue automatically at 11pm (UTC)", now)),
                             "2026-10-18T23:00:00Z")   # cairn-node 0.3.2 names its zone
            self.assertEqual(att.iso(att.parse_reset("2. Wait here, then continue automatically at 11pm", now)),
                             "2026-10-18T22:00:00Z")   # an older node does not: the Mac's zone
        for note in ("Usage limit reached", "resets Oct 20", "resets 25:00", "resets 13pm",
                     "❯ 1. Stop and wait for limit to reset", "continue automatically", None):
            self.assertIsNone(att.parse_reset(note, now), note)


class RouteTests(m2.ApiBase):
    def setUp(self):
        super().setUp()
        notify._topic.clear()
        self.addCleanup(notify._topic.clear)

    def test_restart_stops_then_starts_with_models(self):
        from cairn_hub import nodeops
        nodeops.set_project_models("lab", "demo", "opus", None)
        status, out = self.post("/api/restart", {"node": "lab", "project": "demo"})
        self.assertEqual(status, 200, out)
        argvs = [e["argv"] for e in self.log() if e["argv"][0] in ("stop", "start")]
        self.assertEqual(argvs, [["stop", "demo"], ["start", "demo", "--model", "opus"]])
        self.assertEqual([e["stdin"] for e in self.log() if e["argv"][0] == "start"], [m2.SECRET])
        with mock.patch.object(self.srv.chats, "busy", return_value=True):
            self.assertEqual(self.post("/api/restart", {"node": "lab", "project": "demo"})[0], 409)
        self.assertEqual(self.post("/api/restart", {"node": "ghost", "project": "demo"})[0], 404)
        self.assert_no_secret()

    def test_lead_update_and_usage_relay_to_the_node(self):
        status, out = self.post("/api/lead/update", {"node": "lab", "project": "demo"})
        self.assertEqual((status, out), (200, {"version": 2, "commit": "abc1234"}))
        status, raw = self.req("/api/usage?node=lab&project=demo")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["models"][0]["model"], "claude-fable-5-1")
        self.assertEqual(self.req("/api/usage?node=lab&project=../x")[0], 400)
        self.assertEqual([e["argv"] for e in self.log() if e["argv"][0] in ("relead", "usage")],
                         [["relead", "demo"], ["usage", "demo"]])

    def test_notify_routes_and_state(self):
        self.assertEqual(self.state()["notify"], {"configured": False, "enabled": True, "last_error": None})
        status, out = self.post("/api/notify/test", {})
        self.assertEqual(status, 409)
        status, out = self.post("/api/notify/setup", {"server": "http://ntfy.example"})
        self.assertEqual((status, out["configured"], out["server"]), (200, True, "http://ntfy.example"))
        topic = out["topic"]
        self.assertNotIn(topic, json.dumps(self.state()))   # only GET /api/notify carries it
        self.assertEqual(json.loads(self.req("/api/notify")[1])["topic"], topic)
        sent = Recorder()
        self.syncer.notifier.post = sent
        self.assertEqual(self.post("/api/notify/test", {}), (200, {"ok": True}))
        self.assertEqual(sent.sent[0][2]["Title"], "Cairn: test")
        sent.fail = True
        status, out = self.post("/api/notify/test", {})
        self.assertEqual(status, 502)
        self.assertEqual(self.state()["notify"]["last_error"], "ntfy unreachable: no route to host")
        self.assertEqual(self.post("/api/notify/setup", {"enabled": "yes"})[0], 400)
        self.assertEqual(self.post("/api/notify/setup", {"server": "ftp://x"})[0], 400)

    def test_frame_headers_and_notify_needs_the_token(self):
        for path in ("/", "/api/state"):
            r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), headers={
                "Cookie": "%s=%s" % (self.srv.cookie_name, self.srv.secret)}))
            self.assertEqual(r.headers["X-Frame-Options"], "DENY")
            self.assertTrue(r.headers["Content-Security-Policy"].endswith("frame-ancestors 'none'"), path)
            r.close()
        self.assertEqual(self.req("/api/notify", headers={"X-Cairn-Token": None})[0], 403)
        self.assertEqual(self.req("/api/notify")[0], 200)

    def test_attention_in_state(self):
        self.set_projects([project(lead="alive", lead_state="limit", lead_note="usage limit reached")])
        self.syncer.sync_now()
        a = self.state()["projects"]["lab/demo"]["attention"]
        self.assertEqual((a["kind"], a["note"]), ("limit", "usage limit reached"))
        wait = datetime.fromisoformat(a["resume_at"]) - datetime.now(timezone.utc)   # no reset time: one window
        self.assertTrue(att.DEFAULT_WINDOW < wait.total_seconds() <= att.DEFAULT_WINDOW + att.RESUME_DELAY, wait)


class RunTests(m7.CopyTests):
    def setUp(self):
        super().setUp()
        self.opened = []
        self.srv.browse = self.opened.append
        self.write(os.path.join(self.src, "work", "result.md"), "A game.\n")

    def run_it(self):
        return self.post("/api/run", {"node": "lab", "project": "demo"})

    def test_run_copies_serves_and_stops(self):
        status, out = self.run_it()
        self.assertEqual(status, 200, out)
        self.assertEqual(out["path"], os.path.join(self.tmp, "Downloads", "demo"))
        self.assertEqual(self.revealed, [])            # Run it does not open Finder
        self.assertEqual(self.opened, [out["url"]])
        with urllib.request.urlopen(out["url"]) as r:
            self.assertEqual(r.read().decode(), "<h1>game</h1>\n")
        self.assertEqual(self.syncer.state()["projects"]["lab/demo"]["running_url"], out["url"])
        again = self.run_it()[1]                        # reuses the copy and the server
        self.assertEqual((again["url"], again["path"]), (out["url"], out["path"]))
        self.assertEqual(os.listdir(os.path.join(self.tmp, "Downloads")), ["demo"])
        self.assertEqual(self.post("/api/run/stop", {"node": "lab", "project": "demo"}), (200, {"stopped": True}))
        self.assertIsNone(self.syncer.state()["projects"]["lab/demo"]["running_url"])
        self.assertEqual(self.post("/api/run/stop", {"node": "lab", "project": "demo"}), (200, {"stopped": False}))

    def test_offline_node_serves_the_existing_copy(self):
        self.assertEqual(self.run_it()[0], 200)
        self.post("/api/run/stop", {"node": "lab", "project": "demo"})
        with mock.patch.object(m2.ssh, "run_node", side_effect=m2.ssh.NodeError("ssh: connect timed out")):
            self.syncer.sync_one("lab")
            status, out = self.run_it()
        self.assertEqual(status, 200, out)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "Downloads")), ["demo"])

    def test_dashboard_commit_does_not_make_a_new_copy(self):
        self.snap(self.sha, result="A game.\n", result_sha=self.git("rev-parse", "--short", "HEAD", cwd=self.src))
        self.assertEqual(self.run_it()[0], 200)
        self.post("/api/run/stop", {"node": "lab", "project": "demo"})
        result_sha = self.git("rev-parse", "--short", "HEAD", cwd=self.src)
        self.write(os.path.join(self.src, "work", "ticket.md"), "t\n")
        self.git("add", "-A", cwd=self.src)
        self.git("commit", "-q", "-m", "ticket", cwd=self.src)
        self.snap(self.git("rev-parse", "HEAD", cwd=self.src), result="A game.\n", result_sha=result_sha)
        self.syncer.sync_one("lab")
        self.assertEqual(self.run_it()[0], 200)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "Downloads")), ["demo"])

    def test_not_a_web_result(self):
        os.remove(os.path.join(self.src, "index.html"))
        self.git("commit", "-q", "-am", "no page", cwd=self.src)
        self.snap(self.git("rev-parse", "HEAD", cwd=self.src))
        status, out = self.run_it()
        self.assertEqual(status, 409, out)
        self.assertIn("index.html", out["error"])
        self.assertEqual(self.opened, [])


class ServiceTests(m2.M2Base):
    def test_dry_run_prints_the_plist_and_changes_nothing(self):
        buf = io.StringIO()
        with mock.patch.object(cli.subprocess, "run", side_effect=AssertionError("no launchctl")), \
                contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["service", "install", "--dry-run"]), 0)
        out = buf.getvalue()
        self.assertIn("<string>com.coldlabs.cairn</string>", out)
        self.assertIn("<string>%s</string>" % sys.executable, out)
        self.assertIn(os.path.join(cli.REPO, "hub", "cairn"), out)
        self.assertIn("<key>KeepAlive</key>\n\t<dict>\n\t\t<key>SuccessfulExit</key>\n\t\t<false/>", out)
        self.assertFalse(os.path.exists(cli.plist_path()))


if __name__ == "__main__":
    unittest.main()
