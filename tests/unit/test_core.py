import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfoNotFoundError

import httpx
import pytest
import respx
from pydantic import ValidationError

from qexport.api.client import (
    AuthError, RateLimiter, RateLimitExhausted, RoomAccessError, SchemaDriftError,
)
from qexport.api.models import RawBroadcastLog, RawComment, RawRoom
from qexport.config import Settings
from qexport.load.excel import EXCEL_MAX_CELL_CHARS, sanitize_cell
from qexport.transform.normalize import Normalizer, SenderType, normalize_phone
from tests.conftest import (
    ADMIN_EMAIL, ADMIN_TOKEN, APP, OMNI, SDK, SDK_TOKEN, FakeClock, comment, comments_page, raw_job,
    raw_log, raw_room, rooms_page,
)


# --------------------------------------------------------------------------- config

def test_settings_missing_admin_token_raises_validation_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for k in ("QISCUS_ADMIN_TOKEN", "QISCUS_APP_CODE", "QISCUS_SDK_USER_ID", "QISCUS_SDK_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValidationError) as e:
        Settings(_env_file=None, app_code=APP, sdk_user_id="u", sdk_token="t")
    assert "admin_token" in str(e.value)


def test_settings_repr_never_contains_token_value(settings):
    assert ADMIN_TOKEN not in repr(settings) and ADMIN_TOKEN not in str(settings)
    assert SDK_TOKEN not in repr(settings) and SDK_TOKEN not in str(settings)


# --------------------------------------------------------------------------- rate limiter

def test_rate_limiter_spaces_calls_at_configured_rate():
    clock = FakeClock()
    rl = RateLimiter(2.0, clock)
    start = clock.now
    for _ in range(5):
        rl.acquire()
    assert clock.now - start >= 2.0


def test_rate_limiter_penalize_halves_rate_and_respects_floor():
    rl = RateLimiter(1.2, FakeClock())
    rl.penalize()
    assert rl.current_rate == pytest.approx(0.6)
    for _ in range(10):
        rl.penalize()
    assert rl.current_rate == pytest.approx(0.2)
    for _ in range(200):
        rl.recover()
    assert rl.current_rate == pytest.approx(1.2)


# --------------------------------------------------------------------------- client

@respx.mock
def test_list_customer_rooms_sends_auth_and_app_id_headers(client):
    route = respx.post(f"{OMNI}/api/v2/customer_rooms").mock(
        return_value=httpx.Response(200, json=rooms_page([raw_room(1)], None)))
    client.list_customer_rooms(None)
    req = route.calls.last.request
    assert req.headers["Authorization"] == ADMIN_TOKEN
    assert req.headers["Qiscus-App-Id"] == APP
    assert json.loads(req.content) == {"limit": 100}


@respx.mock
def test_list_customer_rooms_returns_next_cursor_from_meta(client):
    respx.post(f"{OMNI}/api/v2/customer_rooms").mock(
        return_value=httpx.Response(200, json=rooms_page([raw_room(1)], "2026-10-20T05:31:00Z")))
    rooms, cursor = client.list_customer_rooms(None)
    assert cursor == "2026-10-20T05:31:00Z" and rooms[0].room_id == "491000001"


@respx.mock
def test_list_customer_rooms_retries_429_then_succeeds(client, monkeypatch):
    calls = []
    monkeypatch.setattr(client.rooms_limiter, "penalize", lambda *a, **k: calls.append(1))
    route = respx.post(f"{OMNI}/api/v2/customer_rooms").mock(side_effect=[
        httpx.Response(429), httpx.Response(429), httpx.Response(200, json=rooms_page([], None))])
    client.list_customer_rooms(None)
    assert route.call_count == 3 and len(calls) == 2


@respx.mock
def test_list_customer_rooms_raises_rate_limit_exhausted_after_max_attempts(client):
    route = respx.post(f"{OMNI}/api/v2/customer_rooms").mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitExhausted):
        client.list_customer_rooms(None)
    assert route.call_count == 8


@respx.mock
def test_list_customer_rooms_401_raises_auth_error_without_retry(client):
    route = respx.post(f"{OMNI}/api/v2/customer_rooms").mock(return_value=httpx.Response(401))
    with pytest.raises(AuthError):
        client.list_customer_rooms(None)
    assert route.call_count == 1


@respx.mock
def test_load_comments_403_raises_room_access_error_with_room_id(client):
    respx.get(f"{SDK}/api/v2/sdk/load_comments").mock(return_value=httpx.Response(403))
    with pytest.raises(RoomAccessError) as e:
        client.load_comments("491314784", None)
    assert e.value.room_id == "491314784"


@respx.mock
def test_load_comments_passes_last_comment_id_when_given(client):
    route = respx.get(f"{SDK}/api/v2/sdk/load_comments").mock(
        return_value=httpx.Response(200, json=comments_page([])))
    client.load_comments("491314784", 4766464924)
    req = route.calls.last.request
    assert "last_comment_id=4766464924" in str(req.url)
    assert req.headers["QISCUS-SDK-TOKEN"] == SDK_TOKEN and req.headers["QISCUS-SDK-USER-ID"] == ADMIN_EMAIL


@respx.mock
def test_raw_comment_missing_timestamp_raises_schema_drift_error(client):
    bad = comment(1, 491000001, email="x")
    del bad["timestamp"]
    respx.get(f"{SDK}/api/v2/sdk/load_comments").mock(
        return_value=httpx.Response(200, json=comments_page([bad])))
    with pytest.raises(SchemaDriftError) as e:
        client.load_comments("491000001", None)
    assert "timestamp" in str(e.value)


@respx.mock
def test_broadcast_jobs_post_body_and_parse(client):
    route = respx.post(f"{OMNI}/api/v3/admin/broadcast_jobs").mock(
        return_value=httpx.Response(200, json={"data": {"broadcast_jobs": [raw_job(1, "Qya_First_BM")]},
                                               "meta": {"cursor_after": "abc"}}))
    jobs, cur = client.list_broadcast_jobs("prev")
    assert json.loads(route.calls.last.request.content) == {"limit": 100, "cursor_after": "prev"}
    assert jobs[0].name == "Qya_First_BM" and cur == "abc"
    assert jobs[0].created_at.tzinfo is not None          # naive "created_at" is read as UTC


@respx.mock
def test_broadcast_logs_uses_cursor_pagination(client):
    route = respx.get(f"{OMNI}/api/v2/admin/broadcast_logs/42").mock(
        return_value=httpx.Response(200, json={"data": {"broadcast_logs": [raw_log(1, "6281200000001")]},
                                               "meta": {"cursor_after": 99}}))
    logs, cur = client.list_broadcast_logs(42, "100")
    url = str(route.calls.last.request.url)
    assert "pagination=cursor" in url and "cursor_after=100" in url and "limit=100" in url
    assert cur == "99" and logs[0].message_id == "wamid.1"


@respx.mock
def test_ping_reports_each_api(client):
    respx.post(f"{OMNI}/api/v2/customer_rooms").mock(return_value=httpx.Response(401))
    respx.get(f"{SDK}/api/v2/sdk/my_profile").mock(return_value=httpx.Response(200, json={}))
    assert client.ping() == {"omni": False, "sdk": True}


# --------------------------------------------------------------------------- normalizer

def _room(norm, **kw):
    return norm.room(RawRoom.model_validate(raw_room(1, **kw)))


def _msg(norm, room, **kw):
    return norm.message(RawComment.model_validate(comment(10, int(room.room_id), **kw)), room)


def test_normalize_customer_message_sender_type_customer(norm):
    room = _room(norm)
    assert _msg(norm, room, email=room.customer_identifier).sender_type == SenderType.CUSTOMER


def test_normalize_system_event_sender_type_system(norm):
    room = _room(norm)
    m = _msg(norm, room, email="system@x", type_="system_event", text="Room resolved by Agent")
    assert m.sender_type == SenderType.SYSTEM and m.text == "Room resolved by Agent"


def test_normalize_broadcast_message_sender_and_template(norm):
    room = _room(norm)
    m = _msg(norm, room, email=ADMIN_EMAIL, unique_temp_id="wa_wamid.ABC",
             extras={"broadcast_message": {"name": "welcome_by_qya2", "is_broadcast": True}})
    assert m.sender_type == SenderType.BROADCAST
    assert m.broadcast_template == "welcome_by_qya2" and m.wamid == "wamid.ABC"


def test_normalize_agent_message_sender_type_agent(norm):
    room = _room(norm)
    m = _msg(norm, room, email=ADMIN_EMAIL, user_extras={"is_customer": False, "type": "agent"})
    assert m.sender_type == SenderType.AGENT


def test_normalize_button_tap_captures_label_and_context(norm):
    room = _room(norm)
    m = _msg(norm, room, email=room.customer_identifier, type_="reply", text="Build My University Plan",
             payload={"replied_comment_id": 5, "replied_comment_message": "Hi! …"},
             extras={"button": {"payload": "Build My University Plan", "text": "Build My University Plan"},
                     "context": {"from": "6280000000000", "id": "wamid.XYZ"}, "sub_type": "button"})
    assert m.button_text == "Build My University Plan"
    assert m.context_wamid == "wamid.XYZ" and m.reply_to_msg_id == 5 and m.reply_to_text == "Hi! …"


def test_normalize_file_attachment_extracts_url_name_and_caption(norm):
    room = _room(norm)
    m = _msg(norm, room, email=room.customer_identifier, type_="file_attachment",
             text="[file] https://x/y.pdf [/file]",
             payload={"url": "https://x/rapor.pdf", "file_name": "rapor.pdf", "caption": "my report"})
    assert (m.attachment_url, m.attachment_name, m.text) == ("https://x/rapor.pdf", "rapor.pdf", "my report")


def test_normalize_file_attachment_falls_back_to_bracket_file_syntax(norm):
    room = _room(norm)
    m = _msg(norm, room, email=room.customer_identifier, type_="file_attachment",
             text="[file] https://x/y.pdf [/file]", payload={})
    assert m.attachment_url == "https://x/y.pdf" and m.attachment_name == "y.pdf" and m.text == ""


def test_normalize_reply_populates_reply_to_text(norm):
    room = _room(norm)
    m = _msg(norm, room, email=room.customer_identifier, type_="reply", text="yes",
             payload={"replied_comment_message": "Are you interested?", "replied_comment_id": 3})
    assert m.reply_to_text == "Are you interested?" and m.text == "yes"


def test_normalize_unknown_type_preserves_type_and_message(norm):
    room = _room(norm)
    m = _msg(norm, room, email=ADMIN_EMAIL, type_="carousel", text="card text")
    assert m.message_type == "carousel" and m.text == "card text"


def test_to_local_converts_utc_to_wib_plus_7(norm):
    assert norm.to_local(datetime(2026, 10, 20, 6, 5, 8, tzinfo=timezone.utc)) == datetime(2026, 10, 20, 13, 5, 8)


def test_normalizer_invalid_timezone_fails_at_construction():
    with pytest.raises(ZoneInfoNotFoundError):
        Normalizer("Mars/Olympus_Mons")


def test_broadcast_log_maps_status_and_buttons(norm):
    rec = norm.broadcast_log(RawBroadcastLog.model_validate(
        raw_log(7, "6281200000001", status=-1, notes="Error 131026, Message undeliverable")), 42)
    assert rec.status == "failed" and rec.failure_reason.startswith("Error 131026")
    assert rec.buttons == "Build My University Plan" and rec.job_id == 42


def test_normalize_phone_handles_local_prefix_and_symbols():
    assert normalize_phone("0812-3456-789") == "628123456789"
    assert normalize_phone("+62 812 3456 789") == "628123456789"


# --------------------------------------------------------------------------- sanitize_cell

def test_sanitize_cell_prefixes_formula_leading_equals():
    assert sanitize_cell('=HYPERLINK("x")') == ("'=HYPERLINK(\"x\")", False)


@pytest.mark.parametrize("value", ["+6281234", "-1+2", "- item", "@SUM(A1)", "\tx", "\rx"])
def test_sanitize_cell_neutralizes_plus_minus_at_prefixes(value):
    assert sanitize_cell(value)[0].startswith("'")


def test_sanitize_cell_strips_illegal_control_characters():
    assert sanitize_cell("a\x0bb") == ("ab", False)


def test_sanitize_cell_truncates_over_32767_chars_and_flags():
    out, cut = sanitize_cell("x" * 40_000)
    assert cut and len(out) == EXCEL_MAX_CELL_CHARS and out.endswith("…[truncated]")


@pytest.mark.parametrize("value", ["see you 🤍", "pake bahasa Indonesia aj"])
def test_sanitize_cell_preserves_emoji_and_indonesian_text(value):
    assert sanitize_cell(value) == (value, False)
