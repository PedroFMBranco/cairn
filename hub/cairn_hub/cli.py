"""cairn command line."""
import argparse
import getpass
import json
import os
import plistlib
import socket
import subprocess
import sys
import time

from . import VERSION, attention, config, keychain, nodeops, notify, sandbox, ssh


INIT_TIMEOUT = nodeops.INIT_TIMEOUT


def die(msg):
    print("cairn: %s" % msg, file=sys.stderr)
    return 1


def cmd_token_add(a):
    secret = None
    if a.kind == "oauth":
        secret = getpass.getpass("Token for %s (hidden): " % a.label)
    t = nodeops.add_token(a.label, a.kind, a.account or "", secret=secret, overwrite=True)
    print("token %s added (%s%s)" % (a.label, a.kind, ", ..." + t["last4"] if t["last4"] else ""))
    return 0


def cmd_token_ls(a):
    nodes = config.load_nodes()
    rows = [("LABEL", "KIND", "ACCOUNT", "LAST4", "EXPIRES", "NODES")]
    for label, t in sorted(config.load_tokens().items()):
        used = ",".join(n for n, c in sorted(nodes.items()) if label in (c.get("allowed_tokens") or []))
        rows.append((label, t.get("kind") or "", t.get("account") or "", t.get("last4") or "-",
                     (t.get("expires") or "-")[:10], used or "-"))
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, (16, 10, 20, 6, 11, 0))).rstrip())
    return 0


def cmd_token_rm(a):
    sandboxes = sandbox.Sandboxes()
    snaps = {name: {"projects": []} if snap is None and sandboxes.stopped(name) else snap   # a stopped sandbox runs nothing
             for name, snap in nodeops.live_snapshots().items()}
    nodeops.remove_token(a.label, snaps, force=a.force)
    print("token %s removed" % a.label)
    return 0


def cmd_token_clear_limit(a):
    where = "%s on %s" % (a.label, a.node) if a.node else a.label
    if attention.clear_limit(a.label, a.node):
        print("limit of %s cleared; rotation may pick it again" % where)
    else:
        print("token %s has no recorded limit" % where)
    return 0


def cmd_node_tokens(a):
    labels = [t for t in a.labels.split(",") if t]
    nodeops.set_node_tokens(a.node, labels)
    print("node %s allowed tokens: %s" % (a.node, ", ".join(labels) or "(none)"))
    return 0


def cmd_node_add(a):
    labels = [t for t in (a.tokens or "").split(",") if t]
    check = nodeops.add_node(a.name, a.ssh, a.port, labels)
    print("node %s added (%s)" % (a.name, a.ssh))
    print("host %s, free RAM %s MB, free disk %s MB" % (check.get("host"), check.get("free_ram_mb"), check.get("free_disk_mb")))
    for dep, ver in (check.get("deps") or {}).items():
        print("  %-8s %s" % (dep, ver if ver else "MISSING"))
    return 0


def cmd_node_rm(a):
    snap = None
    node = config.load_nodes().get(a.name)
    if node is not None:
        try:
            snap = ssh.snapshot(node, timeout=15)
        except Exception:
            pass  # unreachable: live sessions cannot be checked, so only --force removes it
    nodeops.remove_node(a.name, snap, force=a.force)
    print("node %s removed (the machine is untouched)" % a.name)
    return 0


def node_snapshot(name):
    """The node's snapshot now, or None when it is unknown or unreachable."""
    node = config.load_nodes().get(name)
    try:
        return ssh.snapshot(node, timeout=15) if node else None
    except Exception:
        return None


def cmd_sandbox_ls(a):
    v = sandbox.Sandboxes().view()
    if not v["docker"]:
        return die("docker is not running: %s" % v["error"])
    rows = [("NODE", "IMAGE", "STATE", "PORT", "CPU", "MEMORY")]
    for s in v["sandboxes"]:
        mem = "-" if s["mem_used_mb"] is None else "%d MB / %s" % (s["mem_used_mb"], "%d MB" % s["mem_limit_mb"]
                                                                       if s["mem_limit_mb"] else "no cap")
        rows.append((s["node"], (s["image"] or "?") + (" (adopted)" if s["adopted"] else ""), s["state"], str(s["port"]),
                     "-" if s["cpu_pct"] is None else "%.1f%%" % s["cpu_pct"], mem))
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, (16, 16, 9, 6, 7, 0))).rstrip())
    return 0


def cmd_sandbox_create(a):
    def report(job):
        print(job["phase"] + (": " + job["error"] if job["error"] else ""), flush=True)
    labels = [t for t in (a.tokens or "").split(",") if t]
    job = sandbox.Sandboxes().create(a.name, a.image, a.memory, a.cpus, labels, wait=True, report=report)
    if job["phase"] != "done":
        for line in job["log_tail"]:
            print("  " + line, file=sys.stderr)
        return 1
    print("sandbox %s is node %s (%s port %d)" % (a.name, a.name, sandbox.TARGET, config.load_sandboxes()[a.name]["port"]))
    return 0


def cmd_sandbox_start(a):
    print("sandbox %s %s" % (a.name, sandbox.Sandboxes().start(a.name)["state"]))
    return 0


def cmd_sandbox_stop(a):
    print("sandbox %s %s" % (a.name, sandbox.Sandboxes().stop(a.name, node_snapshot(a.name), force=a.force)["state"]))
    return 0


def cmd_sandbox_recreate(a):
    res = sandbox.Sandboxes().recreate(a.name, node_snapshot(a.name), force=a.force)
    print("sandbox %s recreated (%s); its home volume, with the Claude login, was kept" % (a.name, res["state"]))
    return 0


def cmd_sandbox_login(a):
    entry = config.load_sandboxes().get(a.name)
    if entry is None:
        return die("%s is not a sandbox this hub manages" % a.name)
    argv = sandbox.login_argv(entry["port"])
    print("Claude Code prints a link: open it, sign in, and paste the code the page shows here.", flush=True)
    os.execvp(argv[0], argv)


def cmd_sandbox_rm(a):
    confirm = a.name
    if not a.yes:
        if not sys.stdin.isatty():
            return die("pass --yes to delete without a terminal")
        confirm = input("Delete sandbox %s, its container and both volumes (the mirror stays)? Type its name: " % a.name)
    res = sandbox.Sandboxes().remove(a.name, confirm.strip(), node_snapshot(a.name), force=a.force)
    print("sandbox %s deleted%s" % (a.name, "; mirror kept at %s" % res["mirror"] if res["mirror"] else ""))
    return 0


def cmd_sandbox_adopt(a):
    e = sandbox.Sandboxes().adopt(a.node, a.container)
    print("node %s now manages container %s (port %d, adopted)" % (a.node, e["container"], e["port"]))
    return 0


def cmd_node_update(a):
    res = nodeops.update_node(a.name)
    print("node %s runs cairn-node %s" % (res["node"], res["node_version"]))
    return 0


def cmd_node_ls(a):
    for name, node in sorted(config.load_nodes().items()):
        try:
            ssh.snapshot(node, timeout=15)
            status = "reachable"
        except Exception as e:
            status = "unreachable: %s" % e
        print("%-16s %-28s %s" % (name, node.get("ssh"), status))
    return 0


def cmd_init(a):
    repo = {"kind": a.repo}
    if a.repo == "clone":
        if not a.url:
            return die("--url is required for --repo clone")
        repo["url"] = a.url
    print(json.dumps(nodeops.create_project(a.node, a.project, repo, a.destination, path=a.path,
                                            notes=a.notes or "", token=a.token,
                                            model=a.model, subagent_model=a.subagent_model)))
    return 0


def cmd_project_models(a):
    res = nodeops.set_project_models(a.node, a.project, a.model, a.subagent_model)
    print("project %s/%s: lead %s, subagents %s, from its next start"
          % (a.node, a.project, res["model"] or "default", res["subagent_model"] or "default"))
    return 0


def cmd_project_token(a):
    nodeops.set_project_token(a.node, a.project, a.label)
    print("project %s/%s uses token %s from its next start" % (a.node, a.project, a.label))
    return 0


def cmd_project_tokens(a):
    labels = [t for t in a.labels.split(",") if t]
    nodeops.set_project_token(a.node, a.project, pool=labels)
    print("project %s/%s token pool: %s (active %s)" % (a.node, a.project, ", ".join(config.token_pool(a.node, a.project))
                                                         or "(none)", config.token_label_for(a.node, a.project)))
    return 0


def cmd_ticket(a):
    op = {"op": a.op}
    for key, val in (("title", a.title), ("type", a.type), ("question", a.question), ("id", a.id),
                     ("resolution", a.resolution), ("reason", a.reason)):
        if val is not None:
            op[key] = val
    if a.blocked_by is not None:
        op["blocked_by"] = [t for t in a.blocked_by.split(",") if t]
    print(json.dumps(nodeops.ticket(a.node, a.project, op)))
    return 0


def cmd_start(a):
    print(json.dumps(nodeops.start(a.node, a.project)))
    return 0


def cmd_stop(a):
    print(json.dumps(nodeops.stop(a.node, a.project)))
    return 0


def cmd_review(a):
    res = nodeops.review(a.node, a.project, terminal=not a.no_terminal)
    print(res["attach"])
    return 0


REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LABEL = "com.coldlabs.cairn"
PYTHON = sys.executable   # the Python that runs `cairn service install` runs the hub


def plist_path():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)


def service_plist():
    log = os.path.join(config.home(), "serve.log")
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": [PYTHON, os.path.join(REPO, "hub", "cairn"), "serve", "--port", "8770"],
        "WorkingDirectory": REPO, "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},   # respawn after a crash; a clean exit (port busy) stays down
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": log, "StandardErrorPath": log}).decode()


def port_busy(port):
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def bootout(domain):
    """Unload the agent and wait until launchd has let it go."""
    subprocess.run(["launchctl", "bootout", "%s/%s" % (domain, LABEL)], capture_output=True)
    for _ in range(20):
        if subprocess.run(["launchctl", "print", "%s/%s" % (domain, LABEL)], capture_output=True).returncode != 0:
            return
        time.sleep(0.5)


def cmd_service(a):
    """The LaunchAgent that keeps `cairn serve` running (login, crash)."""
    domain = "gui/%d" % os.getuid()
    if a.action == "status":
        r = subprocess.run(["launchctl", "print", "%s/%s" % (domain, LABEL)], capture_output=True, text=True)
        print("installed" if os.path.exists(plist_path()) else "not installed",
              "· running" if r.returncode == 0 and "state = running" in r.stdout else "· not running")
        return 0
    if a.action == "install":
        text = service_plist()
        if a.dry_run:
            print("would write %s:\n%s" % (plist_path(), text))
            print("would run: launchctl bootstrap %s %s" % (domain, plist_path()))
            return 0
        loaded = subprocess.run(["launchctl", "print", "%s/%s" % (domain, LABEL)], capture_output=True).returncode == 0
        if not loaded and port_busy(8770):
            return die("something already listens on 127.0.0.1:8770 (a `cairn serve` in a terminal?); stop it first")
        os.makedirs(os.path.dirname(plist_path()), exist_ok=True)
        with open(plist_path(), "w") as f:
            f.write(text)
        bootout(domain)
        for attempt in range(5):   # bootstrap right after bootout can fail with "5: Input/output error"
            r = subprocess.run(["launchctl", "bootstrap", domain, plist_path()], capture_output=True, text=True)
            if r.returncode == 0:
                break
            time.sleep(1)
        else:
            return die("launchctl bootstrap failed: %s" % (r.stderr.strip() or r.returncode))
        print("installed %s; the hub runs now and at every login (log %s)" % (LABEL, os.path.join(config.home(), "serve.log")))
        return 0
    if a.dry_run:
        print("would run: launchctl bootout %s/%s and remove %s" % (domain, LABEL, plist_path()))
        return 0
    bootout(domain)
    if os.path.exists(plist_path()):
        os.remove(plist_path())
    print("removed %s; the hub no longer starts by itself" % LABEL)
    return 0


def cmd_notify(a):
    n = notify.Notifier()
    if a.action == "setup":
        v = n.setup(a.server)
        print("notifications set up on %s; run `cairn notify show` to subscribe on your phone" % v["server"])
    elif a.action == "show":
        v = n.view()
        if not v["configured"]:
            return die("not set up; run: cairn notify setup")
        print("server %s\ntopic  %s\n%s" % (v["server"], v["topic"], "on" if v["enabled"] else "off"))
    elif a.action == "test":
        n.test()
        print("sent")
    else:
        if not n.view(with_topic=False)["configured"]:
            return die("not set up; run: cairn notify setup")
        notify.save_settings(enabled=a.action == "on")
        print("notifications %s" % a.action)
    return 0


def cmd_lead_update(a):
    res = nodeops.relead(a.node, a.project)
    print("LEAD.md of %s/%s is now v%s (commit %s)" % (a.node, a.project, res["version"], res["commit"]))
    return 0


def cmd_open(a):
    """Open the dashboard with the link that signs this browser in to the hub."""
    from .server import sign_in_url
    url = sign_in_url(a.port)
    if not port_busy(a.port):
        print("cairn: nothing listens on 127.0.0.1:%d yet; start the hub with `cairn service install`" % a.port,
              file=sys.stderr)
    print(url)
    subprocess.run(["open", url], capture_output=True, check=False)
    return 0


def cmd_serve(a):
    from .server import serve
    serve(a.port, dev=a.dev or os.environ.get("CAIRN_DEV") == "1")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="cairn")
    p.add_argument("--version", action="version", version="cairn " + VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    tok = sub.add_parser("token").add_subparsers(dest="sub", required=True)
    t = tok.add_parser("add")
    t.add_argument("label")
    t.add_argument("--account")
    t.add_argument("--kind", choices=list(nodeops.TOKEN_KINDS), default="oauth",
                   help="adding over an existing label replaces its secret and keeps its nodes and projects")
    t.set_defaults(fn=cmd_token_add)
    tok.add_parser("ls").set_defaults(fn=cmd_token_ls)
    t = tok.add_parser("rm")
    t.add_argument("label")
    t.add_argument("--force", action="store_true",
                   help="remove even when a node that may run it is unreachable (a live lead still refuses)")
    t.set_defaults(fn=cmd_token_rm)
    t = tok.add_parser("clear-limit", help="forget a token's recorded usage limit")
    t.add_argument("label")
    t.add_argument("--node", help="the node whose login a node-login token's limit is")
    t.set_defaults(fn=cmd_token_clear_limit)

    nd = sub.add_parser("node").add_subparsers(dest="sub", required=True)
    n = nd.add_parser("add")
    n.add_argument("name")
    n.add_argument("--ssh", required=True)
    n.add_argument("--port", type=int)
    n.add_argument("--tokens")
    n.set_defaults(fn=cmd_node_add)
    nd.add_parser("ls").set_defaults(fn=cmd_node_ls)
    n = nd.add_parser("update", help="reinstall cairn-node, its LEAD.md template and the review skill")
    n.add_argument("name")
    n.set_defaults(fn=cmd_node_update)
    n = nd.add_parser("rm")
    n.add_argument("name")
    n.add_argument("--force", action="store_true", help="remove even when the node is unreachable")
    n.set_defaults(fn=cmd_node_rm)
    n = nd.add_parser("tokens")
    n.add_argument("node")
    n.add_argument("labels", help="comma-separated token labels, empty string for none")
    n.set_defaults(fn=cmd_node_tokens)

    sb = sub.add_parser("sandbox", help="local Docker sandboxes").add_subparsers(dest="sub", required=True)
    sb.add_parser("ls").set_defaults(fn=cmd_sandbox_ls)
    n = sb.add_parser("create", help="build the image when missing, run the container, add it as a node")
    n.add_argument("name")
    n.add_argument("--image", choices=list(sandbox.IMAGES), required=True)
    n.add_argument("--memory", type=int, help="MB, at least %d; no cap when left out" % sandbox.MIN_MEMORY_MB)
    n.add_argument("--cpus", type=float)
    n.add_argument("--tokens")
    n.set_defaults(fn=cmd_sandbox_create)
    force = "even when its node cannot be checked for a live lead (a live lead still refuses)"
    n = sb.add_parser("start")
    n.add_argument("name")
    n.set_defaults(fn=cmd_sandbox_start)
    n = sb.add_parser("stop")
    n.add_argument("name")
    n.add_argument("--force", action="store_true", help="stop " + force)
    n.set_defaults(fn=cmd_sandbox_stop)
    n = sb.add_parser("recreate", help="a new container from the current image; the home volume and its login stay")
    n.add_argument("name")
    n.add_argument("--force", action="store_true", help="recreate " + force)
    n.set_defaults(fn=cmd_sandbox_recreate)
    n = sb.add_parser("login", help="sign the sandbox's claude in to its own Claude account, in this terminal")
    n.add_argument("name")
    n.set_defaults(fn=cmd_sandbox_login)
    n = sb.add_parser("rm", help="delete the container, its volumes and the node; the mirror stays")
    n.add_argument("name")
    n.add_argument("--yes", action="store_true")
    n.add_argument("--force", action="store_true", help="delete " + force)
    n.set_defaults(fn=cmd_sandbox_rm)
    n = sb.add_parser("adopt", help="record an existing container for an existing node")
    n.add_argument("node")
    n.add_argument("--container")
    n.set_defaults(fn=cmd_sandbox_adopt)

    i = sub.add_parser("init")
    i.add_argument("node")
    i.add_argument("project")
    i.add_argument("--destination", required=True)
    i.add_argument("--notes")
    i.add_argument("--path")
    i.add_argument("--repo", choices=["new", "existing", "clone"], default="new")
    i.add_argument("--url")
    i.add_argument("--token")
    i.add_argument("--model", choices=config.MODELS)
    i.add_argument("--subagent-model", choices=config.MODELS)
    i.set_defaults(fn=cmd_init)

    pr = sub.add_parser("project").add_subparsers(dest="sub", required=True)
    t = pr.add_parser("token")
    t.add_argument("node")
    t.add_argument("project")
    t.add_argument("label")
    t.set_defaults(fn=cmd_project_token)
    t = pr.add_parser("tokens", help="the tokens the lead moves through on usage limits, in preference order")
    t.add_argument("node")
    t.add_argument("project")
    t.add_argument("labels", help="comma-separated token labels, empty string for the active token alone")
    t.set_defaults(fn=cmd_project_tokens)
    t = pr.add_parser("models", help="lead and subagent models; a flag left out means the account default")
    t.add_argument("node")
    t.add_argument("project")
    t.add_argument("--model", choices=config.MODELS)
    t.add_argument("--subagent-model", choices=config.MODELS)
    t.set_defaults(fn=cmd_project_models)

    t = sub.add_parser("ticket")
    t.add_argument("node")
    t.add_argument("project")
    t.add_argument("op", choices=nodeops.TICKET_OPS)
    for flag in ("title", "type", "question", "id", "resolution", "reason"):
        t.add_argument("--" + flag)
    t.add_argument("--blocked-by", help="comma-separated ticket ids")
    t.set_defaults(fn=cmd_ticket)

    for name, fn in (("start", cmd_start), ("stop", cmd_stop), ("review", cmd_review)):
        s = sub.add_parser(name)
        s.add_argument("node")
        s.add_argument("project")
        if name == "review":
            s.add_argument("--no-terminal", action="store_true")
        s.set_defaults(fn=fn)

    s = sub.add_parser("service", help="the LaunchAgent that keeps the hub running")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_service)

    s = sub.add_parser("notify", help="pushes to your phone through ntfy")
    s.add_argument("action", choices=["setup", "show", "test", "on", "off"])
    s.add_argument("--server")
    s.set_defaults(fn=cmd_notify)

    ld = sub.add_parser("lead").add_subparsers(dest="sub", required=True)
    s = ld.add_parser("update", help="re-render LEAD.md from the current template")
    s.add_argument("node")
    s.add_argument("project")
    s.set_defaults(fn=cmd_lead_update)

    s = sub.add_parser("open", help="open the dashboard, signing this browser in to the hub")
    s.add_argument("--port", type=int, default=8770)
    s.set_defaults(fn=cmd_open)

    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8770)
    s.add_argument("--dev", action="store_true", help="serve ui/fixtures/ for the UI's ?fixture= mode (also CAIRN_DEV=1)")
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except (nodeops.OpError, ssh.NodeError, keychain.KeychainError, ValueError, RuntimeError) as e:
        return die(str(e))
    except KeyboardInterrupt:
        return 130
