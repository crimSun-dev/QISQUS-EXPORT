"""Raw API payload models. extra="allow" so unknown fields pass; missing required ones fail loudly."""

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


def _assume_utc(v: datetime | None) -> datetime | None:
    # broadcast_jobs.created_at comes back without a "Z"; every Qiscus timestamp is UTC.
    if v is not None and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


class RawRoom(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int                      # customer_room id (447099445)
    room_id: str                 # SDK topic id ("491314784"), used for load_comments
    channel_id: int
    contact_id: int | None = None
    name: str | None = None
    user_id: str                 # customer phone / identifier
    source: str                  # "wa", ...
    room_type: str               # "individual" | group
    is_resolved: bool
    is_handled_by_bot: bool
    is_waiting: bool | None = None
    last_comment_timestamp: datetime | None = None
    last_customer_timestamp: datetime | None = None

    @field_validator("room_id", "user_id", mode="before")
    @classmethod
    def _to_str(cls, v: Any) -> Any:
        return str(v) if isinstance(v, int) else v

    @field_validator("last_comment_timestamp", "last_customer_timestamp")
    @classmethod
    def _utc(cls, v):
        return _assume_utc(v)


class RawComment(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    room_id: int
    type: str                    # "text" | "reply" | "file_attachment" | "system_event" | other
    message: str = ""
    email: str                   # sender identifier (customer phone, agent email, bot id)
    username: str | None = None
    timestamp: datetime          # UTC, "2026-10-20T06:05:08Z"
    unix_nano_timestamp: int | None = None
    unique_temp_id: str | None = None   # "wa_<wamid>" for WhatsApp messages
    payload: dict | None = None
    extras: dict | None = None
    user_extras: dict | None = None
    is_deleted: bool = False
    status: str | None = None    # "read" | "delivered" | "sent"

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, v):
        return _assume_utc(v)


class RawBroadcastJob(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    name: str
    template_name: str | None = None
    template_category: str | None = None
    status: int
    channel_id: int | None = None
    channel_name: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total_recipient: int | None = None
    total_sent: int | None = None
    total_received: int | None = None   # = delivered
    total_read: int | None = None
    total_failed: int | None = None

    @field_validator("created_at", "started_at", "finished_at")
    @classmethod
    def _utc(cls, v):
        return _assume_utc(v)


class RawBroadcastLog(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    phone_number: str
    customer_name: str | None = None
    username: str | None = None
    status: int
    sent_at: datetime | None = None
    message_id: str | None = None       # WhatsApp "wamid.…"; matches comment.extras.context.id
    notes: str | None = None            # failure reason, e.g. "Error 131026, Message undeliverable"
    button_params: list | None = None
    params: list | None = None
    is_retried: bool | None = None

    @field_validator("phone_number", mode="before")
    @classmethod
    def _to_str(cls, v: Any) -> Any:
        return str(v) if isinstance(v, int) else v

    @field_validator("button_params", "params", mode="before")
    @classmethod
    def _decode_json_list(cls, v: Any) -> Any:
        # Live API returns these as JSON-encoded strings: '[{"button_text": …}]'
        if isinstance(v, str):
            try:
                v = json.loads(v) if v.strip() else None
            except ValueError:
                return [v]
        return v

    @field_validator("sent_at")
    @classmethod
    def _utc(cls, v):
        return _assume_utc(v)
