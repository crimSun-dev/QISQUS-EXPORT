## Context

`qexport` is an extraction and Excel reporting engine for Qiscus Omnichannel chat and broadcast data, used on Windows PCs by non-technical admissions and marketing staff.

Currently:
- The command-line interface runs sequentially: room enumeration → broadcast enumeration → message fetching → broadcast log fetching → action derivation → workbook generation.
- The SQLite store (`CheckpointStore`) operates in WAL mode but lacks a configured `busy_timeout`. Concurrently writing from multiple threads raises immediate `sqlite3.OperationalError: database is locked`.
- Building an Excel workbook currently mutates the store by computing and persisting derived actions (`rebuild_actions` calls `store.replace_actions`), which prevents building a workbook while downloads are active.
- Rate limiters in `QiscusClient` are separate, uncoordinated token buckets without thread synchronization. Running concurrent extractors against the same host risks exceeding the rate ceiling and triggering HTTP 429 penalties.
- If a run is interrupted (power loss, window close, token expiry), no intermediate Excel file exists even if thousands of conversations finished downloading hours earlier.

## Goals / Non-Goals

**Goals:**
- Provide a non-technical graphical interface that runs locally, requires zero remote services, and fits comfortably within low-spec Windows desktop environments.
- Support parallel execution of independent tasks (conversation download, broadcast download, workbook generation) with live progress metrics and Pause/Resume/Cancel controls.
- Enable on-demand "Build Excel now" and automated intermediate workbook generation (after conversation phase, after broadcast phase, and on early stop) without blocking active ingestion.
- Coordinate database transactions with SQLite `busy_timeout` and snapshot isolation via SQLite Online Backup.
- Unify rate limiting across concurrent extraction threads targeting the same Qiscus API host.
- Provide a dedicated Files Panel displaying workbook completeness status (Complete vs. Partial) and native Windows Explorer integration.

**Non-Goals:**
- Multi-user network hosting or remote server deployments (the GUI is strictly a single-user local application).
- Altering the schema, columns, or data definitions of the generated Excel sheets.
- Building an HTML viewer for conversations or message history.

## Decisions

### 1. GUI Technology Choice: Local Web UI over PySide6 and Tkinter

| Criteria | Local Web UI (FastAPI/Starlette or lightweight http.server + Vanilla HTML/CSS/JS) | PySide6 (Qt) | Tkinter |
| :--- | :--- | :--- | :--- |
| **Packaging / Zip Size** | **Optimal (~0 KB extra)**: Uses built-in Python standard library or tiny zero-dependency server; zip remains < 1 MB. | **Poor (~80–120 MB)**: Massively inflates `build_zip.py` package and install time. | **Acceptable**: Bundled with standard Python Windows installer. |
| **Offline Reliability** | **High**: Binds purely to `127.0.0.1`; requires no external internet or CDNs; assets embedded locally. | **High**: Fully offline desktop binaries. | **High**: Fully offline desktop binaries. |
| **Progress Streaming & UI Responsiveness** | **High**: EventSource / SSE (Server-Sent Events) or JSON polling natively decouples background workers from UI. | **Moderate**: Requires Qt signals/slots across QThread; styling tables and badges requires heavy QSS. | **Low**: Difficult to build responsive progress bars, status badges, and modern responsive layouts; prone to UI freezing. |
| **Windows Desktop Feel** | **High**: Can launch in user's default browser or Edge WebView (`pywebview` or `--app=http://127.0.0.1:...` shortcut mode). | **Native**: Standard native window frame. | **Dated**: Native controls look dated and difficult to align. |

**Decision**: Implement a **Local Web UI** served on `127.0.0.1` using Python's standard `http.server` or lightweight ASGI, paired with native browser application mode (`msedge --app=http://127.0.0.1:...`).
- *Security*: Ephemeral random port selected at launch with a unique per-launch session token required on all HTTP requests to prevent local cross-origin probing.
- *Single-instance*: Reuses `RunLock` to verify if an instance is active; a second launch writes a ping to the running server, bringing the existing browser window into focus.

### 2. Concurrency & Storage Model: SQLite WAL + Busy Timeout + Online Backup Snapshots

- **Safe Concurrent Writes**:
  - `CheckpointStore` configures `PRAGMA busy_timeout = 30000` (30 seconds) on connection initialization.
  - Transactions are strictly short-lived using `IMMEDIATE` transactions (`BEGIN IMMEDIATE`) during batch commits to serialize writers immediately at the start of transactions rather than escalating during execution.
- **Snapshot Isolation for Excel Generation**:
  - To fulfill "Build Excel now" while downloads are inserting rows, the workbook builder never runs action derivation or analytical queries directly on the live database.
  - Instead, the builder uses SQLite's Online Backup API (`sqlite3.Connection.backup()`) to copy the active database into a temporary in-memory or ephemeral disk database (`exports/.snapshot_<id>.sqlite3`).
  - The online backup runs concurrently in milliseconds without locking readers or writers.
  - Action derivation (`derive_actions`) and sheet rendering execute entirely against this snapshot. Temporary snapshot files are unlinked on completion.

### 3. Coordinated Host Rate Limiting

- To prevent simultaneous room extraction and broadcast log extraction from triggering HTTP 429 responses against `omni.qiscus.com`, `QiscusClient` replaces isolated rate limiters with a thread-safe `CoordinatedRateLimiter`.
- Both room requests (default ~1.2 rps) and broadcast requests (default ~3.0 rps) acquire tokens from a composite limiter that enforces both per-category ceilings and an overarching host ceiling.
- A thread-safe mutex protects token timestamps. When any thread encounters an HTTP 429, the host limiter enters adaptive backoff, reducing current throughput for all active threads.

### 4. Background Task Manager

- A central `TaskManager` manages named task instances:
  - `DownloadConversationsTask`
  - `DownloadBroadcastsTask`
  - `BuildWorkbookTask`
- Each task runs in a managed background worker thread.
- **Task Lifecycle**:
  - `PENDING` → `RUNNING` → `PAUSED` / `CANCELLED` / `FAILED` / `COMPLETED`.
  - Pause points are checked between pagination boundaries and item commits.
  - Cancel cleanly rolls back uncommitted in-memory batches, flushes current checkpoints, and marks the task cancelled.
- **Mutual Exclusion**:
  - Only one `DownloadConversationsTask` and one `DownloadBroadcastsTask` can run concurrently. Attempting to start a second instance surfaces a clear status message ("Conversation download is already active").
  - `BuildWorkbookTask` can run concurrently with either or both download tasks.

### 5. Automated Workbook Rules & Completeness Classification

- **Automatic Trigger Hooks**:
  - Orchestrator/Task Manager triggers `BuildWorkbookTask` automatically when:
    1. Conversation download completes (rooms done, messages fetched).
    2. Broadcast download completes.
    3. An active run is aborted or cancelled (partial snapshot).
- **Completeness Detection**:
  - A workbook is **Complete** if: `rooms_pending == 0`, `rooms_failed == 0`, `jobs_pending == 0`, `jobs_failed == 0`, and `skipped_count == 0`.
  - Otherwise, it is **Partial**.
  - Partial files are named `qiscus_export_<app>_<stamp>_PARTIAL.xlsx`.
  - The "Export Info" sheet records the `Completeness` field (`Complete` or `Partial (<n> conversations pending, <m> broadcasts pending)`).
- **Locked Target Workbook Handling**:
  - When writing final `.xlsx` files, if `os.replace(partial, out_path)` encounters `PermissionError` (target file open in Excel), the file writer catches the exception, preserves the `.partial` file, and posts a notification event: "Target file is currently open in Excel. Please close it and click Retry."

### 6. Screen Layout Sketch

```text
+----------------------------------------------------------------------------------------------------+
|  Qiscus Omnichannel Exporter                                              [Login: OK (App: demo)]  |
+----------------------------------------------------------------------------------------------------+
|  DATA SAFETY STATUS                                                                                |
|  [ ✓ All 6,826 conversations downloaded | 1,750 Broadcasts to fetch ]         Store Size: 42.4 MB   |
|  Status: Download interrupted — press Continue to fetch remaining broadcasts.                     |
|                                                                                                    |
|  [ > Download / Continue ]   [ ⟳ Update New Data ]   [ ⊞ Build Excel Now ]   [ ⚙ Options ]        |
+-----------------------------------------------------------------+----------------------------------+
|  ACTIVE TASKS                                                   |  GENERATED WORKBOOKS             |
|                                                                 |                                  |
|  [Task 1] Downloading Broadcast Logs                            |  [Complete] 2026-10-20 18:30     |
|  Progress: [=====================>       ] 68% (1,190 / 1,750)  |  - 6,826 convs | 42,108 msgs     |
|  Current: Campaign "Admissions Fall 2026"                       |  - Size: 12.8 MB                 |
|  Rate: 2.8 items/s | ETA: 3m 20s                                |  [Open] [Folder] [Copy] [Delete] |
|  Controls: [ Pause ]  [ Cancel ]                                |                                  |
|                                                                 |  [Partial] 2026-10-20 13:48      |
|  [Task 2] Building Excel Workbook (Snapshot)                    |  - 6,826 convs | 261 msgs        |
|  Progress: [==============>              ] 45% (Writing Leads)  |  - Missing 1,750 broadcasts      |
|  Rate: 1,000 rows/s | ETA: 45s                                  |  [Open] [Folder] [Copy] [Delete] |
|  Controls: [ Cancel ]                                           |                                  |
+-----------------------------------------------------------------+----------------------------------+
|  ACTIVITY LOG                                                                                      |
|  [18:32:01] Fetched 100 broadcast log records for job #8491                                       |
|  [18:32:04] Snapshot backup created in 120ms. Starting Excel build.                                |
|                                                                                                    |
|  [ Copy Diagnostics ]                                                  [ Open Exports Folder ]     |
+----------------------------------------------------------------------------------------------------+
```

## Risks / Trade-offs

- **[Risk] Browser popup blockers or default browser misconfigurations on Windows**
  - *Mitigation*: The launcher first checks for Microsoft Edge app mode (`msedge.exe --app=http://127.0.0.1:<port>?token=<token>`). If Edge is unavailable, it falls back to standard `webbrowser.open()`. The console launcher prints the direct clickable URL as fallback.
- **[Risk] SQLite Online Backup memory overhead during snapshot**
  - *Mitigation*: The backup API copies pages in small chunks (`backup.step(100)`), yielding to other threads and keeping RAM usage minimal.
- **[Risk] Orphaned background Python process if browser window is closed**
  - *Mitigation*: The web frontend sends an active heartbeat ping every 5 seconds. If no heartbeat is received for 30 seconds and no background extraction tasks are running, the server gracefully shuts down. If tasks *are* running, the server continues until tasks complete or user re-opens the window.
- **[Risk] Excel lock on destination filename**
  - *Mitigation*: Atomic replacement catch block preserves `.partial` and notifies the UI with a non-blocking prompt to release the lock and retry.

## Migration Plan

1. Core concurrency updates (WAL busy_timeout, snapshot backup method in `store`, shared rate limiter in `client`) implemented with regression tests.
2. Excel writer update for partial labeling, locked-file recovery, and intermediate workbook hooks in orchestrator.
3. Task management layer (`qexport.tasks`) created and unit tested.
4. GUI server and web frontend built in `qexport.gui`.
5. `install.bat` and desktop shortcut updated to target GUI entry point; `qexport.bat` menu retained.

## Implementation Notes

- **Window close (accepted 2026-10-20):** the in-app *Close app* button shows the three-choice dialog (keep going in the background / stop safely / don't close). The browser window's own close button can only show the browser's generic "Leave site?" prompt while tasks run; leaving keeps the tasks running in the background and the process exits on its own once idle with no window. A native dialog on the window's close button would need pywebview, which was rejected for install fragility.
- **Rate-limit hosts:** room enumeration and broadcast endpoints share `multichannel-api.qiscus.com` (one host limiter at max(rooms_rps, broadcast_rps), default 1.2 req/s); messages use `api3.qiscus.com`. The figures above (`omni.qiscus.com`, broadcasts at 3.0 req/s) did not match the code.
- **Snapshot copy:** the online backup runs in a single step, not `step(100)`: a stepped backup restarts whenever another connection writes, which on a live download is constantly. A single-step backup is one WAL read transaction and still never blocks writers.
