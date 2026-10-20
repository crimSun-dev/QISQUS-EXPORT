"""'Copy diagnostics': everything support needs, nothing that logs into Qiscus."""

from __future__ import annotations

import platform
import re
import sys
from collections.abc import Iterable

from qexport import __version__

REDACTED = "[redacted]"
_SECRET_LINE = re.compile(r"(QISCUS_[A-Z_]*(?:TOKEN|SECRET|PASSWORD)[A-Z_]*\s*[=:]\s*)\S+", re.I)
_AUTH_HEADER = re.compile(r"((?:Authorization|Qiscus-Secret-Key|QISCUS-SDK-TOKEN|X-QX-Token)\s*[:=]\s*)\S+", re.I)
_LONG_NUMBER = re.compile(r"(?<!\d)\+?\d{10,15}(?!\d)")     # phone numbers of students / parents


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove every known secret value, anything shaped like a token assignment or auth header,
    and phone numbers."""
    for s in sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True):
        text = text.replace(s, REDACTED)
    text = _SECRET_LINE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _AUTH_HEADER.sub(lambda m: m.group(1) + REDACTED, text)
    return _LONG_NUMBER.sub("[number]", text)


def build_report(*, state: dict, activity: list[tuple[str, str, str]], history: list[dict],
                 secrets: Iterable[str]) -> str:
    lines = [
        f"qexport {__version__} diagnostics",
        f"Python {sys.version.split()[0]} on {platform.platform()}",
        "",
        "== Data store",
    ]
    store = state.get("store") or {}
    for k in ("exists", "path", "size_mb", "rooms_total", "rooms_done", "rooms_pending", "rooms_failed",
              "rooms_skipped", "messages", "jobs_total", "jobs_done", "jobs_pending", "jobs_failed",
              "jobs_skipped", "broadcast_logs", "completeness", "last_completed"):
        if k in store:
            lines.append(f"{k}: {store[k]}")
    integ = state.get("integrity") or {}
    lines += ["", "== Checks",
              f"integrity: {integ.get('state')} {integ.get('detail', '')}".rstrip(),
              f"login: {(state.get('login') or {}).get('state')}",
              f"blocked: {state.get('blocked') or '-'}",
              f"unclean previous shutdown: {state.get('unclean_shutdown')}",
              "", "== Options"]
    for k, v in (state.get("options") or {}).items():
        lines.append(f"{k}: {v}")
    lines += ["", "== Tasks"]
    for t in state.get("tasks") or []:
        lines.append(f"{t['title']}: {t['state']} {t.get('outcome') or ''} phase={t['phase']!r} "
                     f"{t['done']}/{t['total']} {t.get('message', '')}".rstrip())
    lines += ["", "== Recent history"]
    for h in history[-20:]:
        lines.append(f"{h.get('finished_at')}  {h.get('title')}: {h.get('state')} {h.get('outcome')} "
                     f"{h.get('message', '')}".rstrip())
    lines += ["", "== Activity log"]
    for stamp, who, msg in activity[-150:]:
        lines.append(f"[{stamp}] {who}: {msg}")
    lines += ["", "== Skipped items"]
    for s in (store.get("skipped") or [])[:40]:
        lines.append(s)
    return redact("\n".join(lines), secrets)
