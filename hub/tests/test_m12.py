"""Milestone 12: local Docker sandboxes (docker, ssh reachability and the Keychain faked)."""
import json
import os
import shlex
import sys
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import test_m2 as m2
from cairn_hub import cli, config, nodeops, sandbox, ssh

REAL_RUN_DOCKER = sandbox.run_docker
DOWN = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"
CHECK = {"node_version": "0.2.0", "host": "sb", "free_ram_mb": 5, "free_disk_mb": 6, "deps": {"tmux": "3.3a"}, "projects": []}


class FakeDocker:
    """Stands in for `docker`: containers, volumes and images in memory, every argv logged."""

    def __init__(self):
        self.calls = []
        self.down = False
        self.fail_stats = 0    # the next n `docker stats` calls fail as for a container that just went away
        self.images = set()
        self.containers = {}   # name -> {"labels", "running", "port", "mounts", "image", "memory", "nanocpus"}
        self.volumes = {}      # name -> labels

    def add(self, name, port, labels=None, running=True, mounts=None, image="cairn-dev:0.1"):
        self.containers[name] = {"labels": labels or {}, "running": running, "port": port, "image": image,
                                 "mounts": volumes_of(name) if mounts is None else mounts, "memory": 0, "nanocpus": 0}
        for v in self.containers[name]["mounts"]:
            self.volumes.setdefault(v, dict(labels or {}))

    def mutations(self):
        return [c for c in self.calls if c[0] in ("run", "start", "stop", "rm", "build") or c[:2] in
                (["volume", "rm"], ["volume", "create"])]

    def __call__(self, argv, timeout, on_line=None):
        self.calls.append(list(argv))
        if self.down:
            return 1, "", DOWN
        ok = lambda out="": (0, out, "")
        no = lambda what: (1, "", "Error response from daemon: No such %s: %s" % (what, argv[-1]))
        cmd = argv[0] if argv[0] not in ("container", "image", "volume") else " ".join(argv[:2])
        if cmd == "info":
            return ok("8\n")
        if cmd == "image ls":
            return ok("\n".join(sorted(self.images)) + "\n")
        if cmd == "image inspect":
            return ok("[{}]") if argv[2] in self.images else no("image")
        if cmd == "build":
            on_line("#1 building " + argv[2])
            self.images.add(argv[2])
            return ok()
        if cmd == "ps":
            if argv[1:] == ["-aq"]:
                return ok("".join(n + "\n" for n in self.containers))
            return ok("".join(json.dumps({"Names": n, "State": "running" if c["running"] else "exited"}) + "\n"
                              for n, c in self.containers.items()))
        if cmd == "stats":
            if self.fail_stats:
                self.fail_stats -= 1
                return 1, "", "Error response from daemon: No such container: %s" % argv[-1]
            return ok("".join(json.dumps({"Name": n, "CPUPerc": "12.50%", "MemUsage": "512MiB / 2GiB"}) + "\n"
                              for n in argv[4:]))
        if cmd == "container inspect":
            found = [{
                "Name": "/" + n, "Created": "2026-10-20T01:17:51.003468048Z",
                "Config": {"Labels": c["labels"], "Image": c["image"]},
                "State": {"Running": c["running"], "Status": "running" if c["running"] else "exited"},
                "HostConfig": {"PortBindings": {"22/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(c["port"])}]},
                               "Memory": c["memory"], "NanoCpus": c["nanocpus"]},
                "Mounts": [{"Type": "volume", "Name": v} for v in c["mounts"]]}
                for n, c in ((n, self.containers.get(n)) for n in argv[2:]) if c is not None]
            if len(found) < len(argv[2:]):
                return 1, json.dumps(found) if found else "[]", "Error response from daemon: No such container: x"
            return ok(json.dumps(found))
        if cmd == "volume inspect":
            return ok(json.dumps([{"Name": argv[2], "Labels": self.volumes[argv[2]]}])) if argv[2] in self.volumes else no("volume")
        if cmd == "volume create":
            k, _, v = argv[3].partition("=")
            self.volumes.setdefault(argv[4], {k: v})
            return ok(argv[4] + "\n")
        if cmd == "volume rm":
            return ok() if self.volumes.pop(argv[2], None) is not None else no("volume")
        if cmd == "run":
            opt = lambda flag: [argv[i + 1] for i, a in enumerate(argv) if a == flag]
            k, _, v = opt("--label")[0].partition("=")
            name = opt("--name")[0]
            self.containers[name] = {"labels": {k: v}, "running": True, "image": argv[-1],
                                     "port": int(opt("-p")[0].split(":")[1]),
                                     "mounts": [m.split(":")[0] for m in opt("-v") if not m.startswith("/")],
                                     "memory": 0, "nanocpus": 0}
            return ok("f00d\n")
        if cmd in ("start", "stop"):
            if argv[1] not in self.containers:
                return no("container")
            self.containers[argv[1]]["running"] = cmd == "start"
            return ok(argv[1] + "\n")
        if cmd == "rm":
            return ok() if self.containers.pop(argv[-1], None) is not None else no("container")
        raise AssertionError("unexpected docker call %r" % argv)


def volumes_of(container):
    return [container + "-home", container + "-hostkeys"]


class SandboxBase(m2.ApiBase):
    def setUp(self):
        super().setUp()
        self.docker = FakeDocker()
        self.reachable = True
        self.bound = set()
        self.added = []
        self.sb = sandbox.Sandboxes(run=self.docker, reach=lambda port, timeout: self.reachable,
                                    bindable=lambda p: p not in self.bound, on_added=self.added.append)
        self.srv.sandboxes = self.sb
        self.syncer.sandboxes = self.sb
        # ssh to a sandbox: refused unless a test sets self.probe to the snapshot it answers with
        self.probe = None
        self.ssh_calls = []

        def run_node(node, argv, stdin=None, timeout=30):
            self.ssh_calls.append((node, argv, timeout))
            if self.probe is None:
                raise ssh.NodeError("ssh: connect to host 127.0.0.1 port %s: Connection refused" % node.get("port"))
            return self.probe
        # the CLI builds its own Sandboxes(): the defaults are faked too
        for target, name, fake in ((nodeops, "install", mock.Mock(return_value=CHECK)), (sandbox, "run_docker", self.docker),
                                   (sandbox, "ssh_reachable", lambda port, timeout: self.reachable),
                                   (sandbox, "port_free", lambda p: p not in self.bound), (ssh, "run_node", run_node)):
            p = mock.patch.object(target, name, fake)
            p.start()
            self.addCleanup(p.stop)
        self.synced = []
        p = mock.patch.object(self.syncer, "sync_one", self.synced.append)
        p.start()
        self.addCleanup(p.stop)
        os.makedirs(os.path.join(self.tmp, ".ssh"))
        self.write(os.path.join(self.tmp, ".ssh", "id_ed25519.pub"), "ssh-ed25519 AAAA test\n")

    def wait_job(self, name, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = next((j for j in json.loads(self.req("/api/sandboxes")[1])["jobs"] if j["node"] == name), None)
            if job and job["phase"] in ("done", "failed"):
                return job
            time.sleep(0.02)
        self.fail("job %s did not finish" % name)

    def set_snapshot(self, node, snap):
        with self.syncer.lock:
            self.syncer.entries[node] = {"ok": snap is not None, "error": None, "synced_at": "x", "snapshot": snap}

    def register(self, node, container=None, port=2230, adopted=False):
        reg = config.load_sandboxes()
        reg[node] = {"container": container or "cairn-" + node, "image": "slim", "port": port, "memory_mb": None,
                     "cpus": None, "created": "x", "adopted": adopted}
        config.save_sandboxes(reg)


class TouchRuleTests(SandboxBase):
    def test_unlabelled_unadopted_container_is_left_alone(self):
        self.docker.add("cairn-other", 2230)
        self.register("other")
        config.save_nodes({**config.load_nodes(), "other": {"ssh": sandbox.TARGET, "port": 2230, "allowed_tokens": []}})
        self.set_snapshot("other", {"projects": []})
        for path, body in (("/api/sandboxes/start", {"node": "other"}), ("/api/sandboxes/stop", {"node": "other"}),
                           ("/api/sandboxes/remove", {"node": "other", "confirm": "other"})):
            status, out = self.post(path, body)
            self.assertEqual(status, 409, out)
            self.assertIn("leaves it alone", out["error"])
        self.assertEqual(self.docker.mutations(), [])
        self.assertIn("cairn-other", self.docker.containers)
        self.assertIn("other", config.load_nodes())

    def test_label_for_another_node_is_refused(self):
        self.docker.add("cairn-mine", 2230, labels={"cairn.sandbox": "someone-else"})
        self.register("mine")
        self.assertEqual(self.post("/api/sandboxes/stop", {"node": "mine"})[0], 409)
        self.assertEqual(self.post("/api/sandboxes/remove", {"node": "mine", "confirm": "mine"})[0], 409)
        self.assertEqual(self.docker.mutations(), [])

    def test_unregistered_and_unlabelled_name_collision(self):
        self.docker.add("telecable-db", 5432)
        self.assertEqual(self.post("/api/sandboxes/stop", {"node": "telecable-db"})[0], 409)
        # a create whose container name is taken by something the hub did not label fails without touching it
        self.docker.images.add("cairn-slim:0.1")
        self.docker.add("cairn-taken", 2240)
        self.assertEqual(self.post("/api/sandboxes", {"name": "taken", "image": "slim", "allowed_tokens": []})[0], 200)
        job = self.wait_job("taken")
        self.assertEqual(job["phase"], "failed")
        self.assertIn("leaves it alone", job["error"])
        self.assertEqual(self.docker.mutations(), [])


class PortTests(SandboxBase):
    def test_pick_port_skips_published_taken_and_bound(self):
        self.assertEqual(sandbox.pick_port({2222, 2224}, {2223}, lambda p: p != 2225), 2226)
        infos = [{"HostConfig": {"PortBindings": {"22/tcp": [{"HostIp": "127.0.0.1", "HostPort": "2222"}],
                                                  "80/tcp": [{"HostPort": "2223-2225"}], "9/tcp": [{"HostPort": ""}]}}},
                 {"HostConfig": {"PortBindings": None}}, {}]
        self.assertEqual(sandbox.bound_ports(infos), {2222, 2223, 2224, 2225})
        with self.assertRaises(nodeops.OpError):
            sandbox.pick_port(set(sandbox.PORTS), set(), lambda p: True)

    def test_job_skips_published_registered_node_and_bound_ports(self):
        self.docker.images.add("cairn-slim:0.1")
        self.docker.add("cairn-strand", 2222)                            # published
        self.docker.add("cairn-old", 2223, running=False)                 # stopped: not in ps Ports, but registered
        self.register("old", port=2223)
        config.save_nodes({**config.load_nodes(), "tunnel": {"ssh": "agent@127.0.0.1", "port": 2224, "allowed_tokens": []}})
        self.bound = {2225}                                               # something else listens there
        self.post("/api/sandboxes", {"name": "fresh", "image": "slim", "allowed_tokens": []})
        self.assertEqual(self.wait_job("fresh")["phase"], "done")
        self.assertEqual(config.load_sandboxes()["fresh"]["port"], 2226)
        self.assertEqual(config.load_nodes()["fresh"], {"ssh": "agent@127.0.0.1", "port": 2226, "allowed_tokens": []})

    def test_port_bound_by_a_stopped_foreign_container_is_skipped(self):
        # `docker ps` prints no Ports for an exited container, yet its binding comes back when it starts
        self.docker.images.add("cairn-slim:0.1")
        self.docker.add("telecable-db", 2222, running=False)
        self.docker.add("cairn-strand", 2223)
        self.post("/api/sandboxes", {"name": "fresh", "image": "slim", "allowed_tokens": []})
        self.assertEqual(self.wait_job("fresh")["phase"], "done")
        self.assertEqual(config.load_sandboxes()["fresh"]["port"], 2224)
        inspects = [c for c in self.docker.calls if c[:2] == ["container", "inspect"] and len(c) > 3]
        self.assertEqual(inspects, [["container", "inspect", "telecable-db", "cairn-strand"]])
        self.assertFalse(self.docker.containers["telecable-db"]["running"])


class CreateTests(SandboxBase):
    def test_happy_path_builds_the_base_first_and_runs_every_phase(self):
        phases = []
        job = self.sb.create("dev1", "dev", memory_mb=2048, cpus=2, allowed_tokens=["oa"], wait=True,
                             report=lambda j: phases.append(j["phase"]))
        self.assertEqual((job["phase"], job["error"]), ("done", None))
        self.assertEqual(phases, ["building", "starting", "waiting", "adding", "done"])
        builds = [c[2] for c in self.docker.calls if c[0] == "build"]
        self.assertEqual(builds, ["cairn-slim:0.1", "cairn-dev:0.1"])
        self.assertEqual(job["log_tail"], ["#1 building cairn-slim:0.1", "#1 building cairn-dev:0.1"])
        run = next(c for c in self.docker.calls if c[0] == "run")
        self.assertEqual(run[run.index("--label") + 1], "cairn.sandbox=dev1")
        self.assertEqual((run[run.index("--memory") + 1], run[run.index("--cpus") + 1], run[-1]), ("2048m", "2", "cairn-dev:0.1"))
        self.assertIn("127.0.0.1:2222:22", run)
        self.assertIn(os.path.join(self.tmp, ".ssh", "id_ed25519.pub") + ":/etc/cairn/authorized_key:ro", run)
        self.assertEqual({v: self.docker.volumes[v] for v in volumes_of("cairn-dev1")},
                         {v: {"cairn.sandbox": "dev1"} for v in volumes_of("cairn-dev1")})
        entry = config.load_sandboxes()["dev1"]
        self.assertEqual({k: entry[k] for k in ("container", "image", "port", "memory_mb", "cpus", "adopted")},
                         {"container": "cairn-dev1", "image": "dev", "port": 2222, "memory_mb": 2048, "cpus": 2, "adopted": False})
        self.assertEqual(config.load_nodes()["dev1"]["allowed_tokens"], ["oa"])
        self.assertEqual(self.added, ["dev1"])
        self.assertTrue(self.state_node("dev1")["sandbox"])
        self.assertFalse(self.state_node("lab")["sandbox"])

    def state_node(self, name):
        return next(n for n in self.state()["nodes"] if n["name"] == name)

    def test_retry_after_failed_waiting_reuses_the_container(self):
        self.docker.images.add("cairn-slim:0.1")
        self.reachable = False
        self.assertEqual(self.post("/api/sandboxes", {"name": "slow", "image": "slim", "allowed_tokens": []})[0], 200)
        job = self.wait_job("slow")
        self.assertEqual(job["phase"], "failed")
        self.assertIn("did not answer", job["error"])
        self.assertNotIn("slow", config.load_nodes())
        self.assertIn("slow", config.load_sandboxes())     # registered after starting, so Delete can clean it up
        self.docker.containers["cairn-slow"]["running"] = False
        self.reachable = True
        status, out = self.post("/api/sandboxes", {"name": "slow", "image": "slim", "allowed_tokens": []})
        self.assertEqual((status, out["job"]["phase"] in sandbox.PHASES), (200, True), out)
        self.assertEqual(self.wait_job("slow")["phase"], "done")
        self.assertEqual(len([c for c in self.docker.calls if c[0] == "run"]), 1)
        self.assertEqual([c for c in self.docker.calls if c[0] == "start"], [["start", "cairn-slow"]])
        self.assertEqual(config.load_nodes()["slow"]["port"], 2222)

    def test_stop_and_delete_wait_for_a_running_create_job(self):
        self.docker.images.add("cairn-slim:0.1")
        parked, gate = threading.Event(), threading.Event()

        def reach(port, timeout):
            parked.set()
            return gate.wait(5)
        self.sb.reach = reach
        self.assertEqual(self.post("/api/sandboxes", {"name": "orph", "image": "slim", "allowed_tokens": []})[0], 200)
        self.assertTrue(parked.wait(5))
        for path, body in (("/api/sandboxes/stop", {"node": "orph"}), ("/api/sandboxes/remove", {"node": "orph", "confirm": "orph"})):
            status, out = self.post(path, body)
            self.assertEqual(status, 409, out)
            self.assertIn("a create job for orph is running", out["error"])
        gate.set()
        self.assertEqual(self.wait_job("orph")["phase"], "done")
        self.assertEqual(config.load_nodes()["orph"]["port"], config.load_sandboxes()["orph"]["port"])
        self.assertTrue(self.docker.containers["cairn-orph"]["running"])
        self.assertEqual(sorted(v for v in self.docker.volumes if v.startswith("cairn-orph")), volumes_of("cairn-orph"))
        self.assertEqual([c[0] for c in self.docker.mutations()], ["volume", "volume", "run"])

    def test_a_node_login_label_is_allowed_on_the_new_node_and_no_secret_is_read(self):
        self.docker.images.add("cairn-slim:0.1")
        self.kc.store.clear()   # a node-login token has no Keychain item; reading one would fail
        status, out = self.post("/api/sandboxes", {"name": "own", "image": "slim", "allowed_tokens": ["nl"]})
        self.assertEqual(status, 200, out)
        self.assertEqual(self.wait_job("own")["phase"], "done")
        self.assertEqual(config.load_nodes()["own"]["allowed_tokens"], ["nl"])
        self.assertEqual(self.post("/api/sandboxes", {"name": "own2", "image": "slim", "allowed_tokens": ["ghost"]})[0], 400)

    def test_validation_and_running_job(self):
        bad = [{"name": "Bad Name", "image": "slim"}, {"name": "ok", "image": "ubuntu"},
               {"name": "ok", "image": "slim", "memory_mb": 512}, {"name": "ok", "image": "slim", "memory_mb": "2048"},
               {"name": "ok", "image": "slim", "cpus": 9}, {"name": "ok", "image": "slim", "cpus": 0},
               {"name": "ok", "image": "slim", "allowed_tokens": ["nope"]}]
        for body in bad:
            self.assertEqual(self.post("/api/sandboxes", body)[0], 400, body)
        self.assertEqual(self.post("/api/sandboxes", {"name": "lab", "image": "slim"})[0], 409)   # an existing node
        self.assertEqual(self.docker.mutations(), [])
        with self.sb.lock:
            self.sb.jobs["busy"] = {"node": "busy", "phase": "building", "started_at": "x", "port": None}
        self.assertEqual(self.post("/api/sandboxes", {"name": "busy", "image": "slim"})[0], 409)


class LifecycleTests(SandboxBase):
    def setUp(self):
        super().setUp()
        self.docker.images.add("cairn-slim:0.1")
        self.docker.add("cairn-strand", 2222)
        self.sb.create("box", "slim", wait=True)
        self.docker.volumes["telecable-db"] = {}
        self.box_port = config.load_sandboxes()["box"]["port"]
        self.calls_before = len(self.docker.calls)

    def mutations_since(self):
        return [c for c in self.docker.calls[self.calls_before:] if c in self.docker.mutations()]

    def test_stop_and_remove_refused_while_a_lead_is_alive(self):
        self.set_snapshot("box", {"projects": [m2.snap_project("demo", lead="alive")]})
        for path, body in (("/api/sandboxes/stop", {"node": "box"}), ("/api/sandboxes/remove", {"node": "box", "confirm": "box"})):
            status, out = self.post(path, body)
            self.assertEqual(status, 409)
            self.assertIn("demo", out["error"])
        self.set_snapshot("box", None)   # unknown while the container runs: live sessions cannot be checked
        self.assertEqual(self.post("/api/sandboxes/stop", {"node": "box"})[0], 409)
        self.assertEqual(self.mutations_since(), [])

    def test_stop_then_start(self):
        self.set_snapshot("box", {"projects": [m2.snap_project("demo", lead="dead")]})
        self.assertEqual(self.post("/api/sandboxes/stop", {"node": "box"}), (200, {"node": "box", "state": "exited"}))
        self.assertEqual(self.post("/api/sandboxes/start", {"node": "box"}), (200, {"node": "box", "state": "running"}))
        self.assertEqual(self.mutations_since(), [["stop", "cairn-box"], ["start", "cairn-box"]])
        self.assertEqual(self.synced, ["box", "box", "box"])   # before the stop's live check, after the stop and the start

    def test_stop_and_remove_check_a_fresh_snapshot(self):
        self.set_snapshot("box", {"projects": []})   # the last sync, before a lead started
        fresh = lambda name, force_mirror=False: self.set_snapshot(name, {"projects": [m2.snap_project("demo", lead="alive")]})
        with mock.patch.object(self.syncer, "sync_one", fresh):
            for path, body in (("/api/sandboxes/stop", {"node": "box"}), ("/api/sandboxes/remove", {"node": "box", "confirm": "box"})):
                status, out = self.post(path, body)
                self.assertEqual(status, 409, out)
                self.assertIn("demo", out["error"])
        self.assertEqual(self.mutations_since(), [])

    def test_running_container_without_a_node_record_is_asked_directly(self):
        nodes = config.load_nodes()
        del nodes["box"]   # as `cairn node rm --force` leaves it
        config.save_nodes(nodes)
        self.probe = {"projects": [m2.snap_project("demo", lead="alive")]}
        for path, body in (("/api/sandboxes/stop", {"node": "box"}), ("/api/sandboxes/remove", {"node": "box", "confirm": "box"})):
            status, out = self.post(path, body)
            self.assertEqual(status, 409, out)
            self.assertIn("demo", out["error"])
        self.assertEqual(self.ssh_calls, [({"ssh": sandbox.TARGET, "port": self.box_port}, ["snapshot"], 15)] * 2)
        self.assertEqual(self.mutations_since(), [])
        self.probe = [1]   # a reply that is no snapshot reads as no answer
        self.assertEqual(sandbox.probe_snapshot(self.box_port), None)
        self.ssh_calls.pop()
        self.probe = None   # ssh fails, or cairn-node is gone: nothing to protect
        self.assertEqual(self.post("/api/sandboxes/stop", {"node": "box"}), (200, {"node": "box", "state": "exited"}))
        self.assertEqual(len(self.ssh_calls), 3)
        self.assertEqual(self.post("/api/sandboxes/remove", {"node": "box", "confirm": "box"})[0], 200)
        self.assertEqual(len(self.ssh_calls), 3)   # a stopped container runs nothing, so it is not asked
        self.assertNotIn("cairn-box", self.docker.containers)

    def test_unlabelled_volume_refuses_before_any_change(self):
        self.set_snapshot("box", {"projects": []})
        self.docker.volumes["cairn-box-hostkeys"] = {}
        status, out = self.post("/api/sandboxes/remove", {"node": "box", "confirm": "box"})
        self.assertEqual(status, 409, out)
        self.assertIn("volume cairn-box-hostkeys is not labelled", out["error"])
        self.assertEqual(self.mutations_since(), [])
        self.assertTrue(self.docker.containers["cairn-box"]["running"])
        self.assertIn("box", config.load_sandboxes())
        self.assertIn("box", config.load_nodes())

    def test_syncer_does_not_ssh_a_stopped_sandbox(self):
        self.probe = {"node_version": "0.3.0", "projects": []}
        self.syncer._sync_node("box")
        self.assertEqual(len(self.ssh_calls), 1)
        self.assertEqual({k: self.state_node("box")[k] for k in ("ok", "stopped")}, {"ok": True, "stopped": False})
        self.sb.mutate("box", "stop")
        before = len(self.docker.calls)
        self.syncer._sync_node("box")
        self.syncer._sync_node("box")
        self.assertEqual(len(self.ssh_calls), 1)
        self.assertEqual([c[:2] for c in self.docker.calls[before:]], [["ps", "-a"]])   # the second sync reused it
        n = self.state_node("box")
        self.assertEqual({k: n[k] for k in ("ok", "error", "stopped")}, {"ok": False, "error": "sandbox stopped", "stopped": True})
        self.assertEqual(n["snapshot"], ssh.parse_snapshot(self.probe))   # the last snapshot stays
        self.assertFalse(self.state_node("lab")["stopped"])
        self.sb.mutate("box", "start")   # any docker change drops the cached states
        self.syncer._sync_node("box")
        self.assertEqual(len(self.ssh_calls), 2)
        self.assertEqual({k: self.state_node("box")[k] for k in ("ok", "stopped")}, {"ok": True, "stopped": False})
        self.sb.mutate("box", "stop")
        self.docker.down = True   # Docker cannot say, so the node is synced as any other
        self.syncer._sync_node("box")
        self.assertEqual(len(self.ssh_calls), 3)

    def state_node(self, name):
        return next(n for n in self.state()["nodes"] if n["name"] == name)

    def test_remove_needs_the_name_and_removes_exactly_its_things(self):
        self.set_snapshot("box", {"projects": []})
        mirror_dir = os.path.join(self.tmp, "mirror", "box", "demo.git")
        os.makedirs(mirror_dir)
        projects = config.load_projects()
        projects["box/demo"] = {"token": "oa"}
        config.save_projects(projects)
        known = os.path.join(self.tmp, "known_hosts")
        self.write(known, "[127.0.0.1]:%d ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBox\n"
                          "[127.0.0.1]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIStrand\n" % self.box_port)
        for confirm in (None, "", "Box", "lab"):
            self.assertEqual(self.post("/api/sandboxes/remove", {"node": "box", "confirm": confirm})[0], 400, confirm)
        self.assertEqual(self.mutations_since(), [])
        status, out = self.post("/api/sandboxes/remove", {"node": "box", "confirm": "box"})
        self.assertEqual((status, out), (200, {"removed": "box", "mirror": os.path.join(self.tmp, "mirror", "box")}))
        self.assertEqual(self.mutations_since(), [["rm", "-f", "cairn-box"], ["volume", "rm", "cairn-box-home"],
                                                  ["volume", "rm", "cairn-box-hostkeys"]])
        self.assertEqual(sorted(self.docker.volumes), ["cairn-strand-home", "cairn-strand-hostkeys", "telecable-db"])
        self.assertEqual(list(self.docker.containers), ["cairn-strand"])
        self.assertNotIn("box", config.load_nodes())
        self.assertNotIn("box/demo", config.load_projects())
        self.assertNotIn("box", config.load_sandboxes())
        self.assertTrue(os.path.isdir(mirror_dir))
        with open(known) as f:
            text = f.read()
        self.assertNotIn(":%d " % self.box_port, text)
        self.assertIn("[127.0.0.1]:2222", text)
        self.assertEqual(self.post("/api/sandboxes/remove", {"node": "box", "confirm": "box"})[0], 409)   # gone

    def test_list_shows_state_and_stats(self):
        v = json.loads(self.req("/api/sandboxes")[1])
        self.assertEqual((v["docker"], v["error"], v["images"], v["cpus_max"]), (True, None, {"slim": True, "dev": False}, 8))
        box = v["sandboxes"][0]
        self.assertEqual({k: box[k] for k in ("node", "container", "state", "cpu_pct", "mem_used_mb", "mem_limit_mb", "adopted")},
                         {"node": "box", "container": "cairn-box", "state": "running", "cpu_pct": 12.5,
                          "mem_used_mb": 512.0, "mem_limit_mb": None, "adopted": False})
        self.assertEqual(v["jobs"][0]["phase"], "done")

    def test_list_survives_a_container_gone_before_stats(self):
        self.docker.fail_stats = 1
        status, raw = self.req("/api/sandboxes")
        self.assertEqual(status, 200, raw)
        box = json.loads(raw)["sandboxes"][0]
        self.assertEqual((box["state"], box["cpu_pct"], box["mem_used_mb"]), ("running", None, None))
        box = json.loads(self.req("/api/sandboxes")[1])["sandboxes"][0]   # the failure was not cached
        self.assertEqual(box["cpu_pct"], 12.5)


class RecreateTests(SandboxBase):
    def setUp(self):
        super().setUp()
        self.docker.images.add("cairn-slim:0.1")
        self.sb.create("box", "slim", memory_mb=2048, cpus=2, wait=True)
        self.box_port = config.load_sandboxes()["box"]["port"]
        self.docker.volumes["cairn-box-home"]["login"] = "the node's own Claude login lives here"
        self.set_snapshot("box", {"projects": [m2.snap_project("demo", lead="dead")]})
        self.calls_before = len(self.docker.calls)

    def mutations_since(self):
        return [c for c in self.docker.calls[self.calls_before:] if c in self.docker.mutations()]

    def runs(self):
        return [c for c in self.docker.calls if c[0] == "run"]

    def test_a_new_container_on_the_same_volumes_port_and_flags(self):
        status, out = self.post("/api/sandboxes/recreate", {"node": "box"})
        self.assertEqual((status, out), (200, {"node": "box", "state": "running"}))
        self.assertEqual([c[:2] for c in self.mutations_since()], [["rm", "-f"], ["run", "-d"]])
        self.assertNotIn(["volume", "rm"], [c[:2] for c in self.docker.calls])
        self.assertEqual(self.docker.volumes["cairn-box-home"]["login"], "the node's own Claude login lives here")
        first, again = self.runs()
        self.assertEqual(again, first)   # same name, label, port, volumes, hardening, memory, cpus and image
        self.assertIn("cairn-box-home:/home/agent", again)
        self.assertIn("cairn-box-hostkeys:/etc/ssh/hostkeys", again)
        for flag in sandbox.HARDEN + ["--security-opt", "no-new-privileges", "--restart", "unless-stopped"]:
            self.assertIn(flag, again)
        self.assertEqual(self.docker.containers["cairn-box"]["mounts"], volumes_of("cairn-box"))
        self.assertEqual(config.load_sandboxes()["box"]["port"], self.box_port)
        self.assertIn("box", config.load_nodes())
        self.assertEqual(self.synced, ["box", "box"])   # before the live check and after

    def test_dev_keeps_its_sudo_and_drops_the_same_capabilities(self):
        self.docker.images.add("cairn-dev:0.1")
        self.sb.create("dbox", "dev", wait=True)
        self.set_snapshot("dbox", {"projects": []})
        self.assertEqual(self.post("/api/sandboxes/recreate", {"node": "dbox"})[0], 200)
        first, again = [c for c in self.runs() if "cairn-dbox" in c]
        self.assertEqual(again, first)
        self.assertNotIn("no-new-privileges", again)
        self.assertEqual(again[-1], "cairn-dev:0.1")

    def test_refused_as_stop_is(self):
        self.set_snapshot("box", {"projects": [m2.snap_project("demo", lead="alive")]})
        status, out = self.post("/api/sandboxes/recreate", {"node": "box"})
        self.assertEqual(status, 409)
        self.assertIn("demo", out["error"])
        self.assertEqual(self.post("/api/sandboxes/recreate", {"node": "box", "force": True})[0], 409)   # never forced
        self.set_snapshot("box", None)   # running and cannot be checked
        status, out = self.post("/api/sandboxes/recreate", {"node": "box"})
        self.assertEqual((status, out.get("unchecked")), (409, ["box"]))
        self.assertEqual(self.mutations_since(), [])
        self.assertEqual(self.post("/api/sandboxes/recreate", {"node": "box", "force": True})[0], 200)
        self.assertEqual(self.post("/api/sandboxes/recreate", {"node": "lab"})[0], 409)   # not a sandbox

    def test_nothing_is_removed_when_the_image_or_the_key_is_missing(self):
        self.docker.images.discard("cairn-slim:0.1")
        status, out = self.post("/api/sandboxes/recreate", {"node": "box"})
        self.assertEqual(status, 409)
        self.assertIn("image cairn-slim:0.1 is missing", out["error"])
        self.docker.images.add("cairn-slim:0.1")
        os.remove(os.path.join(self.tmp, ".ssh", "id_ed25519.pub"))
        self.assertEqual(self.post("/api/sandboxes/recreate", {"node": "box"})[0], 409)
        self.assertEqual(self.mutations_since(), [])
        self.assertTrue(self.docker.containers["cairn-box"]["running"])

    def test_a_lost_home_volume_is_refused_before_anything_changes(self):
        del self.docker.volumes["cairn-box-home"]
        status, out = self.post("/api/sandboxes/recreate", {"node": "box"})
        self.assertEqual(status, 409)
        self.assertIn("volume cairn-box-home is gone", out["error"])
        self.assertEqual(self.mutations_since(), [])
        self.assertNotIn("cairn-box-home", self.docker.volumes)

    def test_a_failed_run_keeps_the_volumes_and_a_second_recreate_finishes(self):
        real = self.docker.__call__

        def failing(argv, timeout, on_line=None):
            if argv[0] == "run":
                self.docker.calls.append(list(argv))
                return 125, "", "docker: Error response from daemon: port is already allocated."
            return real(argv, timeout, on_line)
        self.sb.run = failing
        status, out = self.post("/api/sandboxes/recreate", {"node": "box"})
        self.assertEqual(status, 502)
        self.assertIn("its volumes kept", out["error"])
        self.assertNotIn("cairn-box", self.docker.containers)
        self.assertEqual(sorted(v for v in self.docker.volumes if v.startswith("cairn-box")), volumes_of("cairn-box"))
        self.sb.run = self.docker
        self.assertEqual(self.post("/api/sandboxes/recreate", {"node": "box"}), (200, {"node": "box", "state": "running"}))
        self.assertEqual(self.docker.volumes["cairn-box-home"]["login"], "the node's own Claude login lives here")

    def test_an_adopted_sandbox_without_a_known_image_is_refused(self):
        self.docker.add("cairn-old", 2240, image="someone/else:1")
        config.save_nodes({**config.load_nodes(), "old": {"ssh": sandbox.TARGET, "port": 2240, "allowed_tokens": []}})
        self.sb.adopt("old")
        self.set_snapshot("old", {"projects": []})
        status, out = self.post("/api/sandboxes/recreate", {"node": "old"})
        self.assertEqual(status, 409)
        self.assertIn("recreate its container by hand", out["error"])
        self.assertIn("cairn-old", self.docker.containers)

    def test_cli(self):
        self.probe = {"projects": [m2.snap_project("demo", lead="dead")]}
        self.assertEqual(cli.main(["sandbox", "recreate", "box"]), 0)
        self.assertEqual([c[:2] for c in self.mutations_since()], [["rm", "-f"], ["run", "-d"]])
        self.assertIn("home volume, with the Claude login, was kept", sys.stdout.getvalue())


class LoginCommandTests(SandboxBase):
    def test_the_list_and_the_cli_give_the_same_command(self):
        self.docker.images.add("cairn-slim:0.1")
        self.sb.create("box", "slim", wait=True)
        port = config.load_sandboxes()["box"]["port"]
        want = ["ssh", "-t", "-a", "-o", "ForwardAgent=no", "-p", str(port),
                "-o", "UserKnownHostsFile=" + os.path.join(self.tmp, "known_hosts"),
                "-o", "StrictHostKeyChecking=accept-new", "agent@127.0.0.1",
                'PATH="$PATH:$HOME/.local/bin" claude auth login']
        listed = json.loads(self.req("/api/sandboxes")[1])["sandboxes"][0]
        self.assertEqual(listed["login_command"], shlex.join(want))
        with mock.patch("os.execvp", side_effect=SystemExit(0)) as execvp:
            with self.assertRaises(SystemExit):
                cli.main(["sandbox", "login", "box"])
        execvp.assert_called_once_with("ssh", want)
        self.assertIn("paste the code the page shows here", sys.stdout.getvalue())
        with mock.patch("os.execvp") as execvp:
            self.assertEqual(cli.main(["sandbox", "login", "lab"]), 1)   # not a sandbox
        execvp.assert_not_called()


class AdoptTests(SandboxBase):
    def setUp(self):
        super().setUp()
        config.save_nodes({**config.load_nodes(), "strand": {"ssh": sandbox.TARGET, "port": 2222, "allowed_tokens": []}})

    def test_adopt_checks_port_and_volumes_and_changes_nothing_in_docker(self):
        self.docker.add("cairn-strand", 2223)
        self.assertEqual(cli.main(["sandbox", "adopt", "strand"]), 1)
        self.assertIn("port 2223", self.err.getvalue())
        self.docker.add("cairn-strand", 2222, mounts=["cairn-strand-home", "elsewhere"])
        self.assertEqual(cli.main(["sandbox", "adopt", "strand"]), 1)
        self.assertIn("cairn-strand-hostkeys", self.err.getvalue())
        self.assertEqual(cli.main(["sandbox", "adopt", "strand", "--container", "nope"]), 1)
        self.assertEqual(config.load_sandboxes(), {})
        self.docker.add("cairn-strand", 2222)
        self.assertEqual(cli.main(["sandbox", "adopt", "strand"]), 0)
        entry = config.load_sandboxes()["strand"]
        self.assertEqual({k: entry[k] for k in ("container", "image", "port", "memory_mb", "cpus", "adopted", "created")},
                         {"container": "cairn-strand", "image": "dev", "port": 2222, "memory_mb": None, "cpus": None,
                          "adopted": True, "created": "2026-10-20T01:17:51Z"})
        self.assertEqual(self.docker.mutations(), [])
        self.assertEqual(cli.main(["sandbox", "adopt", "strand"]), 1)   # once only

    def test_adopted_unlabelled_container_is_guarded_by_live_sessions_only(self):
        self.docker.add("cairn-strand", 2222)
        self.sb.adopt("strand")
        self.set_snapshot("strand", {"projects": [m2.snap_project("lead", lead="alive")]})
        self.assertEqual(self.post("/api/sandboxes/stop", {"node": "strand"})[0], 409)
        self.assertEqual(self.docker.mutations(), [])
        self.set_snapshot("strand", {"projects": []})
        self.assertEqual(self.post("/api/sandboxes/stop", {"node": "strand"})[0], 200)
        self.assertEqual(self.docker.mutations(), [["stop", "cairn-strand"]])
        v = json.loads(self.req("/api/sandboxes")[1])
        self.assertEqual([(s["node"], s["state"], s["adopted"]) for s in v["sandboxes"]], [("strand", "exited", True)])

    def test_adopted_entry_must_match_the_containers_port(self):
        # a hand-edited entry that names a container serving another node
        self.docker.add("cairn-strand", 2222)
        self.register("ghost", container="cairn-strand", port=2230, adopted=True)
        config.save_nodes({**config.load_nodes(), "ghost": {"ssh": sandbox.TARGET, "port": 2230, "allowed_tokens": []}})
        self.set_snapshot("ghost", {"projects": []})
        for path, body in (("/api/sandboxes/start", {"node": "ghost"}), ("/api/sandboxes/stop", {"node": "ghost"}),
                           ("/api/sandboxes/remove", {"node": "ghost", "confirm": "ghost"})):
            status, out = self.post(path, body)
            self.assertEqual(status, 409, out)
            self.assertIn("leaves it alone", out["error"])
        self.assertEqual(self.docker.mutations(), [])
        self.assertTrue(self.docker.containers["cairn-strand"]["running"])
        self.assertIn("ghost", config.load_sandboxes())


class DockerDownTests(SandboxBase):
    def test_list_reports_docker_false_and_ops_say_so(self):
        self.register("box")
        self.docker.down = True
        status, raw = self.req("/api/sandboxes")
        v = json.loads(raw)
        self.assertEqual((status, v["docker"], v["sandboxes"]), (200, False, []))
        self.assertIn("Cannot connect to the Docker daemon", v["error"])
        status, out = self.post("/api/sandboxes/stop", {"node": "box"})
        self.assertEqual(status, 503)
        self.assertIn("Docker daemon", out["error"])
        self.assertEqual(self.post("/api/sandboxes", {"name": "x", "image": "slim"})[0], 503)

    def test_missing_binary(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError(2, "No such file or directory")):
            with self.assertRaises(sandbox.DockerDown):
                REAL_RUN_DOCKER(["info"], 5)


class AuthTests(SandboxBase):
    def test_routes_need_token_and_origin(self):
        for route in ("/api/sandboxes", "/api/sandboxes/start", "/api/sandboxes/stop", "/api/sandboxes/remove",
                      "/api/sandboxes/recreate"):
            self.assertEqual(self.req(route, "POST", {}, {"X-Cairn-Token": None})[0], 403, route)
            self.assertEqual(self.req(route, "POST", {}, {"Origin": "http://evil.example"})[0], 403, route)
            self.assertEqual(self.req(route, "POST", {})[0], 400, route)
        self.assertEqual(self.docker.mutations(), [])


if __name__ == "__main__":
    unittest.main()
