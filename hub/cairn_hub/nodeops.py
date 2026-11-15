"""Actions on a node shared by the CLI and the API."""
import copy
import fcntl
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone

from . import config, keychain, mirror, ssh

# the node's git clone may run for 600 s; the hub waits longer so a slow clone is not reported as failed
INIT_TIMEOUT = 660
TICKET_OPS = ("create", "close", "out-of-scope", "blockers")
TOKEN_KINDS = ("oauth", "node-login")


class FileLock:
    """Re-entrant across this process's threads, and an flock on ~/.cairn/<name> across processes, so the CLI and a
    running hub never interleave a read-modify-write. flock belongs to the open file, so only the outermost acquire
    opens it; the path is read then, since CAIRN_HOME can change."""

    def __init__(self, name):
        self.name = name
        self.threads = threading.RLock()
        self.depth = 0
        self.fd = None

    def __enter__(self):
        self.threads.acquire()
        if self.depth == 0:
            fd = None
            try:
                os.makedirs(config.home(), mode=0o700, exist_ok=True)
                fd = os.open(config.path(self.name), os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
            except BaseException:
                if fd is not None:
                    os.close(fd)
                self.threads.release()
                raise
            self.fd = fd
        self.depth += 1
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        if self.depth == 0:
            os.close(self.fd)   # drops the flock
            self.fd = None
        self.threads.release()


LOCK = FileLock(".state.lock")  # guards read-modify-write of nodes, tokens, projects, sandboxes and limits .json
# One token add, rename or removal at a time, Keychain calls included, so a label's secret is never written and deleted
# at once. Taken before LOCK and never inside it; LOCK is held only for the file writes, never across a Keychain call.
TOKENS = FileLock(".tokens.lock")
RESERVED = set()  # labels an add_token in flight in this process has claimed (under LOCK), refused at once

NODE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "node"))


class OpError(Exception):
    def __init__(self, msg, status=400, **extra):
        super().__init__(msg)
        self.status = status
        self.extra = extra


def get_node(name):
    node = config.load_nodes().get(name)
    if node is None:
        raise OpError("unknown node %s" % name, 404)
    return node


def check_project(project):
    if not isinstance(project, str) or not config.PROJECT_RE.match(project):
        raise OpError("invalid project name")


def token_line(node_name, project):
    """First stdin line for start/review: the OAuth value, or empty for node-login."""
    label = config.token_label_for(node_name, project)
    if not label:
        return "\n"
    meta = config.load_tokens().get(label)
    if meta is None:
        raise OpError("token %s is not defined" % label, 409)
    if label not in ((config.load_nodes().get(node_name) or {}).get("allowed_tokens") or []):
        raise OpError("token %s is not allowed on node %s; pick another token for %s, or allow it with: "
                      "cairn node tokens %s <label,label>" % (label, node_name, project, node_name), 409)
    if meta.get("kind") == "node-login":
        return "\n"
    if meta.get("kind") != "oauth":
        raise OpError("token %s is of a kind this hub no longer supports (%s); add an OAuth token under the same "
                      "label to keep its nodes and projects: cairn token add %s --kind oauth"
                      % (label, meta.get("kind"), label), 409)
    try:
        secret = keychain.get(label)
    except keychain.KeychainError as e:
        raise OpError(str(e), 409)
    return secret + "\n"


VERSIONS = {}  # node -> cairn-node version from its last good snapshot (the syncer keeps it current)
MODELS_SINCE = (0, 2, 0)
DIALOG_SINCE = (0, 4, 0)


def version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


def bundled_version():
    """The cairn-node version this hub installs (VERSION in node/cairn-node)."""
    try:
        with open(os.path.join(NODE_DIR, "cairn-node")) as f:
            m = re.search(r'^VERSION = "([0-9.]+)"', f.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def update_available(node_version):
    bundled = bundled_version()
    return bool(node_version and bundled and version_tuple(node_version) < version_tuple(bundled))


def model_args(node_name, project):
    """--model / --subagent-model for the project's saved models (start and chat).
    Refused on a node whose cairn-node predates models, which would reject the flags."""
    entry = config.load_projects().get("%s/%s" % (node_name, project)) or {}
    args = []
    for key, flag in (("model", "--model"), ("subagent_model", "--subagent-model")):
        if entry.get(key) in config.MODELS:
            args += [flag, entry[key]]
    known = VERSIONS.get(node_name)
    if args and known and version_tuple(known) < MODELS_SINCE:
        raise OpError("node %s runs cairn-node %s, which has no model choice; update cairn-node on the Nodes page "
                      "(or: cairn node update %s), or set the models back to Default" % (node_name, known, node_name), 409)
    return args


def update_node(name):
    """Reinstall cairn-node, its template and the review skill on a node."""
    snap = install(get_node(name))
    VERSIONS[name] = snap.get("node_version")
    return {"node": name, "node_version": snap.get("node_version")}


def start(node_name, project, token=None):
    """`token` is an already resolved token_line; None resolves it here."""
    check_project(project)
    node = get_node(node_name)
    if token is None:
        token = token_line(node_name, project)
    return ssh.run_node(node, ["start", project] + model_args(node_name, project), stdin=token, timeout=60)


def restart(node_name, project):
    """Stop the lead when it runs, then start it again (it resumes its own conversation).
    The token and models are resolved first, so a token problem never leaves the lead stopped."""
    check_project(project)
    node = get_node(node_name)
    token = token_line(node_name, project)
    args = model_args(node_name, project)
    ssh.run_node(node, ["stop", project], timeout=60)
    return ssh.run_node(node, ["start", project] + args, stdin=token, timeout=60)


def relead(node_name, project):
    check_project(project)
    return ssh.run_node(get_node(node_name), ["relead", project], timeout=60)


def usage(node_name, project):
    check_project(project)
    return ssh.run_node(get_node(node_name), ["usage", project], timeout=60)


def answer(node_name, project, qid, choice, note="", fingerprint=None):
    """A refusal by the node (already answered, bad choice) is a 409; ssh trouble stays a NodeError.
    `fingerprint`, the one the snapshot gave the question shown, makes the node refuse a question rewritten since;
    that 409 carries the node's fresh question."""
    check_project(project)
    if fingerprint is not None and (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
        raise OpError("fingerprint must be the question's sha256")
    op = {"id": qid, "choice": choice, "note": note}
    if fingerprint is not None:
        op["fingerprint"] = fingerprint
    code, out, err = ssh.run_raw(get_node(node_name), ["answer", project], stdin=json.dumps(op), timeout=30)
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    if code == 1 and isinstance(data, dict) and data.get("error"):
        if isinstance(data.get("question"), dict):
            raise OpError(data["error"], 409, question=ssh.parse_question(data["question"]))
        raise OpError(data["error"], 409)
    if code != 0 or not isinstance(data, dict):
        raise ssh.NodeError((err.strip().splitlines() or ["exit %d" % code])[-1])
    return data


def stop(node_name, project):
    check_project(project)
    return ssh.run_node(get_node(node_name), ["stop", project], timeout=30)


def review(node_name, project, terminal=True, opener=None):
    check_project(project)
    node = get_node(node_name)
    res = ssh.run_node(node, ["review", project], stdin=token_line(node_name, project), timeout=60)
    attach = ssh.attach_command(node, project)
    if terminal:
        (opener or open_terminal)(attach)
    return {"attach": attach, "session": res.get("session")}


def open_terminal(command):
    script = command.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", 'tell application "Terminal" to activate',
                    "-e", 'tell application "Terminal" to do script "%s"' % script],
                   capture_output=True, check=False)


def read_file(node_name, project, relpath):
    check_project(project)
    return ssh.run_node(get_node(node_name), ["read", project, relpath])


def pane(node_name, project, review_session=False):
    check_project(project)
    argv = ["pane", project] + (["--review"] if review_session else [])
    return ssh.run_node(get_node(node_name), argv)


def check_dialog_version(node_name):
    known = VERSIONS.get(node_name)
    if known and version_tuple(known) < DIALOG_SINCE:
        raise OpError("node %s runs cairn-node %s, which cannot read dialogs; update cairn-node on the Nodes page "
                      "(or: cairn node update %s)" % (node_name, known, node_name), 409)


def dialog(node_name, project):
    check_project(project)
    node = get_node(node_name)
    check_dialog_version(node_name)
    return ssh.run_node(node, ["dialog", project])


def answer_dialog(node_name, project, fingerprint, n):
    """A refusal by the node (the dialog changed, no such option) is a 409 carrying the dialog it read."""
    check_project(project)
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise OpError("fingerprint must be the dialog's sha256")
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise OpError("n must be an option number")
    node = get_node(node_name)
    check_dialog_version(node_name)
    code, out, err = ssh.run_raw(node, ["dialog-answer", project, fingerprint, str(n)], timeout=30)
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    if code == 1 and isinstance(data, dict) and data.get("error") and "dialog" in data:
        raise OpError(data["error"], 409, dialog=data["dialog"])
    if code != 0 or not isinstance(data, dict):
        msg = data.get("error") if isinstance(data, dict) else None
        raise ssh.NodeError(msg or (err.strip().splitlines() or ["exit %d" % code])[-1])
    return data


def install(node):
    """Copy cairn-node and the review skill to the node. Returns the snapshot."""
    src_bin = os.path.join(NODE_DIR, "cairn-node")
    src_skill = os.path.join(NODE_DIR, "skills", "cairn-review", "SKILL.md")
    src_tmpl = os.path.join(NODE_DIR, "LEAD.md.tmpl")
    for p in (src_bin, src_skill, src_tmpl):
        if not os.path.isfile(p):
            raise OpError("missing %s" % p, 500)
    with open(src_bin, "rb") as f:
        bin_data = f.read()
    with open(src_skill, "rb") as f:
        skill_data = f.read()
    with open(src_tmpl, "rb") as f:
        tmpl_data = f.read()
    if ssh.is_local(node):
        for rel, data, mode in (("local-node/cairn-node", bin_data, 0o755),
                                ("local-node/LEAD.md.tmpl", tmpl_data, 0o644),
                                ("local-node/skills/cairn-review/SKILL.md", skill_data, 0o644)):
            dest = config.path(rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            os.chmod(dest, mode)
    else:
        ssh.run_shell(node, "mkdir -p ~/.cairn && cat > ~/.cairn/cairn-node && chmod 755 ~/.cairn/cairn-node",
                      stdin=bin_data)
        ssh.run_shell(node, "mkdir -p ~/.cairn && cat > ~/.cairn/LEAD.md.tmpl", stdin=tmpl_data)
        ssh.run_shell(node, "mkdir -p ~/.claude/skills/cairn-review && cat > ~/.claude/skills/cairn-review/SKILL.md",
                      stdin=skill_data)
    return ssh.snapshot(node, timeout=60)


# -- ssh hosts

def ssh_hosts(cfg_path=None):
    """Concrete `Host` aliases from ~/.ssh/config and the files its `Include` lines name (globs, relative paths
    under ~/.ssh, as ssh reads them): no wildcards, no negations."""
    hosts = set()
    _read_ssh_config(cfg_path or os.path.expanduser("~/.ssh/config"), hosts, 0)
    return sorted(hosts, key=str.lower)


def _read_ssh_config(cfg_path, hosts, depth):
    if depth > 16:  # ssh's own Include depth limit; also stops include loops
        return
    try:
        with open(cfg_path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        m = re.match(r"^\s*(host|include)(?:\s+|\s*=\s*)(.+)$", line, re.I)
        if not m:
            continue
        try:
            words = shlex.split(m.group(2), comments=True)
        except ValueError:
            continue
        if m.group(1).lower() == "include":
            for w in words:
                pattern = os.path.join(os.path.expanduser("~/.ssh"), os.path.expanduser(w))
                for p in sorted(glob.glob(pattern)):
                    _read_ssh_config(p, hosts, depth + 1)
            continue
        for w in words:
            if not any(c in w for c in "*?!") and config.SSH_RE.match(w):
                hosts.add(w)


# -- sessions in use

def live_projects(snapshot):
    return [p["name"] for p in (snapshot or {}).get("projects") or []
            if p.get("lead") == "alive" or p.get("review") == "alive"]


def unchecked(name, snapshot):
    """Why the snapshot cannot tell whether a lead runs on node `name`, or None when it can."""
    if snapshot is None:
        return "node %s is unreachable or not synced yet" % name
    unknown = [p["name"] for p in snapshot.get("projects") or [] if p.get("lead") == "unknown"]
    return "tmux on node %s did not answer for %s" % (name, ", ".join(unknown)) if unknown else None


def tokens_in_use(snapshots):
    """{label: ["<node>/<project>", ...]} for projects with a live lead or review. snapshots: {node: snapshot|None}."""
    use = {}
    for node, snap in snapshots.items():
        for project in live_projects(snap):
            label = config.token_label_for(node, project)
            if label:
                use.setdefault(label, []).append("%s/%s" % (node, project))
    return {k: sorted(v) for k, v in use.items()}


def live_snapshots(timeout=15):
    """Ask every node now (CLI use, no running hub). An unreachable node maps to None."""
    out = {}
    for name, node in config.load_nodes().items():
        try:
            out[name] = ssh.snapshot(node, timeout=timeout)
        except Exception:
            out[name] = None
    return out


# -- tokens

def public_token(label):
    return next(t for t in config.public_tokens() if t["label"] == label)


def _known_nodes(names):
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise OpError("nodes must be a list of node names")
    unknown = [n for n in names if n not in config.load_nodes()]
    if unknown:
        raise OpError("unknown node(s): %s" % ", ".join(unknown))


def add_token(label, kind, account="", secret=None, nodes=(), overwrite=False):
    """Store a token like `cairn token add`. The secret goes to the Keychain only; returns the public entry."""
    if not isinstance(label, str) or not config.LABEL_RE.match(label):
        raise OpError("invalid label")
    if kind not in TOKEN_KINDS:
        raise OpError("kind must be one of %s" % ", ".join(TOKEN_KINDS))
    if account is not None and not isinstance(account, str):
        raise OpError("account must be text")
    nodes = list(nodes or [])
    _known_nodes(nodes)
    with LOCK:
        if label in RESERVED or (label in config.load_tokens() and not overwrite):
            raise OpError("token %s already exists" % label, 409)
        RESERVED.add(label)
    try:
        return _add_token(label, kind, account, secret, nodes, overwrite)
    finally:
        with LOCK:
            RESERVED.discard(label)


def _add_token(label, kind, account, secret, nodes, overwrite):
    meta = {"kind": kind, "account": account or "", "added": config.now_iso(), "expires": None, "last4": None}
    if kind == "oauth":
        secret = secret.strip() if isinstance(secret, str) else ""
        if len(secret) < 8:
            raise OpError("token too short")
        meta["expires"] = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with TOKENS:
        replaced = config.load_tokens().get(label)
        if replaced and not overwrite:   # the CLI or the hub added it since the check above
            raise OpError("token %s already exists" % label, 409)
        if kind != "node-login":
            keychain.add(label, secret)
            meta["last4"] = secret[-4:]
        with LOCK:
            tokens = config.load_tokens()
            tokens[label] = meta
            config.save_tokens(tokens)
            if nodes:
                cfg = config.load_nodes()
                for n in nodes:
                    allowed = cfg[n].setdefault("allowed_tokens", [])
                    if label not in allowed:
                        allowed.append(label)
                config.save_nodes(cfg)
        if kind == "node-login" and replaced and replaced.get("kind") != "node-login":
            keychain.delete(label)   # the replaced token's secret; after the save, so a crash leaves only an orphan
    return public_token(label)


def update_token(label, new_label=None, account=None, nodes=None):
    """Rename and/or change account and the nodes that allow the token. Returns the public entry under its final label.

    A rename runs: Keychain add(new); tokens.json with both labels; nodes.json and projects.json pointing at new;
    tokens.json without old; Keychain delete(old). After every step each label the files name is defined in
    tokens.json and has its Keychain entry, so a crash anywhere leaves a usable token. On an error the files
    written so far are put back, and the new entry is deleted only once that worked."""
    if isinstance(new_label, str) and new_label != label:
        with LOCK:
            if new_label in RESERVED:   # refused now rather than after TOKENS has waited for that add
                raise OpError("token %s already exists" % new_label, 409)
    with TOKENS:
        tokens = config.load_tokens()
        meta = tokens.get(label)
        if meta is None:
            raise OpError("no such token %s" % label, 404)
        if account is not None and not isinstance(account, str):
            raise OpError("account must be text")
        if nodes is not None:
            _known_nodes(nodes)
        renaming = new_label not in (None, "", label)
        if renaming:
            if not isinstance(new_label, str) or not config.LABEL_RE.match(new_label):
                raise OpError("invalid label")
            if new_label in tokens:
                raise OpError("token %s already exists" % new_label, 409)
        final = new_label if renaming else label
        moving = renaming and meta.get("kind") != "node-login"   # every other kind keeps a Keychain item
        if moving:
            try:
                keychain.add(new_label, keychain.get(label))
            except keychain.KeychainError as e:
                raise OpError(str(e), 409)
        restored = True
        try:
            with LOCK:   # read afresh: a write made during the Keychain calls is kept
                tokens, cfg = config.load_tokens(), config.load_nodes()
                projects, limits = config.load_projects(), config.load_limits()
                before = {config.save_tokens: copy.deepcopy(tokens), config.save_nodes: copy.deepcopy(cfg),
                          config.save_projects: copy.deepcopy(projects), config.save_limits: copy.deepcopy(limits)}
                meta = tokens[label]
                if account is not None:
                    meta["account"] = account
                if renaming:
                    tokens[final] = meta
                    for c in cfg.values():
                        c["allowed_tokens"] = [final if t == label else t for t in c.get("allowed_tokens") or []]
                    for p in projects.values():
                        if p.get("token") == label:
                            p["token"] = final
                        if p.get("tokens"):
                            p["tokens"] = [final if t == label else t for t in p["tokens"]]
                if nodes is not None:
                    for name, c in cfg.items():
                        allowed = [t for t in c.get("allowed_tokens") or [] if t != final]
                        c["allowed_tokens"] = allowed + [final] if name in nodes else allowed
                steps = [(config.save_tokens, tokens), (config.save_nodes, cfg)]
                if renaming:
                    steps += [(config.save_projects, projects),
                              (config.save_tokens, {k: v for k, v in tokens.items() if k != label})]
                    if label in limits:   # a node-login limit is keyed by its node, so it never moves
                        limits[final] = limits.pop(label)
                        steps.append((config.save_limits, limits))
                written = []
                try:
                    for save, data in steps:
                        save(data)
                        if save not in written:
                            written.append(save)
                except BaseException:
                    try:
                        for save in reversed(written):
                            save(before[save])
                    except BaseException:
                        restored = False   # the files may still name the new label, so its Keychain entry stays
                    raise
        except BaseException:
            if moving and restored:
                keychain.delete(new_label)
            raise
        if moving:
            keychain.delete(label)   # under TOKENS, so an add_token of the freed label cannot lose its new secret
    return public_token(final)


def remove_token(label, snapshots, force=False):
    """Refused (409) while a live lead or review uses the token, and, unless `force`, while a node that allows it
    or has a project on it has no snapshot (None: unreachable now or not synced), so its leads cannot be checked.
    The Keychain item goes last, so a failure on the way never leaves a label without its secret."""
    with TOKENS:
        with LOCK:
            tokens = config.load_tokens()
            meta = tokens.get(label)
            if meta is None:
                raise OpError("no such token %s" % label, 404)
            using = tokens_in_use(snapshots).get(label)
            if using:
                raise OpError("token %s is in use by %s" % (label, ", ".join(using)), 409)
            named = {k.partition("/")[0] for k, e in config.load_projects().items() if e.get("token") == label}
            unknown = sorted(n for n, c in config.load_nodes().items()
                             if (label in (c.get("allowed_tokens") or []) or n in named)
                             and unchecked(n, snapshots.get(n)))
            if unknown and not force:
                raise OpError("cannot check %s for a live lead on token %s (%s); remove it anyway with force"
                              % (", ".join(unknown), label, "; ".join(unchecked(n, snapshots.get(n)) for n in unknown)),
                              409, unchecked=unknown)
            del tokens[label]
            config.save_tokens(tokens)
            cfg = config.load_nodes()
            for c in cfg.values():
                c["allowed_tokens"] = [t for t in c.get("allowed_tokens") or [] if t != label]
            config.save_nodes(cfg)
            projects = config.load_projects()
            for key, entry in list(projects.items()):
                if entry.get("token") == label:
                    del entry["token"]   # the project falls back to its node's first allowed token
                if label in (entry.get("tokens") or []):
                    entry["tokens"] = [t for t in entry["tokens"] if t != label]
                    if not entry["tokens"]:
                        del entry["tokens"]
                if not entry:
                    del projects[key]
            config.save_projects(projects)
            limits = config.load_limits()
            if limits.pop(label, None) is not None:
                config.save_limits(limits)
        if meta.get("kind") != "node-login":
            keychain.delete(label)   # under TOKENS, so an add_token of the freed label cannot lose its new secret


# -- nodes

def check_labels(labels):
    """Every label must exist in tokens.json."""
    if not isinstance(labels, list) or not all(isinstance(t, str) for t in labels):
        raise OpError("allowed_tokens must be a list of labels")
    unknown = [t for t in labels if t not in config.load_tokens()]
    if unknown:
        raise OpError("unknown token(s): %s" % ", ".join(unknown))


def add_node(name, target, port, allowed):
    """Install cairn-node and the skill, then save the node. Returns the check summary."""
    if not isinstance(name, str) or not config.NODE_RE.match(name):
        raise OpError("invalid node name")
    if not isinstance(target, str) or not config.SSH_RE.match(target):
        raise OpError("invalid ssh target")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536):
        raise OpError("invalid port")
    check_labels(allowed)
    if name in config.load_nodes():
        raise OpError("node %s already exists; change its tokens with: cairn node tokens %s <label,label>" % (name, name), 409)
    node = {"ssh": target, "port": port, "allowed_tokens": allowed}
    snap = install(node)
    with LOCK:
        nodes = config.load_nodes()
        if name in nodes:
            raise OpError("node %s already exists" % name, 409)
        nodes[name] = node
        config.save_nodes(nodes)
    return {k: snap.get(k) for k in ("deps", "host", "free_ram_mb", "free_disk_mb")}


def set_node_tokens(name, labels):
    check_labels(labels)
    with LOCK:
        nodes = config.load_nodes()
        if name not in nodes:
            raise OpError("unknown node %s" % name, 404)
        nodes[name]["allowed_tokens"] = list(labels)
        config.save_nodes(nodes)


def remove_node(name, snapshot, force=False):
    """Forget the node and its projects.json entries (the machine is untouched). Refused (409) while a lead or
    review is alive there, and while `snapshot` is None (not synced, or unreachable now) unless `force`."""
    with LOCK:
        nodes = config.load_nodes()
        if name not in nodes:
            raise OpError("unknown node %s" % name, 404)
        why = unchecked(name, snapshot)
        if why and not force:
            raise OpError("%s, so live sessions cannot be checked; remove it anyway with force" % why, 409,
                          unchecked=[name])
        alive = live_projects(snapshot)
        if alive:
            raise OpError("node %s has a live lead or review: %s" % (name, ", ".join(alive)), 409)
        del nodes[name]
        config.save_nodes(nodes)
        VERSIONS.pop(name, None)
        projects = config.load_projects()
        kept = {k: v for k, v in projects.items() if not k.startswith(name + "/")}
        if kept != projects:
            config.save_projects(kept)
        limits = config.load_limits()
        if limits.pop("node-login@" + name, None) is not None:   # the node's own login (attention.limit_key)
            config.save_limits(limits)


# -- projects

def check_token_allowed(node_name, node, label):
    if label not in config.load_tokens():
        raise OpError("unknown token %s" % label)
    if label not in (node.get("allowed_tokens") or []):
        raise OpError("token %s is not allowed on node %s; allow it with: cairn node tokens %s <label,label>"
                      % (label, node_name, node_name))


def update_project(node_name, project, **fields):
    """Merge fields into the project's projects.json entry, keeping the other keys; None removes a key."""
    with LOCK:
        projects = config.load_projects()
        key = "%s/%s" % (node_name, project)
        entry = dict(projects.get(key) or {})
        for k, v in fields.items():
            if v is None:
                entry.pop(k, None)
            else:
                entry[k] = v
        projects[key] = entry
        config.save_projects(projects)


def set_project_token(node_name, project, label=None, pool=None):
    """The active token and/or the pool (preference order; [] drops it). The active token is kept in the pool."""
    check_project(project)
    node = get_node(node_name)
    if pool is not None:
        if not isinstance(pool, list) or not all(isinstance(t, str) for t in pool):
            raise OpError("tokens must be a list of labels")
        if len(set(pool)) != len(pool):
            raise OpError("tokens names a label twice")
        for t in pool:
            check_token_allowed(node_name, node, t)
    if label is not None:
        check_token_allowed(node_name, node, label)
    with LOCK:
        entry = config.load_projects().get("%s/%s" % (node_name, project)) or {}
        active = label or config.token_label_for(node_name, project) or (pool or [None])[0]
        tokens = list(entry.get("tokens") or []) if pool is None else pool
        if tokens and active not in tokens:
            tokens = [active] + tokens
        update_project(node_name, project, token=active, tokens=tokens or None)


def check_model(value, field):
    if value is not None and value not in config.MODELS:
        raise OpError("%s must be one of %s, or null" % (field, ", ".join(config.MODELS)))
    return value


def set_project_models(node_name, project, model, subagent_model):
    """Used from the next start; None means the account default."""
    check_project(project)
    get_node(node_name)
    check_model(model, "model")
    check_model(subagent_model, "subagent_model")
    update_project(node_name, project, model=model, subagent_model=subagent_model)
    return {"model": model, "subagent_model": subagent_model}


def free_folder(parent, project):
    """Create and return ~/Downloads/<project>, else <project>-YYYY-MM-DD, -2, -3…; never an existing one."""
    os.makedirs(parent, exist_ok=True)
    day = date.today().isoformat()
    n = 0
    while True:
        name = project if n == 0 else "%s-%s" % (project, day) if n == 1 else "%s-%s-%d" % (project, day, n)
        path = os.path.join(parent, name)
        try:
            os.mkdir(path)
            return path
        except FileExistsError:
            n += 1


def open_folder(path):
    subprocess.run(["open", path], capture_output=True, check=False)


def copy_result(node_name, project, sha, reveal=None, result_sha=None):
    """Export commit `sha` from the mirror (files only, no history) to a new folder in ~/Downloads and open it.
    Links that point outside the folder are left out and listed in `skipped`."""
    check_project(project)
    get_node(node_name)
    if not sha:
        raise OpError("%s has no mirrored commit yet; try again after the next sync" % project, 409)
    if not hasattr(tarfile, "data_filter"):
        raise OpError("this Python has no safe tar extraction; run the hub on Python 3.12+ (or 3.11.4+)", 500)
    git_dir = mirror.mirror_path(node_name, project)
    r = subprocess.run(["git", "--git-dir", git_dir, "rev-parse", "--verify", "--quiet", sha + "^{commit}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise OpError("could not export %s: commit %s is not in the mirror; nothing was copied" % (project, sha), 502)
    full = r.stdout.strip()
    skipped = []

    def keep(member, path):
        try:
            return tarfile.data_filter(member, path)
        except (tarfile.AbsoluteLinkError, tarfile.LinkOutsideDestinationError):
            skipped.append(member.name)
            return None
    with tempfile.TemporaryDirectory(prefix="cairn-copy-") as tmp:
        tar_path = os.path.join(tmp, "export.tar")
        r = subprocess.run(["git", "--git-dir", git_dir, "archive", "--format=tar", "-o", tar_path, full],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise OpError("could not export %s: %s; nothing was copied"
                          % (project, r.stderr.strip() or "git archive exit %d" % r.returncode), 502)
        dest = free_folder(os.path.expanduser("~/Downloads"), project)
        done = False
        try:
            with tarfile.open(tar_path) as tar:
                tar.extractall(dest, filter=keep)
            done = True
        except (tarfile.TarError, OSError) as e:
            raise OpError("could not export %s: %s; nothing was copied" % (project, e), 502)
        finally:
            if not done:
                shutil.rmtree(dest, ignore_errors=True)
    update_project(node_name, project, copied_sha=full, copied_to=dest, copied_result_sha=result_sha)
    (reveal or open_folder)(dest)
    return {"path": dest, "sha": full, "skipped": sorted(skipped)}


def create_project(node_name, project, repo, destination, path=None, notes="", tickets=None, token=None, start_now=False,
                   model=None, subagent_model=None):
    """init on the node, remember the project's token and models, optionally start the lead.
    A failed start is reported in start_error, not raised."""
    check_project(project)
    if project.endswith("-review"):   # only new names: a project registered under one stays usable
        raise OpError("a project name may not end in -review: project x's review session is x-review")
    node = get_node(node_name)
    if token:
        check_token_allowed(node_name, node, token)
    check_model(model, "model")
    check_model(subagent_model, "subagent_model")
    if not isinstance(repo, dict) or repo.get("kind") not in ("new", "existing", "clone"):
        raise OpError("repo.kind must be new, existing or clone")
    spec_repo = {"kind": repo["kind"]}
    if repo["kind"] == "clone":
        url = repo.get("url")
        if not isinstance(url, str) or not url.strip() or url.startswith("-"):
            raise OpError("repo.url is required to clone")
        spec_repo["url"] = url.strip()
    if not isinstance(destination, str) or not destination.strip():
        raise OpError("destination is required")
    if notes is not None and not isinstance(notes, str):
        raise OpError("notes must be text")
    if tickets is not None and (not isinstance(tickets, list) or not all(isinstance(t, dict) for t in tickets)):
        raise OpError("tickets must be a list of objects")
    if path is not None and (not isinstance(path, str) or not path.startswith("/")):
        raise OpError("path must be absolute")
    obj = {"repo": spec_repo, "destination": destination.strip(), "notes": notes or "", "tickets": tickets or []}
    if path:
        obj["path"] = path  # absent: the node defaults it to $HOME/work/<project>
    res = ssh.run_node(node, ["init", project], stdin=json.dumps(obj), timeout=INIT_TIMEOUT)
    with LOCK:   # a fresh entry: a project re-created under an old name inherits nothing
        projects = config.load_projects()
        projects["%s/%s" % (node_name, project)] = {
            k: v for k, v in (("token", token), ("model", model), ("subagent_model", subagent_model)) if v}
        config.save_projects(projects)
    started, start_error = False, None
    if start_now:
        try:
            start(node_name, project)
            started = True
        except (OpError, ssh.NodeError) as e:
            start_error = str(e)
    return {"project": project, "path": res.get("path"), "started": started, "start_error": start_error}


def ticket(node_name, project, op):
    """cairn-node ticket: `op` is the op object including its "op" name."""
    check_project(project)
    if not isinstance(op, dict) or op.get("op") not in TICKET_OPS:
        raise OpError("op must be one of %s" % ", ".join(TICKET_OPS))
    return ssh.run_node(get_node(node_name), ["ticket", project], stdin=json.dumps(op), timeout=30)
