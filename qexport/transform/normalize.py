from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo

from qexport.api import endpoints as ep
from qexport.api.models import RawBroadcastJob, RawBroadcastLog, RawComment, RawRoom


class SenderType(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    BOT = "bot"
    BROADCAST = "broadcast"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class RoomRecord:
    room_id: str
    customer_room_id: int
    channel_id: int
    source: str
    customer_name: str
    customer_identifier: str
    room_type: str
    is_resolved: bool
    is_handled_by_bot: bool
    last_activity_utc: datetime | None


@dataclass(frozen=True, slots=True)
class MessageRecord:
    msg_id: int
    room_id: str
    timestamp_utc: datetime
    sender_type: SenderType
    sender_id: str
    sender_name: str
    message_type: str            # raw type, preserved
    text: str                    # human-readable body (caption for files, event text for system)
    attachment_url: str | None
    attachment_name: str | None
    reply_to_text: str | None    # quoted text for "reply"
    is_deleted: bool
    delivery_status: str | None
    reply_to_msg_id: int | None = None
    wamid: str | None = None             # this message's WhatsApp id ("wa_" prefix stripped)
    context_wamid: str | None = None     # WhatsApp id of the message this one answers (button taps)
    button_text: str | None = None       # set when the customer tapped a quick-reply button
    broadcast_template: str | None = None  # set on broadcast messages


@dataclass(frozen=True, slots=True)
class BroadcastJobRecord:
    job_id: int
    name: str
    template_name: str
    category: str
    status: str
    channel_id: int | None
    channel_name: str
    created_at_utc: datetime
    started_at_utc: datetime | None
    finished_at_utc: datetime | None
    total_recipient: int
    total_sent: int
    total_delivered: int
    total_read: int
    total_failed: int
    is_terminal: bool


@dataclass(frozen=True, slots=True)
class BroadcastLogRecord:
    log_id: int
    job_id: int
    phone: str
    customer_name: str
    status: str
    sent_at_utc: datetime | None
    wamid: str | None
    failure_reason: str | None
    buttons: str | None          # "Build My University Plan | Register"
    params: str | None           # template variables, " | " joined
    is_retried: bool


_FILE_TAG = re.compile(r"\[file\]\s*(\S+)\s*\[/file\]", re.IGNORECASE)
_NON_DIGIT = re.compile(r"\D")


def normalize_phone(value: str | None) -> str:
    """Digits only; Indonesian local '08…' becomes '628…' so rooms and broadcast logs join."""
    if not value:
        return ""
    digits = _NON_DIGIT.sub("", value)
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    return digits


class Normalizer:
    def __init__(self, display_tz: str) -> None:
        self.tz = ZoneInfo(display_tz)   # raises ZoneInfoNotFoundError early

    def room(self, raw: RawRoom) -> RoomRecord:
        return RoomRecord(
            room_id=raw.room_id,
            customer_room_id=raw.id,
            channel_id=raw.channel_id,
            source=raw.source,
            customer_name=(raw.name or "").strip(),
            customer_identifier=raw.user_id,
            room_type=raw.room_type,
            is_resolved=raw.is_resolved,
            is_handled_by_bot=raw.is_handled_by_bot,
            last_activity_utc=raw.last_comment_timestamp,
        )

    def message(self, raw: RawComment, room: RoomRecord) -> MessageRecord:
        """sender_type rules, in order:
           1. raw.type == "system_event"                          -> SYSTEM
           2. raw.email == room.customer_identifier               -> CUSTOMER
           3. extras.broadcast_message present                    -> BROADCAST
           4. extras/user_extras mark bot                         -> BOT
           5. otherwise                                           -> AGENT
        Never raises on content."""
        payload = raw.payload or {}
        extras = raw.extras or {}
        user_extras = raw.user_extras or {}

        if raw.type == "system_event":
            sender = SenderType.SYSTEM
        elif raw.email == room.customer_identifier or user_extras.get("is_customer") is True:
            sender = SenderType.CUSTOMER
        elif isinstance(extras.get("broadcast_message"), dict):
            sender = SenderType.BROADCAST
        elif _marks_bot(extras) or _marks_bot(user_extras):
            sender = SenderType.BOT
        else:
            sender = SenderType.AGENT

        text = raw.message or ""
        attachment_url = attachment_name = reply_to_text = None
        reply_to_msg_id = None

        if raw.type == "file_attachment":
            attachment_url = _str(payload.get("url"))
            attachment_name = _str(payload.get("file_name"))
            caption = _str(payload.get("caption"))
            if not attachment_url:
                m = _FILE_TAG.search(text)
                if m:
                    attachment_url = m.group(1)
            if attachment_url and not attachment_name:
                attachment_name = attachment_url.rsplit("/", 1)[-1].split("?", 1)[0] or None
            text = caption or _FILE_TAG.sub("", text).strip()
        elif raw.type == "reply":
            reply_to_text = _str(payload.get("replied_comment_message"))
            rid = payload.get("replied_comment_id")
            reply_to_msg_id = rid if isinstance(rid, int) else None
            text = raw.message or _str(payload.get("text")) or ""
        elif raw.type == "system_event":
            text = raw.message or _str(payload.get("text")) or ""

        if not text and isinstance(extras.get("body"), str):
            text = extras["body"]

        wamid = None
        if raw.unique_temp_id and raw.unique_temp_id.startswith("wa_wamid."):
            wamid = raw.unique_temp_id[3:]

        context = extras.get("context") if isinstance(extras.get("context"), dict) else {}
        button = extras.get("button") if isinstance(extras.get("button"), dict) else {}
        button_text = None
        if extras.get("sub_type") == "button" or button:
            button_text = _str(button.get("text")) or _str(button.get("payload")) or text or None
        interactive = extras.get("interactive") if isinstance(extras.get("interactive"), dict) else {}
        if not button_text and interactive:
            # Meta interactive replies: {"button_reply": {"id","title"}} / {"list_reply": {...}}
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            if isinstance(reply, dict):
                button_text = _str(reply.get("title")) or _str(reply.get("id"))

        bmsg = extras.get("broadcast_message") if isinstance(extras.get("broadcast_message"), dict) else {}

        return MessageRecord(
            msg_id=raw.id,
            room_id=room.room_id,
            timestamp_utc=raw.timestamp,
            sender_type=sender,
            sender_id=raw.email,
            sender_name=(raw.username or "").strip(),
            message_type=raw.type,
            text=text,
            attachment_url=attachment_url,
            attachment_name=attachment_name,
            reply_to_text=reply_to_text,
            is_deleted=raw.is_deleted,
            delivery_status=raw.status,
            reply_to_msg_id=reply_to_msg_id,
            wamid=wamid,
            context_wamid=_str(context.get("id")),
            button_text=button_text if sender == SenderType.CUSTOMER else None,
            broadcast_template=_str(bmsg.get("name")),
        )

    def broadcast_job(self, raw: RawBroadcastJob) -> BroadcastJobRecord:
        return BroadcastJobRecord(
            job_id=raw.id,
            name=raw.name,
            template_name=raw.template_name or "",
            category=(raw.template_category or "").lower(),
            status=ep.JOB_STATUS.get(raw.status, f"unknown({raw.status})"),
            channel_id=raw.channel_id,
            channel_name=raw.channel_name or "",
            created_at_utc=raw.created_at,
            started_at_utc=raw.started_at,
            finished_at_utc=raw.finished_at,
            total_recipient=raw.total_recipient or 0,
            total_sent=raw.total_sent or 0,
            total_delivered=raw.total_received or 0,
            total_read=raw.total_read or 0,
            total_failed=raw.total_failed or 0,
            is_terminal=raw.status in ep.JOB_TERMINAL_STATUSES,
        )

    def broadcast_log(self, raw: RawBroadcastLog, job_id: int) -> BroadcastLogRecord:
        buttons = []
        for b in raw.button_params or []:
            if isinstance(b, dict):
                label = _str(b.get("button_text")) or _str(b.get("payload"))
                if label:
                    buttons.append(label)
        params = [str(p) for p in (raw.params or []) if p is not None and not isinstance(p, (dict, list))]
        return BroadcastLogRecord(
            log_id=raw.id,
            job_id=job_id,
            phone=normalize_phone(raw.phone_number),
            customer_name=(raw.customer_name or raw.username or "").strip(),
            status=ep.LOG_STATUS.get(raw.status, f"unknown({raw.status})"),
            sent_at_utc=raw.sent_at,
            wamid=raw.message_id or None,
            failure_reason=raw.notes or None,
            buttons=" | ".join(buttons) or None,
            params=" | ".join(params) or None,
            is_retried=bool(raw.is_retried),
        )

    def to_local(self, ts_utc: datetime) -> datetime:
        """tz-naive local datetime for Excel."""
        if ts_utc.tzinfo is None:
            ts_utc = ts_utc.replace(tzinfo=timezone.utc)
        return ts_utc.astimezone(self.tz).replace(tzinfo=None)


def _str(v: object) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _marks_bot(d: dict) -> bool:
    if not isinstance(d, dict):
        return False
    if d.get("is_bot") is True:
        return True
    t = d.get("type") or d.get("sender_type")
    return isinstance(t, str) and t.lower() == "bot"
