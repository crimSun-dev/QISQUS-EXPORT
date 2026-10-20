from datetime import datetime, timedelta, timezone

import pytest

from qexport.transform.actions import (
    ActionType, Attribution, CampaignRef, Touch, derive_actions, is_question, lead_stage, merge_forms,
    parse_form, prompt_labels,
)
from qexport.transform.normalize import MessageRecord, RoomRecord, SenderType

T0 = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
PHONE = "6281200000001"
ROOM = RoomRecord("491000001", 1, 4995, "wa", "Budi", PHONE, "individual", False, False, T0)

PROMPT = ("Good days! you are connected to Sampoerna University Hotline.\n\n"
          "In the meantime, please help to fill the data below:\n\n"
          "Name:\nGrade:\nEmail:\nPhone number:\nSchool name:\nSchool City:\n"
          "Interested majors:\nParents name:\nParents number:\nParents email:\n\nThank you.")


def msg(mid, minutes, sender=SenderType.CUSTOMER, text="", **kw) -> MessageRecord:
    base = dict(msg_id=mid, room_id=ROOM.room_id, timestamp_utc=T0 + timedelta(minutes=minutes),
                sender_type=sender, sender_id=PHONE if sender == SenderType.CUSTOMER else "admin",
                sender_name="", message_type="text", text=text, attachment_url=None, attachment_name=None,
                reply_to_text=None, is_deleted=False, delivery_status=None)
    base.update(kw)
    return MessageRecord(**base)


def no_campaign(_wamid):
    return None


def test_button_tap_is_attributed_to_exact_broadcast_via_wamid():
    lookup = {"wamid.B1": CampaignRef(693643558, "Qya_First_BM")}
    msgs = [
        msg(1, 0, SenderType.BROADCAST, "Hi Budi! …", wamid="wamid.B1", broadcast_template="welcome_by_qya2"),
        msg(2, 5, text="Build My University Plan", message_type="reply", button_text="Build My University Plan",
            context_wamid="wamid.B1", reply_to_msg_id=1),
    ]
    [a] = derive_actions(ROOM, msgs, campaign_for_wamid=lookup.get)
    assert a.action_type == ActionType.BUTTON_CLICKED and a.detail == "Build My University Plan"
    assert (a.campaign_job_id, a.campaign_name, a.attribution) == (693643558, "Qya_First_BM",
                                                                   Attribution.BUTTON_CONTEXT)


def test_button_tap_without_broadcast_logs_falls_back_to_in_room_template():
    msgs = [
        msg(1, 0, SenderType.BROADCAST, "Hi", wamid="wamid.B1", broadcast_template="welcome_by_qya2"),
        msg(2, 5, text="Register", button_text="Register", context_wamid="wamid.B1", reply_to_msg_id=1),
    ]
    [a] = derive_actions(ROOM, msgs, campaign_for_wamid=no_campaign)
    assert a.campaign_name == "welcome_by_qya2" and a.campaign_job_id is None
    assert a.attribution == Attribution.BUTTON_CONTEXT


def test_free_text_after_broadcast_uses_last_touch_within_window():
    touches = [Touch(T0 - timedelta(days=1), CampaignRef(1, "old")), Touch(T0, CampaignRef(2, "webinar"))]
    msgs = [msg(1, 60, text="Mau tanya biayanya berapa ya"), msg(2, 60 * 24 * 10, text="ok")]
    a1, a2 = derive_actions(ROOM, msgs, campaign_for_wamid=no_campaign, touches=touches,
                            window=timedelta(days=7))
    assert (a1.action_type, a1.campaign_name, a1.attribution) == (ActionType.QUESTION, "webinar",
                                                                  Attribution.LAST_TOUCH)
    assert a2.campaign_name is None and a2.attribution == Attribution.NONE   # 10 days later: outside window


def test_labelled_form_reply_is_form_submission():
    text = ("Name: Budi Santoso\nGrade: 12\nEmail: budi@example.com\nPhone number: 081200000001\n"
            "School name: SMA 1\nSchool City: Semarang\nInterested majors: Psychology")
    msgs = [msg(1, 0, SenderType.AGENT, PROMPT), msg(2, 3, text=text)]
    [a] = derive_actions(ROOM, msgs, campaign_for_wamid=no_campaign)
    assert a.action_type == ActionType.FORM_SUBMITTED
    assert a.form_fields["name"] == "Budi Santoso" and a.form_fields["interested_majors"] == "Psychology"
    assert len(a.form_fields) == 7


def test_unlabelled_form_reply_maps_lines_to_prompt_order():
    text = "Budi\n12\nbudi@example.com\n081200000001\nSMA 1\nSemarang\nPsychology"
    msgs = [msg(1, 0, SenderType.AGENT, PROMPT), msg(2, 3, text=text)]
    [a] = derive_actions(ROOM, msgs, campaign_for_wamid=no_campaign)
    assert a.action_type == ActionType.FORM_SUBMITTED
    assert a.form_fields["email"] == "budi@example.com" and a.form_fields["school_city"] == "Semarang"


def test_multiline_chat_after_prompt_is_not_mistaken_for_form():
    text = "halo kak\nsaya mau daftar\ntapi bingung\njurusannya"
    msgs = [msg(1, 0, SenderType.AGENT, PROMPT), msg(2, 3, text=text)]
    [a] = derive_actions(ROOM, msgs, campaign_for_wamid=no_campaign)
    assert a.action_type != ActionType.FORM_SUBMITTED


def test_indonesian_labels_are_recognized():
    f = parse_form("Nama: Siti\nKelas: 11\nAsal sekolah: SMA 3\nJurusan: Bisnis\nNama orang tua: Ibu Ani")
    assert f == {"name": "Siti", "grade": "11", "school": "SMA 3", "interested_majors": "Bisnis",
                 "parent_name": "Ibu Ani"}


def test_placeholder_answers_do_not_count_as_filled():
    f = parse_form("Name: Siti\nGrade: 11\nEmail: s@x.com\nParents name: -\nParents email: tidak ada\n"
                   "Parents number: n/a")
    assert set(f) == {"name", "grade", "email"}


def test_prompt_labels_reads_template_in_order():
    assert prompt_labels(PROMPT)[:3] == ["name", "grade", "email"] and len(prompt_labels(PROMPT)) == 10


@pytest.mark.parametrize("text,expected", [
    ("Berapa biaya kuliahnya?", True), ("Halo, mohon info lebih lanjut", True),
    ("How do I apply to the psychology program", True), ("ok", False), ("Thank you kak", False),
    ("see you 🤍", False), ("https://www.instagram.com/p/abc/?xtok=1", False),
    ("Aku ingin bertanya tentang kegiatan besok", True),
])
def test_is_question(text, expected):
    assert is_question(text) is expected


def test_system_and_agent_messages_produce_no_actions():
    msgs = [msg(1, 0, SenderType.AGENT, "hello"), msg(2, 1, SenderType.SYSTEM, "resolved")]
    assert derive_actions(ROOM, msgs, campaign_for_wamid=no_campaign) == []


def test_lead_stage_precedence():
    btn = derive_actions(ROOM, [msg(1, 0, text="Register", button_text="Register")],
                         campaign_for_wamid=no_campaign)
    assert lead_stage(btn, {}, "read") == "clicked"
    assert lead_stage([], {}, "read") == "read"
    assert lead_stage([], {}, "failed") == "failed"
    assert lead_stage([], {}, None) == "no_response"
    full = {f: "x" for f in ("name", "grade", "email", "phone", "school", "school_city")}
    assert lead_stage(btn, full, "read") == "form_completed"
    assert lead_stage(btn, {"name": "x", "grade": "1", "email": "e"}, "read") == "form_started"


def test_merge_forms_keeps_latest_non_empty_value():
    msgs = [msg(1, 0, text="Name: A\nGrade: 10\nEmail: a@x.com"),
            msg(2, 5, text="Name: A B\nGrade: 11\nSchool: SMA 2")]
    merged = merge_forms(derive_actions(ROOM, msgs, campaign_for_wamid=no_campaign))
    assert merged == {"name": "A B", "grade": "11", "email": "a@x.com", "school": "SMA 2"}
