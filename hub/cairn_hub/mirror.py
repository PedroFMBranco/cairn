"""Bare mirrors of every project repo under ~/.cairn/mirror/<node>/<project>.git, refreshed when the newest commit changes.

Syncs and requests only enqueue; one background worker runs git, so a slow clone never holds up a snapshot or a request."""
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time

from . import config, ssh

TIMEOUT = 300
ERR_TAIL = 4096   # bytes of git's stderr read back for the error line; the node's git server can print without end
BACKOFF_MIN, BACKOFF_MAX = 30, 1800  # a failing project waits 30 s, then twice as long after each failure, up to 30 min
# The mirror is the backup, so it keeps a reflog of every ref change it fetches (a lead's reset or force-push)
# and never expires it, which keeps the objects those changes dropped from being pruned.
KEEP_HISTORY = (("core.logAllRefUpdates", "always"), ("gc.reflogExpire", "never"),
                ("gc.reflogExpireUnreachable", "never"))


def mirror_path(node_name, project):
    return config.path("mirror/%s/%s.git" % (node_name, project))


def remote_url(node, repo_path):
    """ssh://<ssh>[:port]/<path>; a local node is cloned from its path directly. The path comes from the node, so
    without its leading / it would run on into the host part of the URL."""
    if not isinstance(repo_path, str) or not repo_path.startswith("/"):
        raise ValueError("project path %r is not absolute" % (repo_path,))
    if ssh.is_local(node):
        return repo_path
    port = ":%d" % int(node["port"]) if node.get("port") else ""
    return "ssh://%s%s%s" % (node["ssh"], port, repo_path)


def git_env():
    env = dict(os.environ)
    env["GIT_SSH_COMMAND"] = " ".join(shlex.quote(a) for a in ["ssh"] + ssh.ssh_options())
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def is_mirror(dest):
    return os.path.isfile(os.path.join(dest, "HEAD")) and os.path.isdir(os.path.join(dest, "objects"))


def newest_sha(proj):
    commits = proj.get("commits") or []
    return commits[0].get("sha") if commits else None


class Mirror:
    def __init__(self, run=subprocess.run, clock=time.monotonic):
        self.run = run
        self.clock = clock
        self.lock = threading.Lock()
        self.states = {}  # "<node>/<project>" -> {"path", "sha", "synced_at", "error"}
        self.project_locks = {}  # concurrent syncs of one node must not clone the same mirror twice
        self.retry = {}  # "<node>/<project>" -> (failures in a row, clock() before which it is not retried)
        self.configured = set()  # mirror paths whose KEEP_HISTORY settings were checked by this process
        self.pending = {}  # "<node>/<project>" -> (node_name, node, proj); a newer snapshot replaces a queued one
        self.active = 0
        self.changed = threading.Condition(self.lock)
        self.worker = None
        self.stopping = False

    def enqueue(self, node_name, node, snapshot, force=False):
        """Queue the snapshot's projects for the worker and return at once. `force` skips the failure backoff."""
        with self.lock:
            for proj in snapshot.get("projects") or []:
                key = "%s/%s" % (node_name, proj.get("name"))
                self.pending[key] = (node_name, node, proj)
                if force:
                    self.retry.pop(key, None)
            if self.pending and not self.stopping and (self.worker is None or not self.worker.is_alive()):
                self.worker = threading.Thread(target=self._work, daemon=True, name="mirror")
                self.worker.start()
            self.changed.notify_all()

    def _work(self):
        while True:
            with self.lock:
                while not self.pending and not self.stopping:
                    self.changed.wait()
                if self.stopping:
                    return
                key = next(iter(self.pending))
                node_name, node, proj = self.pending.pop(key)
                self.active += 1
            try:
                self.sync_node(node_name, node, {"projects": [proj]})
            finally:
                with self.lock:
                    self.active -= 1
                    self.changed.notify_all()

    def wait_idle(self, timeout=10):
        """Wait until nothing is queued or running. True when idle."""
        with self.lock:
            return self.changed.wait_for(lambda: not self.pending and not self.active, timeout)

    def stop(self):
        """Drop the queue and end the worker once its current git call returns."""
        with self.lock:
            self.stopping = True
            self.pending.clear()
            self.changed.notify_all()

    def get(self, node_name, project):
        with self.lock:
            s = self.states.get("%s/%s" % (node_name, project))
            return dict(s) if s else None

    def sync_node(self, node_name, node, snapshot):
        """Mirror each project whose newest commit differs from the mirror's. Never raises."""
        for proj in snapshot.get("projects") or []:
            try:
                self.sync_project(node_name, node, proj)
            except Exception as e:  # the sync must not fail because of a mirror
                self._record(node_name, proj.get("name"), error="%s: %s" % (type(e).__name__, e))

    def sync_project(self, node_name, node, proj):
        name = proj.get("name")
        if not newest_sha(proj) or not config.PROJECT_RE.match(name or ""):
            return
        with self.lock:
            lock = self.project_locks.setdefault("%s/%s" % (node_name, name), threading.Lock())
        with lock:
            self._sync_project(node_name, node, proj)

    def _sync_project(self, node_name, node, proj):
        name, sha = proj["name"], newest_sha(proj)
        key = "%s/%s" % (node_name, name)
        cur = self.get(node_name, name)
        if cur and cur["sha"] == sha and not cur["error"]:
            return
        with self.lock:
            failures, not_before = self.retry.get(key, (0, 0))
        if self.clock() < not_before:
            return
        dest = mirror_path(node_name, name)
        url = remote_url(node, proj["path"])
        err = None
        if is_mirror(dest):
            if dest not in self.configured:
                err = self._keep_history(dest, check=True)
                if not err:
                    self.configured.add(dest)
            err = err or self._git(["git", "--git-dir", dest, "fetch", "--prune", "--", url, "+refs/*:refs/*"])
        else:
            # Clone beside the mirror and move it into place, so a hub killed mid-clone leaves no
            # directory that looks like a mirror; a dest without HEAD/objects is such a leftover.
            os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
            tmp = dest + ".partial"
            shutil.rmtree(tmp, ignore_errors=True)
            err = self._git(["git", "clone", "--mirror", "--", url, tmp]) or self._keep_history(tmp, check=False)
            if err:
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                shutil.rmtree(dest, ignore_errors=True)
                os.replace(tmp, dest)
                self.configured.add(dest)
        with self.lock:
            if err:
                failures += 1
                self.retry[key] = (failures, self.clock() + min(BACKOFF_MAX, BACKOFF_MIN * 2 ** (failures - 1)))
            else:
                self.retry.pop(key, None)
        if err:
            self._record(node_name, name, error=err[:300])
        else:
            self._record(node_name, name, sha=sha, synced_at=config.now_iso(), error=None)

    def _keep_history(self, git_dir, check):
        """Set KEEP_HISTORY on a mirror; `check` reads each value first and sets only what is missing."""
        for k, v in KEEP_HISTORY:
            if check:
                try:
                    r = self.run(["git", "--git-dir", git_dir, "config", "--get", k], capture_output=True,
                                 text=True, timeout=30, stdin=subprocess.DEVNULL)
                    if r.returncode == 0 and (r.stdout or "").strip() == v:
                        continue
                except (OSError, subprocess.TimeoutExpired):
                    pass  # the set below reports the error
            err = self._git(["git", "--git-dir", git_dir, "config", k, v], timeout=30)
            if err:
                return err
        return None

    def _git(self, argv, timeout=TIMEOUT):
        """Run git; the error line, or None on success. stderr goes to a file whose last ERR_TAIL bytes are read."""
        with tempfile.TemporaryFile() as err:
            try:
                r = self.run(argv, env=git_env(), stdout=subprocess.DEVNULL, stderr=err, timeout=timeout,
                             stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                return "git timed out after %ds" % timeout
            except OSError as e:
                return "cannot run git: %s" % e.strerror
            if r.returncode == 0:
                return None
            err.seek(max(0, err.seek(0, os.SEEK_END) - ERR_TAIL))
            text = err.read().decode(errors="replace")
        return (text.strip().splitlines() or ["git exit %d" % r.returncode])[-1]

    def _record(self, node_name, project, **upd):
        if not project:
            return
        key = "%s/%s" % (node_name, project)
        with self.lock:
            s = self.states.setdefault(key, {"path": mirror_path(node_name, project), "sha": None,
                                             "synced_at": None, "error": None})
            s.update(upd)
