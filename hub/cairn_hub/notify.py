"""Push to the operator's phone through ntfy: new questions, results ready and leads that need attention.
Each item is pushed once; it is recorded only after ntfy accepts it, so a failure retries next sync. The one
exception is an item ntfy refuses outright (a 4xx other than 401, 403 and 429), which is recorded and skipped."""
import http.client
import json
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import config, keychain, nodeops
from .attention import clock

TOPIC_ACCOUNT = "cairn:ntfy-topic"   # token labels cannot hold ":", so this never collides with a token
DEFAULT_SERVER = None   # any ntfy server: self-hosted or https://ntfy.sh
MAX_BODY = 300
TIMEOUT = 10
RETRY_STATUS = (401, 403, 429)   # an access rule or rate limit that may lift: not this item's fault
ATTENTION_WORDS = {"limit": "Hit a usage limit", "dialog": "Waiting at a dialog",
                   "prompt": "Stopped at its prompt", "crashed": "Stopped while working"}
LOCK = nodeops.FileLock(".notify.lock")   # settings.json and notified.json, shared with the CLI
SENDING = set()   # notified.json keys a check is pushing now (under LOCK), so an overlapping sync skips them
MAX_PER_SYNC = 10   # pushes per node per sync


def settings():
    s = config.read_json("settings.json", {}).get("notify") or {}
    return {"server": s.get("server") or DEFAULT_SERVER, "enabled": s.get("enabled", True),
            "configured": bool(s.get("configured"))}


def save_settings(server=None, enabled=None, configured=None):
    """Turning pushes back on forgets which nodes were seen, so what piled up meanwhile is recorded, not pushed."""
    with LOCK:
        d = config.read_json("settings.json", {})
        cur = d.setdefault("notify", {})
        if server is not None:
            cur["server"] = server
        if configured is not None:
            cur["configured"] = configured
        if enabled is not None:
            if enabled and cur.get("enabled") is False:
                sent = config.read_json("notified.json", {})
                sent["_nodes"] = {}
                config.write_json("notified.json", sent)
            cur["enabled"] = bool(enabled)
        config.write_json("settings.json", d)


_topic = {}
READ_RETRY = 60   # seconds before an unreadable Keychain is asked again


def topic():
    """The topic from the Keychain, read only once setup has made one (so a hub without notifications never asks).
    None when it is missing; a Keychain that cannot be read (locked, access denied) raises KeychainError."""
    if not settings()["configured"]:
        return None
    if "value" in _topic:
        return _topic["value"]
    failed = _topic.get("failed")
    if failed and time.time() - failed[0] < READ_RETRY:
        raise keychain.KeychainError(failed[1])
    try:
        _topic["value"] = keychain.get(TOPIC_ACCOUNT) or None
    except keychain.KeychainNotFound:
        _topic["value"] = None
    except keychain.KeychainError as e:
        _topic["failed"] = (time.time(), str(e))
        raise
    _topic.pop("failed", None)
    return _topic["value"]


def check_server(server):
    """An http(s) URL with a host and nothing that could garble the request line."""
    parts = urlsplit(server) if isinstance(server, str) else None
    if (not parts or parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment
            or any(c.isspace() or ord(c) < 32 for c in server)):
        raise ValueError("server must be an http(s) URL like https://ntfy.sh, with no spaces, query or fragment")
    return server.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a redirect (captive portal, forced HTTPS) becomes an error, never a silent GET


class Undeliverable(Exception):
    """This one item cannot be sent (ntfy refused it); later items may still go."""


OPENER = urllib.request.build_opener(NoRedirect)


def first_line(text):
    for line in (text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line
    return ""


def gone(key, node_name, questions):
    """A q:/r: key of this node whose project is no longer in the snapshot, or for a question, whose id is not.
    `questions` maps each project in the snapshot to its question ids."""
    kind, _, rest = key.partition(":")
    if kind not in ("q", "r") or not rest.startswith(node_name + "/"):
        return False
    project, _, qid = rest[len(node_name) + 1:].partition("/")
    return project not in questions or (kind == "q" and qid not in questions[project])


class Notifier:
    def __init__(self, post=None):
        self.post = post or self._post
        self.last_error = None
        self.last_sent_at = None

    # -- setup
    def setup(self, server=None, enabled=None):
        if server is not None:
            server = check_server(server)
        elif not settings()["server"]:
            raise ValueError("give the ntfy server URL, for example https://ntfy.sh or your own")
        if topic() is None:   # only a missing topic is created; an unreadable Keychain raises instead of replacing it
            value = "cairn-" + secrets.token_urlsafe(18)
            keychain.add(TOPIC_ACCOUNT, value)
            _topic["value"] = value
        save_settings(server, enabled, configured=True)
        return self.view()

    def view(self, with_topic=True):
        s = settings()
        try:
            t = topic()
            error = self.last_error
        except keychain.KeychainError as e:
            t, error = None, "cannot read the topic from the Keychain: %s" % e
        out = {"configured": s["configured"] and (t is not None or error is not None), "enabled": bool(s["enabled"]),
               "last_error": error}
        if with_topic:
            out.update(server=s["server"], topic=t, last_sent_at=self.last_sent_at)
        return out

    def test(self):
        t = topic()   # KeychainError propagates
        if t is None:
            raise ValueError("notifications are not set up")
        self._send(t, "Cairn: test", "Notifications from Cairn reach this device.", "default", "white_check_mark")

    # -- sending
    def _post(self, url, body, headers):
        """(status, response body); redirects are refused."""
        req = urllib.request.Request(url, data=body.encode(), method="POST", headers=headers)
        try:
            with OPENER.open(req, timeout=TIMEOUT) as r:
                return r.status, r.read(4096)
        except urllib.error.HTTPError as e:
            return e.code, b""

    def _send(self, t, title, body, priority, tags):
        """Errors never carry the URL, which holds the topic. RuntimeError: ntfy unreachable (retry later);
        Undeliverable: ntfy refused this item."""
        url = "%s/%s" % (settings()["server"].rstrip("/"), t)
        headers = {"Title": title.encode("ascii", "replace").decode(), "Priority": priority, "Tags": tags}
        try:
            status, raw = self.post(url, body[:MAX_BODY], headers)
        except (http.client.HTTPException, ValueError) as e:   # their messages can quote the URL: name only
            self.last_error = "ntfy unreachable: %s" % type(e).__name__
            raise RuntimeError(self.last_error)
        except (urllib.error.URLError, OSError) as e:   # socket errors name the host at most, never the path
            self.last_error = "ntfy unreachable: %s" % (getattr(e, "reason", None) or e)
            raise RuntimeError(self.last_error)
        if 300 <= status < 400:
            self.last_error = "ntfy redirected (%d); check the server URL" % status
            raise RuntimeError(self.last_error)
        if status in RETRY_STATUS:
            self.last_error = "ntfy refused a push (%d); tried again next sync" % status
            raise RuntimeError(self.last_error)
        if 400 <= status < 500:
            self.last_error = "ntfy refused a push (%d)" % status
            raise Undeliverable(self.last_error)
        try:
            ok = status == 200 and json.loads(raw).get("event") == "message"
        except (ValueError, AttributeError):
            ok = False
        if not ok:
            self.last_error = "the server answered %d but is not ntfy; check the server URL" % status
            raise RuntimeError(self.last_error)
        self.last_error = None
        self.last_sent_at = config.now_iso()

    def check(self, node_name, snapshot, entries, attention, episodes=None):
        """Push what is new for one node. `entries` is projects.json with result_ready per key; `attention` maps
        "<node>/<project>" to the attention records that count now; `episodes` holds "<node>/<project>/<kind>" for
        every open episode, counted or not (a prompt under 10 minutes), which keeps its push from repeating."""
        try:
            t = topic()
        except keychain.KeychainError as e:
            self.last_error = "cannot read the topic from the Keychain: %s" % e
            return
        if t is None or not settings()["enabled"]:
            return
        items = []
        for p in (snapshot or {}).get("projects") or []:
            key = "%s/%s" % (node_name, p.get("name"))
            for q in p.get("questions") or []:
                if not q.get("answered"):
                    items.append(("q:%s/%s" % (key, q.get("id")), "%s: question %s" % (p["name"], q.get("id")),
                                  first_line(q.get("context")) or "A new question is waiting.",
                                  "high" if q.get("gate") else "default", "question"))
            entry = entries.get(key) or {}
            if entry.get("result_ready") and p.get("result_sha"):
                items.append(("r:%s/%s" % (key, p["result_sha"]), "%s: result ready" % p["name"],
                              first_line(p.get("result")) or "A finished result is ready to copy.", "default", "package"))
            a = attention.get(key)
            if a:
                words = ATTENTION_WORDS.get(a["kind"], a["kind"])
                body = "%s: %s" % (words, a["note"]) if a.get("note") else words
                if a.get("switched_to"):
                    body = "%s switched to %s: %s hit its limit (%s)" % (
                        p["name"], a["switched_to"], a["token"],
                        "resets " + clock(a["until"]) if a.get("known") else "no reset time shown")
                # a limit pushes once per token that hits it, so each switch of a cascade and its final wait push
                push = "a:%s/%s" % (key, a["kind"]) + ("/" + a["token"] if a.get("token") else "")
                items.append((push, "%s: needs attention" % p["name"], body, "high", "warning"))
                if a.get("resume_error") and not a.get("resume_at"):
                    items.append((push + "/failed", "%s: needs attention" % p["name"],
                                  "Automatic restart failed after %d attempts: %s" % (a["resume_attempts"], a["resume_error"]),
                                  "high", "warning"))
        live = {i[0] for i in items if i[0].startswith("a:")} | {"a:" + e for e in (episodes or ())}
        prefixes = tuple("a:%s/%s/" % (node_name, p.get("name")) for p in (snapshot or {}).get("projects") or [])
        questions = {p.get("name"): {q.get("id") for q in p.get("questions") or []}
                     for p in (snapshot or {}).get("projects") or []}
        with LOCK:
            sent = config.read_json("notified.json", {})
            # an episode is pushed once while it lasts (hub restarts included); when it ends, the next one pushes again
            ended = [k for k in sent if (k.startswith(prefixes) and k not in live) or gone(k, node_name, questions)]
            for k in ended:
                del sent[k]
            nodes_seen = sent.setdefault("_nodes", {})
            baseline = node_name not in nodes_seen
            todo = [i for i in items if i[0] not in sent and i[0] not in SENDING]
            if baseline:   # what the node already had when the hub first saw it is recorded, not pushed
                for item in todo:
                    sent[item[0]] = config.now_iso()
                nodes_seen[node_name] = config.now_iso()
                todo = []
            todo = todo[:MAX_PER_SYNC]   # the rest go at the next syncs
            SENDING.update(i[0] for i in todo)
            if baseline or ended:
                config.write_json("notified.json", sent)
        done = []
        try:
            for key, title, body, priority, tags in todo:   # outside LOCK: each push may take TIMEOUT
                try:
                    self._send(t, title, body.strip(), priority, tags)
                except RuntimeError:
                    break   # ntfy unreachable: the rest retries on the next sync
                except Undeliverable:
                    print("notify: %s refused by ntfy; skipped" % key.split("/")[0], flush=True)
                done.append(key)
        finally:
            with LOCK:
                SENDING.difference_update(i[0] for i in todo)
                if done:
                    sent = config.read_json("notified.json", {})
                    for key in done:
                        sent[key] = config.now_iso()
                    config.write_json("notified.json", sent)
