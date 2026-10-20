from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from qexport.api.client import (
    AuthError, NetworkExhausted, QiscusClient, RateLimitExhausted, RoomAccessError, SchemaDriftError,
)
from qexport.config import Settings
from qexport.extract.broadcasts import BroadcastEnumerator, BroadcastLogFetcher
from qexport.extract.messages import CursorLoopError, MessageFetcher
from qexport.extract.rooms import RoomEnumerator
from qexport.load.excel import ExcelWriter, ExportReport
from qexport.store.checkpoint import CheckpointStore, iso
from qexport.transform.actions import ActionRecord, derive_actions
from qexport.transform.normalize import normalize_phone

EXIT_OK, EXIT_AUTH, EXIT_PARTIAL, EXIT_RATE_LIMIT, EXIT_SCHEMA, EXIT_INTERRUPTED = 0, 2, 3, 4, 5, 130

# Per-item errors: mark that room/job failed and keep going.
_ITEM_ERRORS = (RoomAccessError, CursorLoopError, SchemaDriftError, NetworkExhausted)
_CIRCUIT_BREAKER = 15   # this many consecutive item failures of one kind -> something global is wrong


class ProgressSink(Protocol):
    def phase(self, name: str, total: int | None) -> None: ...
    def advance(self, n: int = 1, **info: object) -> None: ...
    def warn(self, msg: str) -> None: ...


class NullProgress:
    def phase(self, name: str, total: int | None) -> None: ...
    def advance(self, n: int = 1, **info: object) -> None: ...
    def warn(self, msg: str) -> None: ...


class RunAborted(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    report: ExportReport | None
    message: str = ""


def rebuild_actions(store: CheckpointStore, window_days: int) -> int:
    """Derive the actions table from stored messages + broadcast logs. Pure re-computation."""
    window = timedelta(days=window_days)
    cache: dict[str, object] = {}

    def campaign_for_wamid(wamid: str):
        if wamid not in cache:
            cache[wamid] = store.campaign_for_wamid(wamid)
        return cache[wamid]

    def gen() -> Iterator[ActionRecord]:
        for room, msgs in store.iter_room_messages():
            touches = store.touches_for_phone(normalize_phone(room.customer_identifier))
            yield from derive_actions(room, msgs, campaign_for_wamid=campaign_for_wamid,
                                      touches=touches, window=window)

    return store.replace_actions(list(gen()))


class ExportOrchestrator:
    def __init__(self, settings: Settings, client: QiscusClient, store: CheckpointStore,
                 enumerator: RoomEnumerator, fetcher: MessageFetcher, writer: ExcelWriter,
                 progress: ProgressSink | None = None,
                 broadcast_enumerator: BroadcastEnumerator | None = None,
                 log_fetcher: BroadcastLogFetcher | None = None) -> None:
        self._s, self._c, self._store = settings, client, store
        self._enum, self._fetch, self._writer = enumerator, fetcher, writer
        self._bc_enum, self._log_fetch = broadcast_enumerator, log_fetcher
        self._p = progress or NullProgress()

    def run(self, *, since_last_run: bool = False, write_xlsx: bool = True,
            limit_rooms: int | None = None, limit_jobs: int | None = None,
            include_messages: bool = True, include_broadcasts: bool = True,
            include_recipients_sheet: bool = True, out_path: Path | None = None) -> RunResult:
        """1) enumerate rooms + broadcast jobs  2) fetch every room's messages
        3) fetch every job's recipient log  4) derive actions  5) write xlsx."""
        try:
            if include_messages:
                self._enumerate_rooms(since_last_run)
            if include_broadcasts and self._bc_enum is not None:
                self._enumerate_broadcasts(since_last_run)
            if include_messages:
                self._fetch_rooms(limit_rooms)
            if include_broadcasts and self._log_fetch is not None:
                self._fetch_broadcast_logs(limit_jobs)
        except AuthError as e:
            return RunResult(EXIT_AUTH, None,
                             f"{e}. The dashboard token probably expired: refresh .env and run `qexport resume`.")
        except RateLimitExhausted as e:
            return RunResult(EXIT_RATE_LIMIT, None,
                             f"{e}. Progress is saved; wait a few minutes, then `qexport resume` "
                             "with lower --rooms-rps / --messages-rps.")
        except RunAborted as e:
            return RunResult(e.exit_code, None, str(e))
        except KeyboardInterrupt:
            return RunResult(EXIT_INTERRUPTED, None, "Interrupted. Progress is saved; run `qexport resume`.")

        if limit_rooms is None and limit_jobs is None:
            self._store.mark_run_completed()

        report = None
        if write_xlsx:
            report = self.write(out_path, include_recipients=include_recipients_sheet)
        skipped = self._store.skipped()
        if skipped:
            return RunResult(EXIT_PARTIAL, report, f"{len(skipped)} item(s) could not be exported; "
                                                   "see the Export Info sheet or `qexport status`.")
        return RunResult(EXIT_OK, report)

    def write(self, out_path: Path | None = None, include_recipients: bool = True) -> ExportReport:
        self._p.phase("Deriving customer actions", None)
        n = rebuild_actions(self._store, self._s.attribution_window_days)
        self._p.advance(0, actions=n)
        path = out_path or default_out_path(self._s)
        self._p.phase("Writing workbook", None)
        return self._writer.write(self._store, path, on_row=lambda k: self._p.advance(1000),
                                  include_recipients=include_recipients)

    # ------------------------------------------------------------------ phases

    def _enumerate_rooms(self, since_last_run: bool) -> None:
        if not self._store.enumeration_done():
            self._p.phase("Listing conversations", None)
            self._enum.run(on_page=lambda page, total: self._p.advance(1, rooms=total))
            self._p.phase("Catching rooms that moved during the walk", None)
            self._enum.top_up()
        elif since_last_run:
            self._p.phase("Finding conversations with new activity", None)
            changed = self._enum.top_up(on_page=lambda page, ch: self._p.advance(1, changed=ch))
            self._p.advance(0, changed=changed)

    def _enumerate_broadcasts(self, since_last_run: bool) -> None:
        assert self._bc_enum is not None
        if not self._store.broadcast_enumeration_done():
            self._p.phase("Listing broadcast campaigns", None)
            self._bc_enum.run(on_page=lambda page, total: self._p.advance(1, jobs=total))
        elif since_last_run:
            self._p.phase("Finding broadcast campaigns with new status", None)
            self._bc_enum.top_up()

    def _fetch_rooms(self, limit: int | None) -> None:
        rooms = list(self._store.iter_rooms_to_fetch(self._s.max_room_attempts, limit))
        self._p.phase("Downloading conversations", len(rooms))
        streak: tuple[str, int] = ("", 0)
        for room in rooms:
            try:
                msgs = self._fetch.fetch_room(room)
            except _ITEM_ERRORS as e:
                self._store.mark_room_failed(room.room_id, f"{type(e).__name__}: {e}", self._s.max_room_attempts)
                self._p.warn(f"room {room.room_id}: {type(e).__name__}")
                streak = _bump_streak(streak, type(e).__name__)
                self._p.advance(1)
                continue
            self._store.commit_room(room.room_id, msgs, iso(room.last_activity_utc))
            streak = ("", 0)
            self._p.advance(1, messages=len(msgs))

    def _fetch_broadcast_logs(self, limit: int | None) -> None:
        assert self._log_fetch is not None
        jobs = list(self._store.iter_jobs_to_fetch(self._s.max_room_attempts, self._s.broadcasts_since, limit))
        self._p.phase("Downloading broadcast recipient logs", len(jobs))
        streak: tuple[str, int] = ("", 0)
        for job_id, name, _total in jobs:
            try:
                logs = self._log_fetch.fetch_job(job_id)
            except _ITEM_ERRORS as e:
                self._store.mark_job_failed(job_id, f"{type(e).__name__}: {e}", self._s.max_room_attempts)
                self._p.warn(f"broadcast {name!r}: {type(e).__name__}")
                streak = _bump_streak(streak, type(e).__name__)
                self._p.advance(1)
                continue
            self._store.commit_job_logs(job_id, logs)
            streak = ("", 0)
            self._p.advance(1, recipients=len(logs))


def _bump_streak(streak: tuple[str, int], kind: str) -> tuple[str, int]:
    n = streak[1] + 1 if streak[0] == kind else 1
    if n >= _CIRCUIT_BREAKER:
        code = EXIT_SCHEMA if kind == "SchemaDriftError" else EXIT_PARTIAL
        raise RunAborted(code,f"{n} consecutive items failed with {kind}; stopping. "
                               "Progress is saved. Check the error in `qexport status`.")
    return kind, n


def default_out_path(settings: Settings) -> Path:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return settings.out_dir / f"qiscus_export_{settings.app_code}_{stamp}.xlsx"
