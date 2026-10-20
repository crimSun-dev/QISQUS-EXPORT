"""Background tasks: run standalone or side by side, pause / resume / cancel, conflicts, auto-build."""

import threading
import time

import pytest
import respx

from qexport.api.client import Limiters
from qexport.extract.messages import MessageFetcher
from qexport.store.checkpoint import CheckpointStore
from qexport.tasks import (
    Task, TaskConflict, TaskContext, TaskKind, TaskManager, TaskState, Throughput, store_path,
)
from tests.integration.test_pipeline import _tenant


@pytest.fixture
def context(settings):
    lim = Limiters.from_settings(settings)
    return lambda kind: TaskContext(settings, lim, store_path(settings), settings.out_dir)


@pytest.fixture
def gate(monkeypatch):
    """Each fetch_room waits for one release(), so tests decide when items finish."""
    sem = threading.Semaphore(0)
    real = MessageFetcher.fetch_room

    def gated(self, room):
        assert sem.acquire(timeout=10), "test never released the fetch"
        return real(self, room)
    monkeypatch.setattr(MessageFetcher, "fetch_room", gated)
    return sem


def _until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def _done_rooms(settings) -> int:
    s = CheckpointStore(store_path(settings), settings.app_code)
    try:
        return s.progress().done
    finally:
        s.close()


# --------------------------------------------------------------------------- standalone

@respx.mock
def test_each_task_runs_standalone(settings, context):
    _tenant()
    conv = Task(TaskKind.CONVERSATIONS)
    conv.execute(context(conv.kind))
    assert conv.state is TaskState.COMPLETED and conv.outcome == "ok" and conv.committed == 5

    bc = Task(TaskKind.BROADCASTS)
    bc.execute(context(bc.kind))
    assert bc.state is TaskState.COMPLETED and bc.counters["recipients"] == 3

    wb = Task(TaskKind.WORKBOOK)
    wb.execute(context(wb.kind))
    assert wb.state is TaskState.COMPLETED and wb.report.complete and wb.report.path.exists()


@respx.mock
def test_auth_failure_is_reported_in_plain_words(settings, context):
    _tenant(broken_auth=True)
    t = Task(TaskKind.CONVERSATIONS)
    t.execute(context(t.kind))
    assert t.state is TaskState.FAILED and t.outcome == "auth"
    assert "login expired" in t.snapshot()["outcome_text"]


def test_throughput_rate_and_eta():
    now = [0.0]
    tp = Throughput(window_s=60, clock=lambda: now[0])
    for i in range(11):
        tp.record(i * 2)          # 2 items/s
        now[0] += 1
    assert tp.rate() == pytest.approx(2.0)
    assert tp.eta_s(20, 120) == pytest.approx(50.0)
    assert tp.eta_s(120, 120) is None


# --------------------------------------------------------------------------- lifecycle

@respx.mock
def test_task_manager_lifecycle(settings, context, gate):
    _tenant()
    mgr = TaskManager(context, auto_workbook=False)
    t = mgr.start(TaskKind.CONVERSATIONS)
    gate.release()
    _until(lambda: t.committed == 1)
    mgr.pause(t.id)
    gate.release()                                  # the item in flight still finishes
    _until(lambda: t.state is TaskState.PAUSED)
    assert t.committed == 2
    for _ in range(3):
        gate.release()
    time.sleep(0.3)
    assert t.committed == 2 and _done_rooms(settings) == 2   # no requests while paused

    mgr.resume(t.id)
    assert mgr.wait_idle()
    assert t.state is TaskState.COMPLETED and _done_rooms(settings) == 5
    assert t.snapshot()["rate"] is None or t.snapshot()["rate"] > 0
    assert mgr.history.entries[-1]["outcome"] == "ok"


@respx.mock
def test_cancel_keeps_committed_work_and_builds_a_partial_workbook(settings, context, gate):
    _tenant()
    mgr = TaskManager(context)
    t = mgr.start(TaskKind.CONVERSATIONS)
    gate.release()
    gate.release()
    _until(lambda: t.committed == 2)
    mgr.cancel(t.id)
    gate.release()                                  # let the in-flight fetch return
    _until(lambda: t.state is TaskState.CANCELLED)
    assert _done_rooms(settings) in (2, 3)

    _until(lambda: any(x.kind is TaskKind.WORKBOOK for x in mgr.tasks()))
    assert mgr.wait_idle()
    [wb] = [x for x in mgr.tasks() if x.kind is TaskKind.WORKBOOK]
    assert wb.auto and wb.state is TaskState.COMPLETED
    assert wb.report.path.stem.endswith("_PARTIAL") and "conversations not downloaded" in wb.report.completeness


@respx.mock
def test_conversations_broadcasts_and_workbook_run_side_by_side(settings, context, gate):
    _tenant()
    mgr = TaskManager(context, auto_workbook=False)
    conv = mgr.start(TaskKind.CONVERSATIONS)
    bc = mgr.start(TaskKind.BROADCASTS)
    _until(lambda: bc.state is TaskState.COMPLETED)
    wb = mgr.start(TaskKind.WORKBOOK)               # while conversations are still downloading
    _until(lambda: wb.state is TaskState.COMPLETED)
    assert conv.state is TaskState.RUNNING
    for _ in range(5):
        gate.release()
    assert mgr.wait_idle()
    assert conv.state is TaskState.COMPLETED and wb.report.broadcast_recipients == 3


# --------------------------------------------------------------------------- conflicts

@respx.mock
def test_task_manager_conflict_prevention(settings, context, gate):
    _tenant()
    mgr = TaskManager(context, auto_workbook=False)
    t = mgr.start(TaskKind.CONVERSATIONS)
    with pytest.raises(TaskConflict, match="already downloading"):
        mgr.start(TaskKind.CONVERSATIONS)
    assert mgr.conflict(TaskKind.BROADCASTS) is None and mgr.conflict(TaskKind.WORKBOOK) is None

    mgr.blocked_reason = "Another qexport window is downloading."
    assert mgr.conflict(TaskKind.BROADCASTS) == "Another qexport window is downloading."
    assert mgr.conflict(TaskKind.WORKBOOK) is None  # reading the store is always fine
    mgr.blocked_reason = None

    mgr.cancel(t.id)
    gate.release()
    assert mgr.wait_idle()
    assert mgr.conflict(TaskKind.CONVERSATIONS) is None


@respx.mock
def test_workbook_requests_coalesce_while_one_is_building(settings, context, monkeypatch):
    _tenant()
    Task(TaskKind.CONVERSATIONS).execute(context(TaskKind.CONVERSATIONS))
    release = threading.Event()
    import qexport.tasks as tasks_mod
    real = tasks_mod.build_workbook

    def slow(*a, **k):
        release.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(tasks_mod, "build_workbook", slow)
    mgr = TaskManager(context)
    first = mgr.request_workbook("manual")
    assert mgr.request_workbook("again") is None and mgr.request_workbook("and again") is None
    with pytest.raises(TaskConflict):
        mgr.start(TaskKind.WORKBOOK)
    release.set()
    _until(lambda: len([x for x in mgr.tasks() if x.kind is TaskKind.WORKBOOK]) == 2)
    assert mgr.wait_idle()
    builds = [x for x in mgr.tasks() if x.kind is TaskKind.WORKBOOK]
    assert builds[0] is first and all(b.state is TaskState.COMPLETED for b in builds)
