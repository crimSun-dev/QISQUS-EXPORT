"""GuiApp: everything the window shows and every button it has, independent of HTTP.

Owns the store lock (so a console run and the GUI never download into one store at once), the
TaskManager, login state, options, the workbook list and the plain-language status verdict."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError

from qexport import __version__
from qexport.api.client import Limiters, QiscusClient
from qexport.cli import CONSOLE_SNIPPET, CRED_KEYS, parse_credentials, write_env
from qexport.config import Settings, load_settings
from qexport.gui import diagnostics, winshell
from qexport.gui.files import WorkbookIndex, old_partials
from qexport.store.checkpoint import CheckpointStore, RunLock, StoreLockedError
from qexport.tasks import (
    OUTCOME_TEXT, Task, TaskConflict, TaskContext, TaskKind, TaskManager, TaskState, store_path,
)

log = logging.getLogger("qexport.gui")

QISCUS_DASHBOARD = "https://multichannel.qiscus.com/"
LOGIN_RECHECK_S = 15 * 60
MIN_RPS, MAX_RPS = 0.2, 5.0

BLOCK_OTHER_WINDOW = ("A download is running in another qexport window (the black console window). "
                      "Its progress shows here; the download buttons come back when it finishes.")
BLOCK_CHECKING = "Checking the downloaded data after an unexpected shutdown. This takes a minute."
BLOCK_DAMAGED = ("The downloaded data file failed its health check. Copy the diagnostics and send "
                 "them to support before downloading more.")
BLOCK_LOGIN = "Sign in to Qiscus first (the button at the top right)."


class UserError(Exception):
    """A request the app refuses, with a message to show as is."""


@dataclass
class Options:
    include_messages: bool = True
    include_broadcasts: bool = True
    include_recipients: bool = True
    auto_workbook: bool = True
    broadcasts_since: str | None = None      # YYYY-MM-DD
    workbook_dir: str | None = None          # None = the exports folder
    rooms_rps: float | None = None           # None = the safe default in Settings
    messages_rps: float | None = None
    broadcast_rps: float | None = None

    @classmethod
    def load(cls, path: Path) -> Options:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            names = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in data.items() if k in names})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")

    def updated(self, changes: dict) -> Options:
        """A validated copy with `changes` applied; raises UserError with a readable reason."""
        cur = asdict(self)
        for k, v in changes.items():
            if k not in cur:
                raise UserError(f"Unknown option {k!r}.")
            cur[k] = v
        for k in ("include_messages", "include_broadcasts", "include_recipients", "auto_workbook"):
            cur[k] = bool(cur[k])
        if not (cur["include_messages"] or cur["include_broadcasts"]):
            raise UserError("Choose at least one of conversations or broadcast campaigns to download.")
        if cur["broadcasts_since"] in ("", None):
            cur["broadcasts_since"] = None
        else:
            try:
                date.fromisoformat(cur["broadcasts_since"])
            except (TypeError, ValueError):
                raise UserError("The campaign start date must look like 2026-09-01.") from None
        for k in ("rooms_rps", "messages_rps", "broadcast_rps"):
            if cur[k] in ("", None):
                cur[k] = None
                continue
            try:
                cur[k] = float(cur[k])
            except (TypeError, ValueError):
                raise UserError("Request speeds must be numbers.") from None
            if not MIN_RPS <= cur[k] <= MAX_RPS:
                raise UserError(f"Request speeds must be between {MIN_RPS} and {MAX_RPS} per second.")
        if cur["workbook_dir"] in ("", None):
            cur["workbook_dir"] = None
        else:
            p = Path(cur["workbook_dir"])
            if not p.is_absolute():
                raise UserError("Choose a full folder path for the Excel files, e.g. C:\\Users\\me\\Documents.")
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise UserError(f"Can't use that folder: {e}") from None
        return Options(**cur)


class GuiApp:
    def __init__(self, root: Path, *, auto_signin: bool = False) -> None:
        self.root = root
        self.auto_signin = auto_signin      # no login saved yet: open the Qiscus sign-in window at start
        self.env_file = root / ".env"
        self.exports = root / "exports"
        self.exports.mkdir(parents=True, exist_ok=True)
        self._options_path = self.exports / ".qexport_gui_options.json"
        self.options = Options.load(self._options_path)
        self.activity: deque[tuple[str, str, str]] = deque(maxlen=600)
        self._lock = threading.RLock()
        self.index = WorkbookIndex(self.exports / ".qexport_workbooks.json")
        self.manager = TaskManager(self._context, auto_workbook=self.options.auto_workbook,
                                   history_path=self.exports / ".qexport_history.json", on_log=self._task_log)
        self.login: dict = {"state": "unknown"}
        self.integrity: dict = {"state": "not_run"}
        self.unclean_shutdown = False
        self._settings: Settings | None = None
        self._app_code: str | None = None
        self._store_lock: RunLock | None = None
        self._other_window = False
        self._reader: CheckpointStore | None = None
        self._limiters: Limiters | None = None
        self._limiter_rates: tuple | None = None
        self._cache: dict = {"store": {"exists": False}, "files": [], "updated": 0.0}
        self.started = time.monotonic()
        self.last_heartbeat: float | None = None
        self.windows_seen = 0
        self.exit_requested = threading.Event()
        self._stop = threading.Event()
        self._login_checked_at = 0.0

    # ================================================================== lifecycle

    def start(self) -> None:
        self.note("app", f"qexport {__version__} started.")
        self._load_settings()
        self._bind_store()
        self.refresh()
        threading.Thread(target=self._background, name="qexport-gui-refresh", daemon=True).start()
        if self._settings is not None:
            self.check_login_async()
        elif self.auto_signin:
            from qexport.login import find_browser

            if find_browser() is not None:
                self.login_browser_start(delay_s=2.5)    # let the app window open first

    def close(self) -> None:
        """Stop tasks at a safe point and release every lock. Idempotent."""
        self._stop.set()
        self.manager.shutdown(timeout=90)
        with self._lock:
            if self._reader is not None:
                self._reader.close()
                self._reader = None
            if self._store_lock is not None:
                self._store_lock.__exit__(None, None, None)
                self._store_lock = None
        self.note("app", "Closed.")

    def heartbeat(self) -> None:
        if self.last_heartbeat is None or time.monotonic() - self.last_heartbeat > 30:
            self.windows_seen += 1
        self.last_heartbeat = time.monotonic()

    def _background(self) -> None:
        notified_background = False
        while not self._stop.wait(2.0):
            try:
                self.refresh()
            except Exception:   # noqa: BLE001
                log.exception("refresh failed")
            if self._other_window:
                self._try_take_store()
            if self._settings is not None and self.login.get("state") != "signing_in" \
                    and time.monotonic() - self._login_checked_at > LOGIN_RECHECK_S:
                self.check_login_async()
            # No window for a while: keep going while tasks run, then exit on our own.
            last = self.last_heartbeat or self.started
            grace = 45 if self.last_heartbeat else 180
            if time.monotonic() - last > grace:
                if self.manager.active():
                    if not notified_background:
                        self.note("app", "The window was closed; work continues in the background. "
                                         "Open Qiscus Export again to see it.")
                        notified_background = True
                else:
                    self.note("app", "No window open and nothing running; stopping.")
                    self.exit_requested.set()
            else:
                notified_background = False

    # ================================================================== logging

    def note(self, who: str, msg: str) -> None:
        self.activity.append((datetime.now().strftime("%H:%M:%S"), who, msg))
        log.info("%s: %s", who, msg)

    def _task_log(self, task: Task, msg: str) -> None:
        self.note(task.title, msg)
        if task.outcome == "auth" and task.state is not TaskState.RUNNING:
            self.login = {**self.login, "state": "expired"}
        if task.report is not None and msg.startswith("Saved "):
            # Show the new file right away instead of on the next 2-second refresh.
            threading.Thread(target=self.refresh, name="qexport-gui-refresh-now", daemon=True).start()

    # ================================================================== settings & store

    def _overrides(self) -> dict:
        o = self.options
        out: dict = {"out_dir": self.exports}
        if o.broadcasts_since:
            out["broadcasts_since"] = date.fromisoformat(o.broadcasts_since)
        for k in ("rooms_rps", "messages_rps", "broadcast_rps"):
            if getattr(o, k) is not None:
                out[k] = getattr(o, k)
        return out

    def _load_settings(self) -> Settings | None:
        try:
            s = load_settings(env_file=self.env_file, **self._overrides())
        except ValidationError:
            s = None
        with self._lock:
            self._settings = s
            if s is not None:
                self._app_code = s.app_code
            elif self._app_code is None:
                self._app_code = self._discover_app_code()
        if s is None:
            self.login = {"state": "missing"}
        return s

    def _discover_app_code(self) -> str | None:
        """Not logged in yet: still show the data of the store already in exports/."""
        stores = sorted(self.exports.glob("qexport_*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
        return stores[0].stem.removeprefix("qexport_") if stores else None

    def _store_path(self) -> Path | None:
        return self.exports / f"qexport_{self._app_code}.sqlite3" if self._app_code else None

    def _bind_store(self) -> None:
        path = self._store_path()
        if path is None:
            self._update_blocked()
            return
        lock_path = path.with_suffix(".lock")
        left_behind = lock_path.exists()
        lock = RunLock(lock_path)
        try:
            lock.__enter__()
        except StoreLockedError:
            self._other_window = True
            self.note("app", "Another qexport window is using the data; watching it.")
        else:
            self._store_lock = lock
            self._other_window = False
            if left_behind:
                # RunLock deletes its file on a clean exit; one left behind means the last run
                # was cut off (power cut, killed window).
                self.unclean_shutdown = True
                self.note("app", "The last session ended unexpectedly. Checking the downloaded data…")
                self.run_integrity_check()
        self._update_blocked()

    def _try_take_store(self) -> None:
        path = self._store_path()
        if path is None:
            return
        lock = RunLock(path.with_suffix(".lock"))
        try:
            lock.__enter__()
        except StoreLockedError:
            return
        with self._lock:
            self._store_lock = lock
            self._other_window = False
        self.note("app", "The other qexport window finished. You can download from here now.")
        self._update_blocked()

    def _reader_store(self) -> CheckpointStore | None:
        path = self._store_path()
        if path is None or not path.exists():
            return None
        if self._reader is None or self._reader.path != path:
            if self._reader is not None:
                self._reader.close()
            self._reader = CheckpointStore(path, self._app_code)
        return self._reader

    def _update_blocked(self) -> None:
        if self._other_window:
            reason = BLOCK_OTHER_WINDOW
        elif self.integrity.get("state") == "checking" and self.unclean_shutdown:
            reason = BLOCK_CHECKING
        elif self.integrity.get("state") == "damaged":
            reason = BLOCK_DAMAGED
        elif self._settings is None:
            reason = BLOCK_LOGIN
        else:
            reason = None
        self.manager.blocked_reason = reason

    def _settings_for_build(self) -> Settings:
        """Building a workbook needs no login; fill the credential fields with placeholders."""
        if self._settings is not None:
            return self._settings
        if not self._app_code:
            raise TaskConflict("Nothing has been downloaded yet.")
        return Settings(_env_file=None, app_code=self._app_code, admin_token="-", sdk_user_id="-",
                        sdk_token="-", **self._overrides())

    def _context(self, kind: TaskKind) -> TaskContext:
        if kind is TaskKind.WORKBOOK:
            s = self._settings_for_build()
        else:
            s = self._load_settings()       # picks up a login refreshed since the last task
            if s is None:
                raise TaskConflict(BLOCK_LOGIN)
        rates = (s.rooms_rps, s.messages_rps, s.broadcast_rps)
        if self._limiters is None or (rates != self._limiter_rates and not self.manager.downloads_active()):
            self._limiters, self._limiter_rates = Limiters.from_settings(s), rates
        o = self.options
        return TaskContext(settings=s, limiters=self._limiters, store_path=store_path(s),
                           workbook_dir=Path(o.workbook_dir) if o.workbook_dir else self.exports,
                           include_recipients=o.include_recipients)

    # ================================================================== status

    def refresh(self) -> None:
        with self._lock:
            store = self._store_stats()
        folders = [self.exports] + ([Path(self.options.workbook_dir)] if self.options.workbook_dir else [])
        files = [asdict(w) for w in self.index.list(folders)]
        self._cache = {"store": store, "files": files, "updated": time.time()}

    def _store_stats(self) -> dict:
        path = self._store_path()
        if path is None or not path.exists():
            return {"exists": False}
        r = self._reader_store()
        p = r.progress()
        missing = r.missing(self._settings.broadcasts_since if self._settings else None)
        size = sum(f.stat().st_size for f in (path, Path(str(path) + "-wal")) if f.exists())
        gone, gone_newest = r.jobs_gone()
        run = r.run_info()
        return {
            "exists": True, "path": str(path), "size_mb": round(size / 1e6, 1),
            "rooms_total": p.rooms_total, "rooms_done": p.done, "rooms_pending": p.pending,
            "rooms_failed": p.failed, "rooms_skipped": p.skipped, "rooms_listed": p.enumeration_done,
            "messages": p.messages, "jobs_total": p.jobs_total, "jobs_done": p.jobs_done,
            "jobs_pending": missing.jobs_pending, "jobs_failed": p.jobs_failed, "jobs_skipped": p.jobs_skipped,
            "jobs_listed": p.broadcast_enumeration_done, "jobs_not_sent": r.jobs_not_sent_yet(),
            "jobs_gone": gone, "jobs_gone_newest": (gone_newest or "")[:10],
            "broadcast_logs": p.broadcast_logs, "complete": missing.complete,
            "completeness": missing.describe(), "last_completed": run.get("last_completed_at_utc"),
            "rooms_missing": missing.rooms_pending, "rooms_failed_total": missing.rooms_failed,
            "jobs_failed_total": missing.jobs_failed,
            "skipped": [f"{k} {i}: {e}" for k, i, e in r.skipped()[:40]],
        }

    def verdict(self, store: dict, tasks: list[dict]) -> dict:
        """The one sentence at the top of the window, plus the button that fits it."""
        running = [t for t in tasks if t["state"] in ("running", "paused", "pending") and t["kind"] != "workbook"]
        if self.integrity.get("state") == "damaged":
            return {"level": "error", "headline": "The downloaded data file is damaged.",
                    "detail": BLOCK_DAMAGED, "action": "diagnostics"}
        if not store.get("exists") or (store.get("rooms_total", 0) == 0 and store.get("jobs_total", 0) == 0):
            if running:
                return {"level": "busy", "headline": "Downloading for the first time…",
                        "detail": "Excel files appear in the list on the right as soon as there is data.",
                        "action": None}
            return {"level": "empty", "headline": "Nothing downloaded yet.",
                    "detail": "Sign in to Qiscus, then press Download everything." if self._settings is None
                    else "Press Download everything to start. It takes 1.5 to 2.5 hours the first time "
                         "and can be paused or stopped at any point.",
                    "action": "login" if self._settings is None else "download"}
        rooms, jobs = store["rooms_total"], store["jobs_total"]
        conv_line = (f"All {rooms:,} conversations downloaded" if store["rooms_listed"] and not store["rooms_missing"]
                     and not store["rooms_failed_total"]
                     else f"{store['rooms_done']:,} of {rooms:,} conversations downloaded")
        jobs_ok = store["jobs_listed"] and not store["jobs_pending"] and not store["jobs_failed_total"]
        fetchable = jobs - store["jobs_not_sent"] - store.get("jobs_gone", 0)
        bc_line = (f"all {fetchable:,} available broadcast campaigns downloaded" if jobs_ok
                   else f"{store['jobs_done']:,} of {fetchable:,} available broadcast campaigns downloaded")
        notes = []
        if store.get("jobs_gone"):
            notes.append(f"{store['jobs_gone']:,} older campaigns (up to {store['jobs_gone_newest']}): Qiscus no "
                         "longer keeps who received them, so only their totals are in the Excel file.")
        if store["jobs_not_sent"]:
            notes.append(f"{store['jobs_not_sent']:,} campaigns haven't been sent yet; they are picked up "
                         "once they go out.")
        if self.unclean_shutdown and self.integrity.get("state") == "ok":
            notes.append("The last session ended unexpectedly (power cut or closed window); the data "
                         "was checked and is fine.")
        if self._other_window:
            return {"level": "busy", "headline": f"{conv_line}; {bc_line}.", "detail": BLOCK_OTHER_WINDOW,
                    "notes": notes, "action": None}
        if running:
            return {"level": "busy", "headline": f"Downloading… {conv_line}; {bc_line}.",
                    "detail": "Your data is saved as it arrives. Pause or cancel any time without losing it.",
                    "notes": notes, "action": None}
        if store["complete"]:
            return {"level": "ok", "headline": f"{conv_line}; {bc_line}.",
                    "detail": "Everything is safely saved on this PC. Press Build Excel now for a fresh file, "
                              "or Update to fetch new activity.",
                    "notes": notes, "action": "build"}
        failed = store["rooms_failed_total"] + store["jobs_failed_total"]
        left = store["rooms_missing"] + store["jobs_pending"]
        if left or not store["rooms_listed"] or not store["jobs_listed"]:
            what = []
            if store["rooms_missing"] or not store["rooms_listed"]:
                what.append(f"{store['rooms_missing']:,} conversations")
            if store["jobs_pending"] or not store["jobs_listed"]:
                what.append(f"{store['jobs_pending']:,} broadcast campaigns")
            return {"level": "warn", "headline": f"{conv_line}; {bc_line}.",
                    "detail": f"Download interrupted. Everything so far is saved; {' and '.join(what)} "
                              "still to fetch. Press Continue download to pick up where it stopped.",
                    "notes": notes, "action": "download"}
        return {"level": "warn", "headline": f"{conv_line}; {bc_line}.",
                "detail": f"{failed:,} items could not be downloaded (Qiscus refused them). The rest is "
                          "saved. Press Retry failed items to try them again.",
                "notes": notes, "action": "retry"}

    def state(self) -> dict:
        tasks = [t.snapshot() for t in self.manager.tasks()]
        store = self._cache["store"]
        return {
            "version": __version__, "now": datetime.now().isoformat(timespec="seconds"),
            "app_code": self._app_code, "logged_in": self._settings is not None,
            "login": self.login, "integrity": self.integrity, "unclean_shutdown": self.unclean_shutdown,
            "blocked": self.manager.blocked_reason, "other_window": self._other_window,
            "store": store, "verdict": self.verdict(store, tasks), "tasks": tasks,
            "files": self._cache["files"], "options": asdict(self.options),
            "defaults": {"rooms_rps": 1.2, "messages_rps": 3.0, "broadcast_rps": 1.2},
            "activity": list(self.activity)[-120:], "history": self.manager.history.entries[-25:][::-1],
            "exports_dir": str(self.exports),
            "workbook_dir": self.options.workbook_dir or str(self.exports),
            "outcomes": OUTCOME_TEXT, "login_snippet": CONSOLE_SNIPPET,
        }

    # ================================================================== actions: tasks

    def download(self, update: bool = False) -> dict:
        kinds = [k for k, on in ((TaskKind.CONVERSATIONS, self.options.include_messages),
                                 (TaskKind.BROADCASTS, self.options.include_broadcasts)) if on]
        started, notes = [], []
        for kind in kinds:
            try:
                started.append(self.manager.start(kind, since_last_run=update,
                                                  reason="update" if update else "").title)
            except TaskConflict as e:
                notes.append(str(e))
        if not started:
            raise UserError(" ".join(dict.fromkeys(notes)) or "Nothing to download.")
        return {"started": started, "notes": notes}

    def retry_failed(self) -> dict:
        if self.manager.downloads_active():
            raise UserError("Wait for the running download to finish (or cancel it) before retrying failed items.")
        if self.manager.blocked_reason:
            raise UserError(self.manager.blocked_reason)
        with self._lock:
            r = self._reader_store()
            n = r.reset_failed() if r is not None else 0
        self.note("app", f"{n:,} failed item(s) will be tried again.")
        return {**self.download(), "reset": n}

    def build(self) -> dict:
        try:
            return {"started": [self.manager.start(TaskKind.WORKBOOK, reason="requested").title]}
        except TaskConflict as e:
            raise UserError(str(e)) from None

    def task_action(self, task_id: str, action: str) -> None:
        if self.manager.get(task_id) is None:
            raise UserError("That task no longer exists.")
        fn = {"pause": self.manager.pause, "resume": self.manager.resume, "cancel": self.manager.cancel,
              "retry-save": self.manager.retry_save}.get(action)
        if fn is None:
            raise UserError(f"Unknown action {action!r}.")
        fn(task_id)

    def run_integrity_check(self) -> None:
        if self.integrity.get("state") == "checking":
            return
        self.integrity = {"state": "checking", "started": datetime.now().isoformat(timespec="seconds")}
        self._update_blocked()
        threading.Thread(target=self._integrity, name="qexport-integrity", daemon=True).start()

    def _integrity(self) -> None:
        path = self._store_path()
        if path is None or not path.exists():
            self.integrity = {"state": "ok", "detail": "No data yet."}
            self._update_blocked()
            return
        t0 = time.monotonic()
        try:
            s = CheckpointStore(path, self._app_code)
            try:
                result = s.integrity_check()
            finally:
                s.close()
            ok = result == ["ok"]
            detail = "No problems found." if ok else "; ".join(result[:5])
        except Exception as e:   # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"
        self.integrity = {"state": "ok" if ok else "damaged", "detail": detail,
                          "seconds": round(time.monotonic() - t0, 1),
                          "finished": datetime.now().isoformat(timespec="seconds")}
        self.note("app", f"Data health check: {detail}")
        self._update_blocked()

    # ================================================================== actions: files

    def _file(self, name: str) -> Path:
        """Only files the panel lists may be touched (no paths from the browser)."""
        for f in self._cache["files"]:
            if f["name"] == name:
                return Path(f["path"])
        self.refresh()
        for f in self._cache["files"]:
            if f["name"] == name:
                return Path(f["path"])
        raise UserError(f"{name} is no longer in the list.")

    def open_file(self, name: str) -> None:
        winshell.open_path(self._file(name))

    def reveal_file(self, name: str) -> None:
        winshell.reveal(self._file(name))

    def copy_text(self, text: str) -> bool:
        return winshell.clipboard_set(text)

    def open_folder(self, which: str) -> None:
        if which == "workbooks":
            winshell.open_path(Path(self.options.workbook_dir) if self.options.workbook_dir else self.exports)
        elif which == "store":
            p = self._store_path()
            if p is not None and p.exists():
                winshell.reveal(p)
            else:
                winshell.open_path(self.exports)
        else:
            winshell.open_path(self.exports)

    def delete_files(self, names: list[str]) -> dict:
        deleted, errors = [], []
        busy = {t.report.path.name for t in self.manager.active(TaskKind.WORKBOOK) if t.report}
        for name in names:
            if name in busy:
                errors.append(f"{name}: still being written")
                continue
            try:
                winshell.recycle(self._file(name))
                deleted.append(name)
            except (OSError, UserError) as e:
                errors.append(f"{name}: {e}")
        if deleted:
            self.note("app", f"Moved {len(deleted)} file(s) to the Recycle Bin.")
        self.refresh()
        return {"deleted": deleted, "errors": errors}

    def old_files(self) -> list[str]:
        from qexport.gui.files import WorkbookInfo

        return [w.name for w in old_partials([WorkbookInfo(**f) for f in self._cache["files"]])]

    # ================================================================== actions: login

    def login_start(self) -> dict:
        copied = winshell.clipboard_set(CONSOLE_SNIPPET)
        import webbrowser

        webbrowser.open(QISCUS_DASHBOARD)
        return {"copied": copied, "snippet": CONSOLE_SNIPPET}

    def login_read_clipboard(self) -> dict:
        return self.login_save(winshell.clipboard_get(), from_clipboard=True)

    def login_browser_start(self, delay_s: float = 0.0) -> dict:
        """Open the Qiscus sign-in window; the login is picked up in the background."""
        if self.login.get("state") == "signing_in":
            return self.login
        self.login = {"state": "signing_in", "message": "Opening the Qiscus sign-in window…"}
        threading.Thread(target=self._browser_login, args=(delay_s,), name="qexport-signin", daemon=True).start()
        return self.login

    def _browser_login(self, delay_s: float = 0.0) -> None:
        from qexport.login import BrowserLoginError, browser_login

        if delay_s:
            time.sleep(delay_s)

        def status(msg: str) -> None:
            self.login = {**self.login, "message": msg}
            self.note("app", msg)
        try:
            creds = browser_login(self.exports / ".qexport_signin", on_status=status)
            result = self._apply_creds(creds)
        except (BrowserLoginError, UserError) as e:
            self.login = {"state": "signin_failed", "message": str(e)}
            self.note("app", f"Sign-in did not finish: {e}")
            return
        except Exception as e:   # noqa: BLE001
            log.exception("browser sign-in failed")
            self.login = {"state": "signin_failed", "message": f"{type(e).__name__}: {e}"}
            return
        self.login = {**result, "message": "Connected." if result["state"] == "ok"
                      else "Signed in, but Qiscus refused the login. Make sure it's an admin account."}

    def login_save(self, text: str, from_clipboard: bool = False) -> dict:
        creds = parse_credentials(text or "")
        missing = [k for k in CRED_KEYS if not creds.get(k)]
        if missing:
            where = "on the clipboard" if from_clipboard else "in the pasted text"
            raise UserError(f"Couldn't find the login values {where} (missing {', '.join(missing)}). "
                            "Run the snippet again in the Qiscus tab; it says 'Copied!' when it worked.")
        return self._apply_creds(creds)

    def _apply_creds(self, creds: dict[str, str]) -> dict:
        new_app = creds["QISCUS_APP_CODE"]
        if self._app_code and new_app != self._app_code and self.manager.active():
            raise UserError("That login is for a different Qiscus app. Stop the running tasks first.")
        write_env(self.env_file, {k: creds[k] for k in (*CRED_KEYS, "QISCUS_ROLE") if creds.get(k)})
        self.note("app", "Saved the Qiscus login.")
        old_app = self._app_code
        self._load_settings()
        if new_app != old_app:
            with self._lock:
                if self._reader is not None:
                    self._reader.close()
                    self._reader = None
                if self._store_lock is not None:
                    self._store_lock.__exit__(None, None, None)
                    self._store_lock = None
            self._bind_store()
        self._update_blocked()
        result = self.check_login()
        result["not_admin"] = creds.get("QISCUS_ROLE", "admin") != "admin"
        return result

    def check_login_async(self) -> None:
        self._login_checked_at = time.monotonic()
        threading.Thread(target=self.check_login, name="qexport-login-check", daemon=True).start()

    def check_login(self) -> dict:
        self._login_checked_at = time.monotonic()
        s = self._settings
        if s is None:
            self.login = {"state": "missing"}
            return self.login
        self.login = {**self.login, "state": "checking"}
        client = QiscusClient(s, limiters=self._limiters)
        try:
            status = client.ping()
        finally:
            client.close()
        ok = all(status.values())
        self.login = {"state": "ok" if ok else "failed", "omni": status["omni"], "sdk": status["sdk"],
                      "checked": datetime.now().isoformat(timespec="seconds")}
        if ok:
            self.note("app", "Qiscus login checked: OK.")
        else:
            self.note("app", "Qiscus login check failed: "
                             + ", ".join(k for k, v in (("dashboard", status["omni"]), ("chat", status["sdk"]))
                                         if not v) + ".")
        return self.login

    # ================================================================== actions: options & support

    def set_options(self, changes: dict) -> dict:
        new = self.options.updated(changes)
        new.save(self._options_path)
        self.options = new
        self.manager.auto_workbook = new.auto_workbook
        if self._settings is not None:
            self._load_settings()
        self.note("app", "Options saved.")
        self.refresh()
        return asdict(new)

    def diagnostics(self) -> str:
        secrets = []
        if self._settings is not None:
            secrets = [self._settings.admin_token.get_secret_value(), self._settings.sdk_token.get_secret_value()]
        return diagnostics.build_report(state=self.state(), activity=list(self.activity),
                                        history=self.manager.history.entries, secrets=secrets)
