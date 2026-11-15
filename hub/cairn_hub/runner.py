"""Run it: serve a copied web result read-only on its own 127.0.0.1 port (another origin than the hub)."""
import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Runner:
    def __init__(self):
        self.lock = threading.Lock()
        self.servers = {}   # key -> (server, path)

    def start(self, key, path):
        """The URL serving `path`; reuses a server already serving that folder."""
        with self.lock:
            cur = self.servers.get(key)
            if cur and cur[1] == path:
                return self._url(cur[0])
            if cur:
                self._close(cur[0])
            srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=path))
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True, name="run-" + key).start()
            self.servers[key] = (srv, path)
            return self._url(srv)

    def stop(self, key):
        with self.lock:
            cur = self.servers.pop(key, None)
        if cur:
            self._close(cur[0])
        return cur is not None

    def stop_all(self):
        with self.lock:
            servers, self.servers = list(self.servers.values()), {}
        for srv, _ in servers:
            self._close(srv)

    def path(self, key):
        with self.lock:
            cur = self.servers.get(key)
            return cur[1] if cur else None

    def url(self, key):
        with self.lock:
            cur = self.servers.get(key)
            return self._url(cur[0]) if cur else None

    @staticmethod
    def _url(srv):
        return "http://127.0.0.1:%d/" % srv.server_address[1]

    @staticmethod
    def _close(srv):
        srv.shutdown()
        srv.server_close()
