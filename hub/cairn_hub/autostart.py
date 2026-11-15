"""Restart an idle lead once its questions have new answers (replaces the review skill's restart)."""
import threading

from . import nodeops, ssh


def log(msg):
    print("autostart: " + msg, flush=True)


def pending(proj):
    """Answered question ids the lead has not applied, when the project is ready for a restart."""
    if (proj.get("lead") != "dead" or proj.get("review") == "alive"
            or proj.get("status") not in ("idle", "unknown")):
        return None
    ids = frozenset(q["id"] for q in proj.get("questions") or []
                    if q.get("answered") and q.get("applied") is None)
    return ids or None


class AutoStarter:
    def __init__(self, log=log):
        self.log = log
        self.lock = threading.Lock()
        self.started = {}       # (node, project) -> answer-id set last auto-started
        self.token_errors = {}  # (node, project) -> last logged token error
        self.busy = None        # (node, project) -> True while a review chat is live

    def forget_node(self, node_name):
        with self.lock:
            for d in (self.started, self.token_errors):
                for key in [k for k in d if k[0] == node_name]:
                    del d[key]

    def check(self, node_name, snapshot):
        for proj in snapshot.get("projects") or []:
            ids = pending(proj)
            if ids and not (self.busy and self.busy(node_name, proj["name"])):
                self._start(node_name, proj["name"], ids)

    def _start(self, node_name, project, ids):
        key = (node_name, project)
        with self.lock:
            if self.started.get(key) == ids:
                return
            self.started[key] = ids   # claimed, so another sync thread does not start it too
        try:
            line = nodeops.token_line(node_name, project)   # may wait on the Keychain: never under the lock
        except BaseException as e:
            with self.lock:
                if self.started.get(key) == ids:
                    del self.started[key]   # tried again next sync
                if not isinstance(e, nodeops.OpError):
                    raise
                if self.token_errors.get(key) != str(e):
                    self.token_errors[key] = str(e)
                    self.log("%s/%s skipped: %s" % (node_name, project, e))
            return
        with self.lock:
            self.token_errors.pop(key, None)
        names = ", ".join(sorted(ids))
        try:
            nodeops.start(node_name, project, token=line)
            self.log("%s/%s started for answers %s" % (node_name, project, names))
        except (nodeops.OpError, ssh.NodeError) as e:
            self.log("%s/%s start failed for answers %s: %s" % (node_name, project, names, e))
