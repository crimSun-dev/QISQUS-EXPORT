"""Local HTTP front end for GuiApp, plus the `qexport gui` entry point.

Binds 127.0.0.1 on a random free port. Every /api call must carry the per-launch token (header
X-QX-Token, or ?t= for the event stream, which can't send headers) and a Host header naming this
port, so other local programs and web pages can't drive the exporter."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from qexport.gui import winshell
from qexport.gui.app import GuiApp, UserError
from qexport.store.checkpoint import RunLock, StoreLockedError

log = logging.getLogger("qexport.gui")

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
EVENT_INTERVAL_S = 1.0
INSTANCE_FILE = ".qexport_gui.json"
INSTANCE_LOCK = ".qexport_gui.lock"


def _static(name: str) -> bytes | None:
    try:
        return resources.files("qexport.gui").joinpath("static", name).read_bytes()
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None


class GuiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, app: GuiApp, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.app = app
        self.token = secrets.token_urlsafe(24)
        self.port = self.server_address[1]

    @property
    def url(self) -> str:
        # The token travels in the #fragment: the page reads it, the browser never sends it anywhere.
        return f"http://127.0.0.1:{self.port}/#t={self.token}"


class Handler(BaseHTTPRequestHandler):
    server: GuiServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:   # keep request noise out of the log file
        pass

    # ------------------------------------------------------------------ plumbing

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}")

    def _token_ok(self, query: dict) -> bool:
        given = self.headers.get("X-QX-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(given, self.server.token)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise UserError("Request too large.")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise UserError("Bad request.") from None
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ verbs

    def do_GET(self) -> None:   # noqa: N802
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "wrong host")
        url = urlsplit(self.path)
        if not url.path.startswith("/api/"):
            name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            if "/" in name or name.startswith(".") or Path(name).suffix not in STATIC_TYPES:
                return self._error(HTTPStatus.NOT_FOUND, "not found")
            body = _static(name)
            if body is None:
                return self._error(HTTPStatus.NOT_FOUND, "not found")
            return self._send(200, body, STATIC_TYPES[Path(name).suffix],
                              {"Content-Security-Policy": "default-src 'self'; style-src 'self'; "
                                                          "img-src 'self' data:; connect-src 'self'; "
                                                          "frame-ancestors 'none'"})
        query = parse_qs(url.query)
        if not self._token_ok(query):
            return self._error(HTTPStatus.UNAUTHORIZED, "This window is out of date. Close it and open "
                                                        "Qiscus Export again.")
        app = self.server.app
        try:
            if url.path == "/api/state":
                return self._json(app.state())
            if url.path == "/api/events":
                return self._events()
            if url.path == "/api/diagnostics":
                return self._json({"text": app.diagnostics()})
            if url.path == "/api/files/old":
                return self._json({"names": app.old_files()})
            if url.path == "/api/ping":
                return self._json({"ok": True, "windows": app.windows_seen})
        except UserError as e:
            return self._error(HTTPStatus.BAD_REQUEST, str(e))
        except Exception as e:   # noqa: BLE001
            log.exception("GET %s failed", url.path)
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(e).__name__}: {e}")
        return self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:   # noqa: N802
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "wrong host")
        url = urlsplit(self.path)
        if not self._token_ok({}):          # header only: a cross-site form can't set it
            return self._error(HTTPStatus.UNAUTHORIZED, "This window is out of date. Close it and open "
                                                        "Qiscus Export again.")
        app = self.server.app
        try:
            body = self._body()
            parts = url.path.strip("/").split("/")
            result: object = {"ok": True}
            match parts:
                case ["api", "heartbeat"]:
                    app.heartbeat()
                case ["api", "download"]:
                    result = app.download(update=bool(body.get("update")))
                case ["api", "retry-failed"]:
                    result = app.retry_failed()
                case ["api", "build"]:
                    result = app.build()
                case ["api", "tasks", task_id, action]:
                    app.task_action(task_id, action)
                case ["api", "integrity"]:
                    app.run_integrity_check()
                case ["api", "files", "open"]:
                    app.open_file(str(body.get("name", "")))
                case ["api", "files", "reveal"]:
                    app.reveal_file(str(body.get("name", "")))
                case ["api", "files", "delete"]:
                    result = app.delete_files([str(n) for n in body.get("names") or []])
                case ["api", "folder"]:
                    app.open_folder(str(body.get("which", "exports")))
                case ["api", "clipboard"]:
                    result = {"ok": app.copy_text(str(body.get("text", "")))}
                case ["api", "login", "browser"]:
                    result = app.login_browser_start()
                case ["api", "login", "start"]:
                    result = app.login_start()
                case ["api", "login", "clipboard"]:
                    result = app.login_read_clipboard()
                case ["api", "login", "save"]:
                    result = app.login_save(str(body.get("text", "")))
                case ["api", "login", "check"]:
                    result = app.check_login()
                case ["api", "options"]:
                    result = app.set_options(body.get("changes") or {})
                case ["api", "quit"]:
                    result = self._quit(str(body.get("mode", "stop")))
                case _:
                    return self._error(HTTPStatus.NOT_FOUND, "not found")
        except UserError as e:
            return self._error(HTTPStatus.BAD_REQUEST, str(e))
        except Exception as e:   # noqa: BLE001
            log.exception("POST %s failed", url.path)
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(e).__name__}: {e}")
        return self._json(result)

    def _quit(self, mode: str) -> dict:
        app = self.server.app
        if mode == "background":
            app.note("app", "Window closed; tasks continue in the background.")
            return {"ok": True, "running": len(app.manager.active())}
        app.note("app", "Stopping safely…")
        app.exit_requested.set()
        return {"ok": True}

    def _events(self) -> None:
        """Server-sent events: the full state every second until the window goes away."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.close_connection = True
        app = self.server.app
        try:
            while not app.exit_requested.is_set():
                app.heartbeat()
                payload = json.dumps(app.state(), ensure_ascii=False, default=str)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(EVENT_INTERVAL_S)
            self.wfile.write(b"event: bye\ndata: {}\n\n")
            self.wfile.flush()
        except (ConnectionError, OSError):
            pass


# ====================================================================== entry point

def _setup_logging(exports: Path) -> None:
    handler = logging.handlers.RotatingFileHandler(exports / "qexport_gui.log", maxBytes=1_000_000,
                                                   backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("qexport")
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def _hand_over_to_running_instance(exports: Path) -> bool:
    """Another GUI owns the folder: show its window (or open a new one onto it). True if it answered."""
    try:
        info = json.loads((exports / INSTANCE_FILE).read_text(encoding="utf-8"))
        port, token = int(info["port"]), str(info["token"])
        r = httpx.get(f"http://127.0.0.1:{port}/api/ping", headers={"X-QX-Token": token}, timeout=3)
        r.raise_for_status()
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        return False
    if not winshell.focus_existing_window():
        winshell.open_app_window(f"http://127.0.0.1:{port}/#t={token}")
    return True


def main(root: Path | None = None, open_window: bool = True) -> int:
    root = (root or Path.cwd()).resolve()
    exports = root / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    instance = RunLock(exports / INSTANCE_LOCK)
    try:
        instance.__enter__()
    except StoreLockedError:
        for _ in range(10):                # the first instance may still be starting up
            if _hand_over_to_running_instance(exports):
                return 0
            time.sleep(0.5)
        winshell.message_box("Qiscus Export", "Qiscus Export is already running but not responding. "
                                              "Close it from the Task Manager, then try again.")
        return 1
    os.chdir(root)                         # .env and exports/ are relative to the install folder
    _setup_logging(exports)
    app = GuiApp(root, auto_signin=open_window)
    server: GuiServer | None = None
    try:
        app.start()
        server = GuiServer(app)
        (exports / INSTANCE_FILE).write_text(json.dumps(
            {"port": server.port, "token": server.token, "pid": os.getpid()}), encoding="utf-8")
        threading.Thread(target=server.serve_forever, name="qexport-gui-http", daemon=True).start()
        log.info("GUI at http://127.0.0.1:%s/", server.port)
        if open_window:
            winshell.open_app_window(server.url)
        else:
            print(server.url, flush=True)
        app.exit_requested.wait()
        return 0
    except Exception as e:   # noqa: BLE001 - pythonw has no console; tell the user somehow
        log.exception("GUI failed")
        winshell.message_box("Qiscus Export", f"Qiscus Export could not start: {e}\n\n"
                                              f"Details are in {exports / 'qexport_gui.log'}")
        return 1
    finally:
        app.close()
        if server is not None:
            server.shutdown()
            server.server_close()
        try:
            (exports / INSTANCE_FILE).unlink(missing_ok=True)
        except OSError:
            pass
        instance.__exit__(None, None, None)
