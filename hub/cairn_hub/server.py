"""127.0.0.1-only HTTP server: static UI plus the JSON API."""
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import signal
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import attention, config, keychain, mirror, nodeops, notify, ssh
from .chat import ChatManager
from .sandbox import Sandboxes
from .sync import Syncer

UI_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "ui"))
PLACEHOLDER = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>Cairn</title></head>"
               "<body><h1>Cairn</h1><p>The UI is not installed yet. The API is at "
               "<a href=\"/api/state\">/api/state</a>.</p></body></html>")
SIGN_IN_PAGE = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>Cairn</title></head><body><h1>Cairn</h1>"
                "<p>This browser is not signed in to the hub. Run <code>cairn open</code> in a terminal on this Mac: it "
                "opens the dashboard with a link that signs the browser in.</p></body></html>").encode()
MAX_BODY = 64 * 1024
COOKIE_AGE = 365 * 24 * 3600
INLINE_SCRIPT_RE = re.compile(rb"<script(?![^>]*\ssrc=)[^>]*>(.*?)</script>", re.S | re.I)
PAGE_CSP = ("default-src 'self'; script-src 'self'%s; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")


def page_csp(html):
    """The policy for an HTML page: its own inline scripts (the import map) by hash, nothing else inline."""
    hashes = "".join(" 'sha256-%s'" % base64.b64encode(hashlib.sha256(m).digest()).decode()
                     for m in INLINE_SCRIPT_RE.findall(html))
    return PAGE_CSP % hashes


def need_str(body, key):
    v = body.get(key)
    if not isinstance(v, str) or not v:
        raise nodeops.OpError("%s required" % key)
    return v


def post_nodes(srv, body):
    name, target = need_str(body, "name"), need_str(body, "ssh")
    if target == "local" and os.environ.get("CAIRN_ALLOW_LOCAL_NODE") != "1":
        # a lead on the Mac itself runs beside the hub and reads ~/.cairn; only the operator's own terminal adds one
        raise nodeops.OpError("a local node runs leads on this Mac itself; add it in a terminal with "
                              "`cairn node add <name> --ssh local`", 403)
    check = nodeops.add_node(name, target, body.get("port"), body.get("allowed_tokens"))
    srv.syncer.refresh_nodes()
    return {"node": name, "check": check}


def post_nodes_tokens(srv, body):
    name = need_str(body, "name")
    nodeops.set_node_tokens(name, body.get("allowed_tokens"))
    return {"node": name, "allowed_tokens": nodeops.get_node(name).get("allowed_tokens") or []}


def post_nodes_remove(srv, body):
    name = need_str(body, "name")
    nodeops.remove_node(name, srv.syncer.current_snapshot(name), force=body.get("force") is True)
    srv.syncer.forget(name)
    return {"removed": name}


def post_tokens(srv, body):
    return nodeops.add_token(need_str(body, "label"), body.get("kind"), body.get("account") or "",
                             secret=body.get("secret"), nodes=body.get("nodes") or [],
                             overwrite=body.get("replace") is True)


def post_tokens_update(srv, body):
    return nodeops.update_token(need_str(body, "label"), body.get("new_label"), body.get("account"),
                                body.get("nodes"))


def post_tokens_remove(srv, body):
    label = need_str(body, "label")
    nodeops.remove_token(label, srv.syncer.fresh_snapshots(), force=body.get("force") is True)
    return {"removed": label}


def post_tokens_clear_limit(srv, body):
    label, node = need_str(body, "label"), body.get("node")
    if node is not None and not isinstance(node, str):
        raise nodeops.OpError("node must be a node name")
    return {"label": label, "node": node, "cleared": attention.clear_limit(label, node)}


def post_projects(srv, body):
    node = need_str(body, "node")
    res = nodeops.create_project(node, need_str(body, "project"), body.get("repo"), body.get("destination"),
                                 path=body.get("path"), notes=body.get("notes") or "", tickets=body.get("tickets"),
                                 token=body.get("token") or None, start_now=bool(body.get("start")),
                                 model=body.get("model"), subagent_model=body.get("subagent_model"))
    srv.syncer.sync_one(node)
    return res


def post_projects_token(srv, body):
    node, project = need_str(body, "node"), need_str(body, "project")
    label, pool = body.get("token"), body.get("tokens")
    if pool is None or label is not None:
        label = need_str(body, "token")
    nodeops.set_project_token(node, project, label, pool)
    return {"node": node, "project": project, "token": config.token_label_for(node, project),
            "tokens": config.token_pool(node, project)}


def post_projects_models(srv, body):
    node, project = need_str(body, "node"), need_str(body, "project")
    return nodeops.set_project_models(node, project, body.get("model"), body.get("subagent_model"))


def post_projects_copy(srv, body):
    return copy_now(srv, need_str(body, "node"), need_str(body, "project"), srv.reveal)


def newest_commit(srv, node, project):
    proj = next((p for p in (srv.syncer.current_snapshot(node) or {}).get("projects") or []
                 if p.get("name") == project), None)
    return mirror.newest_sha(proj) if proj else None


def copy_now(srv, node, project, reveal):
    """Fetch the node's newest commit into the mirror, then export exactly that commit to ~/Downloads.
    Refused, with nothing copied, unless the mirror holds the commit the node reports now."""
    nodeops.check_project(project)
    nodeops.get_node(node)
    srv.syncer.sync_one(node, force_mirror=True)
    entry = srv.syncer.entries.get(node) or {}
    if not entry.get("ok"):
        raise nodeops.OpError("cannot reach %s: %s; nothing was copied" % (node, entry.get("error")), 502)
    proj = next((p for p in (srv.syncer.current_snapshot(node) or {}).get("projects") or []
                 if p.get("name") == project), None)
    if proj is None:
        raise nodeops.OpError("no project %s on %s" % (project, node), 404)
    head = mirror.newest_sha(proj)
    if not head:
        raise nodeops.OpError("%s has no commit yet; nothing was copied" % project, 409)
    if not srv.syncer.mirror.wait_idle(120):
        raise nodeops.OpError("the mirror is still fetching; try again in a minute; nothing was copied", 409)
    m = srv.syncer.mirror.get(node, project) or {}
    if m.get("error") or m.get("sha") != head:
        raise nodeops.OpError("could not fetch the newest commit %s from %s: %s; nothing was copied"
                              % (head, node, m.get("error") or "the mirror holds %s" % m.get("sha")), 502)
    return nodeops.copy_result(node, project, head, reveal=reveal, result_sha=proj.get("result_sha"))


def post_run(srv, body):
    """Serve the project's copied web result on its own port and open it. The copy is reused when it holds the
    current result (or the node is unreachable); otherwise the newest commit is copied first."""
    node, project = need_str(body, "node"), need_str(body, "project")
    nodeops.check_project(project)
    nodeops.get_node(node)
    key = "%s/%s" % (node, project)
    entry = config.load_projects().get(key) or {}
    path = entry.get("copied_to")
    have = bool(path and os.path.isfile(os.path.join(path, "index.html")))
    running = srv.syncer.runner.url(key)
    if not (have and running and srv.syncer.runner.path(key) == path):
        snap = srv.syncer.current_snapshot(node)
        proj = next((p for p in (snap or {}).get("projects") or [] if p.get("name") == project), None)
        current = have and (snap is None or (proj is not None and not result_changed(proj, entry)))
        if not current:
            path = copy_now(srv, node, project, reveal=lambda p: None)["path"]
    if not os.path.isfile(os.path.join(path, "index.html")):
        raise nodeops.OpError("no index.html at the top of %s; Run it serves web results only" % path, 409)
    url = srv.syncer.runner.start(key, path)
    (srv.browse or open_url)(url)
    return {"url": url, "path": path}


def result_changed(proj, entry):
    """True when the node holds a result (or, without one, a commit) newer than the copy."""
    prefix = lambda a, b: bool(a and b and (a.startswith(b) or b.startswith(a)))
    if proj.get("result_sha"):
        return not prefix(entry.get("copied_result_sha") or entry.get("copied_sha"), proj["result_sha"])
    return not prefix(entry.get("copied_sha"), mirror.newest_sha(proj))


def open_url(url):
    subprocess.run(["open", url], capture_output=True, check=False)


def post_run_stop(srv, body):
    node, project = need_str(body, "node"), need_str(body, "project")
    return {"stopped": srv.syncer.runner.stop("%s/%s" % (node, project))}


def post_restart(srv, body):
    node, project = need_str(body, "node"), need_str(body, "project")
    if srv.chats.busy(node, project):
        raise nodeops.OpError("a review chat is open for %s; end it first" % project, 409)
    res = nodeops.restart(node, project)
    srv.syncer.sync_one(node)
    return res


def post_dialog(srv, body):
    node, project = node_project(body)
    if srv.chats.busy(node, project):
        raise nodeops.OpError("a review chat is open for %s; end it first" % project, 409)
    res = nodeops.answer_dialog(node, project, body.get("fingerprint"), body.get("n"))
    srv.syncer.sync_one(node)
    return res


def post_lead_update(srv, body):
    node, project = need_str(body, "node"), need_str(body, "project")
    res = nodeops.relead(node, project)
    srv.syncer.sync_one(node)
    return res


def post_notify_setup(srv, body):
    enabled = body.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise nodeops.OpError("enabled must be true or false")
    try:
        return srv.syncer.notifier.setup(body.get("server"), enabled)
    except ValueError as e:
        raise nodeops.OpError(str(e))
    except keychain.KeychainError as e:
        raise nodeops.OpError(str(e), 500)


def post_notify_test(srv, body):
    try:
        srv.syncer.notifier.test()
    except ValueError as e:
        raise nodeops.OpError(str(e), 409)
    except (RuntimeError, notify.Undeliverable) as e:
        raise nodeops.OpError(str(e), 502)
    return {"ok": True}


def post_nodes_update(srv, body):
    name = need_str(body, "name")
    res = nodeops.update_node(name)
    srv.syncer.sync_one(name)
    return res


def post_tickets(srv, body):
    node, project = need_str(body, "node"), need_str(body, "project")
    op = {k: v for k, v in body.items() if k not in ("node", "project")}
    res = nodeops.ticket(node, project, op)
    srv.syncer.sync_one(node)
    return res


def node_project(body):
    return need_str(body, "node"), need_str(body, "project")


def post_chat_start(srv, body):
    return srv.chats.start(*node_project(body), resume=body.get("resume") is True)


def post_chat_send(srv, body):
    return srv.chats.send(*node_project(body), body.get("text"))


def post_chat_end(srv, body):
    return srv.chats.end(*node_project(body))


def post_answer(srv, body):
    node, project = node_project(body)
    if srv.chats.busy(node, project):
        raise nodeops.OpError("a review chat is open for %s; answer there" % project, 409)
    note = body.get("note") or ""
    if not isinstance(note, str):
        raise nodeops.OpError("note must be text")
    try:
        res = nodeops.answer(node, project, need_str(body, "id"), need_str(body, "choice"), note, body.get("fingerprint"))
    except nodeops.OpError as e:
        if "question" in e.extra:
            srv.syncer.sync_one(node)   # so the next poll shows the question the node refused against
        raise
    srv.syncer.sync_one(node)
    return res


def post_sandboxes(srv, body):
    return {"job": srv.sandboxes.create(need_str(body, "name"), body.get("image"), body.get("memory_mb"), body.get("cpus"),
                                        body.get("allowed_tokens"))}


def post_sandboxes_start(srv, body):
    node = need_str(body, "node")
    res = srv.sandboxes.start(node)
    srv.syncer.sync_one(node)
    return res


def post_sandboxes_stop(srv, body):
    node = need_str(body, "node")
    srv.syncer.sync_one(node)   # the live check needs the node as it is now, not up to 30 s ago
    res = srv.sandboxes.stop(node, srv.syncer.current_snapshot(node), force=body.get("force") is True)
    srv.syncer.sync_one(node)
    return res


def post_sandboxes_recreate(srv, body):
    node = need_str(body, "node")
    srv.syncer.sync_one(node)   # the live check needs the node as it is now
    res = srv.sandboxes.recreate(node, srv.syncer.current_snapshot(node), force=body.get("force") is True)
    srv.syncer.sync_one(node)
    return res


def post_sandboxes_remove(srv, body):
    node = need_str(body, "node")
    if body.get("confirm") == node:
        srv.syncer.sync_one(node)
    res = srv.sandboxes.remove(node, body.get("confirm"), srv.syncer.current_snapshot(node), force=body.get("force") is True)
    srv.syncer.forget(node)
    return res


POST_ROUTES = {"/api/nodes": post_nodes, "/api/nodes/tokens": post_nodes_tokens,
               "/api/nodes/remove": post_nodes_remove, "/api/nodes/update": post_nodes_update, "/api/tokens": post_tokens,
               "/api/tokens/update": post_tokens_update, "/api/tokens/remove": post_tokens_remove,
               "/api/tokens/clear-limit": post_tokens_clear_limit,
               "/api/projects": post_projects, "/api/projects/token": post_projects_token,
               "/api/projects/models": post_projects_models, "/api/projects/copy": post_projects_copy,
               "/api/run": post_run, "/api/run/stop": post_run_stop, "/api/restart": post_restart, "/api/dialog": post_dialog,
               "/api/lead/update": post_lead_update, "/api/notify/setup": post_notify_setup,
               "/api/notify/test": post_notify_test,
               "/api/tickets": post_tickets, "/api/chat/start": post_chat_start,
               "/api/chat/send": post_chat_send, "/api/chat/end": post_chat_end, "/api/answer": post_answer,
               "/api/sandboxes": post_sandboxes, "/api/sandboxes/start": post_sandboxes_start,
               "/api/sandboxes/stop": post_sandboxes_stop, "/api/sandboxes/remove": post_sandboxes_remove,
               "/api/sandboxes/recreate": post_sandboxes_recreate}


class CairnServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, syncer=None, ui_dir=UI_DIR, opener=None, token=None, chats=None, reveal=None, browse=None,
                 sandboxes=None, secret=None, dev=False):
        super().__init__(("127.0.0.1", port), Handler)
        self.port = self.server_address[1]
        self.syncer = syncer or Syncer()
        self.chats = chats or ChatManager(self.syncer)
        self.syncer.autostart.busy = self.chats.busy
        self.syncer.attention.busy = self.chats.busy
        self.sandboxes = sandboxes or Sandboxes()
        self.sandboxes.on_added = self.syncer.sync_one
        self.syncer.sandboxes = self.sandboxes
        self.ui_dir = ui_dir
        self.opener = opener
        self.reveal = reveal
        self.browse = browse
        # The secret signs a browser in (cookie); the token, derived from it, is the per-request CSRF check on POSTs.
        # Both stay the same across restarts, so a hub restart never breaks an open page.
        self.secret = secret or config.hub_secret()
        self.token = token or hmac.new(self.secret.encode(), b"cairn post token", "sha256").hexdigest()
        self.cookie_name = "cairn-%d" % self.port   # cookies ignore the port; another hub on another port has its own
        self.dev = dev   # serves ui/fixtures/ and tells the page so; the UI honours ?fixture= only then

    def index_html(self):
        try:
            with open(os.path.join(self.ui_dir, "index.html"), encoding="utf-8") as f:
                html = f.read()
        except FileNotFoundError:
            html = PLACEHOLDER
        meta = '<meta name="cairn-token" content="%s">' % self.token
        if self.dev:
            meta += '<meta name="cairn-dev" content="1">'
        if "</head>" in html:
            return html.replace("</head>", meta + "</head>", 1)
        return meta + html


class Handler(BaseHTTPRequestHandler):
    server_version = "cairn"

    def log_message(self, fmt, *args):
        pass

    # -- helpers
    def end_headers(self, csp="frame-ancestors 'none'"):
        # no page may frame the hub (a Run it result on another 127.0.0.1 port could otherwise click-jack it)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", csp)
        super().end_headers()

    def token_ok(self):
        return hmac.compare_digest((self.headers.get("X-Cairn-Token") or "").encode(), self.server.token.encode())

    def send(self, status, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if ctype.startswith("text/html"):
            self.end_headers(page_csp(body))
        else:
            self.end_headers()
        self.wfile.write(body)

    def err(self, status, msg):
        self.send(status, {"error": msg})

    def host_ok(self):
        port = self.server.port
        return self.headers.get("Host") in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def signed_in(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == self.server.cookie_name and hmac.compare_digest(value.encode(), self.server.secret.encode()):
                return True
        return False

    def sign_in(self, key):
        """GET /?k=<secret>, the link `cairn open` opens: set the cookie and drop the secret from the address bar."""
        if not hmac.compare_digest(key.encode(), self.server.secret.encode()):
            return self.send(401, SIGN_IN_PAGE, "text/html; charset=utf-8")
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d"
                         % (self.server.cookie_name, self.server.secret, COOKIE_AGE))
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def handle_one(self, method):
        if not self.host_ok():
            return self.err(403, "bad host")
        url = urlparse(self.path)
        # Every route needs the cookie: a Docker sandbox reaches 127.0.0.1 through host.docker.internal with any Host
        # header it likes, but it never has the browser's cookie.
        key = parse_qs(url.query).get("k")
        if method == "GET" and url.path == "/" and key:
            return self.sign_in(key[0])
        if not self.signed_in():
            if url.path.startswith("/api/"):
                return self.err(401, "this browser is not signed in to the hub; run `cairn open` on the Mac")
            return self.send(401, SIGN_IN_PAGE, "text/html; charset=utf-8")
        try:
            if method == "GET":
                return self.do_get(url)
            return self.do_post(url)
        except nodeops.OpError as e:
            self.send(e.status, {"error": str(e), **e.extra})
        except ssh.NodeError as e:
            self.err(502, str(e))
        except keychain.KeychainError as e:
            self.err(500, str(e))
        except Exception as e:
            self.err(500, "%s: %s" % (type(e).__name__, e))

    def do_GET(self):
        self.handle_one("GET")

    def do_POST(self):
        self.handle_one("POST")

    # -- GET
    def do_get(self, url):
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        p = url.path
        if p == "/api/state":
            return self.send(200, dict(self.server.syncer.state(), chats=self.server.chats.list()))
        if p == "/api/chat":
            chat = self.server.chats.get(q.get("node", ""), q.get("project", ""))
            return self.send(200, chat.view(self.after(q)))
        if p == "/api/chat/events":
            return self.chat_events(self.server.chats.get(q.get("node", ""), q.get("project", "")), self.after(q))
        if p == "/api/file":
            return self.send(200, nodeops.read_file(q.get("node", ""), q.get("project", ""), q.get("path", "")))
        if p == "/api/pane":
            return self.send(200, nodeops.pane(q.get("node", ""), q.get("project", ""), bool(q.get("review"))))
        if p == "/api/dialog":
            return self.send(200, nodeops.dialog(q.get("node", ""), q.get("project", "")))
        if p == "/api/notify":   # the only response with the topic: token required, like POSTs
            if not self.token_ok():
                return self.err(403, "bad token")
            return self.send(200, self.server.syncer.notifier.view())
        if p == "/api/usage":
            return self.send(200, nodeops.usage(q.get("node", ""), q.get("project", "")))
        if p == "/api/sandboxes":
            return self.send(200, self.server.sandboxes.view())
        if p == "/api/ssh-hosts":
            return self.send(200, {"hosts": nodeops.ssh_hosts()})
        if p.startswith("/api/"):
            return self.err(404, "no such route")
        return self.static(p)

    def after(self, q):
        raw = self.headers.get("Last-Event-ID") or q.get("after") or "0"
        try:
            return max(0, int(raw))
        except ValueError:
            raise nodeops.OpError("after must be a number")

    def chat_events(self, chat, after):
        if after > len(chat.events):
            after = 0   # a reconnect after a fresh start: its numbering began again
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                events = self.server.chats.wait_events(chat, after, 15)
                if events is None:
                    return   # replaced by a newer chat; the client reconnects to it
                if events:
                    for ev in events:
                        self.wfile.write(("id: %d\ndata: %s\n\n" % (ev["seq"], json.dumps(ev))).encode())
                    after = events[-1]["seq"]
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except OSError:
            return

    def static(self, p):
        if p in ("/", "/index.html"):
            return self.send(200, self.server.index_html().encode(), "text/html; charset=utf-8")
        root = os.path.realpath(self.server.ui_dir)
        full = os.path.realpath(os.path.join(root, unquote(p).lstrip("/")))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self.err(404, "not found")
        if not self.server.dev and os.path.relpath(full, root).split(os.sep)[0].lower() == "fixtures":   # APFS ignores case
            return self.err(404, "not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self.send(200, f.read(), ctype)

    # -- POST
    def do_post(self, url):
        origin = self.headers.get("Origin")
        if origin != "http://%s" % self.headers.get("Host"):
            return self.err(403, "bad origin")
        tok = self.headers.get("X-Cairn-Token") or ""
        if not hmac.compare_digest(tok.encode(), self.server.token.encode()):
            return self.err(403, "bad token")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0:
            return self.err(400, "bad length")
        if n > MAX_BODY:
            return self.err(413, "body too large")
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            return self.err(400, "invalid JSON")
        if not isinstance(body, dict):
            return self.err(400, "body must be an object")
        p = url.path
        if p == "/api/sync":
            node = body.get("node")
            if node is None:
                self.server.syncer.sync_now()
            elif not isinstance(node, str) or node not in config.load_nodes():
                return self.err(404, "no such node")
            else:
                self.server.syncer.sync_one(node)   # one node: the others, perhaps unreachable, are not waited for
            return self.send(200, self.server.syncer.state())
        if p in ("/api/start", "/api/stop", "/api/review"):
            node, project = body.get("node"), body.get("project")
            if not isinstance(node, str) or not isinstance(project, str):
                return self.err(400, "node and project required")
            if p == "/api/start":
                return self.send(200, nodeops.start(node, project))
            if p == "/api/stop":
                return self.send(200, nodeops.stop(node, project))
            res = nodeops.review(node, project, opener=self.server.opener)
            return self.send(200, {"attach": res["attach"]})
        route = POST_ROUTES.get(p)
        if route:
            return self.send(200, route(self.server, body))
        return self.err(404, "no such route")


LOG_LIMIT = 5 * 1024 * 1024


def trim_log():
    """serve.log is launchd's stdout; start it afresh past 5 MB (launchd appends, so truncating is safe)."""
    p = config.path("serve.log")
    try:
        if os.path.getsize(p) > LOG_LIMIT:
            os.truncate(p, 0)
    except OSError:
        pass


def sign_in_url(port):
    return "http://127.0.0.1:%d/?k=%s" % (port, config.hub_secret())


def serve(port, dev=False):
    # the folder holds the sign-in secret and the state: private even when something made it earlier with a wider mode
    os.makedirs(config.home(), mode=0o700, exist_ok=True)
    os.chmod(config.home(), 0o700)
    trim_log()
    syncer = Syncer()
    try:
        srv = CairnServer(port, syncer, dev=dev)
    except OSError as e:
        # exit 0, so launchd (KeepAlive on failure only) does not respawn into the same busy port every 10 s
        print("cairn: cannot listen on 127.0.0.1:%d: %s; is another hub running? (cairn service status)"
              % (port, e.strerror), flush=True)
        syncer.stop()
        raise SystemExit(0)
    syncer.start()
    print("cairn serving on http://127.0.0.1:%d; sign a browser in with `cairn open`, or open %s"
          % (srv.port, sign_in_url(srv.port)), flush=True)
    if dev:
        print("cairn: dev mode: ui/fixtures/ is served and the page honours ?fixture=", flush=True)

    def on_term(signum, frame):   # launchd stops the hub with SIGTERM: close chats and servers as on Ctrl-C
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.chats.stop()
        syncer.runner.stop_all()
        syncer.stop()
        srv.server_close()
