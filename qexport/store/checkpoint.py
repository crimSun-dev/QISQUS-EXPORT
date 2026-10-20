"""CheckpointStore: the only component that issues SQL. Every multi-row change is one transaction."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from importlib import resources
from pathlib import Path

from qexport.api import endpoints as ep
from qexport.transform.actions import ActionRecord, ActionType, Attribution, CampaignRef, Touch
from qexport.transform.normalize import (
    BroadcastJobRecord, BroadcastLogRecord, MessageRecord, RoomRecord, SenderType, normalize_phone,
)

SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 30_000   # a writer waits this long for another task's transaction before failing
_NO_LOGS_YET = tuple(ep.JOB_STATUS[s] for s in sorted(ep.JOB_NO_LOGS_YET_STATUSES))


class StoreAppMismatchError(Exception):
    """The SQLite file was created for another app_code."""


class StoreLockedError(Exception):
    """Another qexport process is using this store."""


@dataclass(frozen=True)
class Progress:
    rooms_total: int
    done: int
    pending: int
    failed: int
    skipped: int
    messages: int
    enumeration_done: bool
    jobs_total: int = 0
    jobs_done: int = 0
    jobs_pending: int = 0
    jobs_failed: int = 0
    jobs_skipped: int = 0
    broadcast_logs: int = 0
    broadcast_enumeration_done: bool = False


@dataclass(frozen=True)
class Missing:
    """What a workbook built from this store would lack. Campaigns that have not started sending
    (queued/scheduled/preparing) and campaigns outside --broadcasts-since are not counted: they
    have no recipient log to download yet, or were left out on purpose."""
    rooms_listing_incomplete: bool = False
    rooms_pending: int = 0
    rooms_failed: int = 0           # failed + skipped
    jobs_listing_incomplete: bool = False
    jobs_pending: int = 0
    jobs_failed: int = 0            # failed + skipped

    @property
    def complete(self) -> bool:
        return not (self.rooms_listing_incomplete or self.rooms_pending or self.rooms_failed
                    or self.jobs_listing_incomplete or self.jobs_pending or self.jobs_failed)

    def describe(self) -> str:
        """'Complete', or 'Partial (1,359 broadcast campaigns not downloaded, ...)'."""
        if self.complete:
            return "Complete"
        parts = []
        if self.rooms_listing_incomplete:
            parts.append("conversation list not finished")
        if self.rooms_pending:
            parts.append(f"{self.rooms_pending:,} conversations not downloaded")
        if self.rooms_failed:
            parts.append(f"{self.rooms_failed:,} conversations failed")
        if self.jobs_listing_incomplete:
            parts.append("broadcast campaign list not finished")
        if self.jobs_pending:
            parts.append(f"{self.jobs_pending:,} broadcast campaigns not downloaded")
        if self.jobs_failed:
            parts.append(f"{self.jobs_failed:,} broadcast campaigns failed")
        return f"Partial ({', '.join(parts)})"


@dataclass(frozen=True)
class RoomSummary:
    room: RoomRecord
    message_count: int
    customer_msgs: int
    agent_msgs: int
    bot_msgs: int
    broadcast_msgs: int
    first_msg_utc: datetime | None
    last_msg_utc: datetime | None
    fetch_state: str


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def from_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _job_signature(j: BroadcastJobRecord) -> str:
    return f"{j.status}|{j.total_recipient}|{j.total_sent}|{j.total_delivered}|{j.total_read}|{j.total_failed}"


class RunLock:
    """Exclusive OS lock on a file next to the store; a second instance exits with a clear message.

    The OS drops the lock when the process dies (window closed, killed, power loss), so a lockfile
    left behind is harmless: the next run just locks it again."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> RunLock:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        try:
            _lock_fd(fd)
        except OSError as e:
            os.close(fd)
            raise StoreLockedError(
                f"{self.path} is locked: another qexport run is active. Close the other qexport "
                "window first.") from e
        os.ftruncate(fd, 0)
        os.lseek(fd, 1, os.SEEK_SET)     # byte 0 is the lock; keep the PID clear of it
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            os.close(self._fd)           # releases the lock
            self._fd = None
            try:
                self.path.unlink()
            except OSError:              # already gone, or another run grabbed it in between
                pass


def _lock_fd(fd: int) -> None:
    """Non-blocking exclusive lock on byte 0; raises OSError if another process holds it."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


class CheckpointStore:
    """One connection per instance. Several instances (one per task thread) may share a file:
    WAL lets readers run during a write, and writers queue behind each other for BUSY_TIMEOUT_MS.
    An instance may be handed between threads, but never used by two threads at once."""

    def __init__(self, path: Path, app_code: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path, isolation_level=None,   # explicit BEGIN/COMMIT only
                                   timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        # Switching a new file to WAL fails at once (no busy wait) if another connection is doing
        # the same, so tasks opening the store together take turns here.
        with _OPEN_LOCK:
            _retry_busy(lambda: self._db.execute("PRAGMA journal_mode=WAL"))
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA synchronous=NORMAL")
            schema = resources.files("qexport.store").joinpath("schema.sql").read_text(encoding="utf-8")
            self._db.executescript(schema)
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            row = self._db.execute("SELECT app_code FROM run WHERE id=1").fetchone()
            if row is None:
                self._db.execute("INSERT INTO run (id, app_code, started_at_utc) VALUES (1, ?, ?)",
                                 (app_code, iso(datetime.now(timezone.utc))))
        if row is not None and row["app_code"] != app_code:
            self._db.close()
            raise StoreAppMismatchError(
                f"{path} belongs to app {row['app_code']!r}, not {app_code!r}")
        self.app_code = app_code

    # ------------------------------------------------------------------ helpers

    def _tx(self):
        return _Tx(self._db)

    def _run(self) -> sqlite3.Row:
        return self._db.execute("SELECT * FROM run WHERE id=1").fetchone()

    def run_info(self) -> dict:
        return dict(self._run())

    def mark_run_completed(self) -> None:
        with self._tx():
            self._db.execute("UPDATE run SET last_completed_at_utc=? WHERE id=1",
                             (iso(datetime.now(timezone.utc)),))

    # ------------------------------------------------------------------ rooms

    def get_room_cursor(self) -> str | None:
        return self._run()["room_cursor"]

    def enumeration_done(self) -> bool:
        return bool(self._run()["enumeration_done"])

    def upsert_rooms_page(self, rooms: list[RoomRecord], next_cursor: str | None, *,
                          advance_cursor: bool = True) -> int:
        """Atomic: upsert rooms + advance cursor. A done room whose last_activity_utc changed
        vs fetched_last_activity_utc is reset to 'pending'. Returns how many rooms were new or changed."""
        changed = 0
        with self._tx():
            for r in rooms:
                last = iso(r.last_activity_utc)
                prev = self._db.execute(
                    "SELECT last_activity_utc, fetched_last_activity_utc, fetch_state FROM rooms "
                    "WHERE room_id=?", (r.room_id,)).fetchone()
                if prev is None or prev["last_activity_utc"] != last:
                    changed += 1
                self._db.execute(
                    """INSERT INTO rooms (room_id, customer_room_id, channel_id, source, customer_name,
                         customer_identifier, customer_phone, room_type, is_resolved, is_handled_by_bot,
                         last_activity_utc)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(room_id) DO UPDATE SET
                         customer_room_id=excluded.customer_room_id, channel_id=excluded.channel_id,
                         source=excluded.source, customer_name=excluded.customer_name,
                         customer_identifier=excluded.customer_identifier,
                         customer_phone=excluded.customer_phone, room_type=excluded.room_type,
                         is_resolved=excluded.is_resolved, is_handled_by_bot=excluded.is_handled_by_bot,
                         last_activity_utc=excluded.last_activity_utc""",
                    (r.room_id, r.customer_room_id, r.channel_id, r.source, r.customer_name,
                     r.customer_identifier, normalize_phone(r.customer_identifier), r.room_type,
                     int(r.is_resolved), int(r.is_handled_by_bot), last))
                if prev is not None and prev["fetch_state"] == "done" \
                        and prev["fetched_last_activity_utc"] != last:
                    self._db.execute(
                        "UPDATE rooms SET fetch_state='pending', attempts=0 WHERE room_id=?", (r.room_id,))
            if advance_cursor:
                self._db.execute("UPDATE run SET room_cursor=? WHERE id=1", (next_cursor,))
        return changed

    def mark_enumeration_done(self) -> None:
        with self._tx():
            self._db.execute("UPDATE run SET enumeration_done=1, room_cursor=NULL WHERE id=1")

    def iter_rooms_to_fetch(self, max_attempts: int, limit: int | None = None) -> Iterator[RoomRecord]:
        """pending + failed(attempts < max), newest activity first. Materialized, so commits are safe."""
        sql = ("SELECT * FROM rooms WHERE fetch_state='pending' "
               "OR (fetch_state='failed' AND attempts < ?) ORDER BY last_activity_utc DESC")
        params: tuple = (max_attempts,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        rows = self._db.execute(sql, params).fetchall()
        for row in rows:
            yield _room(row)

    def get_room(self, room_id: str) -> RoomRecord | None:
        row = self._db.execute("SELECT * FROM rooms WHERE room_id=?", (room_id,)).fetchone()
        return _room(row) if row else None

    def commit_room(self, room_id: str, messages: list[MessageRecord],
                    fetched_last_activity_utc: str | None) -> None:
        """Atomic: DELETE old msgs for room, INSERT new, set fetch_state='done'."""
        with self._tx():
            self._db.execute("DELETE FROM messages WHERE room_id=?", (room_id,))
            self._db.executemany(
                """INSERT OR REPLACE INTO messages (msg_id, room_id, timestamp_utc, sender_type, sender_id,
                     sender_name, message_type, text, attachment_url, attachment_name, reply_to_text,
                     is_deleted, delivery_status, reply_to_msg_id, wamid, context_wamid, button_text,
                     broadcast_template)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(m.msg_id, room_id, iso(m.timestamp_utc), str(m.sender_type), m.sender_id, m.sender_name,
                  m.message_type, m.text, m.attachment_url, m.attachment_name, m.reply_to_text,
                  int(m.is_deleted), m.delivery_status, m.reply_to_msg_id, m.wamid, m.context_wamid,
                  m.button_text, m.broadcast_template) for m in messages])
            self._db.execute(
                "UPDATE rooms SET fetch_state='done', fetched_last_activity_utc=?, last_error=NULL "
                "WHERE room_id=?", (fetched_last_activity_utc, room_id))

    def mark_room_failed(self, room_id: str, error: str, max_attempts: int) -> None:
        """attempts += 1; state = 'skipped' if attempts >= max_attempts else 'failed'."""
        with self._tx():
            self._db.execute(
                """UPDATE rooms SET attempts=attempts+1, last_error=?,
                     fetch_state=CASE WHEN attempts+1 >= ? THEN 'skipped' ELSE 'failed' END
                   WHERE room_id=?""", (error[:500], max_attempts, room_id))

    def reset_failed(self) -> int:
        with self._tx():
            n = self._db.execute(
                "UPDATE rooms SET fetch_state='pending', attempts=0 "
                "WHERE fetch_state IN ('failed','skipped')").rowcount
            n += self._db.execute(
                "UPDATE broadcast_jobs SET logs_state='pending', attempts=0 "
                "WHERE logs_state IN ('failed','skipped')").rowcount
        return n

    # ------------------------------------------------------------------ broadcasts

    def get_broadcast_cursor(self) -> str | None:
        return self._run()["broadcast_cursor"]

    def broadcast_enumeration_done(self) -> bool:
        return bool(self._run()["broadcast_enumeration_done"])

    def upsert_jobs_page(self, jobs: list[BroadcastJobRecord], next_cursor: str | None, *,
                         advance_cursor: bool = True) -> int:
        """Atomic: upsert jobs + advance cursor. A job whose status/totals changed since its logs
        were fetched goes back to 'pending'. Returns how many jobs were new or changed."""
        changed = 0
        with self._tx():
            for j in jobs:
                sig = _job_signature(j)
                prev = self._db.execute(
                    "SELECT signature, fetched_signature, logs_state FROM broadcast_jobs WHERE job_id=?",
                    (j.job_id,)).fetchone()
                if prev is None or prev["signature"] != sig:
                    changed += 1
                self._db.execute(
                    """INSERT INTO broadcast_jobs (job_id, name, template_name, category, status, channel_id,
                         channel_name, created_at_utc, started_at_utc, finished_at_utc, total_recipient,
                         total_sent, total_delivered, total_read, total_failed, signature)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(job_id) DO UPDATE SET
                         name=excluded.name, template_name=excluded.template_name,
                         category=excluded.category, status=excluded.status,
                         channel_id=excluded.channel_id, channel_name=excluded.channel_name,
                         created_at_utc=excluded.created_at_utc, started_at_utc=excluded.started_at_utc,
                         finished_at_utc=excluded.finished_at_utc,
                         total_recipient=excluded.total_recipient, total_sent=excluded.total_sent,
                         total_delivered=excluded.total_delivered, total_read=excluded.total_read,
                         total_failed=excluded.total_failed, signature=excluded.signature""",
                    (j.job_id, j.name, j.template_name, j.category, j.status, j.channel_id, j.channel_name,
                     iso(j.created_at_utc), iso(j.started_at_utc), iso(j.finished_at_utc),
                     j.total_recipient, j.total_sent, j.total_delivered, j.total_read, j.total_failed, sig))
                if prev is not None and prev["logs_state"] in ("done", "failed", "skipped") and (
                        prev["fetched_signature"] if prev["logs_state"] == "done" else prev["signature"]) != sig:
                    self._db.execute(
                        "UPDATE broadcast_jobs SET logs_state='pending', attempts=0 WHERE job_id=?",
                        (j.job_id,))
            if advance_cursor:
                self._db.execute("UPDATE run SET broadcast_cursor=? WHERE id=1", (next_cursor,))
        return changed

    def mark_broadcast_enumeration_done(self) -> None:
        with self._tx():
            self._db.execute(
                "UPDATE run SET broadcast_enumeration_done=1, broadcast_cursor=NULL WHERE id=1")

    def iter_jobs_to_fetch(self, max_attempts: int, since: date | None = None,
                           limit: int | None = None) -> Iterator[tuple[int, str, int]]:
        """(job_id, name, total_recipient) for pending + failed(attempts < max) jobs, newest first.
        Jobs that have not started sending (queued/scheduled/preparing) wait in 'pending' until a
        later run sees their status change."""
        sql = ("SELECT job_id, name, total_recipient FROM broadcast_jobs WHERE "
               "(logs_state='pending' OR (logs_state='failed' AND attempts < ?)) "
               f"AND status NOT IN ({','.join('?' * len(_NO_LOGS_YET))})")
        params: tuple = (max_attempts, *_NO_LOGS_YET)
        if since is not None:
            sql += " AND created_at_utc >= ?"
            params += (since.isoformat(),)
        sql += " ORDER BY created_at_utc DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        yield from ((r[0], r[1], r[2]) for r in self._db.execute(sql, params).fetchall())

    def commit_job_logs(self, job_id: int, logs: list[BroadcastLogRecord]) -> None:
        """Atomic: replace all recipient rows of a job and mark it done."""
        with self._tx():
            self._db.execute("DELETE FROM broadcast_logs WHERE job_id=?", (job_id,))
            self._db.executemany(
                """INSERT OR REPLACE INTO broadcast_logs (log_id, job_id, phone, customer_name, status,
                     sent_at_utc, wamid, failure_reason, buttons, params, is_retried)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [(lg.log_id, job_id, lg.phone, lg.customer_name, lg.status, iso(lg.sent_at_utc), lg.wamid,
                  lg.failure_reason, lg.buttons, lg.params, int(lg.is_retried)) for lg in logs])
            self._db.execute(
                "UPDATE broadcast_jobs SET logs_state='done', fetched_signature=signature, last_error=NULL "
                "WHERE job_id=?", (job_id,))

    def mark_job_failed(self, job_id: int, error: str, max_attempts: int) -> None:
        with self._tx():
            self._db.execute(
                """UPDATE broadcast_jobs SET attempts=attempts+1, last_error=?,
                     logs_state=CASE WHEN attempts+1 >= ? THEN 'skipped' ELSE 'failed' END
                   WHERE job_id=?""", (error[:500], max_attempts, job_id))

    def campaign_for_wamid(self, wamid: str) -> CampaignRef | None:
        row = self._db.execute(
            "SELECT j.job_id, j.name FROM broadcast_logs l JOIN broadcast_jobs j USING (job_id) "
            "WHERE l.wamid=? LIMIT 1", (wamid,)).fetchone()
        return CampaignRef(row[0], row[1]) if row else None

    def touches_for_phone(self, phone: str) -> list[Touch]:
        rows = self._db.execute(
            "SELECT l.sent_at_utc, j.job_id, j.name FROM broadcast_logs l JOIN broadcast_jobs j USING (job_id) "
            "WHERE l.phone=? AND l.sent_at_utc IS NOT NULL AND l.status NOT IN ('failed','queued','scheduled') "
            "ORDER BY l.sent_at_utc", (phone,)).fetchall()
        return [Touch(from_iso(r[0]), CampaignRef(r[1], r[2])) for r in rows]

    # ------------------------------------------------------------------ actions (derived)

    def replace_actions(self, actions: Iterable[ActionRecord]) -> int:
        n = 0
        with self._tx():
            self._db.execute("DELETE FROM actions")
            for a in actions:
                self._db.execute(
                    """INSERT OR REPLACE INTO actions (msg_id, room_id, customer_phone, timestamp_utc,
                         action_type, detail, form_json, campaign_job_id, campaign_name, attribution)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (a.msg_id, a.room_id, normalize_phone(a.customer_identifier), iso(a.timestamp_utc),
                     str(a.action_type), a.detail,
                     json.dumps(a.form_fields, ensure_ascii=False) if a.form_fields else None,
                     a.campaign_job_id, a.campaign_name, str(a.attribution)))
                n += 1
        return n

    def iter_actions(self) -> Iterator[tuple[RoomRecord, ActionRecord]]:
        rooms: dict[str, RoomRecord] = {}
        for row in self._db.execute(
                "SELECT a.* FROM actions a ORDER BY a.timestamp_utc DESC, a.msg_id DESC"):
            room = rooms.get(row["room_id"])
            if room is None:
                room = rooms[row["room_id"]] = self.get_room(row["room_id"])
            yield room, _action(row, room.customer_identifier)

    def actions_by_room(self) -> dict[str, list[ActionRecord]]:
        out: dict[str, list[ActionRecord]] = {}
        for row in self._db.execute(
                "SELECT a.*, r.customer_identifier FROM actions a JOIN rooms r USING (room_id) "
                "ORDER BY a.timestamp_utc, a.msg_id"):
            out.setdefault(row["room_id"], []).append(_action(row, row["customer_identifier"]))
        return out

    def campaign_engagement(self) -> dict[int, dict[str, int]]:
        """Per job: distinct phones that clicked / replied at all / submitted a form / asked."""
        out: dict[int, dict[str, int]] = {}
        for row in self._db.execute(
                """SELECT campaign_job_id AS job_id,
                     COUNT(DISTINCT customer_phone) AS responders,
                     COUNT(DISTINCT CASE WHEN action_type='button_clicked' THEN customer_phone END) AS clicked,
                     COUNT(DISTINCT CASE WHEN action_type='form_submitted' THEN customer_phone END) AS forms,
                     COUNT(DISTINCT CASE WHEN action_type='question' THEN customer_phone END) AS questions
                   FROM actions WHERE campaign_job_id IS NOT NULL GROUP BY campaign_job_id"""):
            out[row["job_id"]] = {k: row[k] for k in ("responders", "clicked", "forms", "questions")}
        return out

    def recipient_responses(self) -> dict[tuple[int, str], tuple[str, str, str]]:
        """(job_id, phone) -> (buttons clicked, action types, first response ts) for attributed actions."""
        out: dict[tuple[int, str], tuple[str, str, str]] = {}
        for row in self._db.execute(
                """SELECT campaign_job_id, customer_phone,
                     GROUP_CONCAT(DISTINCT CASE WHEN action_type='button_clicked' THEN detail END) AS buttons,
                     GROUP_CONCAT(DISTINCT action_type) AS kinds,
                     MIN(timestamp_utc) AS first_ts
                   FROM actions WHERE campaign_job_id IS NOT NULL
                   GROUP BY campaign_job_id, customer_phone"""):
            out[(row[0], row[1])] = (row["buttons"] or "", row["kinds"] or "", row["first_ts"])
        return out

    # ------------------------------------------------------------------ export reads

    def iter_room_messages(self) -> Iterator[tuple[RoomRecord, list[MessageRecord]]]:
        """Every fetched room with its messages ascending (for action derivation)."""
        rooms = self._db.execute(
            "SELECT * FROM rooms WHERE fetch_state='done' ORDER BY room_id").fetchall()
        for row in rooms:
            room = _room(row)
            msgs = [_message(m) for m in self._db.execute(
                "SELECT * FROM messages WHERE room_id=? ORDER BY timestamp_utc, msg_id", (room.room_id,))]
            yield room, msgs

    def iter_messages_for_export(self) -> Iterator[tuple[RoomRecord, MessageRecord]]:
        """ORDER BY rooms.last_activity_utc DESC, room_id, timestamp_utc, msg_id."""
        cur = self._db.execute(
            """SELECT m.*, r.customer_room_id, r.channel_id, r.source, r.customer_name,
                 r.customer_identifier, r.room_type, r.is_resolved, r.is_handled_by_bot,
                 r.last_activity_utc
               FROM messages m JOIN rooms r USING (room_id)
               ORDER BY r.last_activity_utc DESC, m.room_id, m.timestamp_utc, m.msg_id""")
        last_room: RoomRecord | None = None
        for row in cur:
            if last_room is None or last_room.room_id != row["room_id"]:
                last_room = _room(row)
            yield last_room, _message(row)

    def iter_room_summaries(self) -> Iterator[RoomSummary]:
        cur = self._db.execute(
            """SELECT r.*,
                 COUNT(m.msg_id) AS n,
                 SUM(m.sender_type='customer') AS n_cust,
                 SUM(m.sender_type='agent') AS n_agent,
                 SUM(m.sender_type='bot') AS n_bot,
                 SUM(m.sender_type='broadcast') AS n_bc,
                 MIN(m.timestamp_utc) AS first_ts, MAX(m.timestamp_utc) AS last_ts
               FROM rooms r LEFT JOIN messages m USING (room_id)
               GROUP BY r.room_id ORDER BY r.last_activity_utc DESC, r.room_id""")
        for row in cur:
            yield RoomSummary(
                room=_room(row), message_count=row["n"] or 0, customer_msgs=row["n_cust"] or 0,
                agent_msgs=row["n_agent"] or 0, bot_msgs=row["n_bot"] or 0,
                broadcast_msgs=row["n_bc"] or 0,
                first_msg_utc=from_iso(row["first_ts"]), last_msg_utc=from_iso(row["last_ts"]),
                fetch_state=row["fetch_state"])

    def iter_jobs(self) -> Iterator[sqlite3.Row]:
        yield from self._db.execute("SELECT * FROM broadcast_jobs ORDER BY created_at_utc DESC, job_id DESC")

    def iter_broadcast_logs_for_export(self) -> Iterator[sqlite3.Row]:
        yield from self._db.execute(
            """SELECT l.*, j.name AS job_name, j.template_name, j.created_at_utc AS job_created,
                 (SELECT r.room_id FROM rooms r WHERE r.customer_phone = l.phone LIMIT 1) AS room_id
               FROM broadcast_logs l JOIN broadcast_jobs j USING (job_id)
               ORDER BY j.created_at_utc DESC, l.job_id DESC, l.log_id""")

    def broadcast_status_by_phone(self) -> dict[str, dict]:
        """phone -> {received, best_status, last_campaign, last_sent_utc} over all broadcasts."""
        rank = {"read": 5, "delivered": 4, "sent": 3, "sending": 2, "failed_after_delivered": 1, "failed": 0}
        out: dict[str, dict] = {}
        for row in self._db.execute(
                """SELECT l.phone, l.status, l.sent_at_utc, j.name FROM broadcast_logs l
                   JOIN broadcast_jobs j USING (job_id)
                   WHERE l.phone IN (SELECT customer_phone FROM rooms)
                   ORDER BY l.sent_at_utc"""):
            d = out.setdefault(row[0], {"received": 0, "best_status": None, "last_campaign": None,
                                        "last_sent_utc": None, "campaigns": set()})
            d["received"] += 1
            d["campaigns"].add(row[3])
            if d["best_status"] is None or rank.get(row[1], -1) > rank.get(d["best_status"], -1):
                d["best_status"] = row[1]
            if row[2]:
                d["last_campaign"], d["last_sent_utc"] = row[3], from_iso(row[2])
        return out

    def counts(self) -> dict[str, int]:
        q = lambda sql: self._db.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "rooms": q("SELECT COUNT(*) FROM rooms"),
            "messages": q("SELECT COUNT(*) FROM messages"),
            "jobs": q("SELECT COUNT(*) FROM broadcast_jobs"),
            "broadcast_logs": q("SELECT COUNT(*) FROM broadcast_logs"),
            "actions": q("SELECT COUNT(*) FROM actions"),
        }

    def skipped(self) -> list[tuple[str, str, str]]:
        """(kind, id, last_error) for every room/job that is skipped or still failed."""
        rows = [("room", r[0], r[1] or "") for r in self._db.execute(
            "SELECT room_id, last_error FROM rooms WHERE fetch_state IN ('skipped','failed')")]
        rows += [("broadcast job", str(r[0]), r[1] or "") for r in self._db.execute(
            "SELECT job_id, last_error FROM broadcast_jobs WHERE logs_state IN ('skipped','failed')")]
        return rows

    def progress(self) -> Progress:
        st = dict(self._db.execute("SELECT fetch_state, COUNT(*) FROM rooms GROUP BY fetch_state").fetchall())
        js = dict(self._db.execute(
            "SELECT logs_state, COUNT(*) FROM broadcast_jobs GROUP BY logs_state").fetchall())
        c = self.counts()
        run = self._run()
        return Progress(
            rooms_total=c["rooms"], done=st.get("done", 0), pending=st.get("pending", 0),
            failed=st.get("failed", 0), skipped=st.get("skipped", 0), messages=c["messages"],
            enumeration_done=bool(run["enumeration_done"]),
            jobs_total=c["jobs"], jobs_done=js.get("done", 0), jobs_pending=js.get("pending", 0),
            jobs_failed=js.get("failed", 0), jobs_skipped=js.get("skipped", 0),
            broadcast_logs=c["broadcast_logs"],
            broadcast_enumeration_done=bool(run["broadcast_enumeration_done"]))

    def missing(self, since: date | None = None) -> Missing:
        """Counts behind the Complete / Partial label of a workbook built from this store."""
        run = self._run()
        rooms = dict(self._db.execute(
            "SELECT fetch_state, COUNT(*) FROM rooms GROUP BY fetch_state").fetchall())
        sql = ("SELECT logs_state, COUNT(*) FROM broadcast_jobs "
               f"WHERE status NOT IN ({','.join('?' * len(_NO_LOGS_YET))})")
        params: tuple = _NO_LOGS_YET
        if since is not None:
            sql += " AND created_at_utc >= ?"
            params += (since.isoformat(),)
        jobs = dict(self._db.execute(sql + " GROUP BY logs_state", params).fetchall())
        return Missing(
            rooms_listing_incomplete=not run["enumeration_done"],
            rooms_pending=rooms.get("pending", 0),
            rooms_failed=rooms.get("failed", 0) + rooms.get("skipped", 0),
            jobs_listing_incomplete=not run["broadcast_enumeration_done"],
            jobs_pending=jobs.get("pending", 0),
            jobs_failed=jobs.get("failed", 0) + jobs.get("skipped", 0))

    def jobs_not_sent_yet(self) -> int:
        """Campaigns still queued / scheduled / preparing: no recipient log exists yet."""
        return self._db.execute(
            f"SELECT COUNT(*) FROM broadcast_jobs WHERE status IN ({','.join('?' * len(_NO_LOGS_YET))})",
            _NO_LOGS_YET).fetchone()[0]

    def integrity_check(self) -> list[str]:
        """SQLite's own consistency check: ['ok'] when the file is healthy."""
        return [r[0] for r in self._db.execute("PRAGMA integrity_check").fetchall()]

    # ------------------------------------------------------------------ snapshots

    def create_snapshot(self, dest: Path) -> Path:
        """Point-in-time copy of the whole store at `dest`, via SQLite's online backup.

        Copied in one step, so it is one consistent read: in WAL mode that never blocks the
        writers in other connections (a stepped backup would restart on every write instead)."""
        dest.unlink(missing_ok=True)
        target = sqlite3.connect(dest)
        try:
            self._db.backup(target)
        finally:
            target.close()
        return dest

    @contextmanager
    def snapshot(self) -> Iterator[CheckpointStore]:
        """A throwaway CheckpointStore over a fresh snapshot, deleted afterwards (even on error).
        Work that writes (deriving actions) happens there, never in the live store."""
        _remove_stale_snapshots(self.path.parent)
        dest = self.path.with_name(f"{_SNAPSHOT_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}.sqlite3")
        snap: CheckpointStore | None = None
        try:
            self.create_snapshot(dest)
            snap = CheckpointStore(dest, self.app_code)
            yield snap
        finally:
            if snap is not None:
                snap.close()
            _remove_snapshot_files(dest)

    def close(self) -> None:
        self._db.close()


_OPEN_LOCK = threading.Lock()


def _retry_busy(fn, attempts: int = 50, wait_s: float = 0.1):
    """For the few statements SQLite won't busy-wait on (journal mode change)."""
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or i == attempts - 1:
                raise
            time.sleep(wait_s)


_SNAPSHOT_PREFIX = ".snapshot-"
_STALE_SNAPSHOT_S = 6 * 3600


def _remove_snapshot_files(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            Path(str(path) + suffix).unlink(missing_ok=True)
        except OSError:                  # still open elsewhere; the stale sweep gets it later
            pass


def _remove_stale_snapshots(folder: Path) -> None:
    """Snapshots left by a run that died mid-build (power cut) are ~the size of the store."""
    now = time.time()
    for p in folder.glob(f"{_SNAPSHOT_PREFIX}*.sqlite3"):
        try:
            if now - p.stat().st_mtime > _STALE_SNAPSHOT_S:
                _remove_snapshot_files(p)
        except OSError:
            pass


class _Tx:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    def __enter__(self):
        self._db.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._db.execute("COMMIT")
        else:
            self._db.execute("ROLLBACK")


def _room(row: sqlite3.Row) -> RoomRecord:
    return RoomRecord(
        room_id=row["room_id"], customer_room_id=row["customer_room_id"], channel_id=row["channel_id"],
        source=row["source"], customer_name=row["customer_name"],
        customer_identifier=row["customer_identifier"], room_type=row["room_type"],
        is_resolved=bool(row["is_resolved"]), is_handled_by_bot=bool(row["is_handled_by_bot"]),
        last_activity_utc=from_iso(row["last_activity_utc"]))


def _message(row: sqlite3.Row) -> MessageRecord:
    return MessageRecord(
        msg_id=row["msg_id"], room_id=row["room_id"], timestamp_utc=from_iso(row["timestamp_utc"]),
        sender_type=SenderType(row["sender_type"]), sender_id=row["sender_id"],
        sender_name=row["sender_name"], message_type=row["message_type"], text=row["text"],
        attachment_url=row["attachment_url"], attachment_name=row["attachment_name"],
        reply_to_text=row["reply_to_text"], is_deleted=bool(row["is_deleted"]),
        delivery_status=row["delivery_status"], reply_to_msg_id=row["reply_to_msg_id"],
        wamid=row["wamid"], context_wamid=row["context_wamid"], button_text=row["button_text"],
        broadcast_template=row["broadcast_template"])


def _action(row: sqlite3.Row, customer_identifier: str) -> ActionRecord:
    return ActionRecord(
        msg_id=row["msg_id"], room_id=row["room_id"], customer_identifier=customer_identifier,
        timestamp_utc=from_iso(row["timestamp_utc"]), action_type=ActionType(row["action_type"]),
        detail=row["detail"], form_fields=json.loads(row["form_json"]) if row["form_json"] else {},
        campaign_job_id=row["campaign_job_id"], campaign_name=row["campaign_name"],
        attribution=Attribution(row["attribution"]))
