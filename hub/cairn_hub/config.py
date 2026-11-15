"""nodes.json, tokens.json, projects.json, sandboxes.json, limits.json and hub-secret under ~/.cairn (CAIRN_HOME overrides)."""
import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone

PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}\Z")
NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}\Z")
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}\Z")
SSH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@:-]*\Z")


def home():
    return os.path.abspath(os.path.expanduser(os.environ.get("CAIRN_HOME") or "~/.cairn"))


def path(name):
    return os.path.join(home(), name)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(name, default):
    try:
        with open(path(name)) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(name, data):
    """Atomic write, owner-only permissions."""
    os.makedirs(home(), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=name + ".", dir=home())
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path(name))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def hub_secret():
    """The dashboard's sign-in secret, made on first use and the same across restarts. It is created complete under a
    temporary name and linked into place, so a `cairn open` racing the hub's start never reads half a secret."""
    p = path("hub-secret")
    if not os.path.exists(p):
        os.makedirs(home(), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="hub-secret.", dir=home())   # mode 0600
        try:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32) + "\n")
            os.link(tmp, p)
        except FileExistsError:
            pass
        finally:
            os.unlink(tmp)
    with open(p) as f:
        secret = f.read().strip()
    if len(secret) < 32:
        raise RuntimeError("%s holds no usable secret; delete it and restart the hub" % p)
    return secret


def load_nodes():
    return read_json("nodes.json", {})


def save_nodes(d):
    write_json("nodes.json", d)


def load_tokens():
    return read_json("tokens.json", {})


def save_tokens(d):
    write_json("tokens.json", d)


MODELS = ("fable", "opus", "sonnet", "haiku")  # Claude Code aliases; None = the account default


def load_projects():
    return read_json("projects.json", {})


def save_projects(d):
    write_json("projects.json", d)


def load_sandboxes():
    return read_json("sandboxes.json", {})


def save_sandboxes(d):
    write_json("sandboxes.json", d)


def load_limits():
    """{label: {"since", "until", "known", "note"}}: each token's last usage limit, shared by every project."""
    return read_json("limits.json", {})


def save_limits(d):
    write_json("limits.json", d)


def token_label_for(node, project):
    """The project's chosen token, else the node's first allowed one, else None."""
    chosen = load_projects().get("%s/%s" % (node, project), {}).get("token")
    if chosen:
        return chosen
    allowed = load_nodes().get(node, {}).get("allowed_tokens") or []
    return allowed[0] if allowed else None


def token_pool(node, project):
    """The project's tokens in preference order, its active token always in it; [] when it has no token."""
    pool = list(load_projects().get("%s/%s" % (node, project), {}).get("tokens") or [])
    active = token_label_for(node, project)
    return pool if not active or active in pool else [active] + pool


def public_tokens():
    out = []
    for label, t in sorted(load_tokens().items()):
        out.append({"label": label, "kind": t.get("kind"), "account": t.get("account"),
                    "last4": t.get("last4"), "expires": t.get("expires")})
    return out
