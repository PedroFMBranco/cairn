"""Review chats: the cairn-review skill run with stream-json over ssh (`cairn-node chat`),
relayed to the browser as chat events. The raw stream never leaves the hub."""
import json
import subprocess
import threading
import time

from . import config, nodeops, ssh

LIVE = ("starting", "working", "waiting")
OPENING = "/cairn-review chat"
FINISHED_LINE = "Review finished."
IDLE_LIMIT = 20 * 60      # waiting for the operator this long ends the chat
SILENT_LIMIT = 10 * 60    # starting/working with no output this long is an error
END_WAIT = 10
MAX_TEXT = 8000
MAX_LINE = 32 << 20     # bytes in one stream-json line (one message, tool results included)
MAX_ERR_LINE = 4096     # bytes in one stderr line
AUTH_FAILURES = ("Not logged in", "Please run /login", "Run /login", "Invalid API key", "OAuth token has expired")


def tool_summary(block, redact):
    """One short line for a tool call; redacted before it is cut, so no part of a secret survives."""
    inp = block.get("input") or {}
    if block.get("name") == "Bash" and isinstance(inp.get("command"), str):
        lines = redact(inp["command"]).strip().splitlines()
        text = lines[0] if lines else ""
    elif isinstance(inp.get("file_path"), str):
        text = redact(inp["file_path"])
    else:
        text = redact(json.dumps(inp, separators=(",", ":")))
    return text if len(text) <= 160 else text[:159] + "…"


def project_of(snapshot, project):
    for p in (snapshot or {}).get("projects") or []:
        if p.get("name") == project:
            return p
    return None


def answered(snapshot, project):
    p = project_of(snapshot, project)
    return {q["id"]: q.get("choice") for q in (p or {}).get("questions") or [] if q.get("answered")}


def unanswered(snapshot, project):
    p = project_of(snapshot, project)
    if p is None:
        return None
    return {q["id"] for q in p.get("questions") or [] if not q.get("answered")}


class Chat:
    def __init__(self, node, project, events=None):
        self.node, self.project = node, project
        self.state, self.detail = "starting", None
        self.session_id = None
        self.started_at = config.now_iso()
        self.events = events if events is not None else []
        self.cond = threading.Condition()
        self.proc = None
        self.secret = ""
        self.last_activity = time.time()   # the last time it became the operator's turn, or they sent something
        self.last_output = time.time()     # the last line from the node
        self.seen_answers = None           # None until a baseline exists
        self.finished = False              # the skill said FINISHED_LINE this turn
        self.stderr_tail = []
        self.err_thread = None
        self.closer = None

    @property
    def live(self):
        return self.state in LIVE

    def emit(self, kind, **fields):
        with self.cond:
            ev = {"seq": len(self.events) + 1, "at": config.now_iso(), "kind": kind, **fields}
            self.events.append(ev)
            self.cond.notify_all()
        return ev

    def transition(self, to, detail=None, allowed=LIVE):
        """Move to `to` when the current state is in `allowed`; ended and error are final."""
        with self.cond:
            if self.state not in allowed or (self.state, self.detail) == (to, detail):
                return False
            self.state, self.detail = to, detail
            if to == "waiting":
                self.last_activity = time.time()
            ev = {"seq": len(self.events) + 1, "at": config.now_iso(), "kind": "state", "state": to, "detail": detail}
            self.events.append(ev)
            self.cond.notify_all()
        return True

    def redact(self, text):
        return text.replace(self.secret, "***") if self.secret else text

    def view(self, after=0):
        with self.cond:
            return {"node": self.node, "project": self.project, "state": self.state, "detail": self.detail,
                    "session_id": self.session_id, "started_at": self.started_at,
                    "events": list(self.events[max(0, int(after)):])}


class ChatManager:
    def __init__(self, syncer=None, popen=subprocess.Popen, idle_limit=IDLE_LIMIT, silent_limit=SILENT_LIMIT,
                 reap_every=30):
        self.syncer = syncer
        self.popen = popen
        self.idle_limit = idle_limit
        self.silent_limit = silent_limit
        self.lock = threading.Lock()
        self.chats = {}
        self.stopping = threading.Event()
        threading.Thread(target=self._reaper, args=(reap_every,), daemon=True, name="chat-reaper").start()

    # -- queries
    def busy(self, node, project):
        c = self.chats.get((node, project))
        return c is not None and c.live

    def list(self):
        return [{"node": c.node, "project": c.project, "state": c.state, "detail": c.detail}
                for c in list(self.chats.values())]

    def get(self, node, project):
        c = self.chats.get((node, project))
        if c is None:
            raise nodeops.OpError("no chat for %s/%s" % (node, project), 404)
        return c

    def wait_events(self, chat, after, timeout):
        """Events after `after`, waiting up to `timeout`; None once another chat has replaced this one."""
        with chat.cond:
            if len(chat.events) <= after and self.chats.get((chat.node, chat.project)) is chat:
                chat.cond.wait(timeout)
            if self.chats.get((chat.node, chat.project)) is not chat:
                return None
            return list(chat.events[after:])

    # -- actions
    def start(self, node_name, project, resume=False):
        nodeops.check_project(project)
        node = nodeops.get_node(node_name)
        key = (node_name, project)
        old = self.chats.get(key)
        if old is not None and old.live:
            return old.view()
        if old is not None and old.closer is not None:
            old.closer.join(END_WAIT + 2)   # End then Continue: let the old claude exit first
        if old is not None and old.proc is not None and old.proc.poll() is None:
            raise nodeops.OpError("the last chat is still closing; try again in a few seconds", 409)
        line = nodeops.token_line(node_name, project)   # Keychain read: outside the lock
        with self.lock:
            cur = self.chats.get(key)
            if cur is not None and cur is not old:
                return cur.view()   # another request started one meanwhile
            chat = Chat(node_name, project, events=list(old.events) if (resume and old) else None)
            chat.secret = line.strip()
            snap = self.syncer.current_snapshot(node_name) if self.syncer else None
            chat.seen_answers = answered(snap, project) if snap is not None else None
            argv = ["chat", project] + (["--resume"] if resume else []) + nodeops.model_args(node_name, project)
            try:
                chat.proc = self.popen(ssh.node_command(node, argv), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError as e:
                raise nodeops.OpError("cannot start the chat: %s" % e.strerror, 502)
            self.chats[key] = chat
        if old:
            with old.cond:
                old.cond.notify_all()   # streams on the old chat end and reconnect
        chat.emit("state", state="starting", detail=None)
        self._write(chat, (line if line.endswith("\n") else line + "\n").encode())
        del line
        chat.err_thread = threading.Thread(target=self._read_err, args=(chat,), daemon=True)
        chat.err_thread.start()
        threading.Thread(target=self._read, args=(chat,), daemon=True, name="chat-" + project).start()
        return chat.view()

    def send(self, node_name, project, text):
        chat = self.get(node_name, project)
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise nodeops.OpError("text must be 1 to %d characters" % MAX_TEXT)
        if not chat.transition("working", allowed=("waiting",)):
            raise nodeops.OpError("the chat is %s, not waiting for you" % chat.state, 409)
        chat.last_activity = time.time()
        ev = chat.emit("user", text=text)
        if not self._write_message(chat, text):
            raise nodeops.OpError("the chat process is gone", 409)
        return {"seq": ev["seq"]}

    def end(self, node_name, project, detail="closed"):
        chat = self.get(node_name, project)
        self._finish(chat, "ended", detail)
        return chat.view()

    def stop(self):
        self.stopping.set()
        for c in list(self.chats.values()):
            self._finish(c, "ended", "closed")

    # -- internals
    def _write(self, chat, data):
        try:
            chat.proc.stdin.write(data)
            chat.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _write_message(self, chat, text):
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        if self._write(chat, (json.dumps(msg) + "\n").encode()):
            return True
        self._finish(chat, "error", "the chat process is gone")
        return False

    def _finish(self, chat, state, detail):
        """Enter a final state once, then close claude: stdin first, kill after END_WAIT."""
        if not chat.transition(state, chat.redact(detail) if detail else detail):
            return False
        proc = chat.proc

        def close():
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(END_WAIT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        chat.closer = threading.Thread(target=close, daemon=True)
        chat.closer.start()
        return True

    def _read(self, chat):
        for raw in iter(lambda: chat.proc.stdout.readline(MAX_LINE), b""):
            chat.last_output = time.time()
            if not chat.live:
                continue   # drain what a closing process still says, show nothing
            if len(raw) >= MAX_LINE and not raw.endswith(b"\n"):   # the rest of it is drained above
                self._finish(chat, "error", "the chat stream sent a line over %d MB" % (MAX_LINE >> 20))
                continue
            try:
                msg = json.loads(raw)
                if isinstance(msg, dict):
                    self._handle(chat, msg)
            except Exception as e:
                self._finish(chat, "error", "could not read the chat stream: %s" % type(e).__name__)
        code = chat.proc.wait()
        if chat.err_thread:
            chat.err_thread.join(2)
        if code == 0:
            self._finish(chat, "ended", "exited")
        else:
            tail = chat.stderr_tail[-1] if chat.stderr_tail else "exit %d" % code
            self._finish(chat, "error", tail)

    def _read_err(self, chat):
        skipping = False
        for raw in iter(lambda: chat.proc.stderr.readline(MAX_ERR_LINE), b""):
            if skipping or (len(raw) >= MAX_ERR_LINE and not raw.endswith(b"\n")):
                skipping = not raw.endswith(b"\n")   # an overlong line is dropped whole: no cut secret can show
                continue
            line = raw.decode(errors="replace").strip()
            if line:
                chat.stderr_tail = (chat.stderr_tail + [line])[-5:]

    def _handle(self, chat, msg):
        kind = msg.get("type")
        if kind is None and "error" in msg:
            self._finish(chat, "error", str(msg["error"]))
        elif kind == "cairn":
            chat.session_id = msg.get("session_id")
            if msg.get("resumed"):
                chat.transition("waiting", allowed=("starting",))
            elif chat.transition("working", allowed=("starting",)):
                self._write_message(chat, OPENING)
        elif kind == "assistant":
            for block in (msg.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    text = chat.redact(block["text"])
                    lines = text.rstrip().splitlines()
                    if lines and lines[-1].strip() == FINISHED_LINE:
                        chat.finished = True
                        text = "\n".join(lines[:-1]).rstrip()
                    if text:
                        chat.emit("text", text=text)
                elif block.get("type") == "tool_use":
                    chat.emit("tool", name=str(block.get("name") or "tool"), summary=tool_summary(block, chat.redact))
        elif kind == "result":
            ok = msg.get("subtype") == "success" and not msg.get("is_error")
            chat.emit("turn", ok=ok, seconds=round((msg.get("duration_ms") or 0) / 1000, 1),
                      cost_usd=msg.get("total_cost_usd"))
            if not ok:
                reason = str(msg.get("result") or msg.get("subtype") or "the turn failed")
                if any(f in reason for f in AUTH_FAILURES):
                    reason = "the node's Claude login failed: " + reason
                self._finish(chat, "error", chat.redact(reason)[:300])   # redact, then cut
                return
            if chat.transition("waiting", allowed=("working",)):
                threading.Thread(target=self._after_turn, args=(chat,), daemon=True).start()

    def _after_turn(self, chat):
        """Report new answers; end the chat once nothing is left or the skill said it finished."""
        snap = None
        if self.syncer:
            try:
                self.syncer.sync_one(chat.node)
                snap = self.syncer.current_snapshot(chat.node)
            except Exception:
                snap = None
        if snap is not None:
            now = answered(snap, chat.project)
            if chat.seen_answers is None:
                chat.seen_answers = now   # no baseline at start: take it now, report nothing
            for qid, choice in sorted(now.items()):
                if qid not in chat.seen_answers:
                    chat.seen_answers[qid] = choice
                    chat.emit("answered", id=qid, choice=choice)
        left = unanswered(snap, chat.project)
        if chat.state == "waiting" and (chat.finished or (left is not None and not left)):
            self._finish(chat, "ended", "finished")

    def _reaper(self, every):
        while not self.stopping.wait(every):
            now = time.time()
            for c in list(self.chats.values()):
                if c.state == "waiting" and now - c.last_activity > self.idle_limit:
                    self._finish(c, "ended", "idle")
                elif c.state in ("starting", "working") and now - c.last_output > self.silent_limit:
                    self._finish(c, "error", "no output from the node for %d min" % (self.silent_limit // 60))
