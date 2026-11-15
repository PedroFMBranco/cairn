"""Leads that need the operator: a usage limit, a dialog, a lead stopped at its prompt, or one that died while working.
On a limit the lead moves to the next token in its project's pool that is not limited and restarts at once; when
every token is limited it restarts once, shortly after the first reset, on the token that resets first."""
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config, nodeops, rotation

PROMPT_AFTER = 600    # seconds at the prompt before it counts
RESUME_DELAY = 120    # seconds after a limit's reset
RESUME_RETRY = 300    # seconds between automatic restart attempts that failed
RESUME_TRIES = 3
RECENT = 5 * 3600     # a clock time this far in the past has already reset (one session window)
DEFAULT_WINDOW = 5 * 3600   # how long a limit lasts when its reset time cannot be read
MAX_RESET = 8 * 24 * 60     # minutes; a weekly limit plus a day, so a garbled "resets in 99999h" cannot pin a token
IN_RE = re.compile(r"resets in\s+(?:(\d+)\s*h\w*)?\s*(?:(\d+)\s*m\w*)?", re.I)
AT_RE = re.compile(r"(?:resets\s+(?:at\s+)?|automatically at\s+)(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
                   r"(?:\s*\((UTC|GMT|[A-Za-z_]+(?:/[A-Za-z_+-]+)+)\))?", re.I)


def local_zone():
    """The Mac's zone with its daylight-saving rules (datetime.astimezone() alone gives a fixed offset)."""
    from zoneinfo import ZoneInfo
    name = os.environ.get("TZ") or ""
    if not name:
        try:
            link = os.path.realpath("/etc/localtime")
            name = link.split("zoneinfo/", 1)[1] if "zoneinfo/" in link else ""
        except OSError:
            name = ""
    try:
        return ZoneInfo(name) if name else None
    except Exception:
        return None


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clock(at):
    """"23:00": an ISO time on the Mac's clock."""
    return datetime.fromisoformat(at).astimezone(local_zone()).strftime("%H:%M")


def limit_key(node, label, tokens=None):
    """The limits.json key of a token on a node: an OAuth token is one account wherever it runs, while every
    node-login label on a node is that node's own login, a different account on each node."""
    tokens = config.load_tokens() if tokens is None else tokens
    return "node-login@" + node if (tokens.get(label) or {}).get("kind") == "node-login" else label


def limits_for(node, labels, limits):
    """{label: limit entry} for the labels as they run on `node`, for rotation.choose and the dashboard."""
    tokens = config.load_tokens()
    keys = {label: limit_key(node, label, tokens) for label in labels}
    return {label: limits[key] for label, key in keys.items() if key in limits}


def record_limit(key, now, until, known, note):
    """Save the limit under its limit_key, dropping the limits that have ended; returns them all."""
    with nodeops.LOCK:
        saved = config.load_limits()
        limits = {k: v for k, v in saved.items() if rotation.limited_until(saved, k, now)}
        limits[key] = {"since": iso(now), "until": iso(until), "known": known, "note": note or ""}
        config.save_limits(limits)
    return limits


def clear_limit(label, node=None):
    """Forget a token's recorded limit, so rotation may pick it again at once; False when it had none.
    A node-login label needs its node, since its limit is that node's login: OpError without one."""
    tokens = config.load_tokens()
    if label not in tokens:
        raise nodeops.OpError("no such token %s" % label, 404)
    if node is not None and node not in config.load_nodes():
        raise nodeops.OpError("unknown node %s" % node, 404)
    if tokens[label].get("kind") == "node-login" and node is None:
        raise nodeops.OpError("%s is a node-login token: its limit is that of one node's login; name the node" % label)
    key = limit_key(node, label, tokens) if node else label
    with nodeops.LOCK:
        limits = config.load_limits()
        if key not in limits:
            return False
        del limits[key]
        config.save_limits(limits)
    return True


def eligible_pool(node, project, login="unknown", active=None):
    """The project's pool without the tokens this node cannot run, the active one included. Both choices of a token
    (open_episode, resume_token) go through here. `login` is the node's own Claude login as its snapshot reports
    it: only a definite "no" drops its node-login tokens, except `active`, which a restart keeps as before;
    "unknown" drops none."""
    tokens = config.load_tokens()
    allowed = config.load_nodes().get(node, {}).get("allowed_tokens") or []
    kinds = {label: (tokens.get(label) or {}).get("kind") for label in config.token_pool(node, project)}
    return [label for label, kind in kinds.items()
            if label in allowed and kind in nodeops.TOKEN_KINDS
            and not (kind == "node-login" and login == "no" and label != active)]


def parse_reset(note, now=None):
    """The reset time in a limit line, as an aware datetime; None when it cannot be read."""
    now = now or datetime.now(timezone.utc)
    m = IN_RE.search(note or "")
    if m and (m.group(1) or m.group(2)):
        try:
            minutes = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
        except ValueError:   # more digits than int() reads
            minutes = MAX_RESET
        return now + timedelta(minutes=min(minutes, MAX_RESET))
    m = AT_RE.search(note or "")
    if not m:
        return None
    hour, minute, half, zone = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower(), m.group(4)
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    tz = None
    if zone and zone.upper() in ("UTC", "GMT"):
        tz = timezone.utc
    elif zone:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(zone)
        except Exception:
            tz = None
    tz = tz or local_zone()
    local = now.astimezone(tz) if tz else now.astimezone()
    at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= local and (local - at).total_seconds() > RECENT:
        at += timedelta(days=1)   # a time just passed has already reset; one long past means tomorrow
    return at


class Attention:
    def __init__(self, restart=None, busy=None, clock=time.time, now=None, log=None):
        self.restart = restart            # (node, project) -> start result; raises on failure
        self.busy = busy or (lambda node, project: False)
        self.clock = clock
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.log = log or (lambda msg: print("attention: " + msg, flush=True))
        self.lock = threading.Lock()
        self.episodes = {}                # "<node>/<project>" -> {"kind", "since", "first", "note", "resume_at", ...}

    KEEP = object()   # no reading this sync (the pane capture or tmux failed): leave the episode as it is

    def classify(self, node_name, p):
        if p.get("lead") == "unknown":   # tmux on the node did not answer: neither alive nor dead
            return self.KEEP, None
        if p.get("lead") == "alive":
            state = p.get("lead_state")
            if state is None:
                return self.KEEP, None
            if state in ("limit", "dialog", "prompt"):
                return state, p.get("lead_note")
        elif (p.get("lead") == "dead" and str(p.get("status") or "").startswith("working")
              and not p.get("lead_stopped") and not self.busy(node_name, p.get("name"))):
            return "crashed", None   # a deliberate Stop (or the gap inside Restart) marks lead_stopped
        return None, None

    def open_episode(self, node_name, project, kind, note, login="unknown"):
        now = self.now()
        ep = {"kind": kind, "since": iso(now), "first": self.clock(), "note": note, "resume_at": None,
              "resume_error": None, "resume_attempts": 0, "resuming": False, "restarted_at": None,
              "token": None, "until": None, "known": None, "switch_to": None, "switched_to": None}
        if kind != "limit":
            return ep
        reset = parse_reset(note, now)
        # the pane shows the limit now, so a reset read as past (a clock time read in the wrong zone) is not over
        until = max(reset or now + timedelta(seconds=DEFAULT_WINDOW), now + timedelta(seconds=RESUME_RETRY))
        ep["resume_at"] = iso(until + timedelta(seconds=RESUME_DELAY))
        active = config.token_label_for(node_name, project)
        if active:
            limits = record_limit(limit_key(node_name, active), now, until, reset is not None, note)
            pool = eligible_pool(node_name, project, login, active)
            # no token can run: keep the reset's resume_at, where resume_token reports it
            decision = rotation.choose(pool, active, limits_for(node_name, pool + [active], limits), now) or ("", active)
            label = decision[1]
            if decision[0] == "wait":
                ep["resume_at"] = iso(decision[2] + timedelta(seconds=RESUME_DELAY))
            elif label != active:
                ep["resume_at"] = iso(now)
            ep.update(token=active, until=iso(until), known=reset is not None,
                      switch_to=label if label != active else None)
        return ep

    def limited_again(self, cur, snapshot):
        """A limit in a snapshot taken RESUME_DELAY after the lead's restart is a new limit, on whatever token it
        runs. Before that the snapshot may predate the restart (a second sync of the node) or the resumed pane may
        still show the old limit."""
        if cur["kind"] != "limit" or not cur["restarted_at"]:
            return False
        try:
            taken = datetime.fromisoformat((snapshot or {})["time"])
        except (KeyError, TypeError, ValueError):
            taken = self.now()
        return taken > datetime.fromisoformat(cur["restarted_at"]) + timedelta(seconds=RESUME_DELAY)

    def check(self, node_name, snapshot):
        """Update the episodes for one node's snapshot, then run any resume that is due."""
        due = []
        login = (snapshot or {}).get("login", "unknown")
        with self.lock:
            seen = set()
            for p in (snapshot or {}).get("projects") or []:
                key = "%s/%s" % (node_name, p.get("name"))
                seen.add(key)
                kind, note = self.classify(node_name, p)
                cur = self.episodes.get(key)
                if kind is self.KEEP:
                    continue
                if kind is None and cur is not None and p.get("lead") != "alive" and (cur["resuming"] or cur["resume_error"]):
                    kind, note = cur["kind"], cur["note"]   # our own restart stopped the lead: its retries go on
                if kind is None:
                    self.episodes.pop(key, None)
                    continue
                if cur is None or cur["kind"] != kind or self.limited_again(cur, snapshot):
                    cur = self.episodes[key] = self.open_episode(node_name, p.get("name"), kind, note, login)
                elif not cur["restarted_at"]:
                    cur["note"] = note
                if (cur["resume_at"] and not cur["resuming"] and self.restart
                        and iso(self.now()) >= cur["resume_at"] and not self.busy(node_name, p.get("name"))):
                    cur["resuming"] = True
                    due.append((key, node_name, p.get("name"), cur["token"]))
            for key in [k for k in self.episodes if k.startswith(node_name + "/") and k not in seen]:
                del self.episodes[key]
        for key, node, project, limited in due:
            switched = None
            try:
                label, wait = self.resume_token(node, project, limited, login)
                to = label if label != limited else None
                if wait:
                    with self.lock:
                        cur = self.episodes.get(key)
                        if cur is not None:
                            cur.update(resuming=False, switch_to=to,
                                       resume_at=iso(wait + timedelta(seconds=RESUME_DELAY)))
                    self.log("%s/%s: every token is still limited; waiting for %s" % (node, project, label))
                    continue
                if label and config.token_label_for(node, project) != label:
                    nodeops.set_project_token(node, project, label)
                    self.log("%s/%s switched from %s to %s after a usage limit" % (node, project, limited, label))
                switched = to
                self.restart(node, project)
                error = None
                self.log("%s/%s restarted after its usage limit reset" % (node, project) if not to
                         else "%s/%s restarted on %s" % (node, project, to))
            except Exception as e:
                error = str(e)[:200]
                self.log("%s/%s restart after the usage limit failed: %s" % (node, project, error))
            with self.lock:
                cur = self.episodes.get(key)
                if cur is None:
                    continue
                cur["switched_to"] = switched or cur["switched_to"]
                if switched or not error:
                    cur["switch_to"] = switched
                cur["resuming"] = False
                cur["resume_attempts"] += 1
                cur["resume_error"] = error
                cur["restarted_at"] = None if error else iso(self.now())
                # done after a success; after a failure, try again in 5 minutes, up to 3 times in all
                retry = error and cur["resume_attempts"] < RESUME_TRIES
                cur["resume_at"] = iso(self.now() + timedelta(seconds=RESUME_RETRY)) if retry else None

    def resume_token(self, node, project, limited, login="unknown"):
        """(token to restart on, None), or (token to wait for, until) when every token in the pool is limited now.
        Chosen at resume time, so a token removed or disallowed since the limit is skipped, and so is a node-login
        token on a node that the snapshot just taken shows signed out."""
        if not limited:
            return None, None
        pool = eligible_pool(node, project, login, limited)
        skipped = [l for l in eligible_pool(node, project) if l not in pool]
        if skipped:
            self.log("%s/%s: node %s is not signed in to Claude, so %s cannot run there; not switching to it"
                     % (node, project, node, ", ".join(skipped)))
        limits = limits_for(node, pool + [limited], config.load_limits())
        decision = rotation.choose(pool, limited, limits, self.now())
        if decision is None and skipped:
            raise nodeops.OpError("node %s is not signed in to Claude, and no other token in the pool of %s/%s can run "
                                  "there; sign the node in (claude auth login on it) and the next attempt uses it"
                                  % (node, node, project), 409)
        if decision is None:
            raise nodeops.OpError("no token in the pool of %s/%s is allowed on node %s" % (node, project, node), 409)
        return decision[1], (decision[2] if decision[0] == "wait" else None)

    def current(self):
        """Episodes that count now: limit, dialog and crashed at once, prompt after PROMPT_AFTER."""
        now = self.clock()
        with self.lock:
            return {k: {"kind": e["kind"], "since": e["since"], "note": e["note"], "resume_at": e["resume_at"],
                        "resume_error": e["resume_error"], "resume_attempts": e["resume_attempts"],
                        "token": e["token"], "until": e["until"], "known": e["known"],
                        "switch_to": e["switch_to"], "switched_to": e["switched_to"]}
                    for k, e in self.episodes.items()
                    if e["kind"] != "prompt" or now - e["first"] >= PROMPT_AFTER}

    def forget_node(self, node_name):
        """Drop a removed node's episodes, so a node added again under its name starts clean."""
        with self.lock:
            for key in [k for k in self.episodes if k.startswith(node_name + "/")]:
                del self.episodes[key]

    def open_episodes(self):
        """"<node>/<project>/<kind>" for every open episode, counted yet or not."""
        with self.lock:
            return {"%s/%s" % (k, e["kind"]) for k, e in self.episodes.items()}
