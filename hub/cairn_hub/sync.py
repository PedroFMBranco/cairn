"""Per-node sync threads: one snapshot call every 30 s, last good snapshot kept, then auto-start checks.
Mirror work is only queued here; the mirror's own worker runs git."""
import subprocess
import threading
from datetime import datetime, timezone

from . import VERSION, attention as attn, config, nodeops, rotation, ssh
from .attention import Attention
from .autostart import AutoStarter
from .mirror import Mirror, mirror_path
from .notify import Notifier
from .runner import Runner

INTERVAL = 30


class Syncer:
    def __init__(self, interval=INTERVAL, autostart=None, mirror=None, attention=None, notifier=None, runner=None,
                 sandboxes=None):
        self.interval = interval
        self.sandboxes = sandboxes   # a sandbox.Sandboxes, so a deliberately stopped sandbox is not ssh-ed
        self.autostart = autostart or AutoStarter()
        self.mirror = mirror or Mirror()
        self.attention = attention or Attention(restart=nodeops.restart)
        self.notifier = notifier or Notifier()
        self.runner = runner or Runner()
        self.ancestry = {}
        self.lock = threading.Lock()
        self.entries = {}   # name -> {"ok", "error", "stopped", "synced_at", "snapshot"}
        self.threads = {}
        self.wake = {}      # name -> Event
        self.threads_lock = threading.Lock()
        self.stopping = threading.Event()

    def _sync_node(self, name, force_mirror=False):
        node = config.load_nodes().get(name)
        if node is None:   # removed, perhaps by `cairn node rm` in a terminal
            self.forget(name)
            return
        if self.sandboxes is not None and self.sandboxes.stopped(name):
            upd = {"ok": False, "error": "sandbox stopped", "stopped": True}
        else:
            try:
                snap = ssh.snapshot(node, timeout=25)
                upd = {"ok": True, "error": None, "stopped": False, "synced_at": config.now_iso(), "snapshot": snap}
            except Exception as e:
                upd = {"ok": False, "error": str(e), "stopped": False}
        with self.lock:
            if name not in config.load_nodes():
                return   # removed while its snapshot was read: forget() has run, or this node's next sync runs it
            cur = self.entries.setdefault(name, {"ok": False, "error": None, "synced_at": None, "snapshot": None})
            seen = {w["warning"] for w in (cur["snapshot"] or {}).get("warnings") or []}
            cur.update(upd)
        for w in (upd.get("snapshot") or {}).get("warnings") or []:
            # the hub's own notes only (the node's text is the lead's to write), each once
            if w["warning"].startswith(ssh.DROPPED) and w["warning"] not in seen:
                print("sync: %s: %s" % (name, w["warning"]), flush=True)
        if upd["ok"]:
            nodeops.VERSIONS[name] = (upd["snapshot"] or {}).get("node_version")
        if upd["ok"]:
            snap = upd["snapshot"]
            for what, step in (("autostart", lambda: self.autostart.check(name, snap)),
                               ("attention", lambda: self.attention.check(name, snap)),
                               ("notify", lambda: self.notifier.check(name, snap, self.entries_for(name, snap),
                                                                      self.attention.current(),
                                                                      self.attention.open_episodes()))):
                try:
                    step()
                except Exception as e:
                    print("%s: %s: %s: %s" % (what, name, type(e).__name__, e), flush=True)
            self.mirror.enqueue(name, node, snap, force=force_mirror)

    def forget(self, name):
        """Drop what the hub keeps for a removed node, so one added again under its name starts clean."""
        with self.lock:
            self.entries.pop(name, None)
        self.attention.forget_node(name)
        self.autostart.forget_node(name)

    def _loop(self, name, wake):
        while not self.stopping.is_set():
            self._sync_node(name)
            wake.wait(self.interval)
            wake.clear()

    def refresh_nodes(self):
        """Start a thread for every configured node that has none."""
        with self.threads_lock:
            for name in config.load_nodes():
                t = self.threads.get(name)
                if t is None or not t.is_alive():
                    ev = threading.Event()
                    t = threading.Thread(target=self._loop, args=(name, ev), daemon=True, name="sync-" + name)
                    self.wake[name] = ev
                    self.threads[name] = t
                    t.start()

    def _watch_nodes(self):
        """Pick up nodes added outside the hub (`cairn node add`) without a Refresh."""
        while not self.stopping.wait(self.interval):
            try:
                self.refresh_nodes()
            except Exception as e:   # e.g. a hand-edited nodes.json that is not valid JSON; retry next round
                print("sync: cannot read nodes: %s: %s" % (type(e).__name__, e), flush=True)

    def start(self):
        self.refresh_nodes()
        threading.Thread(target=self._watch_nodes, daemon=True, name="sync-nodes").start()

    def stop(self):
        self.stopping.set()
        with self.threads_lock:
            events = list(self.wake.values())
        for ev in events:
            ev.set()
        self.mirror.stop()

    def sync_now(self, force_mirror=True):
        """Sync every node in parallel and wait for all. Failing mirrors are retried now, not after their backoff."""
        names = list(config.load_nodes())
        ts = [threading.Thread(target=self._sync_node, args=(n, force_mirror)) for n in names]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.refresh_nodes()

    def sync_one(self, name, force_mirror=False):
        """Sync a single node now and wait."""
        self._sync_node(name, force_mirror)
        self.refresh_nodes()

    def result_ready(self, p, entry, node_name=None):
        """Done, with a committed result whose commit (the last one that changed work/result.md) has not been
        copied. Dashboard commits after it (a ticket, Update LEAD.md) do not make it ready again. Node shas are
        abbreviated and grow with the repo, so they match by prefix. A copy from before copied_result_sha counts
        when its commit contains the result commit."""
        entry = entry or {}
        result_sha = p.get("result_sha")
        if not (p.get("status") == "done" and p.get("result") is not None and result_sha):
            return False
        copied = entry.get("copied_result_sha")
        if copied:
            return not (copied.startswith(result_sha) or result_sha.startswith(copied))
        old = entry.get("copied_sha")
        if not old:
            return True
        if old.startswith(result_sha) or result_sha.startswith(old):
            return False
        return not self.contains(node_name, p.get("name"), old, result_sha)

    def contains(self, node_name, project, commit, ancestor):
        """Whether `commit` contains `ancestor` in the project's mirror (cached; False when unknown)."""
        key = (node_name, project, commit, ancestor)
        if key not in self.ancestry:
            r = subprocess.run(["git", "--git-dir", mirror_path(node_name, project), "merge-base", "--is-ancestor",
                                ancestor, commit], capture_output=True)
            self.ancestry[key] = r.returncode == 0
        return self.ancestry[key]

    def entries_for(self, name, snapshot):
        projects = config.load_projects()
        out = {}
        for p in (snapshot or {}).get("projects") or []:
            key = "%s/%s" % (name, p.get("name"))
            out[key] = {"result_ready": self.result_ready(p, projects.get(key), name)}
        return out

    def fresh_snapshots(self):
        """Every node's snapshot as it is now, each node synced first: None for one that cannot be read now, and an
        empty one for a deliberately stopped sandbox, which runs nothing."""
        self.sync_now(force_mirror=False)
        with self.lock:
            entries = {n: self.entries.get(n) or {} for n in config.load_nodes()}
        return {n: e.get("snapshot") if e.get("ok") else {"projects": []} if e.get("stopped") else None
                for n, e in entries.items()}

    def current_snapshot(self, name):
        """The node's snapshot only when its last sync succeeded; None while it is unreachable or not synced."""
        with self.lock:
            e = self.entries.get(name) or {}
            return e.get("snapshot") if e.get("ok") else None

    def state(self):
        nodes_cfg = config.load_nodes()
        sandboxes = config.load_sandboxes()
        out = []
        with self.lock:
            for name, cfg in sorted(nodes_cfg.items()):
                e = self.entries.get(name) or {"ok": False, "error": "not synced yet",
                                               "synced_at": None, "snapshot": None}
                version = (e["snapshot"] or {}).get("node_version")
                out.append({"name": name, "ssh": cfg.get("ssh"), "port": cfg.get("port"),
                            "allowed_tokens": cfg.get("allowed_tokens") or [],
                            "node_version": version, "update_available": nodeops.update_available(version),
                            "sandbox": name in sandboxes, "ok": e["ok"], "error": e["error"],
                            "stopped": bool(e.get("stopped")),
                            "synced_at": e["synced_at"], "snapshot": e["snapshot"]})
        stamps = [n["synced_at"] for n in out if n["synced_at"]]
        in_use = nodeops.tokens_in_use({n["name"]: n["snapshot"] for n in out})
        saved, now = config.load_limits(), datetime.now(timezone.utc)
        limits = {k: {"until": v["until"], "known": v["known"]} for k, v in saved.items()
                  if rotation.limited_until(saved, k, now)}
        tokens = config.public_tokens()
        for t in tokens:
            t["in_use"] = in_use.get(t["label"], [])
            # a node-login label's limit is that of the login on each node that allows it (attention.limit_key)
            keys = [(None, t["label"])] if t["kind"] != "node-login" else [
                (n, "node-login@" + n) for n, c in sorted(nodes_cfg.items()) if t["label"] in (c.get("allowed_tokens") or [])]
            t["limits"] = [dict(limits[k], node=n) for n, k in keys if k in limits]
        projects = config.load_projects()
        ready = {}
        for n in out:
            for p in (n["snapshot"] or {}).get("projects") or []:
                key = "%s/%s" % (n["name"], p["name"])
                ready[key] = self.result_ready(p, projects.setdefault(key, {}), n["name"])
        attention = self.attention.current()
        for key, entry in projects.items():
            node_name, _, project = key.partition("/")
            entry.setdefault("token", config.token_label_for(node_name, project))
            entry["tokens"] = config.token_pool(node_name, project)
            entry["limits"] = attn.limits_for(node_name, entry["tokens"], limits)
            for field in ("model", "subagent_model", "copied_sha", "copied_to", "copied_result_sha"):
                entry.setdefault(field, None)
            entry["result_ready"] = ready.get(key, False)
            entry["attention"] = attention.get(key)
            entry["running_url"] = self.runner.url(key)
            entry["mirror"] = self.mirror.get(node_name, project)
        return {"hub_version": VERSION,
                "synced_at": max(stamps) if stamps else config.now_iso(),
                "nodes": out,
                "tokens": tokens,
                "projects": projects,
                "notify": self.notifier.view(with_topic=False)}
