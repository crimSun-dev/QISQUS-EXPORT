# Technical Design Document — Qiscus Chat Exporter ("qexport")

**Project type:** CLI tool / data pipeline (ETL: Qiscus API → local checkpoint store → single `.xlsx`)
**Target tenant:** Qiscus Omnichannel app `125393` — "Sampoerna University" WhatsApp hotline (channel id `4995`)
**Date:** 2026-10-20
**Status:** Draft v1. Ready for Phase 1.

---

### Source context: findings from the live session analysis (2026-10-20)

Everything below was verified against the live, logged-in Qiscus dashboard through Chrome MCP. Read-only calls were made from inside the page.

| Finding | Evidence | Design consequence |
|---|---|---|
| Dashboard is a Vue 2 SPA on `multichannel.qiscus.com`; data comes from two APIs | Resource timing: `multichannel-api.qiscus.com` (Omnichannel) + `api3.qiscus.com` (Chat SDK) | The bot talks to the APIs directly. No DOM scraping. |
| Room list = `POST multichannel-api.qiscus.com/api/v2/customer_rooms` | 200 OK; body `{limit, cursor_after}`; response `data.customer_rooms[]`, `meta.cursor_after/cursor_before` | Cursor pagination, newest → oldest by `last_comment_timestamp` |
| Room list auth headers: `Authorization: <admin authentication_token>`, `Qiscus-App-Id: <app_code>` | Verified 200 | Admin token required |
| Messages = `GET api3.qiscus.com/api/v2/sdk/load_comments?topic_id=<room_id>&limit=100[&last_comment_id=<id>]` | 200 OK, `results.comments[]` newest-first | Paginate backwards until a page holds fewer than 100 rows |
| Message auth headers: `QISCUS-SDK-APP-ID`, `QISCUS-SDK-TOKEN`, `QISCUS-SDK-USER-ID` (the admin's SDK user) | Verified 200 on 30/30 sampled rooms, 0 denied | Admin SDK user can read every customer room |
| Logged-in account is `type_as_string: "admin"`, email-OTP 2FA enabled | `qismo-data.user` | Password login can't be scripted unattended, so the tool uses token-based auth (§1.4) |
| **6,826 rooms** total, oldest activity 2024-05-13, 4,780 resolved | Full throttled cursor walk, 69 pages × 100 | Sizing baseline |
| ~11.2 msgs/room avg (median 8, max 40 in sample) → **~76k messages** | 30-room stratified sample | Fits one sheet (Excel limit 1,048,576 rows) |
| Message `type`s seen: `text` (68%), `system_event` (16%), `reply` (9%), `file_attachment` (6%) | Sample | Normalizer must handle all four plus a fallback |
| **HTTP 429** after ~20 unthrottled room-list pages (~6 req/s); no `Retry-After`/rate headers exposed | Observed | Client-side rate limiter + blind exponential backoff |
| At 700 ms/page the room list hit only 2× 429s over 69 pages; `load_comments` at 250 ms spacing hit 0 | Observed | Default rates: room list 1.2 req/s, messages 3 req/s |
| Native export is only per-conversation email (`/export/conversations/send_history`) | Inline JS | Confirms the gap: there is no bulk export |

---

## TDD SECTION 1 — SYSTEM ARCHITECTURE OVERVIEW

### 1.1 Architectural Pattern

**Layered ETL pipeline with a durable checkpoint store (Ports & Adapters at the edges).**

- **Extract** adapters (`QiscusClient`, `RoomEnumerator`, `MessageFetcher`) talk HTTP.
- **Transform** (`Normalizer`) is a pure function from raw JSON to typed records.
- **Load** happens in two stages: records first go to a local **SQLite checkpoint store**, then `ExcelWriter` streams them into one `.xlsx`.

Why this pattern and not the alternatives:

- **vs. a browser bookmarklet / userscript that builds the xlsx in-page.** A full run is ~7,000 API calls and about an hour under the observed rate limits. A browser tab can be closed, can sleep, or can lose its session mid-run, and in-memory state then vanishes. The checkpoint store makes the job resumable. That is the decisive requirement for an "export ALL" job.
- **vs. fetching straight into the workbook.** openpyxl's write-only mode can't append to an existing file. So "write as you go" would mean either holding all 76k rows in memory or losing them on a crash. With SQLite as the intermediate store, the run can resume, can be de-duplicated, and the Excel step can be re-run (for example with a different layout) without hitting the API again.
- **vs. Playwright/DOM scraping.** The SPA already exposes clean JSON APIs, so scraping virtualized chat panes would be slower, brittle, and lossy (truncated previews, lazy-loaded history).
- **vs. async/concurrent fetching.** The binding limit is the server's rate limit, not latency. One synchronous worker behind a token-bucket limiter already saturates the safe rate. Concurrency would add 429 storms and ordering complexity for zero throughput gain.

### 1.2 High-Level Component Diagram (ASCII)

```
                         ┌──────────────────────────────┐
   .env / env vars ─────▶│  Config + AuthProvider       │
   (tokens, app code)    │  (pydantic-settings)         │
                         └──────────────┬───────────────┘
                                        │ Credentials
                                        ▼
┌─────────────┐  commands  ┌──────────────────────────────┐
│  CLI (Typer)│───────────▶│        ExportOrchestrator    │
│ auth-check  │◀───────────│  (phases: enumerate → fetch  │
│ export      │  progress  │   → write; resume-aware)     │
│ resume      │  (rich)    └───┬─────────┬─────────┬──────┘
│ status      │                │         │         │
│ write-xlsx  │                ▼         ▼         ▼
└─────────────┘   ┌──────────────┐ ┌───────────┐ ┌──────────────┐
                  │RoomEnumerator│ │MessageFet-│ │ ExcelWriter  │
                  │ cursor walk  │ │cher (per  │ │ (openpyxl    │
                  │ customer_    │ │room, back-│ │  write-only) │
                  │ rooms        │ │ward pages)│ └──────▲───────┘
                  └──────┬───────┘ └─────┬─────┘        │ streams rows
                         │               │              │
                         ▼               ▼              │
                  ┌───────────────────────────────┐     │
                  │  QiscusClient (httpx)         │     │
                  │  ├─ RateLimiter (token bucket)│     │
                  │  └─ RetryPolicy (tenacity)    │     │
                  └───────┬───────────────┬───────┘     │
                          │ HTTPS         │ HTTPS       │
                          ▼               ▼             │
          multichannel-api.qiscus.com   api3.qiscus.com │
          POST /api/v2/customer_rooms   GET /api/v2/sdk/load_comments
                                                        │
          raw JSON ──▶ ┌──────────────┐  typed records  │
                       │  Normalizer  │───────┐         │
                       │  (pure)      │       ▼         │
                       └──────────────┘ ┌──────────────────────┐
                                        │ CheckpointStore      │
                                        │ (SQLite, WAL)        │
                                        │ rooms │ messages │ run│
                                        └──────────────────────┘
                                                   │
                                                   ▼
                                     qiscus_export_<app>_<YYYYMMDD-HHMM>.xlsx
                                     ├─ Messages       (1 row / message)
                                     ├─ Conversations  (1 row / room)
                                     └─ Export Info    (run metadata, counts)
```

### 1.3 Data Flow Topology

1. **Ingress: credentials.** `Config` loads `QISCUS_APP_CODE`, `QISCUS_ADMIN_TOKEN`, `QISCUS_SDK_USER_ID` and `QISCUS_SDK_TOKEN` from `.env` or the environment. `auth-check` makes one call to each API to prove both credential pairs work before any long run starts.
2. **Extract stage A: enumerate (Extract layer).** `RoomEnumerator` walks `POST /api/v2/customer_rooms` with `limit=100`, following `meta.cursor_after` until a page is empty or the cursor stops changing. Every room is **upserted** into `rooms` with `fetch_state='pending'`, and the cursor is saved to `run.room_cursor` after each page, so an interrupted enumeration resumes from the last cursor.
3. **Extract stage B: fetch (Extract → Transform).** For each `rooms` row where `fetch_state IN ('pending','failed')`, `MessageFetcher` pages `load_comments` backwards via `last_comment_id`. `Normalizer` turns each raw comment into a `MessageRecord`. All of a room's records are written **in one SQLite transaction together with** `fetch_state='done'`. A crash mid-room therefore leaves the room `pending`, never half-written.
4. **Load stage: write (Load layer).** `ExcelWriter` streams `SELECT … ORDER BY room_last_activity DESC, room_id, msg_timestamp ASC, msg_id ASC` from SQLite into a write-only workbook. Memory stays flat no matter how many rows there are. It also builds the `Conversations` sheet (one aggregate row per room) and `Export Info`.
5. **Egress.** A single `.xlsx` file goes to `--out` (default `./exports/`). The SQLite file stays alongside it for audit and incremental re-runs.

### 1.4 State Management Contract

- **Single source of truth:** the SQLite database `exports/qexport_<app_code>.sqlite3`, opened in WAL mode.
- **Owner and only mutator:** `CheckpointStore`. No other component issues SQL. The orchestrator moves room state only through `mark_room_done()`, `mark_room_failed()`, `save_room_cursor()`, etc.
- **State machine per room:** `pending → done` or `pending → failed(attempts++) → pending (on resume) → done`. A room that fails `max_room_attempts` (default 5) times becomes `skipped` and is listed in `Export Info`.
- **Propagation:** synchronous. The orchestrator reads counts from `CheckpointStore.progress()` and pushes them to a `rich.progress` bar. There are no events or pub/sub, because nothing else consumes them.
- **Incremental runs:** the latest `last_comment_timestamp` seen per room is stored. Running `export --since-last-run` re-enumerates the room list and resets to `pending` only the rooms whose `last_comment_timestamp` changed. Their messages are replaced, keyed on `msg_id` (`INSERT OR REPLACE`).
- **Stated assumptions:**
  - A1. The admin `authentication_token` stays valid for the whole run. It is the dashboard session token, and its expiry is unknown. On a 401, the tool stops cleanly with a resumable checkpoint (§5.1).
  - A2. The admin SDK user can read every customer room. This was verified on 30/30 sampled rooms; per-room 403s go to `failed` and don't abort the run.
  - A3. Scope is the single WhatsApp channel currently present (`source: wa` on all 6,826 rooms). Other channels are normalized generically through the `source` column.

---

## TDD SECTION 2 — TECHNOLOGY STACK & DEPENDENCY MATRIX

| Layer | Technology | Version | Justification |
|---|---|---|---|
| Language/runtime | CPython | 3.11.x+ (3.11.8 installed locally) | Already on the operator's Windows machine; stdlib `sqlite3` + `zoneinfo` |
| HTTP client | httpx | 0.28.1 | Sync client with connection pooling, timeouts, and first-class mocking via respx |
| Retry/backoff | tenacity | 9.2.1 | Declarative exponential backoff + jitter for 429/5xx; keeps retry logic out of business code |
| Config | pydantic-settings | 2.16.0 | Typed `.env` loading with validation; `SecretStr` keeps tokens out of logs and reprs |
| Data models | pydantic | 2.14.1 | Validates raw API payloads at the boundary; schema drift fails loudly |
| Checkpoint store | SQLite (stdlib `sqlite3`) | bundled with Python 3.11 (SQLite ≥ 3.40) | Zero-install, transactional, resumable; ~76k rows is trivial |
| Excel output | openpyxl | 3.1.5 | `write_only=True` streams rows at constant memory; supports column widths, freeze panes, autofilter |
| Timezones | tzdata | 2025.x (latest) | **Required on Windows**: `zoneinfo` has no system tz database there, so `Asia/Jakarta` raises without it |
| CLI | Typer | 0.27.3 | Typed subcommands with little boilerplate |
| Terminal UX | rich | 15.0.0 | Progress bars, ETA, error panels (already a Typer dependency) |
| Testing | pytest | 9.1.1 | Standard runner |
| HTTP mocking | respx | 0.23.1 | Mocks httpx transports; replays recorded Qiscus fixtures |
| Packaging | `pyproject.toml` + `pip install -e .` (optional PyInstaller 6.x for a single `.exe`) | n/a | Non-developers at the university can run a single `qexport.exe` |

**Dependency risk assessment:**
- **Undocumented Qiscus endpoints (HIGH).** `api/v2/customer_rooms` (POST) and `sdk/load_comments` are what the dashboard itself calls. They are not guaranteed stable public contracts, and Qiscus is visibly migrating ("A new Inbox experience is available"). Mitigation: all endpoint paths and field names live in one module (`qexport/api/endpoints.py`), and pydantic models fail fast on schema drift. Recorded JSON fixtures make breakages show up in CI.
- **Rate limits are undocumented (MEDIUM).** Thresholds were observed empirically. Mitigation: the rates can be set from the CLI, and the backoff handles them adaptively.
- **openpyxl formula interpretation (MEDIUM).** Any string starting with `=` is written as a formula. Customer text like `=HYPERLINK(...)` would become a live formula. Mitigation in §3 (`ExcelWriter`) and §5.
- No licensing concerns: everything is MIT/BSD/Apache.

---

## TDD SECTION 3 — COMPONENT INTERFACE SPECIFICATIONS

Package layout:

```
qexport/
  __init__.py
  cli.py              # Typer app
  config.py           # Settings, Credentials
  api/endpoints.py    # URL + header constants (single point of change)
  api/client.py       # QiscusClient, RateLimiter, errors
  api/models.py       # Raw pydantic models (RawRoom, RawComment)
  extract/rooms.py    # RoomEnumerator
  extract/messages.py # MessageFetcher
  transform/normalize.py
  store/checkpoint.py # CheckpointStore + schema.sql
  load/excel.py       # ExcelWriter
  orchestrator.py
tests/
  fixtures/*.json     # recorded, PII-scrubbed API responses
```

**Interface: Config / AuthProvider** (`qexport/config.py`)
```python
from pathlib import Path
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QISCUS_", env_file=".env", extra="ignore")

    app_code: str                       # e.g. "enckp-1dh7o4rkopdtmh5"
    admin_token: SecretStr              # qismo-data.user.authentication_token
    sdk_user_id: str                    # qismo-data.details.sdk_user.id
    sdk_token: SecretStr                # qismo-data.details.sdk_user.token
    omni_base_url: str = "https://multichannel-api.qiscus.com"
    sdk_base_url: str = "https://api3.qiscus.com"
    rooms_rps: float = 1.2              # observed-safe: ≤1.4 req/s
    messages_rps: float = 3.0           # observed-safe: 4 req/s with 0×429
    page_limit: int = 100
    max_room_attempts: int = 5
    request_timeout_s: float = 30.0
    display_tz: str = "Asia/Jakarta"
    out_dir: Path = Path("exports")

def load_settings(env_file: Path | None = None) -> Settings:
    """Raises pydantic.ValidationError listing every missing/invalid variable."""
```

**Interface: QiscusClient** (`qexport/api/client.py`)
```python
import httpx
from collections.abc import Callable
from typing import Literal, Protocol

class QiscusError(Exception): ...
class AuthError(QiscusError):               # 401/403 on a *global* call → abort run
    def __init__(self, api: Literal["omni", "sdk"], status: int) -> None: ...
class RoomAccessError(QiscusError):         # 403/404 on a single room → mark room failed
    def __init__(self, room_id: str, status: int) -> None: ...
class RateLimitExhausted(QiscusError): ...  # 429 persisted past max retries
class SchemaDriftError(QiscusError):        # response JSON failed pydantic validation
    def __init__(self, endpoint: str, detail: str) -> None: ...

class Clock(Protocol):
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...

class RateLimiter:
    """Token bucket. capacity=1 → strict spacing; never bursts."""
    def __init__(self, rate_per_s: float, clock: Clock) -> None: ...
    def acquire(self) -> None: ...                    # blocks until a token is available
    def penalize(self, factor: float = 0.5, floor_rps: float = 0.2) -> None: ...  # called on 429
    def recover(self, step: float = 1.05) -> None: ...  # called on success; caps at initial rate
    @property
    def current_rate(self) -> float: ...

class QiscusClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None,
                 clock: Clock | None = None) -> None: ...

    def list_customer_rooms(self, cursor_after: str | None, limit: int = 100
                            ) -> tuple[list["RawRoom"], str | None]:
        """POST /api/v2/customer_rooms. Returns (rooms, next_cursor | None).
        Retries 429/5xx/timeouts with exponential backoff (base 2s, max 60s, jitter, 8 attempts).
        Raises AuthError, RateLimitExhausted, SchemaDriftError."""

    def load_comments(self, room_id: str, last_comment_id: int | None, limit: int = 100
                      ) -> list["RawComment"]:
        """GET /api/v2/sdk/load_comments?topic_id=…  Newest-first.
        Raises RoomAccessError (per-room 403/404), AuthError (401), RateLimitExhausted, SchemaDriftError."""

    def ping(self) -> dict[Literal["omni", "sdk"], bool]:
        """One cheap call per API, used by `auth-check`. Never raises; returns per-API status."""

    def close(self) -> None: ...
```

**Interface: Raw API models** (`qexport/api/models.py`)
```python
from datetime import datetime
from pydantic import BaseModel, ConfigDict

class RawRoom(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int                      # customer_room id (447099445)
    room_id: str                 # SDK topic id ("491314784"), used for load_comments
    channel_id: int
    contact_id: int | None = None
    name: str | None = None
    user_id: str                 # customer phone / identifier
    source: str                  # "wa", …
    room_type: str               # "individual" | group
    is_resolved: bool
    is_handled_by_bot: bool
    is_waiting: bool | None = None
    last_comment_timestamp: datetime | None = None
    last_customer_timestamp: datetime | None = None

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
    payload: dict = {}
    extras: dict = {}
    user_extras: dict | None = None
    is_deleted: bool = False
    status: str | None = None    # "read" | "delivered" | "sent"
```

**Interface: RoomEnumerator** (`qexport/extract/rooms.py`)
```python
from collections.abc import Iterator

class RoomEnumerator:
    def __init__(self, client: QiscusClient, store: "CheckpointStore") -> None: ...

    def run(self, on_page: Callable[[int, int], None] | None = None) -> int:
        """Walks cursor pages starting at store.get_room_cursor(), upserting each page via
        store.upsert_rooms() then store.save_room_cursor(next) in the same transaction.
        Terminates when a page is empty OR next_cursor is None OR next_cursor == current.
        Calls on_page(page_no, rooms_so_far). Returns total distinct rooms in store.
        Idempotent: re-running after completion performs one confirmation call and returns."""
```

**Interface: MessageFetcher** (`qexport/extract/messages.py`)
```python
class MessageFetcher:
    MAX_PAGES_PER_ROOM: int = 500   # guards against a cursor loop (50k msgs/room)

    def __init__(self, client: QiscusClient, normalizer: "Normalizer") -> None: ...

    def fetch_room(self, room: "RoomRecord") -> list["MessageRecord"]:
        """Pages backwards via last_comment_id = oldest id of previous page, until a page
        returns < limit rows. De-duplicates by msg_id. Returns records sorted ascending
        by (timestamp, msg_id). Raises RoomAccessError / AuthError / RateLimitExhausted;
        raises CursorLoopError if MAX_PAGES_PER_ROOM is exceeded or an id repeats."""

class CursorLoopError(QiscusError): ...
```

**Interface: Normalizer** (`qexport/transform/normalize.py`)
```python
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

class SenderType(StrEnum):
    CUSTOMER = "customer"; AGENT = "agent"; BOT = "bot"; SYSTEM = "system"

@dataclass(frozen=True, slots=True)
class RoomRecord:
    room_id: str; customer_room_id: int; channel_id: int; source: str
    customer_name: str; customer_identifier: str; room_type: str
    is_resolved: bool; is_handled_by_bot: bool
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

class Normalizer:
    def __init__(self, display_tz: str) -> None: ...   # raises ZoneInfoNotFoundError early
    def room(self, raw: RawRoom) -> RoomRecord: ...
    def message(self, raw: RawComment, room: RoomRecord) -> MessageRecord:
        """sender_type rules, in order:
           1. raw.type == "system_event"                        → SYSTEM
           2. raw.email == room.customer_identifier             → CUSTOMER
           3. extras/user_extras mark bot, or email == bot id   → BOT
           4. otherwise                                         → AGENT
        file_attachment: url/name/caption from payload; falls back to parsing "[file] <url> [/file]".
        reply: reply_to_text from payload.replied_comment_message.
        Unknown types: text = raw.message, message_type preserved verbatim. Never raises on content."""
    def to_local(self, ts_utc: datetime) -> datetime: ...  # tz-naive local datetime for Excel
```

**Interface: CheckpointStore** (`qexport/store/checkpoint.py` + DDL)
```sql
-- schema.sql (PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA user_version=1)
CREATE TABLE IF NOT EXISTS run (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  app_code TEXT NOT NULL,
  started_at_utc TEXT NOT NULL,
  room_cursor TEXT,                 -- NULL = not started; '__DONE__' = enumeration complete
  enumeration_done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rooms (
  room_id TEXT PRIMARY KEY,
  customer_room_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  source TEXT NOT NULL,
  customer_name TEXT NOT NULL,
  customer_identifier TEXT NOT NULL,
  room_type TEXT NOT NULL,
  is_resolved INTEGER NOT NULL,
  is_handled_by_bot INTEGER NOT NULL,
  last_activity_utc TEXT,
  fetched_last_activity_utc TEXT,   -- value at time of last successful fetch (incremental runs)
  fetch_state TEXT NOT NULL DEFAULT 'pending'
     CHECK (fetch_state IN ('pending','done','failed','skipped')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  msg_id INTEGER PRIMARY KEY,
  room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
  timestamp_utc TEXT NOT NULL,
  sender_type TEXT NOT NULL,
  sender_id TEXT NOT NULL,
  sender_name TEXT NOT NULL,
  message_type TEXT NOT NULL,
  text TEXT NOT NULL,
  attachment_url TEXT, attachment_name TEXT, reply_to_text TEXT,
  is_deleted INTEGER NOT NULL, delivery_status TEXT
);
CREATE INDEX IF NOT EXISTS ix_messages_room_ts ON messages(room_id, timestamp_utc, msg_id);
CREATE INDEX IF NOT EXISTS ix_rooms_state ON rooms(fetch_state);
```
```python
from collections.abc import Iterator
from dataclasses import dataclass

@dataclass(frozen=True)
class Progress:
    rooms_total: int; done: int; pending: int; failed: int; skipped: int; messages: int
    enumeration_done: bool

class StoreAppMismatchError(Exception): ...   # db belongs to another app_code

class CheckpointStore:
    def __init__(self, path: Path, app_code: str) -> None: ...   # creates schema; raises StoreAppMismatchError
    def get_room_cursor(self) -> str | None: ...
    def upsert_rooms_page(self, rooms: list[RoomRecord], next_cursor: str | None) -> None:
        """Atomic: upsert rooms + advance cursor. A room whose last_activity_utc changed
        vs fetched_last_activity_utc is reset to 'pending'."""
    def mark_enumeration_done(self) -> None: ...
    def iter_rooms_to_fetch(self) -> Iterator[RoomRecord]:   # pending + failed(attempts < max)
        ...
    def commit_room(self, room_id: str, messages: list[MessageRecord],
                    fetched_last_activity_utc: str | None) -> None:
        """Atomic: DELETE old msgs for room, INSERT new, set fetch_state='done'."""
    def mark_room_failed(self, room_id: str, error: str, max_attempts: int) -> None:
        """attempts += 1; state = 'skipped' if attempts >= max_attempts else 'failed'."""
    def reset_failed(self) -> int: ...                      # for `resume --retry-skipped`
    def progress(self) -> Progress: ...
    def iter_messages_for_export(self) -> Iterator[tuple[RoomRecord, MessageRecord]]:
        """ORDER BY rooms.last_activity_utc DESC, room_id, timestamp_utc, msg_id. Server-side cursor."""
    def iter_room_summaries(self) -> Iterator["RoomSummary"]: ...
    def close(self) -> None: ...
```

**Interface: ExcelWriter** (`qexport/load/excel.py`)
```python
@dataclass(frozen=True)
class RoomSummary:
    room: RoomRecord
    message_count: int; customer_msgs: int; agent_msgs: int; bot_msgs: int
    first_msg_utc: datetime | None; last_msg_utc: datetime | None
    fetch_state: str

MESSAGES_COLUMNS: tuple[str, ...] = (
    "Room ID", "Customer Name", "Customer Phone/ID", "Channel", "Conversation Status",
    "Timestamp (WIB)", "Timestamp (UTC)", "Sender Type", "Sender Name", "Sender ID",
    "Message Type", "Message", "Reply To", "Attachment Name", "Attachment URL",
    "Deleted", "Delivery Status", "Message ID",
)
CONVERSATIONS_COLUMNS: tuple[str, ...] = (
    "Room ID", "Customer Name", "Customer Phone/ID", "Channel", "Resolved", "Bot Handled",
    "First Message (WIB)", "Last Message (WIB)", "Total Messages", "Customer Msgs",
    "Agent Msgs", "Bot Msgs", "Export State",
)

EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_CELL_CHARS = 32_767

class ExcelWriter:
    def __init__(self, normalizer: Normalizer) -> None: ...

    def write(self, store: CheckpointStore, out_path: Path, *,
              on_row: Callable[[int], None] | None = None) -> "ExportReport":
        """openpyxl Workbook(write_only=True). Sheets: Messages, Conversations, Export Info.
        Every string cell passes through sanitize_cell(). Phone/ID columns are written as
        explicit text (number_format '@'). Header row frozen; autofilter on all columns.
        Rows beyond EXCEL_MAX_ROWS-1 roll over to 'Messages (2)', 'Messages (3)', ….
        Writes to <out_path>.partial then os.replace() → out_path (atomic)."""

def sanitize_cell(value: str) -> tuple[str, bool]:
    """Strip XML-illegal control chars (openpyxl ILLEGAL_CHARACTERS_RE); neutralize
    formula/DDE injection by prefixing "'" when value starts with = + - @ \\t \\r;
    truncate to EXCEL_MAX_CELL_CHARS with '…[truncated]' suffix.
    Returns (clean_value, was_truncated)."""

@dataclass(frozen=True)
class ExportReport:
    path: Path; rooms: int; messages: int; truncated_cells: int; skipped_rooms: list[str]
    sheets: list[str]; duration_s: float
```

**Interface: ExportOrchestrator** (`qexport/orchestrator.py`)
```python
class ExportOrchestrator:
    def __init__(self, settings: Settings, client: QiscusClient, store: CheckpointStore,
                 enumerator: RoomEnumerator, fetcher: MessageFetcher, writer: ExcelWriter,
                 progress: "ProgressSink") -> None: ...

    def run(self, *, since_last_run: bool = False, write_xlsx: bool = True,
            limit_rooms: int | None = None) -> ExportReport | None:
        """1) enumerate (skip if done unless since_last_run) 2) fetch every room
        3) write xlsx. Per-room RoomAccessError/CursorLoopError/SchemaDriftError →
        mark_room_failed and continue. AuthError/RateLimitExhausted → flush, exit
        with resumable state. KeyboardInterrupt → finish current room's transaction,
        then exit 130."""

class ProgressSink(Protocol):
    def phase(self, name: str, total: int | None) -> None: ...
    def advance(self, n: int = 1, **info: object) -> None: ...
    def warn(self, msg: str) -> None: ...
```

**Interface: CLI** (`qexport/cli.py`, entry point `qexport`)
```
qexport auth-check                          -> exit 0 both APIs OK | 2 auth failure
qexport export [--out DIR] [--since-last-run] [--limit-rooms N]
               [--rooms-rps F] [--messages-rps F] [--no-xlsx]
                                            -> exit 0 complete | 3 partial (skipped rooms)
                                               | 2 auth expired (resumable) | 130 interrupted
qexport resume [--retry-skipped]            -> same codes as export
qexport status                              -> prints Progress table; exit 0
qexport write-xlsx [--out PATH]             -> rebuilds xlsx from SQLite only, no network; exit 0
```

**Credential acquisition (operator procedure, documented in README):** Log into the dashboard, open DevTools → Console, and run the snippet below. It prints the four values for `.env` and is never stored by the tool itself:
```js
(() => { const d = JSON.parse(localStorage.getItem('qismo-data'));
  console.log(`QISCUS_APP_CODE=${d.user.app.app_code}\nQISCUS_ADMIN_TOKEN=${d.user.authentication_token}\nQISCUS_SDK_USER_ID=${d.details.sdk_user.id}\nQISCUS_SDK_TOKEN=${d.details.sdk_user.token}`); })()
```

---

## TDD SECTION 4 — IMPLEMENTATION PHASES & TEST SUITE STRATEGY

> Phase 3 is a CLI presentation layer, not a GUI. It is kept because the Excel layout *is* the user-facing product.

**Phase 1 — Core Engine: client, models, normalizer, sanitizer (no disk, no CLI)**
- Scope: `config.py`, `api/*`, `transform/normalize.py`, `load/excel.sanitize_cell`. All network calls go through respx with recorded, PII-scrubbed fixtures captured from the live tenant.
- Deliverable: `pytest tests/unit` is green, and `python -m qexport.api.client --smoke` prints the first page of rooms plus one room's messages from the live API.
- Unit tests (write first):
  1. `test_settings_missing_admin_token_raises_validation_error`: loading with no `QISCUS_ADMIN_TOKEN` raises `ValidationError` that names the field.
  2. `test_settings_repr_never_contains_token_value`: `repr(settings)` and `str(settings)` don't contain the token string.
  3. `test_rate_limiter_spaces_calls_at_configured_rate`: with a fake clock and rate 2/s, 5 `acquire()` calls advance the clock by ≥ 2.0 s.
  4. `test_rate_limiter_penalize_halves_rate_and_respects_floor`: `penalize()` from 1.2 gives 0.6; repeated calls never go below 0.2.
  5. `test_list_customer_rooms_sends_auth_and_app_id_headers`: the respx route asserts the `Authorization` and `Qiscus-App-Id` headers and a JSON body `{"limit":100}`.
  6. `test_list_customer_rooms_returns_next_cursor_from_meta`: fixture meta `cursor_after="2026-10-20T05:31:00Z"` is returned as the next cursor.
  7. `test_list_customer_rooms_retries_429_then_succeeds`: route returns 429, 429, 200, and the call succeeds after 3 requests and calls `penalize` twice.
  8. `test_list_customer_rooms_raises_rate_limit_exhausted_after_max_attempts`: 8× 429 raises `RateLimitExhausted`.
  9. `test_list_customer_rooms_401_raises_auth_error_without_retry`: exactly 1 request is made.
  10. `test_load_comments_403_raises_room_access_error_with_room_id`.
  11. `test_load_comments_passes_last_comment_id_when_given`: the query string contains `last_comment_id=4766464924`.
  12. `test_raw_comment_missing_timestamp_raises_schema_drift_error`.
  13. `test_normalize_customer_message_sender_type_customer`: `email == room.customer_identifier` gives `CUSTOMER`.
  14. `test_normalize_system_event_sender_type_system`.
  15. `test_normalize_file_attachment_extracts_url_name_and_caption`.
  16. `test_normalize_file_attachment_falls_back_to_bracket_file_syntax`: `"[file] https://x/y.pdf [/file]"` gives `attachment_url="https://x/y.pdf"`.
  17. `test_normalize_reply_populates_reply_to_text`.
  18. `test_normalize_unknown_type_preserves_type_and_message`: `type="carousel"` doesn't raise and keeps its raw text.
  19. `test_to_local_converts_utc_to_wib_plus_7`: `2026-10-20T06:05:08Z` becomes `2026-10-20 13:05:08` (tz-naive).
  20. `test_normalizer_invalid_timezone_fails_at_construction`.
  21. `test_sanitize_cell_prefixes_formula_leading_equals`: `"=HYPERLINK(\"x\")"` becomes `"'=HYPERLINK(\"x\")"`.
  22. `test_sanitize_cell_neutralizes_plus_minus_at_prefixes`: also covers `-` used as a bullet, like `"- item"`. Over-escaping is acceptable; under-escaping is not.
  23. `test_sanitize_cell_strips_illegal_control_characters`: `"a\x0bb"` becomes `"ab"`.
  24. `test_sanitize_cell_truncates_over_32767_chars_and_flags`.
  25. `test_sanitize_cell_preserves_emoji_and_indonesian_text`: `"see you 🤍"` and `"pake bahasa Indonesia aj"` come back unchanged.

**Phase 2 — State & Data Pipelines: CheckpointStore, RoomEnumerator, MessageFetcher, Orchestrator**
- Scope: SQLite schema, transactional commits, resumability, incremental runs. The orchestrator runs headless with a no-op `ProgressSink`.
- Deliverable: `qexport export --no-xlsx --limit-rooms 50` against the live tenant fills SQLite. Killing it mid-run and re-running `resume` ends with exactly 50 `done` rooms and no duplicate `msg_id`.
- Integration tests:
  1. `test_store_rejects_db_created_for_different_app_code`
  2. `test_upsert_rooms_page_advances_cursor_atomically`: an exception injected after the upsert leaves both rooms and cursor unchanged.
  3. `test_enumerator_resumes_from_saved_cursor`: a pre-seeded cursor makes the first request carry `cursor_after=<seeded>`.
  4. `test_enumerator_stops_when_cursor_repeats`: the fixture returns the same `cursor_after` twice, and the walk terminates with no infinite loop.
  5. `test_enumerator_stops_on_empty_page`
  6. `test_enumerator_deduplicates_room_seen_on_two_pages`: a room whose activity bumps it across a page boundary during the walk is stored once.
  7. `test_fetcher_pages_backwards_until_short_page`: fixtures of 100 + 100 + 37 rows give 237 records, ascending, and 3 requests.
  8. `test_fetcher_raises_cursor_loop_error_on_repeated_oldest_id`
  9. `test_commit_room_replaces_messages_and_marks_done_in_one_transaction`
  10. `test_crash_mid_room_leaves_room_pending_and_no_partial_messages`
  11. `test_room_access_error_marks_failed_and_run_continues`: room 3 of 5 returns 403, giving 4 done and 1 failed.
  12. `test_room_failing_max_attempts_becomes_skipped`
  13. `test_auth_error_mid_fetch_exits_with_code_2_and_state_resumable`: after the 401, `resume` with a fresh token completes the remaining rooms.
  14. `test_since_last_run_refetches_only_rooms_with_new_activity`: of 10 done rooms, 2 have a changed `last_activity`, so exactly 2 `load_comments` routes are hit.
  15. `test_keyboard_interrupt_finishes_current_room_then_exits_130`

**Phase 3 — Presentation Layer: ExcelWriter + CLI UX**
- Scope: workbook layout, formatting, rollover, atomic write, the Typer commands, and `rich` progress with ETA.
- Deliverable: `qexport export` against the live tenant produces `exports/qiscus_export_enckp-1dh7o4rkopdtmh5_<ts>.xlsx` that opens cleanly in Excel 365 and LibreOffice with no repair prompt.
- Validation criteria:
  1. *User-facing:* filtering the Messages sheet by `Customer Phone/ID = <a customer phone>` shows that conversation in order, oldest at the top, matching the dashboard pane message for message (spot-check 10 random rooms).
  2. *User-facing:* the Conversations sheet has exactly one row per room (6,826 ± rooms created during the run), and `Total Messages` equals the count of that room's rows in Messages.
  3. *User-facing:* phone numbers show in full (e.g. `628123456789`), not as `6.28158E+12`.
  4. *User-facing:* the Export Info sheet lists the run start/end, app code, room/message totals, skipped rooms with reasons, the truncated-cell count, and the tool version.
  5. *Performance:* `write-xlsx` for 76k messages finishes in < 60 s with peak RSS < 300 MB.
  6. *Performance:* a full live export (enumerate + fetch) finishes in < 90 min at default rates (estimate: 69 room pages ≈ 1 min plus ~7k message calls ≈ 40 min).
  7. *Edge case:* with a synthetic store of 1,100,000 messages, `write-xlsx` creates `Messages` + `Messages (2)` and both sheets carry headers.
  8. *Edge case:* if the target `.xlsx` is open in Excel (locked on Windows), the tool writes the `.partial` file, reports `PermissionError`, and tells the user to close the file and run `write-xlsx`. The SQLite data is not lost.
- Tests:
  1. `test_excel_writer_creates_three_sheets_with_exact_headers`
  2. `test_excel_writer_phone_column_is_text_format`
  3. `test_excel_writer_rows_ordered_by_room_then_timestamp`
  4. `test_excel_writer_rolls_over_at_excel_row_limit` (patch `EXCEL_MAX_ROWS=10`)
  5. `test_excel_writer_atomic_replace_leaves_no_partial_on_success`
  6. `test_excel_writer_output_round_trips_through_openpyxl_load_workbook`
  7. `test_cli_auth_check_exit_code_2_on_401`
  8. `test_cli_status_prints_progress_counts`

**Phase 4 — Production Hardening**
- Scope: structured logging (a JSONL run log with no tokens and no message bodies), the README with the credential procedure, a PyInstaller single-file `qexport.exe`, `.gitignore` for `.env`/`exports/`, and PII handling guidance.
- Deliverable: `qexport.exe` that runs on a clean Windows 11 machine without Python installed, plus a README.
- Exit criteria:
  1. All Phase 1–3 tests pass, with line coverage ≥ 85% on `api/`, `transform/`, `store/` and `load/`.
  2. `grep` over the log files and the xlsx for the admin/SDK token strings finds nothing.
  3. A full live export completes with 0 skipped rooms, or every skip is explained in Export Info, and the message total is within ±2% of the Source-context estimate (~76k).
  4. The exported file is treated as **sensitive PII**: it holds student and parent names, phones and emails from the intake form. The README states where it may be stored and who may receive it.

---

## TDD SECTION 5 — DESIGN VERIFICATION & EDGE CASES

### 5.1 Failure Mode Catalog

| Failure Mode | System Manifestation | Detection Method | Correct Response |
|---|---|---|---|
| Admin dashboard token expires or is revoked mid-run (agent logs out, password change, 90-day expiry) | `customer_rooms` returns 401 | `AuthError` from `QiscusClient` | Commit nothing partial, print "token expired, refresh .env and run `qexport resume`", exit 2. Checkpoint intact. |
| Room-list rate limit tightened by Qiscus | Repeated 429s; the run crawls | Count of 429s per minute in the run log | Backoff + `penalize()`. If retries are exhausted, exit resumable with a hint to lower `--rooms-rps`. Never hot-loop. |
| Room ordering shifts during enumeration (new customer messages bump rooms to the top while we walk older pages) | Rooms could be skipped or duplicated across page boundaries | Duplicate `room_id` in a page upsert; room count lower than `get_unresolved_count` + resolved | Upsert de-dupes. After enumeration, a short **top-up pass** re-walks from the newest cursor until it reaches rooms already stored with an unchanged `last_activity_utc`. |
| New messages arrive in a room after it was fetched but before the xlsx is written | Export is a near-snapshot, not a point-in-time one | `last_activity_utc` > `fetched_last_activity_utc` on a top-up | Documented as eventually consistent. The Export Info sheet records the `as_of` time. `--since-last-run` catches it next time. |
| `load_comments` cursor loop or API returns an overlapping page | Infinite paging on one room | Repeated `msg_id` or > `MAX_PAGES_PER_ROOM` | `CursorLoopError`: mark room failed, continue. Visible in Export Info. |
| Qiscus migrates to the "new Inbox" and renames fields or endpoints | pydantic validation fails on every call | `SchemaDriftError` on the first page | Abort before writing anything. The error names the endpoint and the missing field. The fix is confined to `api/endpoints.py` + `api/models.py`. |
| Customer text that Excel interprets as a formula (`=cmd\|' /C calc'!A0`, `+62…`, `-` bullets) | Formula execution or a corrupted cell when the analyst opens the file | `sanitize_cell` unit tests | Apostrophe-prefix the text; never write a string as a formula. |
| WhatsApp text with control characters (e.g. `\x0b` from pasted forms) | openpyxl raises `IllegalCharacterError` and the whole write aborts | `sanitize_cell` | Strip the illegal characters; the write continues. |
| Attachment URL is short-lived or signed | URL in the sheet 404s weeks later | Not detectable at export time | Out of scope for v1: the URL is exported as-is. Downloading media is a separate opt-in feature (see Next Steps). |
| Disk full / xlsx locked by Excel on Windows | `OSError` / `PermissionError` at save | Exception in `ExcelWriter.write` | `.partial` file is kept, SQLite is untouched, and the user is told to run `write-xlsx` after fixing it. |

### 5.2 Performance Thresholds

| Metric | Target | Hard Limit | Measurement Method |
|---|---|---|---|
| Full export wall time (6.8k rooms / ~76k msgs) | ≤ 60 min | 120 min | `ExportReport.duration_s` + run log timestamps |
| Room-list request rate | 1.2 req/s | 1.4 req/s sustained (observed 429 threshold ≈ 6 req/s burst, ~1.4 sustained) | Token-bucket counter in the run log |
| Message request rate | 3 req/s | 4 req/s (verified 0×429) | Same |
| 429 responses per full run | < 20 | < 200 (otherwise auto-lower the rate) | Run log counter |
| Peak memory during fetch | < 150 MB | 300 MB | `psutil` sample in a debug build / Task Manager |
| `write-xlsx` time, 76k rows | < 30 s | 60 s | `ExportReport.duration_s` |
| Output file size | ~8–15 MB | < 100 MB | `stat` |
| Resume overhead after crash | Re-fetch at most 1 room | 1 room | Integration test #10 |

### 5.3 Platform / Environment Edge Cases

- **Windows `zoneinfo`:** Windows has no IANA timezone database, so `ZoneInfo("Asia/Jakarta")` raises unless the `tzdata` package is installed. It's pinned as a hard dependency, and `Normalizer.__init__` fails fast.
- **Windows file locking:** Excel holds an exclusive lock on open `.xlsx` files, so the output name gets a timestamp and the write is atomic via `.partial`.
- **Windows console encoding:** emoji and Indonesian text in `rich` output can raise `UnicodeEncodeError` on legacy code pages. Set `PYTHONUTF8=1` in the PyInstaller bootloader and never print message bodies to the console.
- **Path with spaces and apostrophes:** the project root is `G:\Draven's Very Important Files\…`. Every path goes through `pathlib`, and nothing is shell-interpolated.
- **Sleep/hibernate mid-run:** the httpx timeout (30 s) fires and the retry policy covers it. If the token expired during sleep, the run follows the `AuthError` path.
- **Network drop:** `httpx.ConnectError` and `ReadTimeout` are retried with backoff. Once retries run out, the room is marked failed rather than the run aborting. Only an `AuthError` or a global `RateLimitExhausted` stops the run.
- **Concurrent runs against the same SQLite file:** take an exclusive lockfile `qexport_<app>.lock` at startup. A second instance exits with a clear message.
- **Excel limits:** 1,048,576 rows per sheet (rollover implemented) and 32,767 characters per cell (truncate + count). Excel can't store timezone-aware datetimes, so WIB is written as naive datetimes with an explicit column header, and UTC is kept in a separate column for unambiguous audit.
- **Data protection:** the export contains personal data of minors' parents (intake form: parent name, number, email). Indonesia's PDP Law (UU 27/2022) applies. Keep exports out of shared drives and email unless approved, and offer `--redact-phones` as a Phase 4 flag if the university requires it.

---

*(End of TDD)*

## Next Steps
1. Scaffold `qexport/` with the `pyproject.toml` dependency set from Section 2. Capture and PII-scrub fixtures from the live tenant: one `customer_rooms` page, one long room with 3 `load_comments` pages, and one of each message type. Then write the 25 Phase 1 unit tests before any implementation.
2. Implement Phase 2 and prove resumability live: `qexport export --no-xlsx --limit-rooms 50`, kill it with Ctrl+C mid-run, then `qexport resume`.
3. Implement `ExcelWriter`, run the full export, and spot-check 10 random conversations against the dashboard before handing the file to stakeholders.
4. *(Optional v1.1)* Add a `Leads` sheet that parses the bot's intake-form replies (Name / Grade / Email / School / Interested majors / Parents…) into columns. That's likely the highest-value view for admissions.

## Diagnostic Question
**How should the tool authenticate long-term?** v1 reuses the admin's dashboard session token (copied from the browser), which can expire mid-run or on logout. Is that acceptable for a one-off or periodic manual export? Or do you want a scheduled, unattended exporter? The unattended option means asking Qiscus support for the app's **Secret Key** and switching message fetching to the server-side REST API (`api3.qiscus.com/api/v2.1/rest/load_comments` with `QISCUS-SDK-SECRET`), which never expires. That changes the `Config` and `QiscusClient` interfaces in Phase 1.
