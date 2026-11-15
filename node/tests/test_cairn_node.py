import fcntl
import hashlib
import importlib.machinery
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

NODE = Path(__file__).resolve().parent.parent / "cairn-node"
LOGIN_PANES = Path(__file__).resolve().parent / "fixtures" / "login-panes"
TOKEN = "sk-ant-oat01-SECRETTOKENVALUE"

FAKE_TMUX = r'''#!/usr/bin/env python3
import json, os, re, signal, subprocess, sys
state = os.environ["FAKE_STATE"]
argv = sys.argv[1:]
with open(os.path.join(state, "calls.jsonl"), "a") as f:
    f.write(json.dumps(argv) + "\n")
if argv[:1] == ["-V"]:
    print("tmux " + os.environ.get("FAKE_TMUX_VERSION", "3.3a")); sys.exit(0)
assert argv[:2] == ["-L", "cairn"], argv
cmd, rest = argv[2], argv[3:]
def arg(flag):
    return rest[rest.index(flag) + 1]
def sess(t):
    return t.lstrip("=").split(":")[0]
def path(kind, n):
    return os.path.join(state, kind + "-" + n)
def pid_of(n):
    text = open(path("sess", n)).read().strip()
    return int(text) if text else None
def running(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
def dead(n):
    pid = pid_of(n)
    return pid is not None and not running(pid)
def alive(n):
    return os.path.exists(path("sess", n)) and (not dead(n) or os.path.exists(path("remain", n)))
if cmd == "has-session":
    sys.exit(0 if alive(sess(arg("-t"))) else 1)
if cmd == "new-session":
    name = arg("-s")
    if alive(name):
        print("duplicate session: " + name, file=sys.stderr); sys.exit(1)
    if os.path.exists(path("remain", name)):
        os.remove(path("remain", name))
    command = rest[rest.index("--") + 1:]
    pid = ""
    if os.environ.get("FAKE_TMUX_NORUN") != "1":
        log = open(path("pane", name), "w")
        pid = subprocess.Popen(command, cwd=arg("-c"), stdout=log, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, start_new_session=True).pid
    open(path("sess", name), "w").write(str(pid)); sys.exit(0)
if not alive(sess(arg("-t"))):
    print("can't find session", file=sys.stderr); sys.exit(1)
name = sess(arg("-t"))
if cmd == "set-option":
    assert rest[-2] == "remain-on-exit", rest
    if rest[-1] == "on":
        open(path("remain", name), "w").close()
    elif os.path.exists(path("remain", name)):
        os.remove(path("remain", name))
    sys.exit(0)
if cmd == "display-message":
    assert rest[-1] == "#{pane_dead}", rest
    print("1" if dead(name) else "0"); sys.exit(0)
if cmd == "kill-session":
    pid = pid_of(name)
    if pid and running(pid):
        os.killpg(pid, signal.SIGKILL)
    for kind in ("sess", "remain"):
        if os.path.exists(path(kind, name)):
            os.remove(path(kind, name))
    sys.exit(0)
if cmd == "capture-pane":
    if os.path.exists(path("pane", name)):
        # like a real screen: output at the top, blank rows, then the dead-pane line
        sys.stdout.write(open(path("pane", name)).read() + "\n" * 30)
        if dead(name):
            print("Pane is dead (status 1)")
    else:
        for i in range(80): print("line %d" % i)
    print(); print(); sys.exit(0)
if cmd == "send-keys":
    # Up/Down move the dialog's ❯ marker (unless FAKE_TMUX_KEYS_STUCK); Enter shows after-<name> when it exists
    key, pane = rest[-1], path("pane", name)
    if key in ("Up", "Down") and not os.environ.get("FAKE_TMUX_KEYS_STUCK"):
        lines = open(pane).read().split("\n")
        opts = [i for i, l in enumerate(lines) if re.match(r"^\s*(❯\s*)?\d+\.\s", l)]
        cur = next(i for i in opts if "❯" in lines[i])
        nxt = opts.index(cur) + (1 if key == "Down" else -1)
        if 0 <= nxt < len(opts):
            col, to = lines[cur].index("❯"), opts[nxt]
            lines[cur] = lines[cur][:col] + " " + lines[cur][col + 1:]
            lines[to] = lines[to][:col] + "❯" + lines[to][col + 1:]
            gone = [i for i, l in enumerate(lines) if "automatically deny" in l]
            open(pane, "w").write("\n".join(l for i, l in enumerate(lines) if i not in gone and i - 1 not in gone))
    if key == "Enter" and os.path.exists(path("after", name)):
        os.replace(path("after", name), pane)
    if key == "Enter" and os.environ.get("FAKE_TMUX_ENTER_ENDS"):
        os.remove(path("sess", name))
    sys.exit(0)
sys.exit(2)
'''

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import hashlib, json, os, sys, time
if sys.argv[1:] == ["--version"]:
    print("2.1.296 (Claude Code)"); sys.exit(0)
tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
if sys.argv[1:] == ["auth", "status", "--json"]:   # as Claude Code 2.1.304 answers; exit 1 when signed out
    login = "yes" if tok else os.environ.get("FAKE_CLAUDE_LOGIN", "yes")
    if login == "garbage":
        print("Error: unknown command auth"); sys.exit(1)
    print(json.dumps({"loggedIn": login == "yes", "authMethod": "claude.ai" if login == "yes" else "none",
                      "apiProvider": "firstParty"}, indent=2))
    sys.exit(0 if login == "yes" else 1)
with open(os.path.join(os.environ["FAKE_STATE"], "claude.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                        "token_sha": hashlib.sha256(tok.encode()).hexdigest() if tok is not None else None,
                        "subagent_model": os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL")}) + "\n")
if "-p" in sys.argv[1:]:
    for line in sys.stdin:
        print(json.dumps({"type": "echo", "line": line.rstrip("\n")})); sys.stdout.flush()
    sys.exit(0)
# an interactive claude saves its conversation under ~/.claude/projects
args = sys.argv[1:]
sid = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("--session-id", "--resume")), None)
if sid:
    saved = os.path.join(os.environ["HOME"], ".claude", "projects", "-fake")
    os.makedirs(saved, exist_ok=True)
    open(os.path.join(saved, sid + ".jsonl"), "a").close()
if os.environ.get("FAKE_CLAUDE_MODE") == "selfend":
    # what `tmux kill-session -t =demo` run by the lead leaves behind: no session at all
    time.sleep(0.2)
    for kind in ("sess", "remain"):
        p = os.path.join(os.environ["FAKE_STATE"], kind + "-" + os.path.basename(os.getcwd()))
        if os.path.exists(p):
            os.remove(p)
    sys.exit(0)
if os.environ.get("FAKE_CLAUDE_MODE") == "die":
    print("Invalid API key - Please run /login"); sys.stdout.flush(); sys.exit(1)
if os.environ.get("FAKE_CLAUDE_MODE") == "late-die":
    time.sleep(0.7); print("late failure"); sys.stdout.flush(); sys.exit(1)
if os.environ.get("FAKE_CLAUDE_MODE") == "nologin":
    print("Not logged in \u00b7 Please run /login"); sys.stdout.flush()
if os.environ.get("FAKE_CLAUDE_PANE"):   # a pane captured from a real claude
    sys.stdout.write(open(os.environ["FAKE_CLAUDE_PANE"], encoding="utf-8").read()); sys.stdout.flush()
time.sleep(60)
'''

# A git on PATH that, before the first `git <LEAD_COMMITS_BEFORE>` call, lets
# "the lead" run `git add -A && git commit`, then runs the real git.
FAKE_GIT = r'''#!/usr/bin/env python3
import os, subprocess, sys
real, done = os.environ["REAL_GIT"], os.environ["LEAD_DONE"]
if os.environ["LEAD_COMMITS_BEFORE"] in sys.argv[1:] and not os.path.exists(done):
    open(done, "w").close()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    subprocess.run([real, "add", "-A"], check=True, env=env)
    subprocess.run([real, "commit", "-q", "-m", "lead work"], check=True, env=env)
os.execv(real, [real, *sys.argv[1:]])
'''

FAIL_GIT = '''#!/bin/sh
case " $* " in *" commit "*) if [ -n "$FAIL_COMMIT" ]; then echo "commit refused" >&2; exit 1; fi;; esac
exec "$REAL_GIT" "$@"
'''

# Runs cairn-node with an audit hook that logs the argv of every subprocess it starts.
AUDIT_HARNESS = r'''
import json, os, runpy, sys
log = os.environ["AUDIT_LOG"]
def hook(event, args):
    if event in ("subprocess.Popen", "os.exec", "os.posix_spawn", "os.spawn"):
        argv = args[1]
        argv = [argv] if isinstance(argv, (str, bytes)) else list(argv)
        with open(log, "a") as f:
            f.write(json.dumps({"event": event, "argv": [os.fsdecode(a) for a in argv]}) + "\n")
sys.addaudithook(hook)
script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name="__main__")
'''


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class NodeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = root = Path(self.tmp.name).resolve()
        self.home = root / "home"
        self.home.mkdir()
        self.work = root / "work"
        self.work.mkdir()
        self.state = root / "fake-state"
        self.state.mkdir()
        self.addCleanup(self.kill_panes)
        self.bindir = bindir = root / "bin"
        bindir.mkdir()
        for name, body in (("tmux", FAKE_TMUX), ("claude", FAKE_CLAUDE)):
            p = bindir / name
            p.write_text(body)
            p.chmod(0o755)
        self.audit = root / "audit.jsonl"
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
        self.env = dict(env, HOME=str(self.home), FAKE_STATE=str(self.state),
                        AUDIT_LOG=str(self.audit), CAIRN_START_WAIT="0.5",
                        GIT_CONFIG_NOSYSTEM="1",
                        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def kill_panes(self):
        for f in self.state.glob("sess-*"):
            text = f.read_text().strip()
            if text:
                try:
                    os.killpg(int(text), signal.SIGKILL)
                except OSError:
                    pass

    def node(self, *args, stdin="", ok=True, timeout=None):
        r = subprocess.run([sys.executable, "-c", AUDIT_HARNESS, str(NODE), *args], input=stdin,
                           capture_output=True, text=True, env=self.env, timeout=timeout)
        out = json.loads(r.stdout)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn("error", out)
        else:
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("error", out)
        return out

    def jsonl(self, path):
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def tmux_calls(self):
        return self.jsonl(self.state / "calls.jsonl")

    def new_session_calls(self):
        return [c for c in self.tmux_calls() if c[2:3] == ["new-session"]]

    def claude_calls(self):
        return [c for c in self.jsonl(self.state / "claude.jsonl") if c["argv"] != ["--version"]]

    def subprocess_argvs(self):
        return [e["argv"] for e in self.jsonl(self.audit)]

    def register(self, name, path):
        reg = self.home / ".cairn"
        reg.mkdir(exist_ok=True)
        f = reg / "projects.json"
        data = json.loads(f.read_text()) if f.exists() else {}
        data[name] = {"path": str(path)}
        f.write_text(json.dumps(data))

    def init(self, name="demo", **over):
        spec = {"path": str(self.work / name), "repo": {"kind": "new"},
                "destination": "Ship the demo.", "notes": "Use stdlib.",
                "tickets": [{"title": "First thing", "type": "build", "question": "Do it?"}]}
        spec.update(over)
        return self.node("init", name, stdin=json.dumps(spec))

    def write(self, repo, rel, text):
        p = Path(repo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def fail_commits(self):
        """Put a git on PATH whose `commit` fails while FAIL_COMMIT is set."""
        d = self.root / "failbin"
        d.mkdir(exist_ok=True)
        (d / "git").write_text(FAIL_GIT)
        (d / "git").chmod(0o755)
        self.env.update(REAL_GIT=shutil.which("git"), FAIL_COMMIT="1", PATH=f"{d}{os.pathsep}{self.env['PATH']}")

    def disk_has_token(self):
        for p in self.root.rglob("*"):
            if p.is_file() and TOKEN.encode() in p.read_bytes():
                return str(p)
        return None


TICKET = "---\nid: {id}\ntitle: {title}\ntype: {type}\nstatus: {status}\nassignee: {assignee}\nblocked_by: {blocked}\n---\n## Question\nx\n"


def ticket(id, title="T", type="task", status="open", assignee="", blocked="[]"):
    return TICKET.format(id=id, title=title, type=type, status=status,
                         assignee=assignee, blocked=blocked)


QUESTION = "---\nid: {id}\nticket: {ticket}\ngate: {gate}\ndefault: B\nasked_at: 2026-10-17T14:02:00Z\n---\nWhich way?\n\nA. left\nB. right (recommended: shorter)\nC. stay\n"


class SnapshotTests(NodeTestCase):
    def snap_project(self):
        out = self.node("snapshot")
        return out, out["projects"][0]

    def test_shape_and_deps(self):
        out = self.node("snapshot")
        self.assertEqual(out["node_version"], "0.4.6")
        self.assertEqual(out["deps"]["tmux"], "3.3a")
        self.assertEqual(out["deps"]["claude"], "2.1.296")
        self.assertEqual(out["projects"], [])
        self.assertEqual(out["warnings"], [])
        self.assertIsInstance(out["free_disk_mb"], int)

    def test_login_is_what_claude_auth_status_says(self):
        signed_out = "unknown" if sys.platform == "darwin" else "no"
        for fake, login in (("yes", "yes"), ("no", signed_out), ("garbage", "unknown")):
            self.env["FAKE_CLAUDE_LOGIN"] = fake
            self.assertEqual(self.node("snapshot")["login"], login, fake)
        self.env.update(FAKE_CLAUDE_LOGIN="no", CLAUDE_CODE_OAUTH_TOKEN=TOKEN)   # a lead's pane has no token variable,
        self.assertEqual(self.node("snapshot")["login"], signed_out)                # and neither has the check

    def test_login_signed_out_is_unknown_on_macos_only(self):
        loader = importlib.machinery.SourceFileLoader("cairn_node_login", str(NODE))
        node = types.ModuleType(loader.name)
        loader.exec_module(node)
        out = types.SimpleNamespace(returncode=1, stdout='{"loggedIn": false, "authMethod": "none"}', stderr="")
        for platform, login in (("linux", "no"), ("darwin", "unknown")):
            with mock.patch.object(node, "run", return_value=out), mock.patch.object(node.sys, "platform", platform):
                self.assertEqual(node.claude_login("/x/claude"), login)
        with mock.patch.object(node, "run", side_effect=node.CairnError("claude timed out")):
            self.assertEqual(node.claude_login("/x/claude"), "unknown")
        self.assertEqual(node.claude_login(None), "unknown")

    def test_missing_claude_is_null(self):
        (Path(self.env["PATH"].split(os.pathsep)[0]) / "claude").unlink()
        env_path = self.env["PATH"].split(os.pathsep)[0] + os.pathsep + "/usr/bin" + os.pathsep + "/bin"
        self.env["PATH"] = env_path
        out = self.node("snapshot")
        self.assertIsNone(out["deps"]["claude"])
        self.assertEqual(out["login"], "unknown")

    def test_result(self):
        self.init(tickets=[])
        repo = self.work / "demo"
        _, p = self.snap_project()
        self.assertIsNone(p["result"])
        self.write(repo, "work/result.md", "A playable game.\n\n## What was made\nx\n")
        _, p = self.snap_project()
        self.assertEqual(p["result"], "A playable game.\n\n## What was made\nx\n")
        self.assertIsNone(p["result_sha"])   # not committed yet
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *a], cwd=repo,
                                        capture_output=True, text=True, check=True).stdout.strip()
        git("add", "work/result.md")
        git("commit", "-qm", "result")
        sha = git("rev-parse", "--short", "HEAD")
        self.write(repo, "work/tickets/09-x.md", ticket("09"))
        git("add", "-A")
        git("commit", "-qm", "a dashboard ticket")
        _, p = self.snap_project()
        self.assertEqual(p["result_sha"], sha)   # the commit that changed the result, not the newest one
        self.write(repo, "work/result.md", "y" * 5000)
        _, p = self.snap_project()
        self.assertEqual(p["result"], "y" * 4000 + "…")
        # never through a symlink (it could point at the login) and never a FIFO (it would hang the snapshot)
        (repo / "work" / "result.md").unlink()
        secret = self.root / "secret.txt"
        secret.write_text("login")
        (repo / "work" / "result.md").symlink_to(secret)
        _, p = self.snap_project()
        self.assertIsNone(p["result"])
        (repo / "work" / "result.md").unlink()
        os.mkfifo(repo / "work" / "result.md")
        _, p = self.snap_project()
        self.assertIsNone(p["result"])

    def test_ticket_states(self):
        self.init(tickets=[])
        repo = self.work / "demo"
        t = repo / "work" / "tickets"
        self.write(repo, "work/tickets/01-a.md", ticket("01", "Alpha", status="closed"))
        self.write(repo, "work/tickets/02-b.md", ticket("02", "Beta", blocked="[03]"))
        self.write(repo, "work/tickets/03-c.md", ticket("03", "Gamma", type="research"))
        self.write(repo, "work/tickets/04-d.md", ticket("04", "Delta", assignee="lead"))
        self.write(repo, "work/tickets/05-e.md", ticket("05", "Eps", blocked="[01]"))
        self.write(repo, "work/tickets/06-f.md", ticket("06", "Zeta", assignee="lead"))
        self.write(repo, "work/tickets/07-g.md",
                   "---\nid: 07\ntitle: Block list\ntype: task\nstatus: open\nassignee: # none\nblocked_by:\n  - 02\n---\n")
        self.write(repo, "work/inbox/q01.md", QUESTION.format(id="q01", ticket="06", gate="no"))
        self.write(repo, "work/inbox/q02.md", QUESTION.format(id="q02", ticket="03", gate="yes"))
        self.write(repo, "work/inbox/q02.answer.md",
                   "---\nid: q02\nchoice: A\nanswered_at: 2026-10-17T15:10:00Z\n---\nok\n")
        _, p = self.snap_project()
        states = {x["id"]: x["state"] for x in p["tickets"]}
        self.assertEqual(states, {"01": "closed", "02": "blocked", "03": "frontier",
                                  "04": "claimed", "05": "frontier", "06": "waiting",
                                  "07": "blocked"})
        by_id = {x["id"]: x for x in p["tickets"]}
        self.assertEqual(by_id["02"]["blocked_by"], ["03"])
        self.assertEqual(by_id["07"]["blocked_by"], ["02"])
        self.assertEqual(by_id["07"]["assignee"], "")
        self.assertEqual(by_id["03"]["path"], "work/tickets/03-c.md")
        self.assertTrue(t.is_dir())

    def test_questions(self):
        self.init(tickets=[])
        repo = self.work / "demo"
        self.write(repo, "work/inbox/q07.md", QUESTION.format(id="q07", ticket="03", gate="yes"))
        self.write(repo, "work/inbox/q08.md", QUESTION.format(id="q08", ticket="", gate="no"))
        self.write(repo, "work/inbox/q08.answer.md",
                   "---\nid: q08\nchoice: other\nanswered_at: 2026-10-17T15:10:00Z\n---\ntext\n")
        _, p = self.snap_project()
        q7, q8 = p["questions"]
        self.assertTrue(q7["gate"])
        self.assertFalse(q7["answered"])
        self.assertEqual(q7["context"], "Which way?")
        self.assertEqual(q7["options"], ["A. left", "B. right (recommended: shorter)", "C. stay"])
        self.assertEqual((q7["default"], q7["ticket"], q7["path"]), ("B", "03", "work/inbox/q07.md"))
        self.assertEqual(q7["asked_at"], "2026-10-17T14:02:00Z")
        self.assertTrue(q8["answered"])
        self.assertEqual((q8["choice"], q8["answered_at"]), ("other", "2026-10-17T15:10:00Z"))
        self.assertFalse(q8["gate"])

    def test_map_adrs_status_commits(self):
        self.init(tickets=[])
        repo = self.work / "demo"
        self.write(repo, "work/map.md",
                   "# demo\n\n## Destination\n\nline one\nline two\n\n## Decisions so far\n\n"
                   "- [A](tickets/02-x.md) — gist\nprose\n- [B](tickets/03-y.md) — gist2\n\n## Out of scope\n")
        self.write(repo, "work/adr/0001-use-x.md", "# Use X\n\ncontext\n")
        self.write(repo, "work/status", "working 03\n")
        env = dict(self.env, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "first commit"], cwd=repo, check=True, env=env)
        _, p = self.snap_project()
        self.assertEqual(p["status"], "working 03")
        self.assertEqual(p["map"]["destination"], "line one line two")
        self.assertEqual(p["map"]["decisions"],
                         ["- [A](tickets/02-x.md) — gist", "- [B](tickets/03-y.md) — gist2"])
        self.assertEqual(p["adrs"], [{"id": "0001", "title": "Use X", "path": "work/adr/0001-use-x.md"}])
        self.assertEqual(p["commits"][0]["subject"], "first commit")
        self.assertEqual(len(p["commits"][0]["sha"]), 7)

    def test_status_unknown_and_lead_states(self):
        self.init(tickets=[])
        repo = self.work / "demo"
        self.write(repo, "work/status", "banana\n")
        _, p = self.snap_project()
        self.assertEqual((p["status"], p["lead"], p["review"]), ("unknown", "none", "none"))
        (self.home / ".cairn" / "state").mkdir()
        (self.home / ".cairn" / "state" / "demo.started").write_text("x")
        self.assertEqual(self.snap_project()[1]["lead"], "dead")
        (self.state / "sess-demo").touch()
        (self.state / "sess-demo-review").touch()
        _, p = self.snap_project()
        self.assertEqual((p["lead"], p["review"]), ("alive", "alive"))

    def test_broken_files_become_warnings(self):
        self.init(tickets=[])
        repo = self.work / "demo"
        self.write(repo, "work/tickets/01-ok.md", ticket("01"))
        self.write(repo, "work/tickets/02-nofront.md", "just text\n")
        self.write(repo, "work/tickets/03-unclosed.md", "---\nid: 03\ntitle: x\n")
        self.write(repo, "work/inbox/q01.md", "---\nid: q01\n")
        (repo / "work" / "tickets" / "04-bin.md").write_bytes(b"---\nid: 04\n\xff\xfe\n---\n")
        self.register("ghost", self.work / "does-not-exist")
        out, p = self.snap_project()
        self.assertEqual([t["id"] for t in p["tickets"] if t["id"] in ("01", "02", "03")], ["01"])
        text = json.dumps(out["warnings"])
        for needle in ("02-nofront.md", "03-unclosed.md", "q01.md", "ghost"):
            self.assertIn(needle, text)
        self.assertTrue(all("warning" in w for w in out["warnings"]))

    def test_fifos_and_symlinks_in_the_repo_never_hang_or_leak(self):
        self.init(tickets=[])
        repo = self.work / "demo"
        secret = self.root / "secret.md"
        secret.write_text(ticket("09", "Leaked"))
        for rel in ("work/status", "work/map.md", "work/inbox/q01.md", "work/adr/0001-x.md", "work/tickets/01-fifo.md", "LEAD.md"):
            (repo / rel).unlink(missing_ok=True)
            os.mkfifo(repo / rel)
        (repo / "work/tickets/02-link.md").symlink_to(secret)
        out = self.node("snapshot", timeout=30)
        p = out["projects"][0]
        self.assertEqual((p["status"], p["tickets"], p["questions"], p["adrs"], p["lead_version"]), ("unknown", [], [], [], 1))
        text = json.dumps(out["warnings"])
        for needle in ("work/map.md", "01-fifo.md", "02-link.md", "q01.md", "0001-x.md"):
            self.assertIn(needle, text)
        err = self.node("ticket", "demo", stdin=json.dumps({"op": "close", "id": "09", "resolution": "x"}), ok=False, timeout=30)
        self.assertIn("unknown ticket", err["error"])
        self.assertEqual(secret.read_text(), ticket("09", "Leaked"))
        self.assertIn("LEAD.md", self.node("relead", "demo", ok=False, timeout=30)["error"])

    def test_tolerant_frontmatter(self):
        self.init(tickets=[])
        repo = self.work / "demo"
        self.write(repo, "work/tickets/01-q.md",
                   '﻿---\r\nid: "01"\r\ntitle: \'Quoted: title\'\r\ntype: research  # why\r\n'
                   'status: open\r\nassignee: ""\r\nblocked_by: [ "02" , 03 ]\r\nextra: ignored\r\n---\r\nbody\r\n')
        _, p = self.snap_project()
        t = p["tickets"][0]
        self.assertEqual((t["id"], t["title"], t["type"], t["assignee"]),
                         ("01", "Quoted: title", "research", ""))
        self.assertEqual(t["blocked_by"], ["02", "03"])

    def test_quoted_values_keep_hash(self):
        self.init(tickets=[])
        repo = self.work / "demo"
        self.write(repo, "work/tickets/01-a.md",
                   '---\nid: 01\ntitle: "Fix #3 \\"now\\""  # trailing\ntype: task\nstatus: open\n---\n')
        self.write(repo, "work/tickets/02-b.md",
                   "---\nid: 02\ntitle: 'it''s #2'\ntype: task # c\nstatus: open\n---\n")
        self.write(repo, "work/tickets/03-c.md",
                   "---\nid: 03\ntitle: plain # comment\ntype: task\nstatus: open\n---\n")
        _, p = self.snap_project()
        titles = {t["id"]: t["title"] for t in p["tickets"]}
        self.assertEqual(titles, {"01": 'Fix #3 "now"', "02": "it's #2", "03": "plain"})
        self.assertEqual(p["tickets"][1]["type"], "task")

    def test_applied(self):
        self.init(tickets=[])
        repo = self.work / "demo"
        self.write(repo, "work/inbox/q01.md",
                   QUESTION.format(id="q01", ticket="", gate="no").replace(
                       "asked_at:", "applied: 2026-10-17T16:00:00Z\nasked_at:"))
        self.write(repo, "work/inbox/q02.md", QUESTION.format(id="q02", ticket="", gate="no"))
        _, p = self.snap_project()
        self.assertEqual([q["applied"] for q in p["questions"]], ["2026-10-17T16:00:00Z", None])


class PaneLastTests(NodeTestCase):
    def test_last_meaningful_line_skips_the_input_box(self):
        self.init()
        project = lambda: self.node("snapshot")["projects"][0]
        self.assertIsNone(project()["pane_last"])   # no lead running
        (self.state / "sess-demo").write_text("")
        (self.state / "pane-demo").write_text(
            "● Ticket 07: writer streams tiles\n"
            "✻ Writing zoom 15… (esc to interrupt)\n"
            "╭──────────────────────────╮\n"
            "│ >                        │\n"
            "╰──────────────────────────╯\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n")
        self.assertEqual(project()["pane_last"], "✻ Writing zoom 15… (esc to interrupt)")
        # current Claude Code: a ❯ prompt between plain rules
        (self.state / "pane-demo").write_text(
            "● Added the seeded check-digit test; 12 tests pass.\n"
            "──────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
            "  ◐ medium · /effort\n")
        self.assertEqual(project()["pane_last"], "● Added the seeded check-digit test; 12 tests pass.")

    def test_pane_text_never_carries_a_token(self):
        self.init()
        (self.state / "sess-demo").write_text("")
        (self.state / "pane-demo").write_text(f"● Bash(curl -H 'x-api-key: {TOKEN}')\n Do you want to send {TOKEN}?\n ❯ 1. Yes\n   2. No\n")
        out = [self.node("pane", "demo"), self.node("dialog", "demo"), self.node("snapshot")["projects"][0]]
        (self.state / "pane-demo").write_text(f"● export CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n")
        out.append(self.node("snapshot")["projects"][0])
        self.assertEqual(out[3]["pane_last"], "● export CLAUDE_CODE_OAUTH_TOKEN=***")
        self.assertEqual(out[2]["lead_note"], "Do you want to send ***?")
        self.assertNotIn("SECRETTOKENVALUE", json.dumps(out))

    def test_capture_failure_keeps_the_project(self):
        self.init()
        (self.state / "sess-demo").write_text("")
        bad = self.bindir / "tmux"
        real = bad.read_text()
        # has-session works, capture-pane hangs past the node's tmux timeout
        bad.write_text(real.replace('if cmd == "capture-pane":', 'if cmd == "capture-pane":\n    import time; time.sleep(20)'))
        out = self.node("snapshot")
        self.assertEqual([p["name"] for p in out["projects"]], ["demo"])
        self.assertIsNone(out["projects"][0]["pane_last"])


class LeadStateTests(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.env["TZ"] = "UTC"   # the node's zone, which the limit note names

    PANES = {
        "working": ("● Reading logic.js\n✻ Thinking… (12s · esc to interrupt)\n──────\n❯ \n──────\n"
                    "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n", None),
        "limit": ("● Done with ticket 03.\n  ⎿  5-hour limit reached ∙ resets 3pm\n     /upgrade to increase your usage limit.\n"
                  "──────\n❯ \n──────\n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n",
                  "⎿  5-hour limit reached ∙ resets 3pm (UTC)"),
        "dialog": ("● Bash(rm -rf build)\n Do you want to proceed?\n ❯ 1. Yes\n   2. No, and tell Claude what to do differently\n",
                   "Do you want to proceed?"),
        "prompt": ("● All tickets closed; status done.\n──────\n❯ \n──────\n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
                   "  ◐ medium · /effort\n", None),
        "unknown": ("compiling…\nstill compiling\n", None),
    }

    def test_states(self):
        self.init()
        project = lambda: self.node("snapshot")["projects"][0]
        self.assertEqual((project()["lead_state"], project()["lead_note"]), (None, None))   # no lead running
        (self.state / "sess-demo").write_text("")
        for state, (pane, note) in self.PANES.items():
            (self.state / "pane-demo").write_text(pane)
            p = project()
            self.assertEqual((p["lead_state"], p["lead_note"]), (state, note), state)

    def test_layouts_that_must_not_mislead(self):
        self.init()
        (self.state / "sess-demo").write_text("")
        box = "──────\n❯ \n──────\n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n  ◐ medium · /effort\n"
        cases = [
            # working with a todo list under the spinner and a two-line footer
            ("working", "● Writing the tests\n✶ Coalescing… (2m 3s · ↓ 4.1k tokens)\n  ⎿  ☒ board\n     ☐ tests\n     ☐ docs\n\n" + box),
            # the lead's own words: a question to the operator, an API rate limit
            ("prompt", "● I finished the board. Do you want to look at it before I start on the API?\n"
                       "  Added retry when the API says 429 (rate limit reached).\n" + box),
            # the lead's own words never read as a usage limit, which would mark the hub's token limited
            ("prompt", "● Done. I reset the rate limit counter; it resets at 3pm (UTC).\n" + box),
            ("prompt", "● Done.\n\n  The usage limit resets in 5 minutes,\n  you've hit your session limit · resets 3pm\n\n" + box),
        ]
        for state, pane in cases:
            (self.state / "pane-demo").write_text(pane)
            self.assertEqual(self.node("snapshot")["projects"][0]["lead_state"], state, state)

    MENU = ("● Four agents are active (42 security, 35 field app, 47 analytics, 43\n"
            "  admin/commercial). That is the concurrency cap, so there is nothing further to\n"
            "  request this round.\n"
            "4 background task updates waiting while this panel is open\n"
            "▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔\n"
            "   What do you want to do?\n"
            "   ❯ 1. Stop and wait for limit to reset\n"
            "     2. Wait here, then continue automatically at 11pm\n"
            "     3. Upgrade your plan\n"
            "   Enter to confirm · Esc to cancel\n")

    def test_limit_note_carries_the_reset_time(self):
        self.init()
        (self.state / "sess-demo").write_text("")
        cases = [
            (self.MENU, "2. Wait here, then continue automatically at 11pm (UTC)"),
            (self.MENU.replace("then continue automatically at 11pm", "then continue automatically"),
             "❯ 1. Stop and wait for limit to reset"),
            ("● Done.\n  ⎿  You've hit your session limit · resets 6pm (UTC)\n"
             "  ⚠ Usage limit reached · limit resets 6pm\n  Continuing automatically at 6pm · esc to cancel\n",
             "⎿  You've hit your session limit · resets 6pm (UTC)"),
            ("● Done.\n  ⎿  You've hit your session limit\n  Continuing automatically at 6pm · esc to cancel\n",
             "Continuing automatically at 6pm (UTC) · esc to cancel"),
            ("● Done.\n  ⎿  You've hit your session limit · resets in 2h 10m\n", "⎿  You've hit your session limit · resets in 2h 10m"),
        ]
        for pane, note in cases:
            (self.state / "pane-demo").write_text(pane)
            p = self.node("snapshot")["projects"][0]
            self.assertEqual((p["lead_state"], p["lead_note"]), ("limit", note), pane)

    # Claude Code 2.1.304: the agent list under the input box starts with "● main"
    AGENT_LIST = ("● Agent \"Build ticket 70 splicing completeness\" failed: Agent terminated early\n"
                  "due to an API error: You've hit your session limit · resets 2:20pm (UTC) (error\n"
                  "type rate_limit, HTTP 429, request id req_011CgFTRfsSHNEy2ZsUBm3yL, model sent\n"
                  "to the API: claude-opus-5-5)\n\n"
                  "────────────────────────────────────────\n❯\xa0\n────────────────────────────────────────\n"
                  "  ⚠ Usage limit reached · limit resets 2:20pm\n"
                  "    Continuing automatically at 2:20pm · esc to cancel\n"
                  "  ⏵⏵ bypass permissions on · 9 shells · ↓ to manage\n\n"
                  "  ● main\n"
                  "  ◯ general-purpose  Diagnosing trace ENOENT in c… 1h 55m 5s · ↓ 431.8k tokens\n")

    def test_agent_list_under_the_box_does_not_hide_a_limit(self):
        self.init()
        (self.state / "sess-demo").write_text("")
        (self.state / "pane-demo").write_text(self.AGENT_LIST)
        p = self.node("snapshot")["projects"][0]
        self.assertEqual((p["lead_state"], p["lead_note"]),
                         ("limit", "due to an API error: You've hit your session limit · resets 2:20pm (UTC) (error"))
        idle = self.AGENT_LIST.split("────")[0].replace("You've hit your session limit · resets 2:20pm (UTC) ", "") + \
            "──────\n❯ \n──────\n  ⏵⏵ bypass permissions on\n\n  ● main\n  ◯ general-purpose  Reading… 3m\n"
        (self.state / "pane-demo").write_text(idle)
        self.assertEqual(self.node("snapshot")["projects"][0]["lead_state"], "prompt")

    def test_lead_waiting_on_a_writing_agent_is_working(self):
        self.init()
        (self.state / "sess-demo").write_text("")
        (self.state / "pane-demo").write_text(self.PANES["prompt"][0])
        sid = "ef4c8444-40b1-4139-b6ef-75772fd4c314"
        (self.home / ".cairn" / "state").mkdir(parents=True, exist_ok=True)
        (self.home / ".cairn" / "state" / "demo.started").write_text(json.dumps({"session_id": sid}) + "\n")
        folder = re.sub(r"[^A-Za-z0-9]", "-", str(self.work / "demo"))
        agent = self.home / ".claude" / "projects" / folder / sid / "subagents" / "agent-a1.jsonl"
        agent.parent.mkdir(parents=True)
        agent.write_text("{}\n")
        project = lambda: self.node("snapshot")["projects"][0]
        self.assertEqual((project()["lead_state"], project()["lead_note"]), ("working", None))
        quiet = time.time() - 16 * 60   # the agent finished long ago: the lead really is stopped
        os.utime(agent, (quiet, quiet))
        self.assertEqual(project()["lead_state"], "prompt")
        other = agent.parent.parent.parent / "0ce4ad90-cb45-42c5-a651-b48cfa376c98" / "subagents" / "agent-b2.jsonl"
        other.parent.mkdir(parents=True)
        other.write_text("{}\n")   # an agent of another session (a chat or a review) does not count
        self.assertEqual(project()["lead_state"], "prompt")

    def test_limit_note_names_the_node_zone(self):
        self.init()
        (self.state / "sess-demo").write_text("")
        (self.state / "pane-demo").write_text(self.MENU)
        for tz, note in (("Europe/Lisbon", "2. Wait here, then continue automatically at 11pm (Europe/Lisbon)"),
                         (":America/New_York", "2. Wait here, then continue automatically at 11pm (America/New_York)"),
                         ("Etc/UTC", "2. Wait here, then continue automatically at 11pm (Etc/UTC)")):
            self.env["TZ"] = tz
            self.assertEqual(self.node("snapshot")["projects"][0]["lead_note"], note, tz)
        (self.state / "pane-demo").write_text("● Done.\n  ⎿  Usage limit reached · resets 6:30am (UTC)\n")
        self.env["TZ"] = "Europe/Lisbon"   # a zone already shown stays the only one
        self.assertEqual(self.node("snapshot")["projects"][0]["lead_note"], "⎿  Usage limit reached · resets 6:30am (UTC)")

    def test_capture_failure_is_null(self):
        self.init()
        (self.state / "sess-demo").write_text("")
        bad = self.bindir / "tmux"
        bad.write_text(bad.read_text().replace('if cmd == "capture-pane":', 'if cmd == "capture-pane":\n    import time; time.sleep(20)'))
        p = self.node("snapshot")["projects"][0]
        self.assertEqual((p["lead_state"], p["lead_note"]), (None, None))


RULE = "╌" * 82
REAL_DIALOG = (
    "● Bash(cd /tmp/claude-1000/x/scratchpad && mkdir -p t55-trace && cd t55-trace && rm -rf * && unzip …)\n"
    f"{RULE}\n"
    " Bash command\n"
    "\n"
    " │ cd /tmp/claude-1000/-home-agent-work-strand/ef4c8444-40b1-4139-b6ef-75772fd4\n"
    " │ c314/scratchpad && mkdir -p t55-trace && cd t55-trace && rm -rf * && unzip\n"
    " │ -qo ../t55/e2e/test-results/tools-vertex*/trace.zip && ls; python3 -I -\n"
    " │ <<'EOF'\n"
    " │ import json,glob\n"
    " │ EOF\n"
    f"{RULE}\n"
    " │ Dangerous rm operation on statically-unresolvable target:\n"
    " │ /home/agent/work/strand/*\n"
    "\n"
    " Do you want to proceed?\n"
    " ❯ 1. Yes\n"
    "   2. No\n"
    "\n"
    " Esc to cancel · Tab to amend · ctrl+x ctrl+k twice to stop background agents\n")


class DialogTests(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.init()
        (self.state / "sess-demo").write_text("")
        self.show(REAL_DIALOG)

    def show(self, pane):
        (self.state / "pane-demo").write_text(pane)

    def keys(self):
        return [c[3:] for c in self.tmux_calls() if c[2:3] == ["send-keys"]]

    def dialog(self):
        return self.node("dialog", "demo")["dialog"]

    def test_reads_the_real_dialog(self):
        d = self.dialog()
        self.assertEqual(d["lines"][0], " Bash command")
        self.assertEqual(d["lines"][2], " │ cd /tmp/claude-1000/-home-agent-work-strand/ef4c8444-40b1-4139-b6ef-75772fd4")
        self.assertIn(" │ Dangerous rm operation on statically-unresolvable target:", d["lines"])
        self.assertEqual(d["lines"][-1], " Esc to cancel · Tab to amend · ctrl+x ctrl+k twice to stop background agents")
        self.assertEqual(len(d["lines"]), 17)
        self.assertEqual(d["options"], [{"n": 1, "label": "Yes"}, {"n": 2, "label": "No"}])
        self.assertEqual(d["selected"], 1)
        self.assertEqual(d["fingerprint"], sha("\n".join(l for l in d["lines"] if l.strip())))

    def test_header_shapes_wrapped_labels_and_numbered_bodies(self):
        self.show("● Bash(rm -rf build)\n Do you want to proceed?\n ❯ 1. Yes\n   2. No, and tell Claude what to do differently\n")
        d = self.dialog()
        self.assertEqual(d["lines"], [" Do you want to proceed?", " ❯ 1. Yes", "   2. No, and tell Claude what to do differently"])
        self.show("● Here is the plan.\n  continued line of the message\n──────\n Plan\n  1. Install deps\n  2. Build\n\n"
                  " Would you like to proceed?\n   1. Yes, and auto-accept edits\n ❯ 2. No, keep planning and tell\n"
                  "      Claude what to change\n\n Esc to cancel\n")
        d = self.dialog()
        self.assertEqual(d["lines"][0], " Plan")
        self.assertEqual(d["options"], [{"n": 1, "label": "Yes, and auto-accept edits"},
                                        {"n": 2, "label": "No, keep planning and tell Claude what to change"}])
        self.assertEqual(d["selected"], 2)
        self.show("● Fetch(https://example.com)\n Do you want to allow Claude to fetch this content?\n  (no options drawn yet)\n")
        self.assertEqual((self.dialog()["options"], self.dialog()["selected"]), ([], None))

    def test_null_unless_the_lead_is_at_a_dialog(self):
        for state, (pane, _) in LeadStateTests.PANES.items():
            if state != "dialog":
                self.show(pane)
                self.assertIsNone(self.dialog(), state)
        (self.state / "sess-demo").unlink()
        self.assertEqual(self.node("dialog", "demo", ok=False)["error"], "no session demo")

    def test_stale_fingerprint_sends_nothing_and_returns_the_fresh_dialog(self):
        old = self.dialog()["fingerprint"]
        self.show(REAL_DIALOG.replace("strand/*", "strand/build/*"))
        out = self.node("dialog-answer", "demo", old, "2", ok=False)
        self.assertEqual(out["error"], "the dialog changed; read it again")
        self.assertIn(" │ /home/agent/work/strand/build/*", out["dialog"]["lines"])
        self.show(LeadStateTests.PANES["prompt"][0])
        out = self.node("dialog-answer", "demo", old, "2", ok=False)
        self.assertEqual((out["error"], out["dialog"]), ("the dialog changed; read it again", None))
        self.assertEqual(self.keys(), [])

    def test_answer_moves_the_marker_then_enters(self):
        (self.state / "after-demo").write_text(LeadStateTests.PANES["working"][0])
        out = self.node("dialog-answer", "demo", self.dialog()["fingerprint"], "2")
        self.assertEqual(out, {"answered": 2, "label": "No", "dialog": None})
        self.assertEqual(self.keys(), [["-t", "=demo:", "Down"], ["-t", "=demo:", "Enter"]])

    def test_answer_on_the_selected_option_only_enters(self):
        self.env["CAIRN_DIALOG_WAIT"] = "0.3"
        d = self.dialog()
        out = self.node("dialog-answer", "demo", d["fingerprint"], "1")
        self.assertEqual((out["answered"], out["label"], out["dialog"]), (1, "Yes", d))   # still showing: reported as is
        self.assertEqual(self.keys(), [["-t", "=demo:", "Enter"]])

    def test_auto_deny_countdown_is_the_same_dialog(self):
        countdown = " ⚠ Claude Code will automatically deny this request in %s, to avoid blocking progress on an unattended session\n"
        self.show(REAL_DIALOG.replace(" Do you want to proceed?", countdown % "1:26" + "\n Do you want to proceed?"))
        d = self.dialog()
        self.assertIn(countdown.rstrip("\n") % "1:26", d["lines"])
        self.show(REAL_DIALOG.replace(" Do you want to proceed?", countdown % "1:24" + "\n Do you want to proceed?"))
        (self.state / "after-demo").write_text(LeadStateTests.PANES["working"][0])
        out = self.node("dialog-answer", "demo", d["fingerprint"], "2")
        self.assertEqual((out["answered"], out["dialog"]), (2, None))
        self.assertEqual(self.keys(), [["-t", "=demo:", "Down"], ["-t", "=demo:", "Enter"]])

    def test_invalid_option_or_input_sends_nothing(self):
        fp = self.dialog()["fingerprint"]
        self.assertIn("not one of", self.node("dialog-answer", "demo", fp, "3", ok=False)["error"])
        self.node("dialog-answer", "demo", fp, "x", ok=False)
        self.node("dialog-answer", "demo", "abc", "1", ok=False)
        self.node("dialog-answer", "demo", fp, "1", "extra", ok=False)
        self.assertEqual(self.keys(), [])

    def test_marker_that_does_not_move_is_never_confirmed(self):
        self.env["FAKE_TMUX_KEYS_STUCK"] = "1"
        out = self.node("dialog-answer", "demo", self.dialog()["fingerprint"], "2", ok=False)
        self.assertIn("nothing was confirmed", out["error"])
        self.assertEqual(out["dialog"]["selected"], 1)
        self.assertEqual(self.keys(), [["-t", "=demo:", "Down"]])

    def test_a_different_dialog_after_the_move_is_never_confirmed(self):
        # same options, different text: someone answered in tmux and the next dialog came up mid-move
        bad = self.bindir / "tmux"
        bad.write_text(bad.read_text().replace('    if key == "Enter" and', "    if key == \"Down\":\n"
                       "        text = open(pane).read().replace(\"strand/*\", \"strand/other/*\")\n"
                       "        open(pane, \"w\").write(text)\n"
                       '    if key == "Enter" and'))
        out = self.node("dialog-answer", "demo", self.dialog()["fingerprint"], "2", ok=False)
        self.assertIn("nothing was confirmed", out["error"])
        self.assertIn(" │ /home/agent/work/strand/other/*", out["dialog"]["lines"])
        self.assertEqual(self.keys(), [["-t", "=demo:", "Down"]])

    def test_up_moves_the_marker_back(self):
        (self.state / "after-demo").write_text(LeadStateTests.PANES["working"][0])
        self.show(REAL_DIALOG.replace(" ❯ 1. Yes\n   2. No", "   1. Yes\n ❯ 2. No"))
        d = self.dialog()
        self.assertEqual(d["selected"], 2)
        self.assertEqual(self.node("dialog-answer", "demo", d["fingerprint"], "1")["label"], "Yes")
        self.assertEqual(self.keys(), [["-t", "=demo:", "Up"], ["-t", "=demo:", "Enter"]])

    def test_lead_that_ends_its_session_on_the_answer(self):
        self.env["FAKE_TMUX_ENTER_ENDS"] = "1"
        out = self.node("dialog-answer", "demo", self.dialog()["fingerprint"], "1")
        self.assertEqual((out["answered"], out["dialog"]), (1, None))

    def test_one_answer_at_a_time(self):
        lock = self.home / ".cairn" / "state" / "demo.dialog.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            out = self.node("dialog-answer", "demo", self.dialog()["fingerprint"], "2", ok=False)
        self.assertIn("being sent", out["error"])
        self.assertEqual(out["dialog"]["selected"], 1)
        self.assertEqual(self.keys(), [])

    def test_a_dialog_still_showing_after_its_answer_is_not_answered_twice(self):
        self.env["CAIRN_DIALOG_WAIT"] = "0.3"
        fp = self.dialog()["fingerprint"]
        self.node("dialog-answer", "demo", fp, "1")
        out = self.node("dialog-answer", "demo", fp, "1", ok=False)
        self.assertIn("answered moments ago", out["error"])
        self.assertEqual(self.keys(), [["-t", "=demo:", "Enter"]])

    def test_only_the_lead_session_gets_keys(self):
        (self.state / "sess-demo").unlink()
        (self.state / "sess-demo-review").write_text("")
        (self.state / "pane-demo-review").write_text(REAL_DIALOG)
        self.node("dialog", "demo", ok=False)
        self.node("dialog-answer", "demo", sha("x"), "1", ok=False)
        self.assertEqual(self.keys(), [])


class LeadVersionTests(NodeTestCase):
    def test_versions_and_relead(self):
        self.init(notes="Use stdlib.\n\nKeep it small.")
        repo = self.work / "demo"
        out = self.node("snapshot")
        self.assertEqual(out["lead_template_version"], 3)
        self.assertEqual(out["projects"][0]["lead_version"], 3)
        # a project from before milestone 7: no marker and no result rule
        old = (repo / "LEAD.md").read_text()
        old = old.replace("<!-- cairn LEAD.md v3 -->\n", "")
        old = "\n".join(l for l in old.splitlines() if "work/result.md" not in l) + "\n"
        (repo / "LEAD.md").write_text(old)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qam", "old lead"], cwd=repo, check=True)
        self.assertEqual(self.node("snapshot")["projects"][0]["lead_version"], 1)
        (repo / "scratch.txt").write_text("the lead's own work in progress")
        res = self.node("relead", "demo")
        self.assertEqual(res["version"], 3)
        new = (repo / "LEAD.md").read_text()
        self.assertIn("work/result.md", new)
        self.assertIn("Ship the demo.", new)
        self.assertIn("Use stdlib.\n\nKeep it small.", new)
        self.assertTrue(new.rstrip().endswith("<!-- cairn LEAD.md v3 -->"))
        files = subprocess.run(["git", "show", "--name-only", "--format=%s", "HEAD"], cwd=repo,
                               capture_output=True, text=True).stdout.split()
        self.assertEqual(files[:5], ["cairn:", "update", "LEAD.md", "to", "v3"])
        self.assertEqual(files[5:], ["LEAD.md"])
        self.assertEqual(self.node("snapshot")["projects"][0]["lead_version"], 3)
        (repo / "LEAD.md").write_text("# Lead\n\nno headings here\n")
        self.assertIn("Destination", self.node("relead", "demo", ok=False)["error"])

    def test_failed_relead_restores_lead_md(self):
        self.init()
        repo = self.work / "demo"
        old = (repo / "LEAD.md").read_text().replace("<!-- cairn LEAD.md v3 -->\n", "")
        (repo / "LEAD.md").write_text(old)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qam", "old lead"], cwd=repo, check=True)
        status = lambda: subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
        before = status()
        self.fail_commits()
        self.assertIn("commit refused", self.node("relead", "demo", ok=False)["error"])
        self.assertEqual((repo / "LEAD.md").read_text(), old)
        self.assertEqual(status(), before)

    def test_relead_keeps_notes_headings_and_lists_dropped_sections(self):
        self.init(notes="Use stdlib.\n\n## Data sources\n\nOnly the OSM extract.")
        repo = self.work / "demo"
        text = (repo / "LEAD.md").read_text().replace("<!-- cairn LEAD.md v3 -->\n", "")
        (repo / "LEAD.md").write_text(text + "\n## Lead log\n\nmy own notes\n")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qam", "lead edits"], cwd=repo, check=True)
        (repo / "notes.txt").write_text("staged by the lead")
        subprocess.run(["git", "add", "notes.txt"], cwd=repo, check=True)
        res = self.node("relead", "demo")
        self.assertEqual(res["dropped"], ["Lead log"])
        new = (repo / "LEAD.md").read_text()
        self.assertIn("## Data sources\n\nOnly the OSM extract.", new)
        files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=repo,
                               capture_output=True, text=True).stdout.split()
        self.assertEqual(files, ["LEAD.md"])   # the lead's staged file stays staged, out of this commit
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo, capture_output=True, text=True).stdout.split()
        self.assertEqual(staged, ["notes.txt"])


class UsageTests(NodeTestCase):
    def line(self, mid, model, ts, inp=1, out=10, cr=100, cw=5):
        return json.dumps({"type": "assistant", "timestamp": ts, "message": {
            "id": mid, "model": model, "usage": {"input_tokens": inp, "output_tokens": out,
                                                 "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}}})

    def test_usage(self):
        self.init()
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(self.work / "demo"))
        d = self.home / ".claude" / "projects" / slug
        (d / "s1" / "subagents").mkdir(parents=True)
        (d / "s1.jsonl").write_text("\n".join([
            json.dumps({"type": "user", "timestamp": "2026-10-17T10:00:00Z"}),
            self.line("m1", "claude-fable-5-1", "2026-10-17T10:00:10Z"),
            self.line("m1", "claude-fable-5-1", "2026-10-17T10:00:11Z"),          # same message, streamed twice
            self.line("m2", "claude-fable-5-1", "2026-10-17T10:02:00Z", out=30),
            self.line("m9", "<synthetic>", "2026-10-17T10:02:01Z"),
            "not json",
            self.line("m3", "claude-fable-5-1", "2026-10-17T11:00:00Z"),          # 58 min gap: not active time
        ]) + "\n")
        (d / "s1" / "subagents" / "agent-1.jsonl").write_text(
            self.line("m4", "claude-haiku-5-5", "2026-10-17T10:01:00Z", out=7) + "\n")
        (d / "s2.jsonl").write_text(self.line("m5", "claude-sonnet-5-5", "2026-10-18T09:00:00Z", out=5) + "\n")
        out = self.node("usage", "demo")
        self.assertEqual([m["model"] for m in out["models"]], ["claude-fable-5-1", "claude-haiku-5-5", "claude-sonnet-5-5"])
        fable = out["models"][0]
        self.assertEqual((fable["input"], fable["output"], fable["cache_read"], fable["cache_write"], fable["messages"]),
                         (3, 50, 300, 15, 3))
        self.assertEqual((out["sessions"], out["files"]), (2, 3))
        self.assertEqual((out["first_at"], out["last_at"]), ("2026-10-17T10:00:00Z", "2026-10-18T09:00:00Z"))
        self.assertEqual(out["active_minutes"], 2)
        self.assertTrue((self.home / ".cairn" / "state" / "usage-demo.json").exists())
        self.assertEqual(self.node("usage", "demo"), out)   # from the cache
        self.assertEqual(self.node("usage", "ghost", ok=False)["error"][:15], "unknown project")

    def test_symlinked_path_and_messages_without_id(self):
        real = self.root / "real"
        real.mkdir()
        (self.work / "demo").symlink_to(real, target_is_directory=True)
        self.init()
        d = self.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(real))   # Claude uses the real cwd
        d.mkdir(parents=True)
        no_id = lambda ts: json.dumps({"type": "assistant", "timestamp": ts, "message": {
            "model": "claude-opus-5-5", "usage": {"input_tokens": 1, "output_tokens": 2}}})
        (d / "a.jsonl").write_text(no_id("2026-10-17T10:00:00Z") + "\n")
        (d / "b.jsonl").write_text(no_id("2026-10-17T10:00:30Z") + "\n")
        out = self.node("usage", "demo")
        self.assertEqual([(m["model"], m["messages"], m["output"]) for m in out["models"]], [("claude-opus-5-5", 2, 4)])

    def test_bad_entries_files_and_cache_are_skipped(self):
        self.init()
        d = self.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.work / "demo"))
        d.mkdir(parents=True)
        bad = json.loads(self.line("m2", "claude-opus-5-5", "2026-10-17T10:00:30Z"))
        bad["message"]["usage"]["input_tokens"] = "lots"
        (d / "a.jsonl").write_text(self.line("m1", "claude-opus-5-5", "2026-10-17T10:00:00Z") + "\n" + json.dumps(bad) + "\n")
        (d / "gone.jsonl").symlink_to(d / "deleted.jsonl")   # listed, then gone before its stat
        want = self.node("usage", "demo")
        self.assertEqual([(m["model"], m["messages"]) for m in want["models"]], [("claude-opus-5-5", 1)])
        cache = self.home / ".cairn" / "state" / "usage-demo.json"
        entry = json.loads(cache.read_text())
        for broken in ([], {k: "x" for k in entry}, {k: dict(v, messages=["m1"]) for k, v in entry.items()},
                       {k: dict(v, messages={"m1": ["claude-opus-5-5", "1", 2, 3, 4]}) for k, v in entry.items()}):
            cache.write_text(json.dumps(broken))
            self.assertEqual(self.node("usage", "demo"), want, broken)

    def test_no_transcripts(self):
        self.init()
        out = self.node("usage", "demo")
        self.assertEqual((out["models"], out["sessions"], out["files"], out["first_at"], out["active_minutes"]),
                         ([], 0, 0, None, 0))


class ReadTests(NodeTestCase):
    def test_read_and_escape(self):
        self.init()
        out = self.node("read", "demo", "work/map.md")
        self.assertEqual(out["path"], "work/map.md")
        self.assertIn("Ship the demo.", out["content"])
        (self.work / "secret.txt").write_text("nope")
        for bad in ("../secret.txt", "work/../../secret.txt", str(self.work / "secret.txt"), "/etc/passwd"):
            self.assertIn("escapes", self.node("read", "demo", bad, ok=False)["error"])

    def test_symlink_escape_and_missing(self):
        self.init()
        (self.work / "secret.txt").write_text("nope")
        os.symlink(self.work / "secret.txt", self.work / "demo" / "link.txt")
        self.assertIn("escapes", self.node("read", "demo", "link.txt", ok=False)["error"])
        self.assertIn("not a file", self.node("read", "demo", "work/nothing.md", ok=False)["error"])
        self.assertIn("unknown project", self.node("read", "other", "x", ok=False)["error"])


class InitTests(NodeTestCase):
    def test_new(self):
        out = self.init()
        repo = self.work / "demo"
        self.assertEqual(out, {"project": "demo", "path": str(repo)})
        self.assertTrue((repo / ".git").is_dir())
        for d in ("tickets", "adr", "inbox"):
            self.assertTrue((repo / "work" / d).is_dir())
        self.assertEqual((repo / "work" / "status").read_text(), "")
        m = (repo / "work" / "map.md").read_text()
        for h in ("## Destination", "## Notes", "## Decisions so far", "## Not yet specified", "## Out of scope"):
            self.assertIn(h, m)
        lead = (repo / "LEAD.md").read_text()
        self.assertIn("Ship the demo.", lead)
        self.assertIn("Use stdlib.", lead)
        self.assertNotIn("{{", lead)
        self.assertEqual(json.loads((self.home / ".cairn" / "projects.json").read_text()),
                         {"demo": {"path": str(repo)}})
        snap = self.node("snapshot")["projects"][0]
        self.assertEqual(snap["tickets"][0]["title"], "First thing")
        self.assertEqual(snap["tickets"][0]["type"], "build")
        self.assertEqual(snap["tickets"][0]["state"], "frontier")
        self.assertEqual(snap["status"], "unknown")
        self.assertEqual([c["subject"] for c in snap["commits"]], ["cairn: init demo"])
        git = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True).stdout
        self.assertEqual(sorted(git("show", "--name-only", "--format=").split()),
                         ["LEAD.md", "work/adr/.gitkeep", "work/inbox/.gitkeep", "work/map.md",
                          "work/tickets/01-first-thing.md"])
        self.assertEqual(git("log", "-1", "--format=%an|%ae"), "cairn dashboard|dashboard@cairn.local\n")

    def test_files_the_node_creates_are_private(self):
        self.init()
        self.node("start", "demo", stdin="\n")
        for p in (".cairn", ".cairn/projects.json", ".cairn/state", ".cairn/state/demo.started"):
            self.assertEqual((self.home / p).stat().st_mode & 0o077, 0, p)

    def test_default_path(self):
        out = self.node("init", "dflt", stdin=json.dumps({"repo": {"kind": "new"}, "destination": "d"}))
        repo = self.home / "work" / "dflt"
        self.assertEqual(out, {"project": "dflt", "path": str(repo)})
        self.assertTrue((repo / "work" / "map.md").is_file())
        self.assertEqual(json.loads((self.home / ".cairn" / "projects.json").read_text())["dflt"],
                         {"path": str(repo)})
        # no tickets: tickets dir is kept via .gitkeep
        self.assertTrue((repo / "work" / "tickets" / ".gitkeep").is_file())

    def test_existing_commits_only_init_files(self):
        repo = self.work / "ex2"
        repo.mkdir()
        run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, env=self.env)
        run("init"); run("config", "user.name", "S"); run("config", "user.email", "s@e")
        (repo / "a.txt").write_text("a"); run("add", "a.txt"); run("commit", "-m", "base")
        (repo / "staged.txt").write_text("s"); run("add", "staged.txt")
        (repo / "loose.txt").write_text("l")
        self.init("ex2", path=str(repo), repo={"kind": "existing"}, tickets=[])
        files = run("show", "--name-only", "--format=", "HEAD").stdout.split()
        self.assertEqual(sorted(files), ["LEAD.md", "work/adr/.gitkeep", "work/inbox/.gitkeep",
                                         "work/map.md", "work/tickets/.gitkeep"])
        self.assertEqual(run("diff", "--cached", "--name-only").stdout.split(), ["staged.txt"])

    def failing_hook(self, hooks):
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\necho hook says no >&2\nexit 1\n")
        (hooks / "pre-commit").chmod(0o755)

    def test_failed_commit_on_existing_rolls_back_and_retries(self):
        repo = self.work / "ex3"
        repo.mkdir()
        run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, env=self.env)
        run("init"); run("config", "user.name", "S"); run("config", "user.email", "s@e")
        (repo / "a.txt").write_text("a"); run("add", "a.txt"); run("commit", "-m", "base")
        (repo / "staged.txt").write_text("s"); run("add", "staged.txt")
        (repo / "loose.txt").write_text("l")
        (repo / "LEAD.md").write_text("mine\n")
        status, head = run("status", "--short", "-uall").stdout, run("rev-parse", "HEAD").stdout
        self.fail_commits()
        spec = {"path": str(repo), "repo": {"kind": "existing"}, "destination": "d", "tickets": []}
        err = self.node("init", "ex3", stdin=json.dumps(spec), ok=False)
        self.assertIn("commit refused", err["error"])
        self.assertEqual(run("status", "--short", "-uall").stdout, status)
        self.assertEqual(run("rev-parse", "HEAD").stdout, head)
        self.assertEqual((repo / "LEAD.md").read_text(), "mine\n")
        self.assertFalse((repo / "work").exists())
        self.assertNotIn("ex3", self.node("snapshot")["projects"])
        del self.env["FAIL_COMMIT"]
        self.node("init", "ex3", stdin=json.dumps(spec))
        self.assertEqual(run("log", "-1", "--format=%s").stdout, "cairn: init ex3\n")
        self.assertEqual(run("diff", "--cached", "--name-only").stdout.split(), ["staged.txt"])

    def test_init_and_relead_skip_repo_hooks_and_signing(self):
        hooks = self.root / "hooks"
        self.failing_hook(hooks)
        (self.home / ".gitconfig").write_text(f"[core]\n\thooksPath = {hooks}\n[commit]\n\tgpgsign = true\n")
        self.init()
        self.node("relead", "demo")

    def test_failed_commit_on_new_removes_repo_and_keeps_an_older_registration(self):
        self.fail_commits()
        self.register("demo", self.work / "demo")
        self.register("other", self.work / "other")
        err = self.node("init", "demo", stdin=json.dumps(
            {"path": str(self.work / "demo"), "repo": {"kind": "new"}, "destination": "d"}), ok=False)
        self.assertIn("commit refused", err["error"])
        self.assertFalse((self.work / "demo").exists())
        # this init did not register demo, so its failure does not unregister it
        self.assertEqual(sorted(json.loads((self.home / ".cairn" / "projects.json").read_text())), ["demo", "other"])
        self.node("init", "fresh", stdin=json.dumps(
            {"path": str(self.work / "fresh"), "repo": {"kind": "new"}, "destination": "d"}), ok=False)
        self.assertEqual(sorted(json.loads((self.home / ".cairn" / "projects.json").read_text())), ["demo", "other"])
        del self.env["FAIL_COMMIT"]
        self.init()
        self.assertEqual(json.loads((self.home / ".cairn" / "projects.json").read_text())["demo"], {"path": str(self.work / "demo")})

    def test_existing_ok_and_refused_with_map(self):
        repo = self.work / "ex"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        self.init("ex", path=str(repo), repo={"kind": "existing"}, tickets=[])
        self.assertTrue((repo / "work" / "map.md").exists())
        err = self.node("init", "ex2", stdin=json.dumps(
            {"path": str(repo), "repo": {"kind": "existing"}, "destination": "d"}), ok=False)
        self.assertIn("work/map.md", err["error"])

    def test_init_that_fails_before_writing_keeps_the_index(self):
        repo = self.work / "ex"
        repo.mkdir()
        run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, env=self.env)
        run("init"); run("config", "user.name", "S"); run("config", "user.email", "s@e")
        (repo / "a.txt").write_text("a"); run("add", "a.txt"); run("commit", "-m", "base")
        (repo / "staged.txt").write_text("s"); run("add", "staged.txt")
        (repo / "work").symlink_to(self.root)
        spec = {"path": str(repo), "repo": {"kind": "existing"}, "destination": "d"}
        self.node("init", "ex", stdin=json.dumps(spec), ok=False)
        self.assertEqual(run("diff", "--cached", "--name-only").stdout.split(), ["staged.txt"])

    def test_init_never_writes_through_a_symlink(self):
        secret = self.root / "secret.txt"
        outside = self.root / "outside"
        outside.mkdir()
        repo = self.work / "ex"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        spec = {"path": str(repo), "repo": {"kind": "existing"}, "destination": "d", "tickets": []}
        links = (("LEAD.md", secret), ("work/status", secret), ("work/tickets/01-first-thing.md", secret), ("work", outside))
        for rel, target in links:
            shutil.rmtree(repo / "work", ignore_errors=True)
            (repo / "LEAD.md").unlink(missing_ok=True)
            secret.write_text("keep me\n")
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).symlink_to(target)
            out = self.node("init", "ex", stdin=json.dumps(dict(spec, tickets=[{"title": "First thing"}])), ok=False)
            self.assertIn("symlink", out["error"], rel)
            self.assertEqual(secret.read_text(), "keep me\n", rel)
            self.assertEqual(list(outside.iterdir()), [], rel)
            self.assertTrue((repo / rel).is_symlink(), rel)
        # a cloned repo that ships the link
        src = self.work / "src"
        src.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=src, check=True)
        (src / "LEAD.md").symlink_to(secret)
        subprocess.run(["git", "add", "-A"], cwd=src, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "c"], cwd=src, check=True)
        out = self.node("init", "cl", stdin=json.dumps({"path": str(self.work / "cl"), "repo": {"kind": "clone", "url": str(src)},
                                                        "destination": "d"}), ok=False)
        self.assertIn("symlink", out["error"])
        self.assertEqual(secret.read_text(), "keep me\n")
        self.assertFalse((self.work / "cl").exists())

    def test_existing_requires_git(self):
        (self.work / "plain").mkdir()
        err = self.node("init", "plain", stdin=json.dumps(
            {"path": str(self.work / "plain"), "repo": {"kind": "existing"}, "destination": "d"}), ok=False)
        self.assertIn("not a git repo", err["error"])

    def test_name_of_a_review_session_refused(self):
        self.init()
        err = self.node("init", "demo-review", stdin=json.dumps({"repo": {"kind": "new"}, "destination": "d"}), ok=False)
        self.assertIn("-review", err["error"])
        self.assertFalse((self.home / "work" / "demo-review").exists())
        self.assertEqual(list(json.loads((self.home / ".cairn" / "projects.json").read_text())), ["demo"])

    def test_bad_inputs(self):
        self.node("init", "Bad Name", stdin="{}", ok=False)
        self.node("init", "demo", stdin="not json", ok=False)
        self.node("init", "demo", stdin=json.dumps({"path": "rel", "destination": "d"}), ok=False)
        self.node("init", "demo", stdin=json.dumps(
            {"path": str(self.work / "x"), "destination": "d",
             "tickets": [{"title": "t", "type": "bogus"}]}), ok=False)
        self.assertFalse((self.work / "x").exists())

    def test_quoted_titles_round_trip(self):
        title = 'Fix #3 regression "now" in café'
        self.init(tickets=[{"title": title, "type": "task", "question": "q"}])
        f = next((self.work / "demo" / "work" / "tickets").glob("01-*.md"))
        self.assertIn('title: "Fix #3 regression \\"now\\" in café"', f.read_text())
        self.assertEqual(self.node("snapshot")["projects"][0]["tickets"][0]["title"], title)

    def test_git_identity(self):
        self.init()
        git = lambda key, repo: subprocess.run(["git", "config", "--local", "--get", key], cwd=repo,
                                               capture_output=True, text=True, env=self.env).stdout.strip()
        repo = self.work / "demo"
        self.assertEqual((git("user.name", repo), git("user.email", repo)),
                         ("cairn lead", "lead@cairn.local"))
        (self.home / ".gitconfig").write_text("[user]\n\tname = Jane Doe\n\temail = jane@example.com\n")
        self.init("other", tickets=[])
        self.assertEqual((git("user.name", self.work / "other"), git("user.email", self.work / "other")),
                         ("", ""))

    def test_clone_url_is_not_an_option(self):
        marker = self.work / "pwned"
        err = self.node("init", "cl", stdin=json.dumps(
            {"path": str(self.work / "cl"), "repo": {"kind": "clone", "url": f"--upload-pack=touch {marker}"},
             "destination": "d"}), ok=False)
        self.assertIn("git clone failed", err["error"])
        self.assertFalse(marker.exists())
        clone = [a for a in self.subprocess_argvs() if a[:2] == ["git", "clone"]][0]
        self.assertEqual(clone[2], "--")

    def test_init_that_finishes_during_a_clone_keeps_its_registration(self):
        src = self.work / "src"
        src.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=src, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty", "-m", "c"], cwd=src, check=True)
        self.init("first", tickets=[])
        d = self.root / "clonebin"
        d.mkdir()
        # another init registers its project while this one clones
        (d / "git").write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                               "if 'clone' in sys.argv:\n"
                               "    p = os.environ['PROJECTS_JSON']; data = json.load(open(p))\n"
                               "    data['other'] = {'path': '/srv/other'}; json.dump(data, open(p, 'w'))\n"
                               "os.execv(os.environ['REAL_GIT'], ['git', *sys.argv[1:]])\n")
        (d / "git").chmod(0o755)
        self.env.update(REAL_GIT=shutil.which("git"), PROJECTS_JSON=str(self.home / ".cairn" / "projects.json"),
                        PATH=f"{d}{os.pathsep}{self.env['PATH']}")
        self.init("cl", repo={"kind": "clone", "url": str(src)}, tickets=[])
        self.assertEqual(sorted(json.loads((self.home / ".cairn" / "projects.json").read_text())), ["cl", "first", "other"])
        self.assertEqual([p.name for p in (self.home / ".cairn").iterdir() if "tmp" in p.name], [])

    def test_clone(self):
        src = self.work / "src"
        src.mkdir()
        env = dict(self.env, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "init", "-q"], cwd=src, check=True)
        (src / "a.txt").write_text("a")
        subprocess.run(["git", "add", "."], cwd=src, check=True)
        subprocess.run(["git", "commit", "-qm", "c"], cwd=src, check=True, env=env)
        self.init("cl", repo={"kind": "clone", "url": str(src)}, tickets=[])
        self.assertTrue((self.work / "cl" / "a.txt").exists())
        self.assertTrue((self.work / "cl" / "work" / "map.md").exists())


class SessionTests(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.init()
        self.repo = self.work / "demo"
        self.claude = str(self.bindir / "claude")

    def pane_command(self, call):
        """The argv tmux runs in the pane: /bin/sh -c PANE_SH cairn-pane FIFO SUBAGENT_MODEL claude ..."""
        cmd = call[call.index("--") + 1:]
        self.assertEqual(cmd[:2], ["/bin/sh", "-c"])
        self.assertEqual(cmd[3], "cairn-pane")
        return cmd

    def assert_token_nowhere(self):
        for argv in self.subprocess_argvs() + self.tmux_calls() + [c["argv"] for c in self.claude_calls()]:
            self.assertFalse(any(TOKEN in a for a in argv), argv)
        self.assertIsNone(self.disk_has_token())
        run = self.home / ".cairn" / "run"
        self.assertEqual(list(run.iterdir()), [])
        self.assertEqual(run.stat().st_mode & 0o777, 0o700)

    def test_session_id_then_resume(self):
        out = self.node("start", "demo", stdin=TOKEN + "\n")
        sid = out["session_id"]
        self.assertEqual(out, {"session": "demo", "resumed": False, "session_id": sid})
        uuid.UUID(sid)
        marker = json.loads((self.home / ".cairn" / "state" / "demo.started").read_text())
        self.assertEqual(marker["session_id"], sid)
        call = self.new_session_calls()[0]
        self.assertEqual(call[:9], ["-L", "cairn", "new-session", "-d", "-s", "demo",
                                    "-c", str(self.repo), "--"])
        cmd = self.pane_command(call)
        self.assertEqual(cmd[5], "")
        self.assertEqual(cmd[6:], [self.claude, "--dangerously-skip-permissions",
                                   "--session-id", sid, "Read LEAD.md and work the map."])
        first = self.claude_calls()[0]
        self.assertEqual((first["cwd"], first["token_sha"]), (str(self.repo), sha(TOKEN)))
        self.node("start", "demo", stdin=TOKEN + "\n", ok=False)
        self.assertEqual(self.node("stop", "demo"), {"stopped": True})
        self.assertEqual(self.node("stop", "demo"), {"stopped": False})
        out = self.node("start", "demo", "--model", "opus", stdin=TOKEN + "\n")
        self.assertEqual(out, {"session": "demo", "resumed": True, "session_id": sid})
        self.assertEqual(self.claude_calls()[1]["argv"], [
            "--dangerously-skip-permissions", "--model", "opus", "--resume", sid,
            "Read new answer files in work/inbox and check work/tickets for open tickets added while you were away (the dashboard can add them), then carry on per LEAD.md."])
        for c in self.claude_calls():
            self.assertNotIn("--continue", c["argv"])
        self.assert_token_nowhere()

    def test_start_without_the_saved_conversation_starts_fresh(self):
        sid = self.node("start", "demo", stdin=TOKEN + "\n")["session_id"]
        self.node("stop", "demo")
        (self.home / ".claude" / "projects" / "-fake" / f"{sid}.jsonl").unlink()   # Claude Code cleaned it up
        out = self.node("start", "demo", stdin=TOKEN + "\n")
        self.assertFalse(out["resumed"])
        self.assertNotEqual(out["session_id"], sid)
        self.assertEqual(self.claude_calls()[1]["argv"][1:], ["--session-id", out["session_id"], "Read LEAD.md and work the map."])
        marker = json.loads((self.home / ".cairn" / "state" / "demo.started").read_text())
        self.assertEqual(marker["session_id"], out["session_id"])

    def test_fifo_delivery_keeps_token_out_of_argv_and_disk(self):
        self.node("start", "demo", stdin=TOKEN + "\n")
        self.node("review", "demo", stdin=TOKEN + "\n")
        self.assertEqual([c["token_sha"] for c in self.claude_calls()], [sha(TOKEN)] * 2)
        self.assertTrue(any(a[0] == "tmux" for a in self.subprocess_argvs()))
        for call in self.new_session_calls():
            self.assertNotIn("-e", call[:call.index("--")])
        self.assert_token_nowhere()

    def test_models(self):
        self.env["CLAUDE_CODE_SUBAGENT_MODEL"] = "inherited"
        self.node("start", "demo", "--model", "fable", "--subagent-model", "haiku", stdin=TOKEN + "\n")
        first = self.claude_calls()[0]
        self.assertEqual(first["argv"][1:3], ["--model", "fable"])
        self.assertEqual(first["subagent_model"], "haiku")
        self.node("stop", "demo")
        self.node("start", "demo", stdin=TOKEN + "\n")
        again = self.claude_calls()[1]
        self.assertNotIn("--model", again["argv"])
        self.assertIsNone(again["subagent_model"])   # the node's own value never leaks in
        self.node("review", "demo", stdin=TOKEN + "\n")
        self.assertIsNone(self.claude_calls()[2]["subagent_model"])
        self.assert_token_nowhere()

    def test_unknown_model_refused(self):
        for flags in (["--model", "gpt-5"], ["--subagent-model", "claude-opus-5-5"], ["--model"],
                      ["--subagent-model", ""]):
            out = self.node("start", "demo", *flags, stdin=TOKEN + "\n", ok=False)
            self.assertTrue("model" in out["error"] or "usage" in out["error"], out)
        self.assertEqual(self.new_session_calls(), [])

    def test_deliberate_stop_is_marked_until_the_next_start(self):
        self.node("start", "demo", stdin=TOKEN + "\n")
        project = lambda: self.node("snapshot")["projects"][0]
        self.assertEqual((project()["lead"], project()["lead_stopped"]), ("alive", False))
        self.node("stop", "demo")
        self.assertEqual((project()["lead"], project()["lead_stopped"]), ("dead", True))
        self.node("start", "demo", stdin=TOKEN + "\n")   # start clears the mark
        self.assertFalse(project()["lead_stopped"])
        self.kill_panes()
        (self.state / "sess-demo").unlink()   # the lead ended its own session (kill-session): not a deliberate stop
        self.assertEqual((project()["lead"], project()["lead_stopped"]), ("dead", False))

    def slow_tmux(self, command):
        fake = self.bindir / "tmux"
        fake.write_text(fake.read_text().replace(f'if cmd == "{command}":', f'if cmd == "{command}":\n    import time; time.sleep(20)'))
        self.env["CAIRN_TMUX_TIMEOUT"] = "1"

    def test_tmux_failure_before_the_token_kills_the_session(self):
        self.slow_tmux("set-option")
        self.assertIn("timed out", self.node("start", "demo", stdin=TOKEN + "\n", ok=False, timeout=30)["error"])
        self.assertFalse((self.state / "sess-demo").exists())
        self.assertFalse((self.home / ".cairn" / "state" / "demo.started").exists())
        self.assert_token_nowhere()

    def test_start_that_cannot_watch_its_lead_keeps_the_marker(self):
        self.slow_tmux("capture-pane")
        self.node("start", "demo", stdin=TOKEN + "\n", ok=False, timeout=30)
        self.assertTrue((self.state / "sess-demo").exists())   # the lead runs, so its session id is kept
        self.assertTrue(json.loads((self.home / ".cairn" / "state" / "demo.started").read_text())["session_id"])
        sets = [c[-1] for c in self.tmux_calls() if c[2:3] == ["set-option"]]
        self.assertEqual(sets, ["on", "off"])   # once it exits, its session goes

    def test_failed_resume_keeps_the_previous_marker(self):
        self.node("start", "demo", stdin=TOKEN + "\n")
        self.node("stop", "demo")
        before = (self.home / ".cairn" / "state" / "demo.started").read_text()
        self.env["FAKE_CLAUDE_MODE"] = "die"
        self.node("start", "demo", stdin=TOKEN + "\n", ok=False)
        self.assertEqual((self.home / ".cairn" / "state" / "demo.started").read_text(), before)

    def test_lead_that_ends_itself_during_stop_is_not_a_failure(self):
        self.node("start", "demo", stdin=TOKEN + "\n")
        fake = self.bindir / "tmux"
        # the session is gone by the time kill-session runs
        fake.write_text(fake.read_text().replace('if cmd == "kill-session":',
                                                 'if cmd == "kill-session":\n    os.remove(path("sess", name)); sys.exit(1)'))
        self.assertEqual(self.node("stop", "demo"), {"stopped": False})

    def test_tmux_that_does_not_answer_is_unknown_not_dead(self):
        self.node("start", "demo", stdin=TOKEN + "\n")
        self.node("stop", "demo")
        fake = self.bindir / "tmux"
        fake.write_text(fake.read_text().replace('if cmd == "has-session":', 'if cmd == "has-session":\n    import time; time.sleep(20)'))
        p = self.node("snapshot", timeout=30)["projects"][0]
        self.assertEqual((p["lead"], p["lead_state"]), ("unknown", None))

    def test_node_login_unsets_inherited_token(self):
        # e.g. the review skill's restart from a session that has its own token
        self.env["CLAUDE_CODE_OAUTH_TOKEN"] = "sk-ant-inherited"
        self.node("start", "demo", stdin="\n")
        self.assertIsNone(self.claude_calls()[0]["token_sha"])

    def test_claude_json_merge_keeps_keys(self):
        cfg = self.home / ".claude.json"
        cfg.write_text(json.dumps({"theme": "dark", "numStartups": 7,
                                   "projects": {"/other": {"x": 1},
                                                str(self.repo): {"allowedTools": ["Bash"]}}}))
        self.node("start", "demo", stdin="\n")
        data = json.loads(cfg.read_text())
        self.assertEqual((data["theme"], data["numStartups"]), ("dark", 7))
        self.assertTrue(data["hasCompletedOnboarding"])
        self.assertTrue(data["bypassPermissionsModeAccepted"])
        self.assertEqual(data["projects"]["/other"], {"x": 1})
        self.assertEqual(data["projects"][str(self.repo)],
                         {"allowedTools": ["Bash"], "hasTrustDialogAccepted": True})

    def test_claude_json_created_private_and_bad_json_refused(self):
        self.node("review", "demo", stdin="\n")
        cfg = self.home / ".claude.json"
        self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)
        self.assertTrue(json.loads(cfg.read_text())["projects"][str(self.repo)]["hasTrustDialogAccepted"])
        cfg.write_text("{broken")
        self.assertIn(".claude.json", self.node("start", "demo", stdin="\n", ok=False)["error"])
        self.assertEqual(cfg.read_text(), "{broken")
        self.assertEqual(self.new_session_calls()[1:], [])

    def test_liveness_failure_reports_pane_lines(self):
        self.env["FAKE_CLAUDE_MODE"] = "die"
        err = self.node("start", "demo", stdin=TOKEN + "\n", ok=False)["error"]
        self.assertIn("exited right after start", err)
        self.assertIn("Invalid API key - Please run /login", err)
        self.assertNotIn(TOKEN, err)
        self.assertFalse((self.state / "sess-demo").exists())
        self.assertFalse((self.home / ".cairn" / "state" / "demo.started").exists())
        sets = [c for c in self.tmux_calls() if c[2:3] == ["set-option"]]
        self.assertEqual(sets[0][-2:], ["remain-on-exit", "on"])
        self.assert_token_nowhere()

    def run_failing(self, *args):
        r = subprocess.run([sys.executable, str(NODE), *args], input=TOKEN + "\n",
                           capture_output=True, text=True, env=self.env)
        self.assertNotEqual(r.returncode, 0)
        return json.loads(r.stdout)

    def test_not_logged_in_fails_start_and_kills_session(self):
        # claude without a login stays alive at its prompt; only the pane says so
        self.env["FAKE_CLAUDE_MODE"] = "nologin"
        out = self.run_failing("start", "demo")
        self.assertIn("no usable login: Not logged in \u00b7 Please run /login", out["error"])
        self.assertIn("Not logged in \u00b7 Please run /login", out["pane"])
        self.assertFalse((self.state / "sess-demo").exists())
        self.assertEqual([c[-1] for c in self.tmux_calls() if c[2:3] == ["kill-session"]], ["=demo"])
        self.assertFalse((self.home / ".cairn" / "state" / "demo.started").exists())
        self.assert_token_nowhere()

    def test_not_logged_in_fails_review(self):
        self.env["FAKE_CLAUDE_MODE"] = "nologin"
        out = self.run_failing("review", "demo")
        self.assertIn("no usable login", out["error"])
        self.assertTrue(out["pane"])
        self.assertFalse((self.state / "sess-demo-review").exists())

    def test_exit_failure_returns_pane_lines(self):
        self.env["FAKE_CLAUDE_MODE"] = "die"
        self.assertIn("Invalid API key - Please run /login", self.run_failing("start", "demo")["pane"])

    def test_healthy_start_waits_out_the_window(self):
        self.env["CAIRN_START_WAIT"] = "1.2"
        t0 = time.monotonic()
        self.node("start", "demo", stdin="\n")
        self.assertGreaterEqual(time.monotonic() - t0, 1.2)
        self.assertTrue((self.state / "sess-demo").exists())

    def test_late_exit_inside_the_window_fails_start(self):
        self.env["CAIRN_START_WAIT"] = "3"
        self.env["FAKE_CLAUDE_MODE"] = "late-die"
        out = self.run_failing("start", "demo")
        self.assertIn("exited right after start", out["error"])
        self.assertIn("late failure", out["pane"])

    def test_live_session_turns_remain_on_exit_off(self):
        self.node("start", "demo", stdin="\n")
        sets = [c[-1] for c in self.tmux_calls() if c[2:3] == ["set-option"]]
        self.assertEqual(sets, ["on", "off"])

    def test_pane_never_reads_token(self):
        self.env["FAKE_TMUX_NORUN"] = "1"
        self.env["CAIRN_FIFO_TIMEOUT"] = "1"
        err = self.node("start", "demo", stdin=TOKEN + "\n", ok=False)["error"]
        self.assertIn("did not read its token", err)
        self.assertFalse((self.state / "sess-demo").exists())
        self.assert_token_nowhere()

    def test_tmux_too_old(self):
        self.env["FAKE_TMUX_VERSION"] = "3.1c"
        err = self.node("start", "demo", stdin="\n", ok=False)["error"]
        self.assertIn("tmux 3.2 or later is required, found 3.1", err)
        self.assertEqual(self.new_session_calls(), [])

    def test_claude_from_local_bin(self):
        (self.bindir / "claude").rename(self.root / "claude")
        local = self.home / ".local" / "bin"
        local.mkdir(parents=True)
        (self.root / "claude").rename(local / "claude")
        self.env["PATH"] = f"{self.bindir}{os.pathsep}/usr/bin{os.pathsep}/bin"
        self.node("start", "demo", stdin="\n")
        self.assertEqual(self.pane_command(self.new_session_calls()[0])[6], str(local / "claude"))
        self.assertEqual(self.node("snapshot")["deps"]["claude"], "2.1.296")

    def test_claude_missing(self):
        (self.bindir / "claude").unlink()
        self.env["PATH"] = f"{self.bindir}{os.pathsep}/usr/bin{os.pathsep}/bin"
        self.assertIn("claude not found", self.node("start", "demo", stdin="\n", ok=False)["error"])

    def test_failed_start_writes_no_marker(self):
        (self.state / "sess-demo").touch()
        self.node("start", "demo", stdin="\n", ok=False)
        self.assertFalse((self.home / ".cairn" / "state" / "demo.started").exists())

    def test_token_not_in_output_or_unknown_project(self):
        r = subprocess.run([sys.executable, str(NODE), "start", "nope"], input=TOKEN + "\n",
                           capture_output=True, text=True, env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        r = subprocess.run([sys.executable, str(NODE), "start", "demo"], input=TOKEN + "\n",
                           capture_output=True, text=True, env=self.env)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)

    def test_lead_that_ends_its_own_session_quickly_is_not_a_failure(self):
        self.env["FAKE_CLAUDE_MODE"] = "selfend"
        self.env["CAIRN_START_WAIT"] = "2"
        out = self.node("start", "demo", stdin=TOKEN + "\n")
        self.assertFalse(out["resumed"])
        self.assertFalse((self.state / "sess-demo").exists())

    def test_review(self):
        out = self.node("review", "demo", stdin=TOKEN + "\n")
        self.assertEqual(out, {"session": "demo-review",
                               "attach": "tmux -L cairn attach -t demo-review"})
        call = self.new_session_calls()[0]
        self.assertEqual(call[:9], ["-L", "cairn", "new-session", "-d", "-s", "demo-review",
                                    "-c", str(self.repo), "--"])
        cmd = self.pane_command(call)
        self.assertEqual(cmd[5:8], ["", self.claude, "--dangerously-skip-permissions"])
        self.assertEqual((cmd[8], cmd[10:]), ("--session-id", ["/cairn-review"]))
        first_sid = cmd[9]
        uuid.UUID(first_sid)
        # already alive: no second session
        again = self.node("review", "demo", stdin=TOKEN + "\n")
        self.assertEqual(again["session"], "demo-review")
        self.assertEqual(len(self.new_session_calls()), 1)
        # after the skill ends its session, the next review is fresh
        (self.state / "sess-demo-review").unlink()
        self.node("review", "demo", stdin=TOKEN + "\n")
        self.assertNotEqual(self.pane_command(self.new_session_calls()[1])[8], first_sid)
        # review does not leave a lead marker
        self.assertFalse((self.home / ".cairn" / "state" / "demo.started").exists())
        self.assert_token_nowhere()

    def test_pane(self):
        self.node("pane", "demo", ok=False)
        (self.state / "sess-demo").touch()
        lines = self.node("pane", "demo")["lines"]
        self.assertEqual(len(lines), 60)
        self.assertEqual(lines[-1], "line 79")
        capture = [c for c in self.tmux_calls() if c[2:3] == ["capture-pane"]][0]
        self.assertIn("-p", capture)
        self.assertEqual(capture[capture.index("-S") + 1], "-60")
        self.node("pane", "demo", "--review", ok=False)
        (self.state / "sess-demo-review").touch()
        self.assertEqual(len(self.node("pane", "demo", "--review")["lines"]), 60)


class LoginPaneTests(NodeTestCase):
    """The start watch against panes captured from a real Claude Code (fixtures/login-panes, see each file's "how")."""

    def setUp(self):
        super().setUp()
        self.init()

    def frames(self):
        for f in sorted(LOGIN_PANES.glob("*.json")):
            case = json.loads(f.read_text(encoding="utf-8"))
            for frame in case["frames"]:
                yield f.stem, case, frame

    def start_on(self, case, frame):
        """`cairn-node start` with a claude that draws the frame; a "continue" frame is a resumed start."""
        for p in self.state.glob("sess-*"):
            self.kill_panes()
            p.unlink()
        marker = self.home / ".cairn" / "state" / "demo.started"
        marker.unlink(missing_ok=True)
        if case["prompt"] == "continue":
            sid = str(uuid.uuid4())
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"session_id": sid}))
            saved = self.home / ".claude" / "projects" / "-fake"
            saved.mkdir(parents=True, exist_ok=True)
            (saved / (sid + ".jsonl")).touch()
        pane = self.root / "pane.txt"
        pane.write_text("\n".join(frame["lines"]) + "\n", encoding="utf-8")
        self.env["FAKE_CLAUDE_PANE"] = str(pane)
        r = subprocess.run([sys.executable, str(NODE), "start", "demo"], input="\n", capture_output=True, text=True,
                           env=self.env)
        out = json.loads(r.stdout)
        self.assertEqual(out.get("resumed", case["prompt"] == "continue"), case["prompt"] == "continue", out)
        return r.returncode, out

    def test_real_failures_are_caught_and_replayed_history_is_not(self):
        cases = list(self.frames())
        self.assertEqual(len(cases), 12)
        for name, case, frame in cases:
            with self.subTest(fixture=name, t=frame["t"]):
                code, out = self.start_on(case, frame)
                if frame["expect"] == "failure":
                    self.assertEqual(code, 1, out)
                    self.assertIn("no usable login", out["error"])
                else:
                    self.assertEqual(code, 0, out)
                    self.assertTrue((self.state / "sess-demo").exists())

    def test_the_matcher_on_every_frame(self):
        """The same verdicts without tmux timing: what login_failure finds in each frame as captured."""
        loader = importlib.machinery.SourceFileLoader("cairn_node_frames", str(NODE))
        node = types.ModuleType(loader.name)
        loader.exec_module(node)
        for name, case, frame in self.frames():
            prompt = node.CONTINUE_PROMPT if case["prompt"] == "continue" else node.START_PROMPT
            with self.subTest(fixture=name, t=frame["t"]):
                found = node.login_failure(frame["lines"], prompt)
                self.assertEqual(found is not None, frame["expect"] == "failure", found)

    def test_fixtures_say_where_they_come_from(self):
        for f in LOGIN_PANES.glob("*.json"):
            case = json.loads(f.read_text(encoding="utf-8"))
            self.assertRegex(case["claude_code"], r"^\d+\.\d+\.\d+$")
            self.assertEqual(case["synthetic"], "SYNTHETIC" in f.name)
            self.assertIn(case["prompt"], ("start", "continue"))


class TrustTests(unittest.TestCase):
    def test_a_write_by_claude_during_the_merge_is_kept(self):
        loader = importlib.machinery.SourceFileLoader("cairn_node", str(NODE))
        node = types.ModuleType(loader.name)
        loader.exec_module(node)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name) / ".claude.json"
        cfg.write_text(json.dumps({"theme": "dark"}))
        real_open, raced = os.open, []

        def racing_open(p, *args, **kwargs):
            if ".claude.json.cairn-" in str(p) and not raced:   # claude saves while the node writes its copy
                raced.append(1)
                cfg.write_text(json.dumps(dict(json.loads(cfg.read_text()), numStartups=8)))
            return real_open(p, *args, **kwargs)

        with mock.patch.dict(os.environ, {"HOME": tmp.name}), mock.patch("os.open", racing_open):
            node.trust_project(Path(tmp.name) / "repo")
        data = json.loads(cfg.read_text())
        self.assertEqual(raced, [1])
        self.assertEqual((data["theme"], data["numStartups"]), ("dark", 8))
        self.assertTrue(data["projects"][str(Path(tmp.name) / "repo")]["hasTrustDialogAccepted"])


class ChatTests(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.init()
        self.repo = self.work / "demo"
        self.claude = str(self.bindir / "claude")

    def chat(self, *args, stdin):
        r = subprocess.run([sys.executable, "-c", AUDIT_HARNESS, str(NODE), "chat", "demo", *args],
                           input=stdin, capture_output=True, text=True, env=self.env)
        return r.returncode, [json.loads(l) for l in r.stdout.splitlines()]

    def test_fresh_chat_passes_the_rest_of_stdin_to_claude(self):
        msg = json.dumps({"type": "user", "message": {"role": "user", "content": "/cairn-review chat"}})
        code, lines = self.chat(stdin=TOKEN + "\n" + msg + "\n")
        self.assertEqual(code, 0)
        head = lines[0]
        self.assertEqual((head["type"], head["resumed"]), ("cairn", False))
        sid = head["session_id"]
        uuid.UUID(sid)
        self.assertEqual(lines[1:], [{"type": "echo", "line": msg}])
        call = self.claude_calls()[0]
        self.assertEqual(call["argv"], ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                                        "--verbose", "--dangerously-skip-permissions", "--session-id", sid])
        self.assertEqual(call["cwd"], str(self.repo))
        self.assertEqual(call["token_sha"], sha(TOKEN))
        stored = json.loads((self.home / ".cairn" / "state" / "demo.review.json").read_text())
        self.assertEqual(stored["session_id"], sid)
        for argv in self.subprocess_argvs():
            self.assertFalse(any(TOKEN in a for a in argv), argv)
        self.assertIsNone(self.disk_has_token())
        trust = json.loads((self.home / ".claude.json").read_text())
        self.assertTrue(trust["projects"][str(self.repo)]["hasTrustDialogAccepted"])

    def saved(self, sid):
        d = self.home / ".claude" / "projects" / "-x"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.jsonl").write_text("{}\n")

    def test_resume_uses_the_stored_session(self):
        _, first = self.chat(stdin=TOKEN + "\n")
        sid = first[0]["session_id"]
        # claude never saved the conversation (it failed at once): resume starts fresh
        _, unsaved = self.chat("--resume", stdin=TOKEN + "\n")
        self.assertFalse(unsaved[0]["resumed"])
        sid = unsaved[0]["session_id"]
        self.saved(sid)
        _, again = self.chat("--resume", stdin=TOKEN + "\n")
        self.assertEqual(again[0], {"type": "cairn", "session_id": sid, "resumed": True})
        self.assertEqual(self.claude_calls()[2]["argv"][-2:], ["--resume", sid])
        # a plain chat starts a new conversation and becomes the one to resume
        _, fresh = self.chat(stdin=TOKEN + "\n")
        self.assertNotEqual(fresh[0]["session_id"], sid)
        self.saved(fresh[0]["session_id"])
        _, resumed = self.chat("--resume", stdin=TOKEN + "\n")
        self.assertEqual(resumed[0]["session_id"], fresh[0]["session_id"])

    def test_chat_models(self):
        self.env["CLAUDE_CODE_SUBAGENT_MODEL"] = "inherited"
        code, _ = self.chat("--model", "sonnet", "--subagent-model", "haiku", stdin=TOKEN + "\n")
        self.assertEqual(code, 0)
        call = self.claude_calls()[0]
        self.assertEqual(call["argv"][-4:-2], ["--model", "sonnet"])
        self.assertEqual(call["subagent_model"], "haiku")
        self.chat(stdin=TOKEN + "\n")
        self.assertNotIn("--model", self.claude_calls()[1]["argv"])
        self.assertIsNone(self.claude_calls()[1]["subagent_model"])
        code, lines = self.chat("--model", "gpt", stdin=TOKEN + "\n")
        self.assertEqual((code, list(lines[0])), (1, ["error"]))
        self.assertEqual(len(self.claude_calls()), 2)

    def test_resume_without_a_stored_session_starts_fresh(self):
        _, lines = self.chat("--resume", stdin="\n")
        self.assertFalse(lines[0]["resumed"])
        self.assertEqual(self.claude_calls()[0]["argv"][-2], "--session-id")
        self.assertIsNone(self.claude_calls()[0]["token_sha"])

    def test_errors(self):
        code, lines = self.chat(stdin="two words\n")
        self.assertEqual((code, list(lines[0])), (1, ["error"]))
        r = subprocess.run([sys.executable, str(NODE), "chat", "ghost"], input="\n",
                           capture_output=True, text=True, env=self.env)
        self.assertIn("unknown project", json.loads(r.stdout)["error"])
        self.assertEqual(self.claude_calls(), [])


class AnswerTests(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.init()
        self.repo = self.work / "demo"
        self.write(self.repo, "work/inbox/q01.md", QUESTION.format(id="q01", ticket="", gate="no"))

    def answer(self, ok=True, **op):
        return self.node("answer", "demo", stdin=json.dumps(op), ok=ok)

    def test_writes_the_answer_file(self):
        out = self.answer(id="q01", choice="C", note="  stay put  ")
        self.assertEqual((out["id"], out["choice"], out["path"]), ("q01", "C", "work/inbox/q01.answer.md"))
        text = (self.repo / "work/inbox/q01.answer.md").read_text()
        self.assertEqual(text, "---\nid: q01\nchoice: C\nanswered_at: %s\n---\nstay put\n" % out["answered_at"])
        q = self.node("snapshot")["projects"][0]["questions"][0]
        self.assertEqual((q["answered"], q["choice"]), (True, "C"))
        self.assertIn("already answered", self.answer(ok=False, id="q01", choice="A")["error"])

    def test_refusals(self):
        self.assertIn("unknown question", self.answer(ok=False, id="q09", choice="A")["error"])
        self.assertIn("one of A, B, C, other", self.answer(ok=False, id="q01", choice="D")["error"])
        self.assertIn("needs a note", self.answer(ok=False, id="q01", choice="other", note=" ")["error"])
        self.assertIn("invalid question id", self.answer(ok=False, id="../x", choice="A")["error"])
        self.assertIn("at most 4000", self.answer(ok=False, id="q01", choice="A", note="x" * 4001)["error"])
        self.assertFalse((self.repo / "work/inbox/q01.answer.md").exists())

    def test_answer_bound_to_the_question_shown(self):
        q = self.node("snapshot")["projects"][0]["questions"][0]
        self.assertEqual(q["fingerprint"], sha("Which way?\nA. left\nB. right (recommended: shorter)\nC. stay"))
        # the lead rewrites the options between the poll and the click
        self.write(self.repo, "work/inbox/q01.md", QUESTION.format(id="q01", ticket="", gate="no").replace("A. left", "A. delete it all"))
        err = self.answer(ok=False, id="q01", choice="A", fingerprint=q["fingerprint"])
        self.assertIn("changed", err["error"])
        self.assertEqual(err["question"]["options"][0], "A. delete it all")
        self.assertIn("fingerprint", self.answer(ok=False, id="q01", choice="A", fingerprint=7)["error"])
        self.assertFalse((self.repo / "work/inbox/q01.answer.md").exists())
        fresh = self.node("snapshot")["projects"][0]["questions"][0]["fingerprint"]
        self.assertEqual(self.answer(id="q01", choice="A", fingerprint=fresh)["choice"], "A")

    def test_other_with_a_note(self):
        self.answer(id="q01", choice="other", note="Neither; ask again after the benchmark.")
        self.assertIn("choice: other", (self.repo / "work/inbox/q01.answer.md").read_text())


class TicketTests(NodeTestCase):
    def setUp(self):
        super().setUp()
        self.init()
        self.repo = self.work / "demo"
        self.git("config", "user.name", "Someone")
        self.git("config", "user.email", "someone@example.com")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

    def git(self, *args, check=True):
        r = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, env=self.env)
        if check:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def op(self, ok=True, **fields):
        return self.node("ticket", "demo", stdin=json.dumps(fields), ok=ok)

    def head(self):
        return self.git("rev-parse", "HEAD").strip()

    def files_of(self, rev="HEAD"):
        return self.git("show", "--name-only", "--format=", rev).split()

    def tree(self):
        return {str(p.relative_to(self.repo)): p.read_text()
                for p in (self.repo / "work").rglob("*") if p.is_file()}

    def refused(self, message, **fields):
        before, head, staged = self.tree(), self.head(), self.git("diff", "--cached", "--name-only")
        out = self.op(ok=False, **fields)
        self.assertIn(message, out["error"])
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.head(), head)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), staged)

    def test_oversized_ticket_is_never_written_back_cut(self):
        big = ticket("02", "Big") + "x" * (2 * 1024 * 1024)
        self.write(self.repo, "work/tickets/02-big.md", big)
        self.git("add", "-A"); self.git("commit", "-q", "-m", "big")
        self.refused("unknown ticket", op="close", id="02", resolution="done")
        self.assertEqual((self.repo / "work/tickets/02-big.md").read_text(), big)

    def test_unwritable_map_rolls_the_ticket_back(self):
        mappath = self.repo / "work" / "map.md"
        mappath.chmod(0o444)
        self.addCleanup(mappath.chmod, 0o644)
        self.refused("PermissionError", op="close", id="01", resolution="done")

    def test_create(self):
        self.write(self.repo, "work/tickets/07-old.md", ticket("07", status="closed"))
        self.git("add", "-A"); self.git("commit", "-q", "-m", "old")
        out = self.op(op="create", title='Say "hi": a #1 thing', type="research",
                      question="Why?\n\nBecause.", blocked_by=["07", "01"])
        self.assertEqual(out["id"], "08")
        self.assertEqual(out["path"], "work/tickets/08-say-hi-a-1-thing.md")
        self.assertEqual(out["commit"], self.head()[:len(out["commit"])])
        text = (self.repo / out["path"]).read_text()
        self.assertIn('title: "Say \\"hi\\": a #1 thing"\n', text)
        self.assertIn("status: open\nassignee:\nblocked_by: [07, 01]\n", text)
        self.assertTrue(text.endswith("## Question\n\nWhy?\n\nBecause.\n"))
        self.assertEqual(self.files_of(), [out["path"]])
        self.assertEqual(self.git("log", "-1", "--format=%s|%an|%ae|%cn|%ce"),
                         "cairn: create ticket 08|cairn dashboard|dashboard@cairn.local|"
                         "cairn dashboard|dashboard@cairn.local\n")
        t = next(t for t in self.node("snapshot")["projects"][0]["tickets"] if t["id"] == "08")
        self.assertEqual((t["title"], t["type"], t["status"], t["assignee"], t["blocked_by"]),
                         ('Say "hi": a #1 thing', "research", "open", "", ["07", "01"]))

    def test_create_first_id_and_refusals(self):
        self.refused("unknown blocker", op="create", title="X", type="task", question="q", blocked_by=["99"])
        self.refused("type must be", op="create", title="X", type="nope", question="q")
        self.refused("title is required", op="create", title="  ", type="task", question="q")
        self.assertEqual(self.op(op="create", title="Second", type="task", question="q")["id"], "02")

    def test_close(self):
        out = self.op(op="close", id="1", resolution="Done it.\nLine two.")
        self.assertEqual(out["path"], "work/tickets/01-first-thing.md")
        self.assertEqual(sorted(self.files_of()), ["work/map.md", "work/tickets/01-first-thing.md"])
        text = (self.repo / out["path"]).read_text()
        self.assertIn("status: closed\n", text)
        self.assertNotIn("status: open", text)
        self.assertTrue(text.endswith("## Question\n\nDo it?\n\n## Resolution\n\nDone it.\nLine two.\n"))
        m = (self.repo / "work/map.md").read_text()
        self.assertIn("## Decisions so far\n\n- [First thing](tickets/01-first-thing.md) — Done it. Line two.\n\n"
                      "## Not yet specified", m)
        self.assertEqual(self.node("snapshot")["projects"][0]["map"]["decisions"],
                         ["- [First thing](tickets/01-first-thing.md) — Done it. Line two."])
        self.assertEqual(self.git("log", "-1", "--format=%s"), "cairn: close ticket 01\n")

    def test_close_second_appends_after_first(self):
        self.op(op="create", title="Other", type="task", question="q")
        self.op(op="close", id="01", resolution="one")
        self.op(op="close", id="02", resolution="two")
        m = (self.repo / "work/map.md").read_text()
        self.assertIn("- [First thing](tickets/01-first-thing.md) — one\n"
                      "- [Other](tickets/02-other.md) — two\n\n## Not yet specified", m)

    def test_out_of_scope(self):
        out = self.op(op="out-of-scope", id="01", reason="Not needed")
        text = (self.repo / out["path"]).read_text()
        self.assertIn("status: closed", text)
        self.assertTrue(text.endswith("## Resolution\n\nOut of scope: Not needed\n"))
        m = (self.repo / "work/map.md").read_text()
        self.assertTrue(m.endswith("## Out of scope\n\n- [First thing](tickets/01-first-thing.md) — Not needed\n"))
        self.assertNotIn("First thing", m.split("## Out of scope")[0])
        self.assertEqual(sorted(self.files_of()), ["work/map.md", "work/tickets/01-first-thing.md"])
        self.assertEqual(self.git("log", "-1", "--format=%s"), "cairn: out-of-scope ticket 01\n")

    def test_blockers(self):
        self.op(op="create", title="Second", type="task", question="q")
        out = self.op(op="blockers", id="02", blocked_by=["01"])
        self.assertIn("blocked_by: [01]\n", (self.repo / out["path"]).read_text())
        self.assertEqual(self.files_of(), [out["path"]])
        self.op(op="blockers", id="02", blocked_by=[])
        self.assertIn("blocked_by: []\n", (self.repo / out["path"]).read_text())
        self.assertEqual(self.git("log", "-1", "--format=%s"), "cairn: blockers ticket 02\n")

    def test_blockers_replaces_block_style_list(self):
        self.write(self.repo, "work/tickets/05-b.md",
                   "---\nid: 05\ntitle: B\ntype: task\nstatus: open\nassignee:\nblocked_by:\n  - 01\n---\n## Question\nx\n")
        self.git("add", "-A"); self.git("commit", "-q", "-m", "b")
        self.op(op="blockers", id="05", blocked_by=[])
        self.assertIn("assignee:\nblocked_by: []\n---\n", (self.repo / "work/tickets/05-b.md").read_text())

    def test_refuse_claimed(self):
        self.write(self.repo, "work/tickets/02-mine.md", ticket("02", title="Mine", assignee="lead"))
        self.git("add", "-A"); self.git("commit", "-q", "-m", "claim")
        for fields in (dict(op="close", id="02", resolution="x"), dict(op="out-of-scope", id="02", reason="x"),
                       dict(op="blockers", id="02", blocked_by=["01"])):
            self.refused("claimed", **fields)

    def test_refuse_closed(self):
        self.write(self.repo, "work/tickets/02-gone.md", ticket("02", title="Gone", status="closed"))
        self.git("add", "-A"); self.git("commit", "-q", "-m", "closed")
        for fields in (dict(op="close", id="02", resolution="x"), dict(op="out-of-scope", id="02", reason="x"),
                       dict(op="blockers", id="02", blocked_by=["01"])):
            self.refused("closed", **fields)

    def test_refuse_unknown_ticket_and_blocker(self):
        for fields in (dict(op="close", id="42", resolution="x"), dict(op="out-of-scope", id="42", reason="x"),
                       dict(op="blockers", id="42", blocked_by=[])):
            self.refused("unknown ticket", **fields)
        self.refused("unknown blocker", op="blockers", id="01", blocked_by=["77"])
        self.refused("block itself", op="blockers", id="01", blocked_by=["01"])

    def test_refuse_bad_input(self):
        self.refused("op must be", op="delete", id="01")
        self.refused("resolution is required", op="close", id="01", resolution="")
        self.refused("reason is required", op="out-of-scope", id="01")
        self.refused("blocked_by is required", op="blockers", id="01")
        r = subprocess.run([sys.executable, str(NODE), "ticket", "demo"], input="not json",
                           capture_output=True, text=True, env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("error", json.loads(r.stdout))

    def test_commit_only_touched_paths(self):
        self.write(self.repo, "other.txt", "staged\n")
        self.write(self.repo, "work/inbox/note.md", "untracked\n")
        self.write(self.repo, "work/tickets/09-x.md", ticket("09", title="X"))
        self.git("add", "other.txt", "work/tickets/09-x.md")
        self.git("commit", "-q", "-m", "tracked x", "--", "work/tickets/09-x.md")
        self.git("add", "other.txt")
        out = self.op(op="close", id="09", resolution="r")
        self.assertEqual(sorted(self.files_of()), ["work/map.md", "work/tickets/09-x.md"])
        self.assertEqual(self.git("diff", "--cached", "--name-only").split(), ["other.txt"])
        self.assertIn("?? work/inbox/note.md", self.git("status", "--short", "-uall"))
        created = self.op(op="create", title="New", type="task", question="q")
        self.assertEqual(self.files_of(), [created["path"]])
        self.assertEqual(self.git("diff", "--cached", "--name-only").split(), ["other.txt"])
        self.assertTrue(out["commit"])

    def test_retries_index_lock(self):
        (self.repo / ".git" / "index.lock").write_text("")
        env = dict(self.env, CAIRN_LOCK_DELAY="0.1")
        proc = subprocess.Popen([sys.executable, str(NODE), "ticket", "demo"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, env=env)
        threading.Timer(0.4, (self.repo / ".git" / "index.lock").unlink).start()
        stdout, _ = proc.communicate(json.dumps(dict(op="close", id="01", resolution="r")))
        out = json.loads(stdout)
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(out["id"], "01")

    def test_stuck_index_lock_fails_and_reverts(self):
        (self.repo / ".git" / "index.lock").write_text("")
        before = self.tree()
        env = dict(self.env, CAIRN_LOCK_DELAY="0.01", CAIRN_LOCK_TRIES="3")
        r = subprocess.run([sys.executable, str(NODE), "ticket", "demo"], capture_output=True, text=True, env=env,
                           input=json.dumps(dict(op="create", title="Z", type="task", question="q")))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("index.lock", json.loads(r.stdout)["error"])
        self.assertEqual(self.tree(), before)

    def lead_commits_before(self, step):
        bindir = self.root / "gitbin"
        bindir.mkdir()
        (bindir / "git").write_text(FAKE_GIT)
        (bindir / "git").chmod(0o755)
        return dict(self.env, PATH=f"{bindir}{os.pathsep}{self.env['PATH']}", REAL_GIT=shutil.which("git"),
                    LEAD_DONE=str(self.root / "lead-done"), LEAD_COMMITS_BEFORE=step)

    def op_env(self, env, **fields):
        r = subprocess.run([sys.executable, str(NODE), "ticket", "demo"], input=json.dumps(fields),
                           capture_output=True, text=True, env=env)
        out = json.loads(r.stdout)
        self.assertEqual(r.returncode, 0, out)
        return out

    def assert_head_holds_worktree(self, *rels):
        for rel in rels:
            self.assertEqual(self.git("show", f"HEAD:{rel}"), (self.repo / rel).read_text())
        self.assertEqual(self.git("status", "--porcelain", "-uall", "--", "work"), "")

    def test_lead_commit_taking_the_files_is_success(self):
        # The reviewer's interleaving: the lead's `git add -A && git commit` lands
        # between the dashboard's write and its add, or between its add and commit.
        for n, step in enumerate(("add", "commit")):
            with self.subTest(step=step):
                env = self.lead_commits_before(step) if n == 0 else dict(env, LEAD_COMMITS_BEFORE=step)
                Path(env["LEAD_DONE"]).unlink(missing_ok=True)
                out = self.op_env(env, op="create", title=f"Raced {step}", type="task", question="q")
                self.assertTrue(Path(env["LEAD_DONE"]).exists())
                self.assertEqual(self.git("log", "-1", "--format=%s|%an"), "lead work|Someone\n")
                self.assertEqual(out["commit"], self.head()[:len(out["commit"])])
                self.assert_head_holds_worktree(out["path"])
                Path(env["LEAD_DONE"]).unlink()
                out = self.op_env(env, op="close", id=out["id"], resolution="r")
                self.assertEqual(self.git("log", "-1", "--format=%s"), "lead work\n")
                self.assertIn("status: closed", (self.repo / out["path"]).read_text())
                self.assert_head_holds_worktree(out["path"], "work/map.md")

    def test_retries_ref_lock(self):
        ref = self.git("symbolic-ref", "HEAD").strip()
        lock = self.repo / ".git" / f"{ref}.lock"
        lock.write_text("")
        env = dict(self.env, CAIRN_LOCK_DELAY="0.1")
        timer = threading.Timer(0.5, lambda: lock.unlink(missing_ok=True))
        timer.start()
        self.addCleanup(timer.cancel)
        out = self.op_env(env, op="close", id="01", resolution="r")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "cairn: close ticket 01\n")
        self.assert_head_holds_worktree(out["path"], "work/map.md")

    def test_repo_hooks_do_not_block_ticket_ops(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.git("config", "commit.gpgsign", "true")
        out = self.op(op="create", title="Hooked", type="task", question="q")
        self.assertEqual(self.files_of(), [out["path"]])

    def test_concurrent_creates_get_distinct_ids(self):
        procs = [subprocess.Popen([sys.executable, str(NODE), "ticket", "demo"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, env=self.env) for _ in range(4)]
        for i, p in enumerate(procs):
            p.stdin.write(json.dumps(dict(op="create", title="Same title", type="task", question=str(i))))
            p.stdin.close()
        outs = [json.loads(p.stdout.read()) for p in procs]
        for p in procs:
            p.wait()
            p.stdout.close()
        self.assertEqual(sorted(o.get("id") for o in outs), ["02", "03", "04", "05"], outs)
        self.assertEqual(len(list((self.repo / "work" / "tickets").glob("*.md"))), 5)
        self.assert_head_holds_worktree(*(o["path"] for o in outs))

    def test_ticket_ops_wait_for_the_repo_lock(self):
        with open(self.repo / ".git" / "cairn-ticket.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            proc = subprocess.Popen([sys.executable, str(NODE), "ticket", "demo"], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, text=True, env=self.env)
            proc.stdin.write(json.dumps(dict(op="create", title="Waits", type="task", question="q")))
            proc.stdin.close()
            time.sleep(1)
            self.assertIsNone(proc.poll())
            self.assertFalse((self.repo / "work/tickets/02-waits.md").exists())
        out = json.loads(proc.stdout.read())
        proc.stdout.close()
        self.assertEqual(proc.wait(), 0, out)
        self.assertEqual(out["path"], "work/tickets/02-waits.md")

    def test_create_skips_ids_of_unreadable_ticket_files(self):
        self.write(self.repo, "work/tickets/02-half-written.md", "---\nid: 02\n")
        out = self.op(op="create", title="Half written", type="task", question="q")
        self.assertEqual(out["id"], "03")
        self.assertEqual((self.repo / "work/tickets/02-half-written.md").read_text(), "---\nid: 02\n")

    def test_refuse_close_when_map_has_uncommitted_changes(self):
        self.write(self.repo, "work/map.md", (self.repo / "work/map.md").read_text() + "lead draft\n")
        for fields in (dict(op="close", id="01", resolution="x"), dict(op="out-of-scope", id="01", reason="x")):
            self.refused("work/map.md has uncommitted changes", **fields)
        self.git("add", "work/map.md")
        self.refused("work/map.md has uncommitted changes", op="close", id="01", resolution="x")

    def test_refuse_blocker_cycle(self):
        self.op(op="create", title="Two", type="task", question="q", blocked_by=["01"])
        self.op(op="create", title="Three", type="task", question="q", blocked_by=["02"])
        self.refused("cycle: 01 -> 03 -> 02 -> 01", op="blockers", id="01", blocked_by=["03"])
        self.refused("cycle: 02 -> 03 -> 02", op="blockers", id="02", blocked_by=["03"])
        self.op(op="blockers", id="03", blocked_by=["01"])

    def test_unknown_project(self):
        out = self.node("ticket", "nope", stdin="{}", ok=False)
        self.assertIn("unknown project", out["error"])


class CliTests(NodeTestCase):
    def test_unknown_command(self):
        r = subprocess.run([sys.executable, str(NODE), "wat"], capture_output=True,
                           text=True, env=self.env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("error", json.loads(r.stdout))

    def check_too_old(self, r, version):
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(r.stdout.count("\n"), 1)
        error = json.loads(r.stdout)["error"]
        self.assertTrue(error.startswith("cairn-node needs Python 3.11 or newer;"), error)
        self.assertTrue(error.endswith(" is " + version), error)
        self.assertFalse((self.home / ".cairn").exists())

    def test_an_older_python_is_refused_in_one_line(self):
        run_as = "import runpy, sys; sys.version_info = (3, 10, 14, 'final', 0); sys.argv = sys.argv[1:]; " \
                 "runpy.run_path(sys.argv[0], run_name='__main__')"
        r = subprocess.run([sys.executable, "-c", run_as, str(NODE), "snapshot"], capture_output=True, text=True,
                           env=self.env)
        self.check_too_old(r, "3.10.14")

    def test_an_older_interpreter_gets_the_error_not_a_traceback(self):
        """The check only helps if the file still parses there; any python3 older than 3.11 on PATH proves it."""
        for py in ("/usr/bin/python3", "python3.10", "python3.9", "python3.8"):
            path = shutil.which(py)
            if not path:
                continue
            v = subprocess.run([path, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                               capture_output=True, text=True).stdout.strip()
            if v and tuple(map(int, v.split("."))) < (3, 11):
                r = subprocess.run([path, str(NODE), "snapshot"], capture_output=True, text=True, env=self.env)
                return self.check_too_old(r, v)
        self.skipTest("no python3 older than 3.11 here")


if __name__ == "__main__":
    unittest.main()
