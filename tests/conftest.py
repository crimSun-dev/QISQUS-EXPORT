"""Shared fixtures. Payload shapes mirror live responses captured on 2026-10-20 (PII replaced)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from qexport.api.client import QiscusClient
from qexport.config import Settings
from qexport.store.checkpoint import CheckpointStore
from qexport.transform.normalize import Normalizer

OMNI = "https://multichannel-api.qiscus.com"
SDK = "https://api3.qiscus.com"
APP = "test-app-code"
ADMIN_TOKEN = "admin-token-SECRET-123"
SDK_TOKEN = "sdk-token-SECRET-456"
ADMIN_EMAIL = "test-app_admin@qismo.com"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += max(0.0, seconds)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None, app_code=APP, admin_token=ADMIN_TOKEN, sdk_user_id=ADMIN_EMAIL,
        sdk_token=SDK_TOKEN, out_dir=tmp_path, rooms_rps=100, messages_rps=100, broadcast_rps=100,
        max_retry_attempts=8)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(settings, clock):
    c = QiscusClient(settings, http=httpx.Client(), clock=clock)
    yield c
    c.close()


@pytest.fixture
def norm() -> Normalizer:
    return Normalizer("Asia/Jakarta")


@pytest.fixture
def store(tmp_path):
    s = CheckpointStore(tmp_path / "qexport_test.sqlite3", APP)
    yield s
    s.close()


T0 = datetime(2026, 10, 15, 8, 56, 0, tzinfo=timezone.utc)


def ts(minutes: float = 0) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def raw_room(i: int, *, phone: str | None = None, last: str | None = None, resolved=False) -> dict:
    return {
        "id": 447000000 + i, "room_id": str(491000000 + i), "channel_id": 4995, "contact_id": 1000 + i,
        "name": f"Customer {i}", "user_id": phone or f"62812000{i:05d}", "source": "wa",
        "room_type": "individual", "is_resolved": resolved, "is_handled_by_bot": False,
        "is_waiting": False, "last_comment_timestamp": last or ts(-i),
        "last_customer_timestamp": last or ts(-i),
    }


def rooms_page(rooms: list[dict], cursor_after: str | None) -> dict:
    return {"data": {"customer_rooms": rooms},
            "meta": {"cursor_after": cursor_after, "cursor_before": None}, "status": 200}


def comment(cid: int, room_id: int, *, email: str, text: str = "hi", type_: str = "text",
            minutes: float = 0, payload: dict | None = None, extras: dict | None = None,
            unique_temp_id: str | None = None, user_extras: dict | None = None) -> dict:
    return {
        "id": cid, "room_id": room_id, "type": type_, "message": text, "email": email,
        "username": "Sampoerna University" if email == ADMIN_EMAIL else "Customer",
        "timestamp": ts(minutes), "unix_nano_timestamp": 0, "payload": payload or {},
        "extras": extras or {}, "user_extras": user_extras, "is_deleted": False, "status": "read",
        "unique_temp_id": unique_temp_id,
    }


def comments_page(comments: list[dict]) -> dict:
    return {"results": {"comments": comments}, "status": 200}


def raw_job(job_id: int, name: str, *, status: int = 2, created: str = "2026-10-15T08:00:00",
            recipients: int = 3, read: int = 1, delivered: int = 1, failed: int = 1, sent: int = 0) -> dict:
    return {
        "app_id": 125393, "approval": None, "canceled_at": None, "canceled_by": None, "channel_id": 4995,
        "channel_name": "Sampoerna University", "created_at": created, "finished_at": "2026-10-15T09:00:00Z",
        "id": job_id, "is_mmlite": True, "is_optimize": True, "label": None, "name": name,
        "recipient_category": "all", "started_at": "2026-10-15T08:56:00Z", "status": status,
        "template_category": "MARKETING", "template_detail_id": None, "template_id": None,
        "template_name": "welcome_by_qya2", "total_failed": failed, "total_read": read,
        "total_received": delivered, "total_recipient": recipients, "total_sent": sent,
        "wa_template_id": 925714, "waba_id": "1",
    }


def jobs_page(jobs: list[dict], cursor_after: str | None) -> dict:
    return {"data": {"broadcast_jobs": jobs},
            "meta": {"cursor_after": cursor_after, "cursor_before": None, "per_page": 100,
                     "total_count": len(jobs)}, "status": 200}


def raw_log(log_id: int, phone: str, *, status: int = 4, wamid: str | None = None,
            notes: str | None = None, minutes: float = 0) -> dict:
    # button_params / params / card_header are JSON-encoded *strings* in the live API.
    return {
        "button_params": json.dumps([{"button_text": "Build My University Plan", "index": 0,
                                      "payload": "Build My University Plan", "type": "quick_reply"}]),
        "card_button": None, "card_header": "[]", "card_payload": None, "charged_credit": None,
        "customer_name": f"Name {phone[-4:]}", "header_params": None, "id": log_id, "is_carousel": False,
        "is_optimize": True, "is_retried": False, "max_price_credit": None,
        "message_id": wamid or f"wamid.{log_id}", "notes": notes, "params": json.dumps([f"Name {phone[-4:]}"]),
        "phone_number": phone, "sent_at": ts(minutes), "status": status, "username": "x",
    }


def logs_page(logs: list[dict], cursor_after) -> dict:
    return {"data": {"broadcast_logs": logs},
            "meta": {"cursor_after": cursor_after, "cursor_before": None, "total_page": 1}, "status": 200}
