"""Milestone 4: review chat (hub side) and one-click answers."""
import json
import os
import socket
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_m2 as m2
from test_hub import SECRET
from cairn_hub import chat as chatmod, nodeops

QUESTION = {"id": "q01", "ticket": "", "gate": False, "default": "B", "asked_at": "2026-10-17T12:00:00Z",
            "context": "Which way?", "options": ["A. left", "B. right (recommended: shorter)"],
            "answered": False, "choice": None, "answered_at": None, "applied": None, "path": "work/inbox/q01.md"}


def project_with_question(**over):
    p = m2.snap_project(lead="dead")
    p["questions"] = [dict(QUESTION, **over)]
    return p


class ChatBase(m2.ApiBase):
    def setUp(self):
        super().setUp()
        self.set_projects([project_with_question()])
        self.syncer.sync_now()

    def until(self, pred, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(0.02)
        self.fail("condition not met in %ss" % timeout)

    def chat(self):
        status, raw = self.req("/api/chat?node=lab&project=demo")
        self.assertEqual(status, 200, raw)
        return json.loads(raw)

    def kinds(self, events):
        return [e["kind"] if e["kind"] != "state" else "state:%s" % e["state"] for e in events]

    def wait_state(self, state):
        return self.until(lambda: (lambda c: c if c["state"] == state else None)(self.chat()))

    def starts(self):
        return [e for e in self.log() if e.get("argv", [None])[0] == "start"]

    def chat_inputs(self):
        return [e["chat_in"] for e in self.log() if "chat_in" in e]


class ChatFlowTests(ChatBase):
    def test_start_sends_the_opening_and_relays_a_turn(self):
        status, view = self.post("/api/chat/start", {"node": "lab", "project": "demo"})
        self.assertEqual(status, 200, view)
        c = self.wait_state("waiting")
        self.assertEqual(c["session_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(self.chat_inputs(), ["/cairn-review chat"])
        self.assertEqual(self.kinds(c["events"]),
                         ["state:starting", "state:working", "text", "tool", "turn", "state:waiting"])
        text, tool, turn = c["events"][2:5]
        self.assertEqual(text["text"], "reply to /cairn-review chat")
        self.assertEqual((tool["name"], tool["summary"]), ("Bash", "cat work/inbox/q01.md"))
        self.assertEqual((turn["ok"], turn["seconds"], turn["cost_usd"]), (True, 1.5, 0.01))
        self.assertEqual([e["seq"] for e in c["events"]], list(range(1, 7)))
        # the token went in on stdin, never in argv, and is nowhere in what the hub serves or logs
        chat_call = [e for e in self.log() if e.get("argv", [None])[0] == "chat"][0]
        self.assertEqual((chat_call["argv"], chat_call["stdin"]), (["chat", "demo"], SECRET))
        self.assertNotIn(SECRET, json.dumps(c))
        self.assert_no_secret()
        # a live chat is returned, not started twice
        self.post("/api/chat/start", {"node": "lab", "project": "demo"})
        self.assertEqual(len([e for e in self.log() if e.get("argv", [None])[0] == "chat"]), 1)
        self.assertEqual(self.state()["chats"], [{"node": "lab", "project": "demo", "state": "waiting", "detail": None}])
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})

    def test_a_node_login_token_sends_an_empty_line_and_reads_no_keychain(self):
        nodeops.set_project_token("lab", "demo", "nl")
        self.kc.store.clear()   # a Keychain read would now fail the start
        status, view = self.post("/api/chat/start", {"node": "lab", "project": "demo"})
        self.assertEqual(status, 200, view)
        self.wait_state("waiting")
        chat_call = [e for e in self.log() if e.get("argv", [None])[0] == "chat"][0]
        self.assertEqual((chat_call["argv"], chat_call["stdin"]), (["chat", "demo"], ""))
        self.assertEqual(self.chat_inputs(), ["/cairn-review chat"])
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})

    def test_answering_the_last_question_ends_the_chat(self):
        self.post("/api/chat/start", {"node": "lab", "project": "demo"})
        self.wait_state("waiting")
        self.assertTrue(self.srv.chats.busy("lab", "demo"))
        status, res = self.post("/api/chat/send", {"node": "lab", "project": "demo", "text": "b"})
        self.assertEqual(status, 200, res)
        c = self.wait_state("ended")
        self.assertEqual(c["detail"], "finished")
        kinds = self.kinds(c["events"])
        self.assertEqual(kinds[6:], ["state:working", "user", "text", "tool", "turn", "state:waiting",
                                     "answered", "state:ended"])
        self.assertEqual(c["events"][12], dict(c["events"][12], id="q01", choice="B"))
        self.assertFalse(self.srv.chats.busy("lab", "demo"))

    def test_send_needs_a_waiting_chat_and_sane_text(self):
        status, _ = self.post("/api/chat/send", {"node": "lab", "project": "demo", "text": "hi"})
        self.assertEqual(status, 404)
        self.post("/api/chat/start", {"node": "lab", "project": "demo"})
        self.wait_state("waiting")
        self.assertEqual(self.post("/api/chat/send", {"node": "lab", "project": "demo", "text": " "})[0], 400)
        self.assertEqual(self.post("/api/chat/send", {"node": "lab", "project": "demo", "text": "x" * 8001})[0], 400)
        self.srv.chats.get("lab", "demo").state = "working"
        self.assertEqual(self.post("/api/chat/send", {"node": "lab", "project": "demo", "text": "hi"})[0], 409)
        self.srv.chats.get("lab", "demo").state = "waiting"
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})

    def test_end_then_resume_appends(self):
        self.post("/api/chat/start", {"node": "lab", "project": "demo"})
        self.wait_state("waiting")
        status, view = self.post("/api/chat/end", {"node": "lab", "project": "demo"})
        self.assertEqual((view["state"], view["detail"]), ("ended", "closed"))
        before = len(self.chat()["events"])
        self.post("/api/chat/start", {"node": "lab", "project": "demo", "resume": True})
        c = self.wait_state("waiting")
        calls = [e["argv"] for e in self.log() if e.get("argv", [None])[0] == "chat"]
        self.assertEqual(calls[-1], ["chat", "demo", "--resume"])
        self.assertEqual(self.chat_inputs(), ["/cairn-review chat"])  # no second opening on a resume
        self.assertEqual(c["events"][before]["seq"], before + 1)
        # after=N returns only newer events
        status, raw = self.req("/api/chat?node=lab&project=demo&after=%d" % before)
        self.assertEqual(json.loads(raw)["events"][0]["seq"], before + 1)
        # a fresh start forgets the old events
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})
        self.post("/api/chat/start", {"node": "lab", "project": "demo"})
        self.assertEqual(self.wait_state("waiting")["events"][0]["seq"], 1)
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})

    def test_autostart_waits_for_the_chat(self):
        p = project_with_question(answered=True, choice="B")
        p["questions"].append(dict(QUESTION, id="q02", path="work/inbox/q02.md"))  # still open: the chat stays
        self.set_projects([p])
        self.post("/api/chat/start", {"node": "lab", "project": "demo"})
        self.wait_state("waiting")
        self.syncer.sync_now()
        self.assertEqual(self.starts(), [])
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})
        self.syncer.sync_now()
        self.until(self.starts)   # the sync that ending the chat asks for may be the one that starts it
        self.assertEqual(len(self.starts()), 1)


class ChatFailureTests(ChatBase):
    def test_login_failure_is_an_error(self):
        with mock.patch.dict(os.environ, {"FAKE_CHAT_MODE": "nologin"}):
            self.post("/api/chat/start", {"node": "lab", "project": "demo"})
            c = self.wait_state("error")
        self.assertIn("login failed", c["detail"])
        proc = self.srv.chats.get("lab", "demo").proc
        self.until(lambda: proc.poll() is not None)   # an error closes claude
        # final: End and late output change nothing
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})
        self.assertEqual(self.chat()["state"], "error")

    def test_quoted_login_words_in_a_question_are_not_an_error(self):
        with mock.patch.dict(os.environ, {"FAKE_CHAT_MODE": "quote-key"}):
            self.post("/api/chat/start", {"node": "lab", "project": "demo"})
            c = self.wait_state("waiting")
        self.assertIn("Invalid API key", c["events"][2]["text"])
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})

    def test_tool_summary_is_redacted_before_it_is_cut(self):
        with mock.patch.dict(os.environ, {"FAKE_CHAT_MODE": "tool-secret"}):
            self.post("/api/chat/start", {"node": "lab", "project": "demo"})
            c = self.wait_state("waiting")
        tool = [e for e in c["events"] if e["kind"] == "tool"][0]
        self.assertNotIn(SECRET[:12], tool["summary"])
        self.assertIn("***", tool["summary"])
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})

    def test_ssh_drop_mid_turn_reports_its_stderr_line(self):
        with mock.patch.dict(os.environ, {"FAKE_CHAT_MODE": "die-midturn"}):
            self.post("/api/chat/start", {"node": "lab", "project": "demo"})
            c = self.wait_state("error")
        self.assertEqual(c["detail"], "client_loop: send disconnect: Broken pipe")

    def test_node_error_before_claude(self):
        with mock.patch.dict(os.environ, {"FAKE_CHAT_MODE": "die"}):
            self.post("/api/chat/start", {"node": "lab", "project": "demo"})
            c = self.wait_state("error")
        self.assertEqual(c["detail"], "unknown project: demo")

    def test_token_in_assistant_text_is_redacted(self):
        with mock.patch.dict(os.environ, {"FAKE_CHAT_MODE": "echo-secret"}):
            self.post("/api/chat/start", {"node": "lab", "project": "demo"})
            c = self.wait_state("waiting")
        self.assertEqual(c["events"][2]["text"], "reply to /cairn-review chat ***")
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})

    def test_idle_chat_ends(self):
        mgr = chatmod.ChatManager(self.syncer, idle_limit=0.2, reap_every=0.05)
        self.addCleanup(mgr.stop)
        mgr.start("lab", "demo")
        self.until(lambda: mgr.get("lab", "demo").state == "waiting")
        self.until(lambda: mgr.get("lab", "demo").state == "ended")
        self.assertEqual(mgr.get("lab", "demo").detail, "idle")

    def test_routes_need_token_and_origin(self):
        for path in ("/api/chat/start", "/api/chat/send", "/api/chat/end", "/api/answer"):
            status, _ = self.req(path, "POST", {"node": "lab", "project": "demo"}, headers={"X-Cairn-Token": None})
            self.assertEqual(status, 403, path)
            status, _ = self.req(path, "POST", {"node": "lab", "project": "demo"}, headers={"Origin": "http://evil"})
            self.assertEqual(status, 403, path)
        self.assertEqual([e for e in self.log() if e.get("argv", [None])[0] == "chat"], [])


class ChatLifecycleTests(ChatBase):
    def test_end_while_starting_ends_closed(self):
        with mock.patch.dict(os.environ, {"FAKE_CHAT_MODE": "slow-head"}):
            self.post("/api/chat/start", {"node": "lab", "project": "demo"})
            self.post("/api/chat/end", {"node": "lab", "project": "demo"})
        proc = self.srv.chats.get("lab", "demo").proc
        self.until(lambda: proc.poll() is not None)
        c = self.chat()
        self.assertEqual((c["state"], c["detail"]), ("ended", "closed"))
        self.assertEqual(self.kinds(c["events"]), ["state:starting", "state:ended"])
        self.assertEqual(self.chat_inputs(), [])

    def test_end_mid_turn_then_continue_keeps_old_output_out(self):
        with mock.patch.dict(os.environ, {"FAKE_CHAT_MODE": "slow"}):   # every turn takes 1.5 s
            self.post("/api/chat/start", {"node": "lab", "project": "demo"})
        self.until(lambda: self.chat()["state"] == "waiting", timeout=8)
        old = self.srv.chats.get("lab", "demo")
        self.post("/api/chat/send", {"node": "lab", "project": "demo", "text": "why?"})
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})
        status, res = self.post("/api/chat/start", {"node": "lab", "project": "demo", "resume": True})
        if status == 409:   # still closing
            self.assertIn("still closing", res["error"])
            self.until(lambda: old.proc.poll() is not None)
            status, res = self.post("/api/chat/start", {"node": "lab", "project": "demo", "resume": True})
        self.assertEqual(status, 200, res)
        c = self.wait_state("waiting")
        ended_at = [e["seq"] for e in c["events"] if e.get("state") == "ended"][0]
        after_end = [e for e in c["events"] if e["seq"] > ended_at]
        self.assertNotIn("reply to why?", [e.get("text") for e in after_end])
        self.assertEqual([e["seq"] for e in c["events"]], list(range(1, len(c["events"]) + 1)))
        self.assertIsNot(old.events, self.srv.chats.get("lab", "demo").events)
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})

    def test_finished_line_ends_the_chat_and_is_not_shown(self):
        p = project_with_question()
        p["questions"].append(dict(QUESTION, id="q02", path="work/inbox/q02.md"))
        self.set_projects([p])
        self.syncer.sync_now()
        with mock.patch.dict(os.environ, {"FAKE_CHAT_MODE": "finish"}):
            self.post("/api/chat/start", {"node": "lab", "project": "demo"})
            c = self.wait_state("ended")
        self.assertEqual(c["detail"], "finished")
        self.assertEqual(c["events"][2]["text"], "reply to /cairn-review chat")

    def test_idle_clock_starts_when_it_becomes_the_operators_turn(self):
        mgr = chatmod.ChatManager(self.syncer, idle_limit=0.5, reap_every=0.05)
        self.addCleanup(mgr.stop)
        with mock.patch.dict(os.environ, {"FAKE_CHAT_MODE": "slow"}):
            mgr.start("lab", "demo")   # the opening turn takes 1.5 s, longer than the idle limit
        self.until(lambda: mgr.get("lab", "demo").state == "waiting")
        time.sleep(0.2)
        self.assertEqual(mgr.get("lab", "demo").state, "waiting")
        self.until(lambda: mgr.get("lab", "demo").state == "ended")

    def test_silent_node_is_an_error(self):
        mgr = chatmod.ChatManager(self.syncer, silent_limit=0.3, reap_every=0.05)
        self.addCleanup(mgr.stop)
        with mock.patch.dict(os.environ, {"FAKE_CHAT_MODE": "slow-head"}):
            mgr.start("lab", "demo")
        self.until(lambda: mgr.get("lab", "demo").state == "error")
        self.assertIn("no output", mgr.get("lab", "demo").detail)

    def test_no_snapshot_means_no_old_answers_reported(self):
        self.set_projects([project_with_question(answered=True, choice="A"),
                           ])
        with mock.patch.object(self.syncer, "current_snapshot", return_value=None):
            mgr = chatmod.ChatManager(None)
            self.addCleanup(mgr.stop)
            mgr.start("lab", "demo")
            self.until(lambda: mgr.get("lab", "demo").state == "waiting")
        self.assertNotIn("answered", [e["kind"] for e in mgr.get("lab", "demo").events])
        mgr.end("lab", "demo")


class EventStreamTests(ChatBase):
    def test_stream_sends_backlog_then_live_events(self):
        self.assertEqual(self.req("/api/chat/events?node=lab&project=demo")[0], 404)
        self.post("/api/chat/start", {"node": "lab", "project": "demo"})
        self.wait_state("waiting")
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(s.close)
        s.sendall(("GET /api/chat/events?node=lab&project=demo&after=4 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nCookie: %s=%s\r\n\r\n"
                   % (self.port, self.srv.cookie_name, self.srv.secret)).encode())
        buf = b""
        while b"id: 6" not in buf:
            buf += s.recv(4096)
        head, _, body = buf.partition(b"\r\n\r\n")
        self.assertIn(b"text/event-stream", head)
        frames = [f for f in body.decode().split("\n\n") if f.startswith("id:")]
        self.assertEqual(frames[0].splitlines()[0], "id: 5")
        self.assertEqual(json.loads(frames[0].splitlines()[1][6:])["kind"], "turn")
        self.post("/api/chat/send", {"node": "lab", "project": "demo", "text": "why?"})
        while b'"why?"' not in buf:
            buf += s.recv(4096)
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})

    def test_last_event_id_wins_over_after(self):
        self.post("/api/chat/start", {"node": "lab", "project": "demo"})
        self.wait_state("waiting")
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(s.close)
        s.sendall(("GET /api/chat/events?node=lab&project=demo&after=0 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                   "Cookie: %s=%s\r\nLast-Event-ID: 5\r\n\r\n" % (self.port, self.srv.cookie_name, self.srv.secret)).encode())
        buf = b""
        while b"id: 6" not in buf:
            buf += s.recv(4096)
        self.assertNotIn(b"id: 5\n", buf)
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})


class AnswerRouteTests(ChatBase):
    def test_answer_goes_to_the_node_and_syncs(self):
        status, res = self.post("/api/answer", {"node": "lab", "project": "demo", "id": "q01", "choice": "A",
                                                "note": "left is fine"})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["path"], "work/inbox/q01.answer.md")
        call = [e for e in self.log() if e.get("argv", [None])[0] == "answer"][0]
        self.assertEqual(call["argv"], ["answer", "demo"])
        self.assertEqual(json.loads(call["stdin"]), {"id": "q01", "choice": "A", "note": "left is fine"})

    def test_refused_while_a_chat_is_live_and_errors_surface(self):
        self.post("/api/chat/start", {"node": "lab", "project": "demo"})
        self.wait_state("waiting")
        status, res = self.post("/api/answer", {"node": "lab", "project": "demo", "id": "q01", "choice": "A"})
        self.assertEqual(status, 409)
        self.assertIn("answer there", res["error"])
        self.post("/api/chat/end", {"node": "lab", "project": "demo"})
        with mock.patch.dict(os.environ, {"FAKE_NODE_ANSWER_FAIL": "1"}):
            status, res = self.post("/api/answer", {"node": "lab", "project": "demo", "id": "q01", "choice": "A"})
        self.assertEqual((status, res["error"]), (409, "q01 is already answered"))
        self.assertEqual(self.post("/api/answer", {"node": "lab", "project": "demo", "choice": "A"})[0], 400)


class AnswerFingerprintTests(ChatBase):
    SHOWN = "a" * 64

    def setUp(self):
        super().setUp()
        self.set_projects([project_with_question(fingerprint=self.SHOWN)])
        self.syncer.sync_now()

    def answer(self, **extra):
        return self.post("/api/answer", dict({"node": "lab", "project": "demo", "id": "q01", "choice": "A"}, **extra))

    def answer_calls(self):
        return [json.loads(e["stdin"]) for e in self.log() if e.get("argv", [None])[0] == "answer"]

    def test_the_snapshot_question_carries_its_fingerprint(self):
        q = self.state()["nodes"][0]["snapshot"]["projects"][0]["questions"][0]
        self.assertEqual(q["fingerprint"], self.SHOWN)

    def test_a_fingerprint_that_is_not_text_reads_as_none(self):
        self.set_projects([project_with_question(fingerprint=["x"])])
        self.syncer.sync_now()
        self.assertIsNone(self.state()["nodes"][0]["snapshot"]["projects"][0]["questions"][0]["fingerprint"])

    def test_the_fingerprint_shown_goes_to_the_node(self):
        status, res = self.answer(fingerprint=self.SHOWN)
        self.assertEqual(status, 200, res)
        self.assertEqual(self.answer_calls(), [{"id": "q01", "choice": "A", "note": "", "fingerprint": self.SHOWN}])

    def test_without_a_fingerprint_none_is_sent(self):
        self.assertEqual(self.answer()[0], 200)
        self.assertNotIn("fingerprint", self.answer_calls()[0])

    def test_a_malformed_fingerprint_is_refused_before_the_node(self):
        for bad in ("A" * 64, "a" * 63, 7, ""):
            self.assertEqual(self.answer(fingerprint=bad)[0], 400, bad)
        self.assertEqual(self.answer_calls(), [])

    def test_a_rewritten_question_is_a_409_with_the_fresh_question_and_the_state_has_it(self):
        fresh = dict(QUESTION, options=["A. up", "B. down"], fingerprint="b" * 64, extra=1)
        self.set_projects([dict(project_with_question(), questions=[dict(fresh, gate="yes")])])
        status, res = self.answer(fingerprint=self.SHOWN)
        self.assertEqual(status, 409, res)
        self.assertEqual(res["error"], "q01 changed since it was shown; read it again")
        self.assertEqual((res["question"]["options"], res["question"]["fingerprint"]), (["A. up", "B. down"], "b" * 64))
        self.assertIs(res["question"]["gate"], False)   # the node's question is coerced like a snapshot's
        # synced on the refusal, so the next poll shows the question the node refused against
        q = self.state()["nodes"][0]["snapshot"]["projects"][0]["questions"][0]
        self.assertEqual((q["options"], q["fingerprint"]), (["A. up", "B. down"], "b" * 64))


if __name__ == "__main__":
    unittest.main()
