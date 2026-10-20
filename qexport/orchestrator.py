from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from qexport.api.client import (
    ApiRequestError, AuthError, ItemGoneError, ItemRejectedError, NetworkExhausted, QiscusClient,
    RateLimitExhausted, RoomAccessError, SchemaDriftError,
)
from qexport.config import Settings
from qexport.extract.broadcasts import BroadcastEnumerator, BroadcastLogFetcher
from qexport.extract.messages import CursorLoopError, MessageFetcher
from qexport.extract.rooms import RoomEnumerator
from qexport.load.excel import ExcelWriter, ExportReport, expected_rows
from qexport.store.checkpoint import CheckpointStore, iso
from qexport.transform.actions import ActionRecord, derive_actions
from qexport.transform.normalize import normalize_phone

EXIT_OK, EXIT_AUTH, EXIT_PARTIAL, EXIT_RATE_LIMIT, EXIT_SCHEMA, EXIT_NETWORK, EXIT_INTERRUPTED = (
    0, 2, 3, 4, 5, 6, 130)

# Per-item errors: mark that room/job failed and keep going.
_ITEM_ERRORS = (RoomAccessError, ItemRejectedError, CursorLoopError, SchemaDriftError, NetworkExhausted)
_CIRCUIT_BREAKER = 15   # this many consecutive item failures of one kind -> something global is wrong


class ProgressSink(Protocol):
    """advance() runs after each committed item or page, so a sink may block there (pause) or
    raise RunCancelled (cancel) without losing work."""
    def phase(self, name: str, total: int | None) -> None: ...
    def advance(self, n: int = 1, **info: object) -> None: ...
    def item(self, label: str) -> None: ...
    def warn(self, msg: str) -> None: ...


class NullProgress:
    def phase(self, name: str, total: int | None) -> None: ...
    def advance(self, n: int = 1, **info: object) -> None: ...
    def item(self, label: str) -> None: ...
    def warn(self, msg: str) -> None: ...


class RunAborted(Exception):
    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class RunCancelled(Exception):
    """Raised by a ProgressSink when the user cancels. Committed rooms/jobs stay committed."""


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    report: ExportReport | None        # the last workbook written, if any
    message: str = ""
    reports: tuple[ExportReport, ...] = ()   # every workbook this run wrote, oldest first


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


def build_workbook(store: CheckpointStore, settings: Settings, writer: ExcelWriter,
                   progress: ProgressSink | None = None, out_path: Path | None = None,
                   include_recipients: bool = True) -> ExportReport:
    """Snapshot the store, derive actions in the snapshot, write the workbook from it.

    The live store is only read (for the snapshot), so this can run while downloads commit.
    Without out_path the file is named after the data's completeness (…_PARTIAL.xlsx)."""
    p = progress or NullProgress()
    p.phase("Taking a snapshot of the downloaded data", None)
    with store.snapshot() as snap:
        p.phase("Deriving customer actions", None)
        n = rebuild_actions(snap, settings.attribution_window_days)
        p.advance(0, actions=n)
        missing = snap.missing(settings.broadcasts_since)
        path = out_path or default_out_path(settings, partial=not missing.complete)
        p.phase("Writing workbook", expected_rows(snap, include_recipients))
        return writer.write(snap, path, on_row=lambda k: p.advance(1000),
                            include_recipients=include_recipients, missing=missing)


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
        self._broadcast_note = ""

    def run(self, *, since_last_run: bool = False, write_xlsx: bool = True,
            limit_rooms: int | None = None, limit_jobs: int | None = None,
            include_messages: bool = True, include_broadcasts: bool = True,
            include_recipients_sheet: bool = True, out_path: Path | None = None) -> RunResult:
        """1) enumerate rooms + broadcast jobs  2) fetch every room's messages
        3) fetch every job's recipient log  4) derive actions  5) write xlsx.

        With write_xlsx, a workbook is also written as soon as conversations are done (when
        broadcast logs are still to come) and when the run stops early, so finished downloads
        are never stuck in the store waiting for the slow broadcast phase."""
        reports: list[ExportReport] = []
        stop: RunResult | None = None
        try:
            if include_messages:
                self._enumerate_rooms(since_last_run)
            if include_broadcasts and self._bc_enum is not None:
                self._enumerate_broadcasts(since_last_run)
            if include_messages:
                self._fetch_rooms(limit_rooms)
            if include_broadcasts and self._log_fetch is not None:
                if write_xlsx and include_messages and self._broadcast_work_left(limit_jobs):
                    self._write_quietly(reports, out_path, include_recipients_sheet,
                                        "Conversations finished; writing a workbook before broadcasts")
                self._fetch_broadcast_logs(limit_jobs)
        except AuthError as e:
            stop = RunResult(EXIT_AUTH, None,
                             f"{e}. The dashboard token probably expired: refresh .env and run `qexport resume`.")
        except RateLimitExhausted as e:
            stop = RunResult(EXIT_RATE_LIMIT, None,
                             f"{e}. Progress is saved; wait a few minutes, then `qexport resume` "
                             "with lower --rooms-rps / --messages-rps.")
        except NetworkExhausted as e:
            stop = RunResult(EXIT_NETWORK, None,
                             f"{e}. Could not reach Qiscus. Progress is saved; check the internet "
                             "connection, then `qexport resume`.")
        except (SchemaDriftError, ApiRequestError) as e:
            stop = RunResult(EXIT_SCHEMA, None, f"{e}. Progress is saved; `qexport resume` once this is fixed.")
        except RunAborted as e:
            stop = RunResult(e.exit_code, None, str(e))
        except RunCancelled:
            stop = RunResult(EXIT_INTERRUPTED, None, "Cancelled. Everything downloaded so far is saved.")
        except KeyboardInterrupt:
            stop = RunResult(EXIT_INTERRUPTED, None, "Interrupted. Progress is saved; run `qexport resume`.")

        if stop is not None:
            if write_xlsx and self._has_data():
                self._write_quietly(reports, out_path, include_recipients_sheet,
                                    "Run stopped early; writing a partial workbook of what was downloaded "
                                    "(Ctrl+C again to skip)")
            return RunResult(stop.exit_code, reports[-1] if reports else None, stop.message, tuple(reports))

        if limit_rooms is None and limit_jobs is None:
            self._store.mark_run_completed()

        if write_xlsx:
            reports.append(self.write(out_path, include_recipients=include_recipients_sheet))
        report = reports[-1] if reports else None
        # Judge this run by what it fetched: a conversations-only run isn't partial because of
        # broadcast campaigns, and vice versa.
        kinds = {k for k, on in (("room", include_messages),
                                 ("broadcast job", include_broadcasts and self._log_fetch is not None)) if on}
        skipped = [s for s in self._store.skipped() if s[0] in kinds]
        if skipped or self._broadcast_note:
            return RunResult(EXIT_PARTIAL, report, self._broadcast_note + (
                f"{len(skipped)} item(s) could not be exported; "
                "see the Export Info sheet or `qexport status`." if skipped else ""), tuple(reports))
        return RunResult(EXIT_OK, report, reports=tuple(reports))

    def write(self, out_path: Path | None = None, include_recipients: bool = True) -> ExportReport:
        return build_workbook(self._store, self._s, self._writer, self._p, out_path, include_recipients)

    def _write_quietly(self, reports: list[ExportReport], out_path: Path | None,
                       include_recipients: bool, why: str) -> None:
        """An extra workbook must never cost the run: failures (file open in Excel, Ctrl+C)
        are reported and skipped."""
        self._p.warn(why)
        try:
            reports.append(self.write(out_path, include_recipients=include_recipients))
        except (KeyboardInterrupt, RunCancelled):
            self._p.warn("Workbook skipped.")
        except Exception as e:   # noqa: BLE001 - the downloaded data is safe either way
            self._p.warn(f"Could not write the workbook ({e}). The data is safe; rebuild it with "
                         "`qexport write-xlsx`.")

    def _broadcast_work_left(self, limit: int | None) -> bool:
        return any(True for _ in self._store.iter_jobs_to_fetch(
            self._s.max_room_attempts, self._s.broadcasts_since, 1 if limit is None else min(limit, 1)))

    def _has_data(self) -> bool:
        p = self._store.progress()
        return p.done > 0 or p.jobs_done > 0

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
            self._p.item(room.customer_name or room.room_id)
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
        gone = 0
        for job_id, name, _total in jobs:
            self._p.item(name)
            try:
                logs = self._log_fetch.fetch_job(job_id)
            except ItemGoneError as e:
                # Expired at Qiscus: nothing to retry, and a run of these is normal, not an outage.
                self._store.mark_job_gone(job_id, e.detail or str(e))
                if gone == 0:
                    self._p.warn(f"broadcast {name!r} and older: Qiscus no longer keeps the recipient "
                                 "list (only the campaign totals). Marking such campaigns as not kept.")
                gone += 1
                self._p.advance(1, recipients=0)
                continue
            except _ITEM_ERRORS as e:
                self._store.mark_job_failed(job_id, f"{type(e).__name__}: {e}", self._s.max_room_attempts)
                self._p.warn(f"broadcast {name!r}: {type(e).__name__}")
                self._p.advance(1)
                try:
                    streak = _bump_streak(streak, type(e).__name__)
                except RunAborted as stop:
                    # Recipient logs are an extra; never let them cost the conversations workbook.
                    self._broadcast_note = (f"Broadcast recipient download stopped: {stop} "
                                            f"Last error: {e}. ")
                    self._p.warn(self._broadcast_note)
                    return
                continue
            self._store.commit_job_logs(job_id, logs)
            streak = ("", 0)
            self._p.advance(1, recipients=len(logs))
        if gone:
            self._p.warn(f"{gone:,} campaign(s): Qiscus no longer keeps their recipient lists.")


def _bump_streak(streak: tuple[str, int], kind: str) -> tuple[str, int]:
    n = streak[1] + 1 if streak[0] == kind else 1
    if n >= _CIRCUIT_BREAKER:
        code = EXIT_SCHEMA if kind == "SchemaDriftError" else EXIT_PARTIAL
        raise RunAborted(code, f"{n} consecutive items failed with {kind}; stopping. "
                               "Progress is saved. Check the error in `qexport status`.")
    return kind, n


def default_out_path(settings: Settings, partial: bool = False) -> Path:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return settings.out_dir / f"qiscus_export_{settings.app_code}_{stamp}{'_PARTIAL' if partial else ''}.xlsx"
