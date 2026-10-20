"""Turn raw conversation history into "what did the recipient do" records.

This is the view the admissions team asked for: not the full transcript, but whether a person
tapped a button, filled the intake form, asked a question, or just replied -- and which
broadcast campaign caused it. Pure functions; no I/O.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from qexport.transform.normalize import MessageRecord, RoomRecord, SenderType


class ActionType(StrEnum):
    BUTTON_CLICKED = "button_clicked"
    FORM_SUBMITTED = "form_submitted"
    QUESTION = "question"
    REPLIED = "replied"
    ATTACHMENT_SENT = "attachment_sent"


class Attribution(StrEnum):
    BUTTON_CONTEXT = "button_context"        # exact: button tap carries the broadcast's wamid
    REPLY_CONTEXT = "reply_context"          # exact: WhatsApp reply quoting the broadcast
    LAST_TOUCH = "last_touch"                # most recent broadcast to this phone within the window
    NONE = "none"


# Canonical intake-form fields, in the order the bot's prompt lists them.
FORM_FIELDS: tuple[str, ...] = (
    "name", "grade", "email", "phone", "school", "school_city",
    "interested_majors", "parent_name", "parent_phone", "parent_email",
)
FORM_FIELD_LABELS: dict[str, str] = {
    "name": "Name", "grade": "Grade", "email": "Email", "phone": "Phone Number",
    "school": "School Name", "school_city": "School City", "interested_majors": "Interested Majors",
    "parent_name": "Parent Name", "parent_phone": "Parent Number", "parent_email": "Parent Email",
}
_ALIASES: dict[str, str] = {}
for _field, _names in {
    "name": ["name", "nama", "full name", "nama lengkap", "student name", "nama siswa"],
    "grade": ["grade", "kelas", "class", "tingkat"],
    "email": ["email", "e-mail", "e mail", "alamat email", "student email"],
    "phone": ["phone number", "phone", "phone no", "no hp", "nomor hp", "no. hp", "nomor telepon",
              "no telp", "nomor whatsapp", "no wa", "whatsapp", "mobile", "nomor"],
    "school": ["school name", "school", "nama sekolah", "sekolah", "asal sekolah", "school origin"],
    "school_city": ["school city", "city", "kota", "kota sekolah", "asal kota", "domisili"],
    "interested_majors": ["interested majors", "interested major", "interested majors/program",
                          "major", "majors", "jurusan", "jurusan yang diminati", "program",
                          "prodi", "program studi", "interest"],
    "parent_name": ["parents name", "parent name", "parents' name", "parent's name", "nama orang tua",
                    "nama ortu", "nama orangtua", "father/mother name", "guardian name"],
    "parent_phone": ["parents number", "parent number", "parents' number", "parent's number",
                     "parents phone", "parent phone", "parents phone number", "parent phone number",
                     "no hp orang tua", "nomor orang tua", "nomor hp orang tua", "no ortu",
                     "nomor ortu", "no hp ortu"],
    "parent_email": ["parents email", "parent email", "parents' email", "parent's email",
                     "email orang tua", "email ortu"],
}.items():
    for _n in _names:
        _ALIASES[_n] = _field

_LABEL_LINE = re.compile(r"^\s*(?:[-*•>\d.)]+\s*)?([A-Za-z][A-Za-z' ./()-]{0,40}?)\s*[:=]\s*(.*?)\s*$")
_WS = re.compile(r"\s+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?:\+?62|0)8\d{7,12}")
FORM_MIN_FIELDS = 3            # a customer message with >= 3 recognized fields counts as a submission
FORM_COMPLETE_RATIO = 0.6      # >= 60 % of fields filled -> lead stage "form_completed"

_QUESTION_WORDS = {
    # Indonesian
    "apa", "apakah", "bagaimana", "gimana", "gmn", "berapa", "brp", "kapan", "dimana", "mana",
    "kenapa", "mengapa", "knp", "siapa", "bisakah", "bolehkah", "adakah", "tanya", "nanya",
    # English
    "how", "what", "when", "where", "why", "who", "which", "can", "could", "is", "are", "do",
    "does", "may", "will", "would", "should",
}
_QUESTION_PHRASES = (
    "mau tanya", "ingin tanya", "boleh tanya", "mau nanya", "bertanya", "mohon info", "minta info", "info dong",
    "informasi mengenai", "informasi tentang", "info tentang", "info mengenai", "i want to ask",
    "i would like to ask", "i'd like to know", "can i ask", "may i ask", "please inform",
)


@dataclass(frozen=True, slots=True)
class CampaignRef:
    job_id: int | None
    name: str


@dataclass(frozen=True, slots=True)
class Touch:
    """One broadcast that reached a phone (from broadcast logs or the room itself)."""
    sent_at_utc: datetime
    campaign: CampaignRef


@dataclass(frozen=True, slots=True)
class ActionRecord:
    msg_id: int
    room_id: str
    customer_identifier: str
    timestamp_utc: datetime
    action_type: ActionType
    detail: str                         # button label / question text / filled field names
    form_fields: dict[str, str] = field(default_factory=dict)
    campaign_job_id: int | None = None
    campaign_name: str | None = None
    attribution: Attribution = Attribution.NONE


# --------------------------------------------------------------------------- form parsing

def _norm_label(label: str) -> str:
    return _WS.sub(" ", label.lower().replace("’", "'").strip(" .-_"))


def match_label(label: str) -> str | None:
    key = _norm_label(label)
    if key in _ALIASES:
        return _ALIASES[key]
    key2 = key.replace("'", "")
    return _ALIASES.get(key2)


def prompt_labels(text: str) -> list[str]:
    """Canonical fields a form *prompt* asks for, in order ("Name:\\nGrade:\\n…" with empty values)."""
    out: list[str] = []
    for line in text.splitlines():
        m = _LABEL_LINE.match(line)
        if m and not m.group(2):
            f = match_label(m.group(1))
            if f and f not in out:
                out.append(f)
    return out if len(out) >= 4 else []


def parse_form(text: str, prompt: Sequence[str] = ()) -> dict[str, str]:
    """Extract intake-form values from a customer message.

    1. Labelled lines ("Name: Budi", "Kelas = 12").
    2. If no labels were found and the conversation's last form prompt is known, map the
       message's non-empty lines to the prompt's fields by position (people often answer
       the template line-by-line without repeating the labels).
    """
    fields: dict[str, str] = {}
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for line in lines:
        m = _LABEL_LINE.match(line)
        if not m:
            continue
        f = match_label(m.group(1))
        value = m.group(2).strip()
        if f and _filled(value) and f not in fields:
            fields[f] = value
    if (not fields and prompt and len(lines) >= min(4, len(prompt))
            and (_EMAIL.search(text) or _PHONE.search(text))):
        for f, line in zip(prompt, lines):
            if _filled(line.strip()):
                fields[f] = line.strip()
    return fields


_PLACEHOLDERS = {"-", "--", "---", ".", "..", "...", "_", "n/a", "na", "none", "nil", "null", "x",
                 "tidak ada", "gak ada", "ga ada", "nggak ada", "belum ada", "tdk ada", "kosong"}


def _filled(value: str) -> bool:
    return bool(value) and value.lower().strip(" .") not in _PLACEHOLDERS and value.strip(" -_.") != ""


# --------------------------------------------------------------------------- classification

_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def is_question(text: str) -> bool:
    t = _URL.sub(" ", text).strip().lower()
    if not t:
        return False
    if "?" in t:
        return True
    if any(p in t for p in _QUESTION_PHRASES):
        return True
    first = re.split(r"[\s,.!]+", t, maxsplit=1)[0]
    return first in _QUESTION_WORDS and len(t.split()) >= 3


def _last_touch(touches: Sequence[Touch], keys: Sequence[datetime], at: datetime,
                window: timedelta) -> Touch | None:
    i = bisect.bisect_right(keys, at)
    if i == 0:
        return None
    t = touches[i - 1]
    return t if at - t.sent_at_utc <= window else None


def derive_actions(
    room: RoomRecord,
    messages: Iterable[MessageRecord],
    *,
    campaign_for_wamid: Callable[[str], CampaignRef | None],
    touches: Sequence[Touch] = (),
    window: timedelta = timedelta(days=7),
) -> list[ActionRecord]:
    """One ActionRecord per customer message (oldest first).

    `messages` must be in ascending time order. `touches` are broadcasts sent to this
    customer's phone (from broadcast logs); in-room broadcast messages are added automatically
    so attribution still works when broadcast logs were not exported.
    """
    msgs = list(messages)
    by_id = {m.msg_id: m for m in msgs}

    all_touches = list(touches)
    for m in msgs:
        if m.sender_type == SenderType.BROADCAST:
            ref = (campaign_for_wamid(m.wamid) if m.wamid else None) or \
                CampaignRef(None, m.broadcast_template or "(broadcast)")
            all_touches.append(Touch(m.timestamp_utc, ref))
    all_touches.sort(key=lambda t: t.sent_at_utc)
    keys = [t.sent_at_utc for t in all_touches]

    out: list[ActionRecord] = []
    current_prompt: list[str] = []
    for m in msgs:
        if m.sender_type != SenderType.CUSTOMER:
            labels = prompt_labels(m.text) if m.text else []
            if labels:
                current_prompt = labels
            continue
        if m.is_deleted:
            continue

        # -- what did they do?
        form: dict[str, str] = {}
        if m.button_text:
            kind, detail = ActionType.BUTTON_CLICKED, m.button_text
        elif m.message_type == "file_attachment" and not m.text:
            kind, detail = ActionType.ATTACHMENT_SENT, m.attachment_name or "attachment"
        else:
            form = parse_form(m.text, current_prompt)
            if len(form) >= FORM_MIN_FIELDS:
                kind = ActionType.FORM_SUBMITTED
                detail = ", ".join(FORM_FIELD_LABELS[f] for f in FORM_FIELDS if f in form)
            else:
                form = {}
                kind = ActionType.QUESTION if is_question(m.text) else ActionType.REPLIED
                detail = m.text

        # -- which campaign caused it?
        ref: CampaignRef | None = None
        how = Attribution.NONE
        if m.context_wamid:
            ref = campaign_for_wamid(m.context_wamid)
            if ref:
                how = Attribution.BUTTON_CONTEXT if m.button_text else Attribution.REPLY_CONTEXT
        if ref is None and m.reply_to_msg_id is not None:
            quoted = by_id.get(m.reply_to_msg_id)
            if quoted is not None and quoted.sender_type == SenderType.BROADCAST:
                ref = (campaign_for_wamid(quoted.wamid) if quoted.wamid else None) or \
                    CampaignRef(None, quoted.broadcast_template or "(broadcast)")
                how = Attribution.BUTTON_CONTEXT if m.button_text else Attribution.REPLY_CONTEXT
        if ref is None:
            t = _last_touch(all_touches, keys, m.timestamp_utc, window)
            if t is not None:
                ref, how = t.campaign, Attribution.LAST_TOUCH

        out.append(ActionRecord(
            msg_id=m.msg_id,
            room_id=m.room_id,
            customer_identifier=room.customer_identifier,
            timestamp_utc=m.timestamp_utc,
            action_type=kind,
            detail=detail,
            form_fields=form,
            campaign_job_id=ref.job_id if ref else None,
            campaign_name=ref.name if ref else None,
            attribution=how,
        ))
    return out


# --------------------------------------------------------------------------- lead stage

LEAD_STAGES: tuple[str, ...] = (
    "no_response", "failed", "sent", "delivered", "read",
    "replied", "question_received", "clicked", "form_started", "form_completed",
)


def lead_stage(actions: Sequence[ActionRecord], merged_form: dict[str, str],
               best_broadcast_status: str | None) -> str:
    """Furthest funnel stage reached (broadcasted -> delivered -> read -> clicked/replied -> form)."""
    if merged_form:
        ratio = len(merged_form) / len(FORM_FIELDS)
        return "form_completed" if ratio >= FORM_COMPLETE_RATIO else "form_started"
    kinds = {a.action_type for a in actions}
    if ActionType.BUTTON_CLICKED in kinds:
        return "clicked"
    if ActionType.QUESTION in kinds:
        return "question_received"
    if kinds:
        return "replied"
    if best_broadcast_status in ("read", "delivered", "sent"):
        return best_broadcast_status
    if best_broadcast_status in ("failed", "failed_after_delivered"):
        return "failed"
    return "no_response"


def merge_forms(actions: Iterable[ActionRecord]) -> dict[str, str]:
    """Latest non-empty value per field across all of a customer's form submissions."""
    merged: dict[str, str] = {}
    for a in actions:
        if a.action_type == ActionType.FORM_SUBMITTED:
            merged.update({k: v for k, v in a.form_fields.items() if v})
    return merged
