## Why

The primary users of `qexport` are non-technical admissions and marketing staff on Windows PCs who launch the tool via desktop shortcuts. The existing console interface presents critical usability and reliability challenges: mid-run power cuts or window closures leave staff uncertain if data was saved, Excel workbooks are only generated after all phases (hours of broadcast fetching) complete, incomplete workbooks sit indistinguishable from complete ones in `exports/`, and concurrent execution of tasks (such as downloading conversations while building an Excel report or downloading broadcasts) risks database contention and rate limit breaches.

Providing a resilient graphical interface with snapshot-based workbook creation, automatic intermediate saves, unified rate limiting, and plain-language data safety guarantees allows non-technical staff to operate exports independently and safely.

## What Changes

- **Core Concurrency & Storage Safety**:
  - Configure `CheckpointStore` with SQLite `busy_timeout` and WAL concurrency safeguards.
  - Decouple action derivation and workbook building from the live store using SQLite online backup snapshots so "Build Excel now" can run simultaneously with downloads without database locking or corruption.
  - Implement a coordinated host-level rate limiter shared across conversation room enumeration, message fetching, and broadcast log extraction to keep combined throughput under Qiscus API limits.
- **Automated & Resilient Workbook Generation**:
  - Automatically write an Excel workbook when the conversation phase completes, again when broadcasts complete, and a partial workbook whenever a run stops early (cancelled, network loss, token expiration).
  - Label all workbooks as `Complete` or `Partial` (identifying missing items) in file naming, Export Info metadata sheet, and the UI.
  - Detect when target workbooks are locked in Microsoft Excel and offer a graceful retry prompt rather than crashing with `PermissionError`.
- **Background Task Management**:
  - Implement a multi-task execution engine supporting concurrent tasks (Conversation Download, Broadcast Download, Workbook Generation) with independent progress tracking, ETA, throughput, and Pause/Resume/Cancel controls.
  - Enforce conflict detection to prevent mutually exclusive operations with actionable user feedback.
- **Desktop Graphical Interface**:
  - Provide an intuitive single-window GUI (evaluating a zero-install local web app served on localhost vs. native PySide6/Tkinter).
  - Data safety dashboard on startup showing current local database state ("All 6,826 conversations downloaded", "Download interrupted — press Continue") and SQLite integrity verification.
  - Interactive login wizard guiding Qiscus token retrieval and setup, with automatic token health checks and one-click resumption upon re-authentication.
  - Files panel with file badges (Complete / Partial), record counts, Open, Open File Location (Windows Explorer), Copy Path, and Delete Old actions.
  - Sanitized "Copy diagnostics" bundle (logs, version, record counts without credentials) for troubleshooting.
  - Single-instance process lock reusing `RunLock` to focus existing windows and prevent database corruption.
  - Unclean shutdown recovery and confirmation prompt when attempting to close while tasks are active.
  - User-friendly configuration controls replacing CLI flags with plain-language inputs.
- **Packaging & Launchers**:
  - Update `install.bat` and desktop shortcuts to launch the GUI, while retaining `qexport.bat` menu and CLI as full-featured fallbacks.

## Capabilities

### New Capabilities
- `concurrent-engine`: Thread-safe database access with `busy_timeout`, SQLite snapshot creation for read operations, and coordinated cross-task rate limiting across Qiscus API endpoints.
- `auto-workbook`: Automatic intermediate workbook generation upon phase completion or run interruption, locked-file retry handling, and Complete vs. Partial workbook classification.
- `task-manager`: Multi-task orchestration layer managing concurrent background jobs (conversation extraction, broadcast extraction, workbook export) with progress streaming, rate/ETA computation, pause/resume/cancel lifecycles, and conflict prevention.
- `desktop-gui`: Desktop graphical interface providing startup data safety status, task control panels, file management, credential setup wizard, friendly configuration controls, diagnostic copying, and single-instance window lifecycle.

### Modified Capabilities
<!-- No existing capabilities under openspec/specs/ -->

## Impact

- **Codebase**:
  - `qexport.store.checkpoint`: Connection parameters (`busy_timeout`), thread-safe SQLite access, snapshot backup method.
  - `qexport.api.client`: Coordinated/shared rate limiting across room and broadcast endpoints.
  - `qexport.orchestrator`: Decoupling phases into orchestratable tasks; intermediate workbook hooks on phase end and abortion; progress sink enhancements for pause/cancel.
  - `qexport.load.excel`: Partial/Complete workbook labeling in file names and Export Info sheet; Excel file lock detection.
  - New modules: `qexport.tasks` (task runner, concurrency controls), `qexport.gui` or `qexport.ui` (GUI backend, web server / UI assets / desktop window integration).
  - Launchers: `install.bat`, `qexport.bat`, `tools/build_zip.py`.
- **Dependencies**:
  - GUI framework dependencies added to `pyproject.toml` (e.g. lightweight local web server or GUI toolkit).
- **APIs & Compatibility**:
  - Fully backward compatible with the CLI interface (`qexport export`, `qexport status`, etc.) and existing SQLite checkpoint stores. No schema-breaking changes to store tables.
