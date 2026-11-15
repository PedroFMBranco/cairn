"""Answering a lead's Claude Code dialog from the dashboard."""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_m2 as m2
from cairn_hub import nodeops

FP = "f" * 64


class DialogRouteTests(m2.ApiBase):
    def calls(self):
        return [e["argv"] for e in self.log() if e["argv"][0] in ("dialog", "dialog-answer")]

    def test_read_relays_the_node(self):
        status, raw = self.req("/api/dialog?node=lab&project=demo")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["dialog"]["options"], [{"n": 1, "label": "Yes"}, {"n": 2, "label": "No"}])
        self.assertEqual(self.req("/api/dialog?node=lab&project=../x")[0], 400)
        self.assertEqual(self.req("/api/dialog?node=ghost&project=demo")[0], 404)
        self.assertEqual(self.calls(), [["dialog", "demo"]])

    def test_answer_relays_the_fingerprint_and_option_then_syncs(self):
        with mock.patch.object(self.syncer, "sync_one", wraps=self.syncer.sync_one) as sync:
            status, out = self.post("/api/dialog", {"node": "lab", "project": "demo", "fingerprint": FP, "n": 2})
        self.assertEqual((status, out), (200, {"answered": 2, "label": "No", "dialog": None}))
        self.assertEqual(self.calls(), [["dialog-answer", "demo", FP, "2"]])
        sync.assert_called_once_with("lab")

    def test_bad_input_never_reaches_the_node(self):
        for body in ({"fingerprint": "abc", "n": 1}, {"fingerprint": FP, "n": "1"}, {"fingerprint": FP, "n": 0},
                     {"fingerprint": FP, "n": True}, {"fingerprint": FP}):
            self.assertEqual(self.post("/api/dialog", dict(body, node="lab", project="demo"))[0], 400, body)
        self.assertEqual(self.calls(), [])

    def test_a_changed_dialog_is_a_409_with_the_fresh_one(self):
        with mock.patch.dict(os.environ, {"FAKE_NODE_DIALOG_STALE": "1"}):
            status, out = self.post("/api/dialog", {"node": "lab", "project": "demo", "fingerprint": FP, "n": 2})
        self.assertEqual((status, out["error"]), (409, "the dialog changed; read it again"))
        self.assertEqual(out["dialog"]["fingerprint"], FP)
        with mock.patch.dict(os.environ, {"FAKE_NODE_DIALOG_GONE": "1"}):
            status, out = self.post("/api/dialog", {"node": "lab", "project": "demo", "fingerprint": FP, "n": 2})
        self.assertEqual((status, out), (502, {"error": "no session demo"}))

    def test_refused_while_a_review_chat_is_open(self):
        with mock.patch.object(self.srv.chats, "busy", return_value=True):
            status, out = self.post("/api/dialog", {"node": "lab", "project": "demo", "fingerprint": FP, "n": 2})
        self.assertEqual(status, 409)
        self.assertIn("review chat is open", out["error"])
        self.assertEqual(self.calls(), [])

    def test_post_needs_the_token(self):
        body = {"node": "lab", "project": "demo", "fingerprint": FP, "n": 2}
        self.assertEqual(self.req("/api/dialog", "POST", body, {"X-Cairn-Token": None})[0], 403)
        self.assertEqual(self.req("/api/dialog", "POST", body, {"X-Cairn-Token": "wrong"})[0], 403)
        self.assertEqual(self.calls(), [])

    def test_old_node_is_told_to_update(self):
        self.addCleanup(nodeops.VERSIONS.pop, "lab", None)
        with mock.patch.dict(os.environ, {"FAKE_NODE_VERSION": "0.3.0"}):
            self.syncer.sync_now()
        status, raw = self.req("/api/dialog?node=lab&project=demo")
        self.assertEqual(status, 409)
        self.assertIn("update cairn-node", json.loads(raw)["error"])
        self.assertEqual(self.post("/api/dialog", {"node": "lab", "project": "demo", "fingerprint": FP, "n": 1})[0], 409)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
