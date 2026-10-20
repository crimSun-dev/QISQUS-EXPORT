"""Background tasks over the export engine: download conversations, download broadcast logs and
build the workbook, each in its own thread, with progress, pause / resume / cancel.

Every task opens its own CheckpointStore connection and its own QiscusClient; the clients share
one `Limiters`, so parallel downloads stay inside the per-host request ceilings."""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from qexport.api.client import Limiters, QiscusClient
from qexport.config import Settings
from qexport.extract.broadcasts import BroadcastEnumerator, BroadcastLogFetcher
from qexport.extract.messages import MessageFetcher
from qexport.extract.rooms import RoomEnumerator
from qexport.load.excel import ExcelWriter, ExportReport, WorkbookLockedError, finish_locked_workbook
from qexport.orchestrator import (
    EXIT_AUTH, EXIT_INTERRUPTED, EXIT_NETWORK, EXIT_OK, EXIT_PARTIAL, EXIT_RATE_LIMIT, EXIT_SCHEMA,
    ExportOrchestrator, RunCancelled, RunResult, build_workbook,
)
from qexport.store.checkpoint import CheckpointStore
from qexport.transform.normalize import Normalizer

log = logging.getLogger("qexport.tasks")


def store_path(settings: Settings) -> Path:
    return settings.out_dir / f"qexport_{settings.app_code}.sqlite3"


class TaskKind(StrEnum):
    CONVERSATIONS = "conversations"
    BROADCASTS = "broadcasts"
    WORKBOOK = "workbook"


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


ACTIVE_STATES = {TaskState.PENDING, TaskState.RUNNING, TaskState.PAUSED}

TITLES = {
    TaskKind.CONVERSATIONS: "Downloading conversations",
    TaskKind.BROADCASTS: "Downloading broadcast campaigns",
    TaskKind.WORKBOOK: "Building the Excel file",
}

# Why a task ended, in words for the people using the tool. Keys are Task.outcome.
OUTCOME_TEXT = {
    "ok": "Finished.",
    "partial": "Finished, but some items could not be downloaded. They are listed in the Excel "
               "file's Export Info sheet.",
    "auth": "The Qiscus login expired. Everything downloaded so far is safe. Refresh the login, "
            "then continue.",
    "rate": "Qiscus asked us to slow down. Everything downloaded so far is safe. Wait a few "
            "minutes, then continue.",
    "network": "Could not reach Qiscus. Everything downloaded so far is safe. Check the internet "
               "connection, then continue.",
    "api": "Qiscus refused a request or changed how it works. Everything downloaded so far is safe. "
           "Copy the diagnostics and send them to support.",
    "cancelled": "Cancelled. Everything downloaded so far is safe; continue any time.",
    "interrupted": "Stopped. Everything downloaded so far is safe; continue any time.",
    "locked": "The Excel file is open in another program. Close it, then press Retry.",
    "error": "Something unexpected went wrong. The downloaded data is safe. Copy the diagnostics "
             "and send them to support.",
}
_EXIT_OUTCOME = {EXIT_OK: "ok", EXIT_PARTIAL: "partial", EXIT_AUTH: "auth", EXIT_RATE_LIMIT: "rate",
                 EXIT_NETWORK: "network", EXIT_SCHEMA: "api", EXIT_INTERRUPTED: "interrupted"}


class TaskConflict(Exception):
    """A task can't start now; the message says why in plain words."""


@dataclass
class TaskContext:
    """Everything a task needs, captured when it starts (fresh login, current options)."""
    settings: Settings
    limiters: Limiters
    store_path: Path
    workbook_dir: Path
    include_recipients: bool = True
    since_last_run: bool = False


class Throughput:
    """Items/s over a sliding window, and the time left at that pace."""

    def __init__(self, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._window, self._clock = window_s, clock
        self._samples: deque[tuple[float, int]] = deque()

    def reset(self) -> None:
        self._samples.clear()

    def record(self, done: int) -> None:
        now = self._clock()
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[0][0] > self._window:
            self._samples.popleft()

    def rate(self) -> float | None:
        if len(self._samples) < 2:
            return None
        (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
        return (d1 - d0) / (t1 - t0) if t1 > t0 and d1 > d0 else None

    def eta_s(self, done: int, total: int | None) -> float | None:
        r = self.rate()
        if r is None or not total or done >= total:
            return None
        return (total - done) / r


class Task:
    def __init__(self, kind: TaskKind, *, auto: bool = False, reason: str = "") -> None:
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.title = TITLES[kind]
        self.auto, self.reason = auto, reason
        self.state = TaskState.PENDING
        self.phase = "Waiting to start"
        self.total: int | None = None
        self.done = 0
        self.current = ""
        self.counters: dict[str, int] = {}
        self.committed = 0          # conversations / campaigns saved by this task
        self.warnings = 0
        self.created_at = datetime.now(timezone.utc)
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.outcome: str | None = None
        self.message = ""
        self.result: RunResult | None = None
        self.report: ExportReport | None = None
        self.locked: WorkbookLockedError | None = None
        self.log: deque[tuple[str, str]] = deque(maxlen=400)
        self.throughput = Throughput()
        self._cond = threading.Condition()
        self._pause = False
        self._cancel = False
        self.on_log: Callable[[Task, str], None] | None = None

    # ------------------------------------------------------------------ controls

    def pause(self) -> None:
        with self._cond:
            if self.state in (TaskState.RUNNING, TaskState.PENDING):
                self._pause = True
                self._log("Pausing after the current item…")

    def resume(self) -> None:
        with self._cond:
            if self._pause:
                self._pause = False
                self._cond.notify_all()

    def cancel(self) -> None:
        with self._cond:
            if self.state in ACTIVE_STATES and not self._cancel:
                self._cancel = True
                self._log("Cancelling after the current item…")
                self._cond.notify_all()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel

    def checkpoint(self) -> None:
        """Called between committed items: blocks while paused, raises RunCancelled on cancel."""
        with self._cond:
            if self._pause and not self._cancel:
                self.state = TaskState.PAUSED
                self._log("Paused.")
                self.throughput.reset()
                while self._pause and not self._cancel:
                    self._cond.wait()
                if not self._cancel:
                    self.state = TaskState.RUNNING
                    self._log("Resumed.")
            if self._cancel:
                raise RunCancelled()

    # ------------------------------------------------------------------ progress (ProgressSink)

    def _log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.append((stamp, msg))
        if self.on_log:
            self.on_log(self, msg)

    def phase_(self, name: str, total: int | None) -> None:
        self.phase, self.total, self.done = name, total, 0
        self.throughput.reset()
        self.throughput.record(0)
        self._log(name + (f" ({total:,})" if total else ""))

    def advance_(self, n: int, info: dict) -> None:
        self.done += n
        if "messages" in info or "recipients" in info:   # one conversation / campaign committed
            self.committed += 1
        for k, v in info.items():
            if isinstance(v, int):
                self.counters[k] = self.counters.get(k, 0) + v if k in ("messages", "recipients") else v
        if n:
            self.throughput.record(self.done)
        self.checkpoint()

    def snapshot(self) -> dict:
        rate = self.throughput.rate()
        eta = self.throughput.eta_s(self.done, self.total)
        return {
            "id": self.id, "kind": str(self.kind), "title": self.title, "state": str(self.state),
            "auto": self.auto, "reason": self.reason, "phase": self.phase, "total": self.total,
            "done": self.done, "current": self.current, "counters": dict(self.counters),
            "committed": self.committed,
            "warnings": self.warnings, "rate": rate, "eta_s": eta,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "outcome": self.outcome, "outcome_text": OUTCOME_TEXT.get(self.outcome or "", ""),
            "message": self.message, "can_retry_save": self.locked is not None,
            "report": _report_dict(self.report),
            "log": list(self.log)[-60:],
        }

    # ------------------------------------------------------------------ execution

    def execute(self, ctx: TaskContext) -> None:
        """Run to the end in the calling thread. Never raises; the outcome is on the task."""
        self.state = TaskState.RUNNING
        self.started_at = datetime.now(timezone.utc)
        self._log(f"Started{' (' + self.reason + ')' if self.reason else ''}.")
        try:
            self.checkpoint()
            if self.kind is TaskKind.WORKBOOK:
                self._build(ctx)
            else:
                self._download(ctx)
        except RunCancelled:
            self.outcome = "cancelled"
        except WorkbookLockedError as e:
            self.locked, self.report, self.outcome = e, e.report, "locked"
            self.message = str(e)
        except Exception as e:   # noqa: BLE001 - shown to the user, full trace in the log file
            log.exception("task %s (%s) crashed", self.id, self.kind)
            self.outcome, self.message = "error", f"{type(e).__name__}: {e}"
        self._finish()

    def retry_save(self) -> None:
        """After 'locked': try moving the finished workbook into place again."""
        if self.locked is None:
            return
        try:
            self.report = finish_locked_workbook(self.locked)
        except WorkbookLockedError as e:
            self.locked = e
            self._log("The file is still open in another program.")
            return
        self.locked = None
        self.outcome, self.message = "partial" if not self.report.complete else "ok", ""
        self.state = TaskState.COMPLETED
        self._log(f"Saved {self.report.path.name}.")

    def _finish(self) -> None:
        if self._cancel and self.outcome in ("interrupted", "cancelled", None):
            self.outcome = "cancelled"
        self.state = {
            "ok": TaskState.COMPLETED, "partial": TaskState.COMPLETED, "cancelled": TaskState.CANCELLED,
        }.get(self.outcome or "", TaskState.FAILED)
        self.finished_at = datetime.now(timezone.utc)
        self.current = ""
        self._log(OUTCOME_TEXT.get(self.outcome or "error", "") + (f" {self.message}" if self.message else ""))

    def _sink(self) -> _TaskProgress:
        return _TaskProgress(self)

    def _download(self, ctx: TaskContext) -> None:
        s = ctx.settings
        norm = Normalizer(s.display_tz)
        store = CheckpointStore(ctx.store_path, s.app_code)
        client = QiscusClient(s, limiters=ctx.limiters)
        try:
            conversations = self.kind is TaskKind.CONVERSATIONS
            orch = ExportOrchestrator(
                s, client, store, RoomEnumerator(client, store, norm, s.page_limit),
                MessageFetcher(client, norm, s.page_limit), ExcelWriter(norm), self._sink(),
                None if conversations else BroadcastEnumerator(client, store, norm, s.page_limit,
                                                               s.broadcasts_since),
                None if conversations else BroadcastLogFetcher(client, norm, s.page_limit))
            result = orch.run(since_last_run=ctx.since_last_run, write_xlsx=False,
                              include_messages=conversations, include_broadcasts=not conversations)
        finally:
            client.close()
            store.close()
        self.result = result
        self.outcome = _EXIT_OUTCOME.get(result.exit_code, "error")
        if self.outcome not in ("ok",):
            self.message = result.message

    def _build(self, ctx: TaskContext) -> None:
        s = ctx.settings.model_copy(update={"out_dir": ctx.workbook_dir})
        store = CheckpointStore(ctx.store_path, s.app_code)
        try:
            self.report = build_workbook(store, s, ExcelWriter(Normalizer(s.display_tz)), self._sink(),
                                         include_recipients=ctx.include_recipients)
        finally:
            store.close()
        self.outcome = "ok"
        why = self.report.completeness.removeprefix("Partial").strip(" ()")
        self.message = "" if self.report.complete else f"This file is partial: {why}."
        self._log(f"Saved {self.report.path.name}.")


class _TaskProgress:
    """ProgressSink that feeds a Task (and lets it pause/cancel between items)."""

    def __init__(self, task: Task) -> None:
        self._t = task

    def phase(self, name: str, total: int | None) -> None:
        self._t.phase_(name, total)

    def advance(self, n: int = 1, **info: object) -> None:
        self._t.advance_(n, info)

    def item(self, label: str) -> None:
        self._t.current = label

    def warn(self, msg: str) -> None:
        self._t.warnings += 1
        self._t._log(msg)


def _report_dict(r: ExportReport | None) -> dict | None:
    if r is None:
        return None
    return {"path": str(r.path), "name": r.path.name, "complete": r.complete,
            "completeness": r.completeness, "rooms": r.rooms, "messages": r.messages,
            "broadcast_jobs": r.broadcast_jobs, "broadcast_recipients": r.broadcast_recipients,
            "actions": r.actions, "skipped": len(r.skipped_rooms), "duration_s": round(r.duration_s, 1)}


# ====================================================================== manager

@dataclass
class _History:
    path: Path | None
    entries: list[dict] = field(default_factory=list)
    limit: int = 200

    def load(self) -> None:
        if self.path and self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text(encoding="utf-8"))[-self.limit:]
            except (OSError, ValueError):
                self.entries = []

    def add(self, entry: dict) -> None:
        self.entries = (self.entries + [entry])[-self.limit:]
        if self.path:
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.entries, ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                log.warning("could not save task history to %s", self.path)


class TaskManager:
    """Starts tasks in threads, enforces which may run together, and builds a workbook
    automatically after each download task stops (finished, failed or cancelled)."""

    def __init__(self, context: Callable[[TaskKind], TaskContext], *, auto_workbook: bool = True,
                 history_path: Path | None = None,
                 on_log: Callable[[Task, str], None] | None = None) -> None:
        self._context = context
        self.auto_workbook = auto_workbook
        self._lock = threading.RLock()
        self._tasks: list[Task] = []
        self._threads: dict[str, threading.Thread] = {}
        self._rebuild_after: Task | None = None
        self._on_log = on_log
        self.blocked_reason: str | None = None     # e.g. "another qexport window is downloading"
        self.history = _History(history_path)
        self.history.load()

    # ------------------------------------------------------------------ queries

    def tasks(self) -> list[Task]:
        with self._lock:
            return list(self._tasks)

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return next((t for t in self._tasks if t.id == task_id), None)

    def active(self, kind: TaskKind | None = None) -> list[Task]:
        with self._lock:
            return [t for t in self._tasks if t.state in ACTIVE_STATES and (kind is None or t.kind is kind)]

    def downloads_active(self) -> bool:
        return any(t.kind is not TaskKind.WORKBOOK for t in self.active())

    def conflict(self, kind: TaskKind) -> str | None:
        """Why `kind` can't start right now, or None."""
        if kind is not TaskKind.WORKBOOK and self.blocked_reason:
            return self.blocked_reason
        if self.active(kind):
            return {
                TaskKind.CONVERSATIONS: "Conversations are already downloading.",
                TaskKind.BROADCASTS: "Broadcast campaigns are already downloading.",
                TaskKind.WORKBOOK: "An Excel file is already being built. Another one will be made "
                                   "automatically when it finishes, if new data arrives.",
            }[kind]
        return None

    # ------------------------------------------------------------------ commands

    def start(self, kind: TaskKind, *, auto: bool = False, reason: str = "", **ctx_overrides) -> Task:
        """ctx_overrides replace TaskContext fields for this task only (e.g. since_last_run=True)."""
        with self._lock:
            why = self.conflict(kind)
            if why:
                raise TaskConflict(why)
            ctx = dataclasses.replace(self._context(kind), **ctx_overrides)
            task = Task(kind, auto=auto, reason=reason)
            task.on_log = self._on_log
            finished = [t for t in self._tasks if t.state not in ACTIVE_STATES]
            drop = {t.id for t in finished[:-30]}        # keep the 30 newest finished, in order
            self._tasks = [t for t in self._tasks if t.id not in drop] + [task]
            th = threading.Thread(target=self._run, args=(task, ctx), name=f"qexport-{kind}-{task.id}",
                                  daemon=True)
            self._threads[task.id] = th
            th.start()
            return task

    def pause(self, task_id: str) -> None:
        if t := self.get(task_id):
            t.pause()

    def resume(self, task_id: str) -> None:
        if t := self.get(task_id):
            t.resume()

    def cancel(self, task_id: str) -> None:
        if t := self.get(task_id):
            t.cancel()

    def cancel_all(self) -> None:
        with self._lock:
            self._rebuild_after = None
            for t in self.active():
                t.cancel()

    def retry_save(self, task_id: str) -> None:
        if t := self.get(task_id):
            t.retry_save()

    def request_workbook(self, reason: str) -> Task | None:
        """Build now, or once the build in progress ends (so it includes the newest data)."""
        with self._lock:
            running = self.active(TaskKind.WORKBOOK)
            if running:
                self._rebuild_after = running[0]
                return None
            return self.start(TaskKind.WORKBOOK, auto=True, reason=reason)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Test/shutdown helper: wait until no task is active."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not self.active():
                with self._lock:
                    threads = list(self._threads.values())
                if all(not th.is_alive() for th in threads):
                    return True
            time.sleep(0.02)
        return False

    def shutdown(self, timeout: float = 60.0) -> bool:
        """Cancel everything and wait for each task to stop at a safe point."""
        self.auto_workbook = False
        self.cancel_all()
        return self.wait_idle(timeout)

    # ------------------------------------------------------------------ internals

    def _run(self, task: Task, ctx: TaskContext) -> None:
        task.execute(ctx)
        self.history.add({
            "id": task.id, "kind": str(task.kind), "title": task.title, "auto": task.auto,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
            "state": str(task.state), "outcome": task.outcome,
            "outcome_text": OUTCOME_TEXT.get(task.outcome or "", ""), "message": task.message[:500],
            "counters": task.counters, "file": task.report.path.name if task.report else None,
        })
        with self._lock:
            self._threads.pop(task.id, None)
            follow_up = None
            if task.kind is TaskKind.WORKBOOK:
                if self._rebuild_after is task:
                    self._rebuild_after = None
                    follow_up = "new data arrived while the last file was being built"
            elif self.auto_workbook and task.outcome != "error" and task.committed > 0:
                follow_up = (f"{task.title.lower().replace('downloading ', '')} "
                             f"{'finished' if task.state is TaskState.COMPLETED else 'stopped'}")
            if follow_up and self.auto_workbook:
                try:
                    self.request_workbook(follow_up)
                except TaskConflict:
                    pass
