"""macOS Keychain access. Secrets go on `security -i` stdin, never argv."""
import subprocess

SERVICE = "cairn"
TIMEOUT = 30   # a locked Keychain makes `security` wait on a prompt; nothing waits on it longer than this


class KeychainError(Exception):
    pass


class KeychainNotFound(KeychainError):
    """The item does not exist (security exit 44), as opposed to a locked Keychain or a denied prompt."""


def _quote(s):
    if "\n" in s or "\r" in s or "\0" in s:
        raise KeychainError("value contains a control character")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _security(what, label, argv, **kw):
    try:
        return subprocess.run(["security"] + argv, capture_output=True, text=True, timeout=TIMEOUT, **kw)
    except subprocess.TimeoutExpired:
        raise KeychainError("keychain %s for %s timed out after %d s; is the Keychain locked?" % (what, label, TIMEOUT))


def add(label, value):
    cmd = "add-generic-password -U -s %s -a %s -w %s\n" % (_quote(SERVICE), _quote(label), _quote(value))
    r = _security("add", label, ["-i"], input=cmd)
    if r.returncode != 0:
        raise KeychainError("keychain add failed for %s (exit %d)" % (label, r.returncode))


def get(label):
    r = _security("read", label, ["find-generic-password", "-s", SERVICE, "-a", label, "-w"])
    if r.returncode == 44:
        raise KeychainNotFound("no keychain item for token %s" % label)
    if r.returncode != 0:
        raise KeychainError("keychain read failed for %s (exit %d)" % (label, r.returncode))
    return r.stdout.rstrip("\n")


def delete(label):
    try:
        r = _security("delete", label, ["delete-generic-password", "-s", SERVICE, "-a", label])
    except KeychainError:
        return False
    return r.returncode == 0
