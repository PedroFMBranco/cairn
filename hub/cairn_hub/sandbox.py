"""Local Docker sandboxes: the registry, the docker calls the hub may make, and the create job (CLI and API)."""
import contextlib
import json
import os
import re
import shlex
import socket
import subprocess
import threading
import time

from . import config, nodeops, ssh

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
# Building an image builds its missing bases first.
IMAGES = {"slim": {"tag": "cairn-slim:0.1", "dir": "nodes/slim", "base": None},
          "dev": {"tag": "cairn-dev:0.1", "dir": "nodes/dev", "base": "slim"}}
LABEL = "cairn.sandbox"
TARGET = "agent@127.0.0.1"
PORTS = range(2222, 2300)
MIN_MEMORY_MB = 1024
PIDS_LIMIT = 4096   # stops a fork bomb; a lead's builds and agents stay far below it
# As nodes/slim/run.sh: sshd needs Docker's other default capabilities (setuid, chroot, chown, audit), so only these go.
HARDEN = ["--cap-drop", "NET_RAW", "--cap-drop", "MKNOD", "--pids-limit", str(PIDS_LIMIT)]
DOCKER_TIMEOUT = 30
BUILD_TIMEOUT = 600
WAIT_TIMEOUT = 60
STATS_TTL = 8   # docker stats takes about 2 s; the form polls every 2 s while a job runs
PS_TTL = 10     # how stale the syncer's view of container states may be; any docker change clears it
STOPPED = ("exited", "created")   # deliberately stopped; restarting, paused, dead or missing are not
PROBE_TIMEOUT = 15
LOG_LINES = 20
# Run in the operator's terminal over ssh: Claude Code prints a link, then waits for the code the page shows.
LOGIN_CMD = 'PATH="$PATH:$HOME/.local/bin" claude auth login'
PHASES = ("building", "starting", "waiting", "adding", "done", "failed")
RUNNING = PHASES[:4]
JOB_FIELDS = ("node", "phase", "error", "log_tail", "started_at", "finished_at")
DOWN_RE = re.compile(r"Cannot connect to the Docker daemon|Is the docker daemon running|error during connect", re.I)
MiB = 1024 * 1024

# Every docker call that changes an existing container or volume. Names come from the registry entry, never the caller.
MUTATIONS = {
    "start": lambda c: [["start", c]],
    "stop": lambda c: [["stop", c]],
    "rm": lambda c: [["rm", "-f", c]],
    "volume-rm": lambda c: [["volume", "rm", v] for v in volumes(c)],
}
GONE_OK = ("rm", "volume-rm")


class DockerDown(nodeops.OpError):
    def __init__(self, msg):
        super().__init__(msg, 503)


def volumes(container):
    return (container + "-home", container + "-hostkeys")


def last_line(text):
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1] if lines else ""


def run_docker(argv, timeout, on_line=None):
    """(returncode, stdout, stderr) of `docker <argv>`. With on_line the merged output is streamed to it instead."""
    cmd = ["docker"] + list(argv)
    try:
        if on_line is None:
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
                               stdin=subprocess.DEVNULL)
            return r.returncode, r.stdout, r.stderr
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                             stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise nodeops.OpError("docker %s timed out after %d s" % (argv[0], timeout), 504)
    except OSError as e:
        raise DockerDown("docker is not installed or not on PATH (%s)" % e.strerror)
    killed = []
    timer = threading.Timer(timeout, lambda: (killed.append(True), p.kill()))
    timer.start()
    try:
        for line in p.stdout:
            on_line(line.rstrip("\n"))
        code = p.wait()
    finally:
        timer.cancel()
    if killed:
        raise nodeops.OpError("docker %s timed out after %d s" % (argv[0], timeout), 504)
    return code, "", ""


def ssh_reachable(port, timeout):
    """Whether the sandbox answers ssh within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            ssh.run_shell({"ssh": TARGET, "port": port}, "true", timeout=10)
            return True
        except ssh.NodeError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(1)


def port_free(port):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def bound_ports(infos):
    """Host ports in the HostConfig.PortBindings of `docker container inspect` output, running or not, ranges included."""
    out = set()
    for info in infos:
        for bindings in ((info.get("HostConfig") or {}).get("PortBindings") or {}).values():
            for b in bindings or []:
                m = re.fullmatch(r"(\d+)(?:-(\d+))?", str(b.get("HostPort") or ""))
                if m:
                    out.update(range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1))
    return out


def pick_port(published, taken, bindable=port_free):
    for port in PORTS:
        if port not in published and port not in taken and bindable(port):
            return port
    raise nodeops.OpError("no free port in %d-%d" % (PORTS[0], PORTS[-1]), 409)


def ssh_port(info):
    """The host port the container publishes 22/tcp on, or None."""
    for b in ((info.get("HostConfig") or {}).get("PortBindings") or {}).get("22/tcp") or []:
        if str(b.get("HostPort") or "").isdigit():
            return int(b["HostPort"])
    return None


def label_of(info):
    return ((info.get("Config") or {}).get("Labels") or {}).get(LABEL)


def build_chain(image):
    chain = []
    while image:
        chain.insert(0, image)
        image = IMAGES[image]["base"]
    return chain


def size_mb(text):
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?i?B)", text or "", re.I)
    if not m:
        return None
    scale = {"b": 1 / MiB, "kb": 1e3 / MiB, "kib": 1 / 1024, "mb": 1e6 / MiB, "mib": 1, "gb": 1e9 / MiB, "gib": 1024,
             "tb": 1e12 / MiB, "tib": 1024 * 1024}.get(m.group(2).lower())
    return round(float(m.group(1)) * scale, 1) if scale else None


def pubkey_path():
    for p in ("~/.ssh/id_ed25519.pub", "~/.ssh/id_rsa.pub"):
        full = os.path.expanduser(p)
        if os.path.isfile(full):
            return full
    raise nodeops.OpError("no public key at ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub", 409)


def run_argv(node, container, image, port, memory_mb, cpus, pubkey):
    """`docker run` for a sandbox container: create and recreate both use this, so both get the same flags."""
    home, keys = volumes(container)
    argv = ["run", "-d", "--name", container, "--restart", "unless-stopped", "--label", "%s=%s" % (LABEL, node),
            "-p", "127.0.0.1:%d:22" % port, "-v", home + ":/home/agent", "-v", keys + ":/etc/ssh/hostkeys",
            "-v", pubkey + ":/etc/cairn/authorized_key:ro"] + HARDEN
    if image == "slim":   # dev gives the agent sudo, which no-new-privileges would break
        argv += ["--security-opt", "no-new-privileges"]
    if memory_mb:
        argv += ["--memory", "%dm" % memory_mb]
    if cpus:
        argv += ["--cpus", "%g" % cpus]
    return argv + [IMAGES[image]["tag"]]


def login_argv(port):
    """The command that signs the sandbox's claude in to its own account, run in the operator's own terminal. The
    link and the code stay between that terminal, the browser and the sandbox; none of it passes through the hub."""
    return ["ssh", "-t", "-a", "-o", "ForwardAgent=no", "-p", str(int(port)),
            "-o", "UserKnownHostsFile=" + os.path.join(config.home(), "known_hosts"),
            "-o", "StrictHostKeyChecking=accept-new", TARGET, LOGIN_CMD]


def forget_host_key(port):
    path = config.path("known_hosts")
    if os.path.exists(path):
        subprocess.run(["ssh-keygen", "-R", "[127.0.0.1]:%d" % port, "-f", path], capture_output=True)


def probe_snapshot(port):
    """A snapshot from the sandbox itself, for a container whose node record is gone; None when ssh or cairn-node fails."""
    try:
        return ssh.snapshot({"ssh": TARGET, "port": port}, timeout=PROBE_TIMEOUT)
    except ssh.NodeError:
        return None


def check_idle(node, snapshot, entry, info, force=False):
    """Refused (409) while a lead or review is alive on the node, or, unless `force`, while that cannot be checked
    and it runs. A running container with no node record is asked directly; it is allowed when it does not answer."""
    running = info is not None and (info.get("State") or {}).get("Running")
    if node not in config.load_nodes():
        if running:
            alive = nodeops.live_projects(probe_snapshot(entry["port"]))
            if alive:
                raise nodeops.OpError("sandbox %s has a live lead or review: %s" % (node, ", ".join(alive)), 409)
        return
    alive = nodeops.live_projects(snapshot)
    if alive:
        raise nodeops.OpError("node %s has a live lead or review: %s" % (node, ", ".join(alive)), 409)
    why = nodeops.unchecked(node, snapshot)
    if why and running and not force:
        raise nodeops.OpError("%s, so live sessions cannot be checked; do it anyway with force" % why, 409,
                              unchecked=[node])


class Sandboxes:
    """run: the docker runner (argv, timeout, on_line) -> (code, out, err). reach: (port, timeout) -> bool.
    on_added: called with the node name once a create job has added the node."""

    def __init__(self, run=None, reach=None, bindable=None, on_added=None):
        self.run = run or run_docker
        self.reach = reach or ssh_reachable
        self.bindable = bindable or port_free
        self.on_added = on_added
        self.lock = threading.Lock()   # guards jobs, port reservations and busy
        self.jobs = {}
        self.busy = set()   # nodes a stop or delete is changing; a create for them is refused meanwhile
        self.stats_cache = (0.0, (), {})
        self.ps_cache = (0.0, None)

    # -- docker

    def docker(self, *args, timeout=DOCKER_TIMEOUT, on_line=None):
        code, out, err = self.run(list(args), timeout, on_line)
        if code != 0 and DOWN_RE.search(err or ""):
            raise DockerDown(last_line(err))
        return code, out, err

    def check(self, *args, **kw):
        code, out, err = self.docker(*args, **kw)
        if code != 0:
            raise nodeops.OpError("docker %s: %s" % (" ".join(args[:2]), last_line(err) or "exit %d" % code), 502)
        return out

    def inspect(self, kind, name):
        """`docker <kind> inspect`, or None when there is no such object."""
        code, out, err = self.docker(kind, "inspect", name)
        if code != 0:
            if "no such" in (err or "").lower():
                return None
            raise nodeops.OpError("docker %s inspect: %s" % (kind, last_line(err) or "exit %d" % code), 502)
        return json.loads(out)[0]

    def ps(self, max_age=0, timeout=DOCKER_TIMEOUT):
        """{container name: `docker ps -a` row}, reused while younger than max_age seconds."""
        at, rows = self.ps_cache
        if rows is not None and time.monotonic() - at < max_age:
            return rows
        rows = {}
        for line in self.check("ps", "-a", "--format", "{{json .}}", timeout=timeout).splitlines():
            c = json.loads(line)
            rows[c.get("Names")] = c
        self.ps_cache = (time.monotonic(), rows)
        return rows

    def stopped(self, node):
        """Whether the node is a sandbox whose container is deliberately stopped. False when Docker cannot tell."""
        entry = config.load_sandboxes().get(node)
        if entry is None:
            return False
        try:
            rows = self.ps(PS_TTL, timeout=10)
        except nodeops.OpError:
            return False
        return (rows.get(entry["container"]) or {}).get("State") in STOPPED

    def cpus_max(self):
        return int(self.check("info", "--format", "{{.NCPU}}").strip())

    # -- the touch rule

    def touchable(self, node):
        """The registry entry and container (None when gone) of a sandbox the hub may change: its container carries
        cairn.sandbox=<node>, or the entry was adopted and names it. Anything else is refused before any mutation."""
        entry = config.load_sandboxes().get(node)
        if entry is None:
            raise nodeops.OpError("%s is not a sandbox this hub manages" % node, 409)
        info = self.inspect("container", entry["container"])
        if info is not None:
            adopted = (entry.get("adopted") and info.get("Name") == "/" + entry["container"]
                       and ssh_port(info) == entry.get("port"))
            if label_of(info) != node and not adopted:
                raise nodeops.OpError("container %s is not labelled %s=%s, or was not adopted on port %s; the hub "
                                      "leaves it alone" % (entry["container"], LABEL, node, entry.get("port")), 409)
        if not entry.get("adopted"):
            for vol in volumes(entry["container"]):
                v = self.inspect("volume", vol)
                if v is not None and (v.get("Labels") or {}).get(LABEL) != node:
                    raise nodeops.OpError("volume %s is not labelled %s=%s; the hub leaves it alone" % (vol, LABEL, node), 409)
        return entry, info

    def mutate(self, node, action):
        entry, _ = self.touchable(node)
        self.ps_cache = (0.0, None)
        for argv in MUTATIONS[action](entry["container"]):
            code, out, err = self.docker(*argv)
            if code != 0 and not (action in GONE_OK and "no such" in (err or "").lower()):
                raise nodeops.OpError("docker %s: %s" % (" ".join(argv[:2]), last_line(err) or "exit %d" % code), 502)

    # -- view

    def stats(self, names):
        names = tuple(sorted(names))
        at, cached_names, cached = self.stats_cache
        if not names:
            return {}
        if cached_names == names and time.monotonic() - at < STATS_TTL:
            return cached
        out = {}
        for line in self.check("stats", "--no-stream", "--format", "{{json .}}", *names).splitlines():
            s = json.loads(line)
            used, _, limit = (s.get("MemUsage") or "").partition("/")
            try:
                cpu = float((s.get("CPUPerc") or "").rstrip("%"))
            except ValueError:
                cpu = None
            out[s.get("Name")] = {"cpu_pct": cpu, "mem_used_mb": size_mb(used), "mem_limit_mb": size_mb(limit)}
        self.stats_cache = (time.monotonic(), names, out)
        return out

    def public(self, job):
        return {k: (list(job[k]) if k == "log_tail" else job[k]) for k in JOB_FIELDS}

    def view(self):
        with self.lock:
            jobs = [self.public(j) for j in sorted(self.jobs.values(), key=lambda j: j["started_at"])]
        out = {"docker": True, "error": None, "images": {}, "cpus_max": None, "sandboxes": [], "jobs": jobs}
        reg = config.load_sandboxes()
        try:
            out["cpus_max"] = self.cpus_max()
            tags = set(self.check("image", "ls", "--format", "{{.Repository}}:{{.Tag}}").split())
            out["images"] = {k: v["tag"] in tags for k, v in IMAGES.items()}
            ps = self.ps()
            try:
                stats = self.stats(e["container"] for e in reg.values()
                                   if (ps.get(e["container"]) or {}).get("State") == "running")
            except DockerDown:
                raise
            except nodeops.OpError:   # a container went away between ps and stats; the next poll asks again
                stats = {}
        except DockerDown as e:
            return dict(out, docker=False, error=str(e))
        for node, e in sorted(reg.items()):
            c, st = ps.get(e["container"]), stats.get(e["container"]) or {}
            out["sandboxes"].append({
                "node": node, "container": e["container"], "image": e.get("image"), "port": e.get("port"),
                "memory_mb": e.get("memory_mb"), "cpus": e.get("cpus"), "adopted": bool(e.get("adopted")),
                "created": e.get("created"), "state": c.get("State") if c else "missing",
                "cpu_pct": st.get("cpu_pct"), "mem_used_mb": st.get("mem_used_mb"),
                "mem_limit_mb": st.get("mem_limit_mb") if e.get("memory_mb") else None,
                "login_command": shlex.join(login_argv(e["port"])) if e.get("port") else None})
        return out

    # -- create job

    def create(self, name, image, memory_mb=None, cpus=None, allowed_tokens=None, wait=False, report=None):
        """Validate, then run the job on a thread (wait=False, the API) or here (the CLI). report(job) sees each phase."""
        if not isinstance(name, str) or not config.NODE_RE.match(name):
            raise nodeops.OpError("invalid node name")
        if image not in IMAGES:
            raise nodeops.OpError("image must be one of %s" % ", ".join(IMAGES))
        if memory_mb is not None and (isinstance(memory_mb, bool) or not isinstance(memory_mb, int)
                                      or memory_mb < MIN_MEMORY_MB):
            raise nodeops.OpError("memory_mb is a whole number of MB, at least %d" % MIN_MEMORY_MB)
        allowed_tokens = [] if allowed_tokens is None else allowed_tokens
        nodeops.check_labels(allowed_tokens)
        if cpus is not None:
            top = self.cpus_max()
            if isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or not 1 <= cpus <= top:
                raise nodeops.OpError("cpus is a number from 1 to %d" % top)
        missing = [k for k in build_chain(image) if self.inspect("image", IMAGES[k]["tag"]) is None]
        with self.lock:
            old = self.jobs.get(name)
            if old and old["phase"] in RUNNING:
                raise nodeops.OpError("a sandbox job for %s is already running" % name, 409)
            if name in self.busy:
                raise nodeops.OpError("sandbox %s is being stopped or deleted" % name, 409)
            if name in config.load_nodes() and not (old and old["phase"] == "failed"):
                raise nodeops.OpError("node %s already exists" % name, 409)
            job = self.jobs[name] = {
                "node": name, "phase": "building" if missing else "starting", "error": None, "log_tail": [],
                "started_at": config.now_iso(), "finished_at": None, "image": image, "memory_mb": memory_mb,
                "cpus": cpus, "allowed_tokens": list(allowed_tokens), "port": None, "missing": missing}
        if wait:
            self._run(job, report)
        else:
            threading.Thread(target=self._run, args=(job, report), daemon=True, name="sandbox-" + name).start()
        return self.public(job)

    def _phase(self, job, phase, report):
        with self.lock:
            job["phase"] = phase
            if phase in ("done", "failed"):
                job["finished_at"] = config.now_iso()
        if report:
            report(self.public(job))

    def _run(self, job, report=None):
        try:
            if job["missing"]:
                self._phase(job, "building", report)
                self._build(job)
            self._phase(job, "starting", report)
            self._start_container(job)
            self._phase(job, "waiting", report)
            if not self.reach(job["port"], WAIT_TIMEOUT):
                raise nodeops.OpError("ssh to %s port %d did not answer within %d s" % (TARGET, job["port"], WAIT_TIMEOUT), 504)
            self._phase(job, "adding", report)
            self._add(job)
            self._phase(job, "done", report)
        except Exception as e:
            job["error"] = str(e) or type(e).__name__
            self._phase(job, "failed", report)

    def _build(self, job):
        deadline = time.monotonic() + BUILD_TIMEOUT

        def tail(line):
            with self.lock:
                job["log_tail"] = (job["log_tail"] + [line])[-LOG_LINES:]
        for key in job["missing"]:
            img = IMAGES[key]
            left = max(1, int(deadline - time.monotonic()))
            code, _, err = self.docker("build", "-t", img["tag"], os.path.join(REPO, img["dir"]), timeout=left, on_line=tail)
            if code != 0:
                raise nodeops.OpError("docker build %s failed: %s" % (img["tag"], last_line(err) or
                                                                       (job["log_tail"] or ["exit %d" % code])[-1]), 502)

    def _start_container(self, job):
        """Reuse a container this hub labelled for the node, else create it on a free port with its two volumes."""
        name = job["node"]
        container = "cairn-" + name
        info = self.inspect("container", container)
        if info is not None:
            if label_of(info) != name:
                raise nodeops.OpError("container %s exists and is not labelled %s=%s; the hub leaves it alone"
                                      % (container, LABEL, name), 409)
            job["port"] = ssh_port(info)
            if job["port"] is None:
                raise nodeops.OpError("container %s does not publish 22/tcp; delete it and create the sandbox again" % container, 409)
            self._register(job, container, created=False)
            if not (info.get("State") or {}).get("Running"):
                self.mutate(name, "start")
            return
        pubkey = pubkey_path()
        self._reserve_port(job)
        for vol in volumes(container):
            self.check("volume", "create", "--label", "%s=%s" % (LABEL, name), vol)
            if ((self.inspect("volume", vol) or {}).get("Labels") or {}).get(LABEL) != name:
                raise nodeops.OpError("volume %s already exists and is not labelled %s=%s" % (vol, LABEL, name), 409)
        self.ps_cache = (0.0, None)
        self.check(*run_argv(name, container, job["image"], job["port"], job["memory_mb"], job["cpus"], pubkey))
        forget_host_key(job["port"])   # a new container on a reused port brings its own host key
        self._register(job, container, created=True)

    def _reserve_port(self, job):
        ids = self.check("ps", "-aq").split()
        published = set()
        if ids:
            code, out, err = self.docker("container", "inspect", *ids)
            if code != 0 and not out.strip():   # a container removed since ps still leaves the others' JSON
                raise nodeops.OpError("docker container inspect: %s" % (last_line(err) or "exit %d" % code), 502)
            published = bound_ports(json.loads(out or "[]"))
        taken = {e.get("port") for e in config.load_sandboxes().values()}
        taken |= {n.get("port") or 22 for n in config.load_nodes().values()
                  if (n.get("ssh") or "").rpartition("@")[2] in ("127.0.0.1", "localhost")}
        with self.lock:
            taken |= {j["port"] for j in self.jobs.values() if j is not job and j["phase"] in RUNNING}
            job["port"] = pick_port(published, taken, self.bindable)

    def _register(self, job, container, created):
        """A new container replaces any entry left from an old one; a reused one keeps its entry, port refreshed."""
        with nodeops.LOCK:
            reg = config.load_sandboxes()
            fresh = {"container": container, "image": job["image"], "port": job["port"], "memory_mb": job["memory_mb"],
                     "cpus": job["cpus"], "created": config.now_iso(), "adopted": False}
            entry = fresh if created or job["node"] not in reg else dict(reg[job["node"]], container=container,
                                                                         port=job["port"])
            if reg.get(job["node"]) != entry:
                reg[job["node"]] = entry
                config.save_sandboxes(reg)

    def _add(self, job):
        name, port = job["node"], job["port"]
        node = config.load_nodes().get(name)
        if node is None:
            nodeops.add_node(name, TARGET, port, job["allowed_tokens"])
        elif node.get("ssh") != TARGET or node.get("port") != port:
            raise nodeops.OpError("node %s already exists with another target (%s port %s)"
                                  % (name, node.get("ssh"), node.get("port")), 409)
        if self.on_added:
            try:
                self.on_added(name)
            except Exception as e:
                print("sandbox: %s: sync after add: %s: %s" % (name, type(e).__name__, e), flush=True)

    # -- lifecycle

    def state_of(self, entry):
        info = self.inspect("container", entry["container"])
        return (info.get("State") or {}).get("Status") if info else "missing"

    def start(self, node):
        entry, info = self.touchable(node)
        if info is None:
            raise nodeops.OpError("container %s is gone; delete the sandbox and create it again" % entry["container"], 409)
        self.mutate(node, "start")
        self.reach(entry["port"], 20)   # best effort, so the sync right after sees the node
        return {"node": node, "state": self.state_of(entry)}

    @contextlib.contextmanager
    def exclusive(self, node):
        """Refused (409) while a create job for the node runs; holds off a new one until the block ends."""
        with self.lock:
            job = self.jobs.get(node)
            if job and job["phase"] in RUNNING:
                raise nodeops.OpError("a create job for %s is running; wait for it or let it fail" % node, 409)
            if node in self.busy:
                raise nodeops.OpError("sandbox %s is already being stopped or deleted" % node, 409)
            self.busy.add(node)
        try:
            yield
        finally:
            with self.lock:
                self.busy.discard(node)

    def stop(self, node, snapshot, force=False):
        with self.exclusive(node):
            entry, info = self.touchable(node)
            if info is None:
                raise nodeops.OpError("container %s is gone" % entry["container"], 409)
            check_idle(node, snapshot, entry, info, force)
            self.mutate(node, "stop")
            return {"node": node, "state": self.state_of(entry)}

    def recreate(self, node, snapshot, force=False):
        """A new container from the image as it is now, on the same port with the same two volumes, so the home
        folder (the node's own Claude login, its repos) and the host key stay. Refused as stop is. A container
        already gone is simply run again, so a recreate that failed after the removal can be repeated."""
        with self.exclusive(node):
            entry, info = self.touchable(node)
            if entry.get("image") not in IMAGES:
                raise nodeops.OpError("sandbox %s was adopted and its image is not one the hub builds; recreate its "
                                      "container by hand" % node, 409)
            check_idle(node, snapshot, entry, info, force)
            gone = [v for v in volumes(entry["container"]) if self.inspect("volume", v) is None]
            if gone:   # docker run would make it again, empty and without the label the touch rule needs
                raise nodeops.OpError("volume %s is gone, so a new container would not keep what it held; delete the "
                                      "sandbox and create it again" % ", ".join(gone), 409)
            tag = IMAGES[entry["image"]]["tag"]
            if self.inspect("image", tag) is None:
                raise nodeops.OpError("image %s is missing; build it first (a new sandbox of that image builds it)"
                                      % tag, 409)
            argv = run_argv(node, entry["container"], entry["image"], entry["port"], entry.get("memory_mb"),
                            entry.get("cpus"), pubkey_path())
            if info is not None:
                self.mutate(node, "rm")
            self.ps_cache = (0.0, None)
            code, _, err = self.docker(*argv)
            if code != 0:
                raise nodeops.OpError("the old container of %s is removed, its volumes kept, and the new one did not "
                                      "start: %s; Recreate again" % (node, last_line(err) or "exit %d" % code), 502)
            with nodeops.LOCK:
                reg = config.load_sandboxes()
                if node in reg:
                    reg[node] = dict(reg[node], created=config.now_iso())
                    config.save_sandboxes(reg)
            self.reach(entry["port"], 20)   # best effort, so the sync right after sees the node
            return {"node": node, "state": self.state_of(entry)}

    def remove(self, node, confirm, snapshot, force=False):
        if confirm != node:
            raise nodeops.OpError("confirm must be the node name %s" % node)
        with self.exclusive(node):
            return self._remove(node, snapshot, force)

    def _remove(self, node, snapshot, force=False):
        """Container, both volumes, the node, its known_hosts entry and the registry entry. The mirror stays."""
        entry, info = self.touchable(node)
        check_idle(node, snapshot, entry, info, force)
        self.mutate(node, "rm")
        self.mutate(node, "volume-rm")
        if node in config.load_nodes():
            nodeops.remove_node(node, snapshot, force=True)
        forget_host_key(entry["port"])
        with nodeops.LOCK:
            reg = config.load_sandboxes()
            reg.pop(node, None)
            config.save_sandboxes(reg)
        with self.lock:
            self.jobs.pop(node, None)
        mirror = config.path("mirror/" + node)
        return {"removed": node, "mirror": mirror if os.path.isdir(mirror) else None}

    def adopt(self, node, container=None):
        """Record an existing container for an existing node; nothing in Docker changes."""
        cfg = config.load_nodes().get(node)
        if cfg is None:
            raise nodeops.OpError("unknown node %s" % node, 404)
        container = container or "cairn-" + node
        if node in config.load_sandboxes():
            raise nodeops.OpError("%s is already a sandbox" % node, 409)
        info = self.inspect("container", container)
        if info is None or info.get("Name") != "/" + container:
            raise nodeops.OpError("no container named %s" % container, 404)
        port = ssh_port(info)
        if port is None or port != cfg.get("port"):
            raise nodeops.OpError("container %s publishes 22/tcp on port %s, node %s uses port %s"
                                  % (container, port, node, cfg.get("port")), 409)
        mounted = {m.get("Name") for m in info.get("Mounts") or [] if m.get("Type") == "volume"}
        missing = [v for v in volumes(container) if v not in mounted]
        if missing:
            raise nodeops.OpError("container %s does not mount %s" % (container, ", ".join(missing)), 409)
        hc = info.get("HostConfig") or {}
        entry = {"container": container, "port": port, "adopted": True,
                 "image": next((k for k, v in IMAGES.items() if v["tag"] == (info.get("Config") or {}).get("Image")), None),
                 "memory_mb": (hc.get("Memory") or 0) // MiB or None, "cpus": (hc.get("NanoCpus") or 0) / 1e9 or None,
                 "created": (info.get("Created") or "")[:19] + "Z" if info.get("Created") else config.now_iso()}
        with nodeops.LOCK:
            reg = config.load_sandboxes()
            if node in reg:
                raise nodeops.OpError("%s is already a sandbox" % node, 409)
            reg[node] = entry
            config.save_sandboxes(reg)
        return dict(entry, node=node)
