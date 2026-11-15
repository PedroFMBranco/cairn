"""Running cairn-node on a node: over ssh, or as a local subprocess for ssh == "local"."""
import json
import os
import re
import shlex
import subprocess
import threading

from . import config

REMOTE_BIN = "~/.cairn/cairn-node"
MAX_OUTPUT = 32 * 1024 * 1024   # a node's `read` returns at most a 2 MB file, which JSON escaping can grow about sixfold


class NodeError(Exception):
    pass


def is_local(node):
    return node.get("ssh") == "local"


def local_bin():
    return config.path("local-node/cairn-node")


def ssh_options():
    h = config.home()
    os.makedirs(h, mode=0o700, exist_ok=True)
    return ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "ForwardAgent=no",   # a lead must never reach the operator's ssh agent
            "-o", "ControlMaster=auto", "-o", "ControlPath=%s/cm-%%C" % h,
            "-o", "ControlPersist=120",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
            "-o", "UserKnownHostsFile=%s/known_hosts" % h,
            "-o", "StrictHostKeyChecking=accept-new"]


def ssh_argv(node, remote_cmd):
    argv = ["ssh"] + ssh_options()
    if node.get("port"):
        argv += ["-p", str(int(node["port"]))]
    return argv + ["--", node["ssh"], remote_cmd]


def attach_command(node, project):
    tmux = "tmux -L cairn attach -t %s-review" % project
    if is_local(node):
        return tmux
    port = " -p %d" % int(node["port"]) if node.get("port") else ""
    known = shlex.quote(os.path.join(config.home(), "known_hosts"))
    opts = " -o UserKnownHostsFile=%s -o StrictHostKeyChecking=accept-new" % known
    return "ssh -t -a -o ForwardAgent=no%s%s %s %s" % (port, opts, node["ssh"], tmux)


def node_command(node, argv):
    """The local argv that runs `cairn-node <argv>` on the node."""
    if is_local(node):
        return [local_bin()] + list(argv)
    return ssh_argv(node, REMOTE_BIN + " " + " ".join(shlex.quote(a) for a in argv))


def run_capped(cmd, stdin, timeout):
    """(returncode, stdout bytes, stderr bytes), like subprocess.run(capture_output=True), except that a stream
    longer than MAX_OUTPUT kills the command: a hostile node must not grow the hub's memory without bound."""
    if isinstance(stdin, str):
        stdin = stdin.encode()
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        raise NodeError("cannot run %s: %s" % (cmd[0], e.strerror))
    got = {}

    def feed():
        try:
            p.stdin.write(stdin or b"")
            p.stdin.close()
        except OSError:
            pass   # the command exited without reading it all

    def drain(stream):
        with stream:
            got[stream] = stream.read(MAX_OUTPUT + 1)
        if len(got[stream]) > MAX_OUTPUT:
            p.kill()

    threads = [threading.Thread(target=feed, daemon=True)] + [
        threading.Thread(target=drain, args=(s,), daemon=True) for s in (p.stdout, p.stderr)]
    for t in threads:
        t.start()
    try:
        p.wait(timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        raise NodeError("timed out after %ds" % timeout)
    finally:
        for t in threads:
            t.join(5)
    out, err = got.get(p.stdout, b""), got.get(p.stderr, b"")
    if max(len(out), len(err)) > MAX_OUTPUT:
        raise NodeError("the node sent more than %d MB; stopped reading" % (MAX_OUTPUT // (1024 * 1024)))
    return p.returncode, out, err


def run_raw(node, argv, stdin=None, timeout=30):
    """Returns (returncode, stdout, stderr). stdin is bytes/str or None."""
    code, out, err = run_capped(node_command(node, argv), stdin, timeout)
    return code, out.decode(errors="replace"), err.decode(errors="replace")


def run_node(node, argv, stdin=None, timeout=30):
    """Run a cairn-node command and return its parsed JSON."""
    code, out, err = run_raw(node, argv, stdin, timeout)
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    if code != 0:
        msg = data.get("error") if isinstance(data, dict) else None
        if not msg and err.strip():
            msg = err.strip().splitlines()[-1]
        raise NodeError(msg or "exit %d" % code)
    if data is None:
        raise NodeError("node returned invalid JSON")
    return data


SHA_RE = re.compile(r"[0-9a-f]{4,64}")
DROPPED = "the hub dropped project"


def _str(v):
    return v if isinstance(v, str) else ""


def _opt_str(v):
    return v if isinstance(v, str) else None


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _bool(v):
    return v if isinstance(v, bool) else False


def _strs(v):
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _sha(v):
    return v if isinstance(v, str) and SHA_RE.fullmatch(v) else None


def _deps(v):
    return {k: _opt_str(x) for k, x in v.items()} if isinstance(v, dict) else {}


def _record(fields):
    """A dict carrying every field in its expected type (a wrong or missing one gets the empty value); other keys kept."""
    return lambda v: {**v, **{k: f(v.get(k)) for k, f in fields.items()}} if isinstance(v, dict) else _record(fields)({})


def _records(fields, keep=lambda r: True):
    rec = _record(fields)
    return lambda v: [r for r in (rec(x) for x in v if isinstance(x, dict)) if keep(r)] if isinstance(v, list) else []


# The snapshot as the hub and the dashboard read it. A lead owns cairn-node on its node and can make it print
# anything, so every field is coerced to its type here, once, before anything else reads it.
QUESTION_FIELDS = {"id": _str, "ticket": _str, "gate": _bool, "default": _str, "asked_at": _str, "context": _str,
                   "options": _strs, "answered": _bool, "choice": _opt_str, "answered_at": _opt_str,
                   "applied": _opt_str, "path": _str, "fingerprint": _opt_str}
parse_question = _record(QUESTION_FIELDS)   # also the fresh question in a refused answer
PROJECT_FIELDS = {
    "status": _str, "lead": _str, "lead_stopped": _bool, "pane_last": _opt_str, "lead_state": _opt_str,
    "lead_note": _opt_str, "lead_version": _int, "review": _str,
    "map": _record({"destination": _str, "decisions": _strs}),
    "tickets": _records({"id": _str, "title": _str, "type": _str, "status": _str, "assignee": _str,
                         "blocked_by": _strs, "path": _str, "state": _str}),
    "questions": _records(QUESTION_FIELDS),
    "result": _opt_str, "result_sha": _sha,
    "adrs": _records({"id": _str, "title": _str, "path": _str}),
    "commits": _records({"sha": _sha, "date": _str, "subject": _str}, keep=lambda c: c["sha"]),
}
SNAPSHOT_FIELDS = {"node_version": _opt_str, "lead_template_version": _int, "host": _str, "time": _str,
                   "free_ram_mb": _int, "free_disk_mb": _int, "deps": _deps,
                   "login": lambda v: v if v in ("yes", "no") else "unknown",   # cairn-node 0.4.6; older: unknown
                   "warnings": _records({"warning": _str})}


def parse_snapshot(raw):
    """The node's snapshot reply with every field in its expected type. A project without a valid name or an
    absolute path, or with a name seen before, is dropped and named in "warnings". ValueError when the top level is
    not a snapshot at all."""
    if not isinstance(raw, dict) or not isinstance(raw.get("projects"), list):
        raise ValueError("malformed snapshot: not an object with a projects list")
    snap = _record(SNAPSHOT_FIELDS)(raw)
    projects, seen = [], set()
    for i, p in enumerate(raw["projects"]):
        name = p.get("name") if isinstance(p, dict) else None
        path = p.get("path") if isinstance(p, dict) else None
        if not isinstance(name, str) or not config.PROJECT_RE.match(name):
            why = "no valid name"
        elif name in seen:
            why = "a duplicate name"
        elif not isinstance(path, str) or not path.startswith("/"):
            why = "no absolute path"
        else:
            seen.add(name)
            projects.append(_record(PROJECT_FIELDS)(p))
            continue
        note = "%s %d of the snapshot (%s): %r" % (DROPPED, i + 1, why, name if isinstance(name, str) else p)
        snap["warnings"].append({"warning": note[:300]})
    snap["projects"] = projects
    return snap


def snapshot(node, timeout=25):
    """The node's snapshot, parsed; NodeError when it cannot be had or is not a snapshot."""
    try:
        return parse_snapshot(run_node(node, ["snapshot"], timeout=timeout))
    except ValueError as e:
        raise NodeError(str(e))


def run_shell(node, remote_cmd, stdin=None, timeout=30):
    """Plain shell command on the node (install steps). Local nodes run it with sh."""
    if is_local(node):
        cmd = ["sh", "-c", remote_cmd]
    else:
        cmd = ssh_argv(node, remote_cmd)
    code, out, err = run_capped(cmd, stdin, timeout)
    if code != 0:
        raise NodeError(err.decode(errors="replace").strip() or "exit %d" % code)
    return out.decode(errors="replace")
