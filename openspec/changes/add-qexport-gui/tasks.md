## 1. Core Concurrency, Busy Timeout & Snapshot Building

- [x] 1.1 Configure `CheckpointStore` with `PRAGMA busy_timeout = 30000` and use `BEGIN IMMEDIATE` for write transactions; verify with concurrent writer tests in `tests/unit/test_store_concurrency.py`.
- [x] 1.2 Implement SQLite Online Backup snapshot capability in `CheckpointStore.create_snapshot()` and update `rebuild_actions` and `ExcelWriter.write()` to execute against isolated snapshot databases; verify with unit test `test_snapshot_build_does_not_block_live_store`.
- [x] 1.3 Implement thread-safe `CoordinatedRateLimiter` in `qexport.api.client` shared across room, message, and broadcast endpoints; verify with multi-threaded rate limiting and HTTP 429 backoff tests in `tests/unit/test_rate_limiter.py`.

## 2. Auto-Workbook, Locked-File Recovery & Complete/Partial Classification

- [x] 2.1 Update `ExcelWriter` and metadata generation to inspect store completion status, classify workbooks as `Complete` or `Partial`, and reflect status in filename (`_PARTIAL.xlsx`) and Export Info sheet; verify with `test_workbook_completeness_labeling`.
- [x] 2.2 Add locked-file detection and retry recovery to `ExcelWriter.write()` when target `.xlsx` is open in Excel (`PermissionError`), keeping `.partial` intact; verify with `test_excel_writer_locked_file_retry`.
- [x] 2.3 Refactor `ExportOrchestrator` to automatically build workbooks upon conversation phase completion, broadcast phase completion, and upon early run abortion (token expiry, network failure, interrupt); verify with `test_auto_workbook_intermediate_generation`.

## 3. Background Task Manager & Orchestrator Decoupling

- [x] 3.1 Refactor orchestrator phases into discrete background tasks (`DownloadConversationsTask`, `DownloadBroadcastsTask`, `BuildWorkbookTask`) sharing a common runnable task interface; verify tasks can run standalone in `tests/unit/test_tasks.py`.
- [x] 3.2 Implement `TaskManager` with thread-pool execution, state tracking (`PENDING`, `RUNNING`, `PAUSED`, `CANCELLED`, `COMPLETED`, `FAILED`), live throughput/ETA calculations, and Pause/Resume/Cancel lifecycle hooks; verify with `test_task_manager_lifecycle`.
- [x] 3.3 Add task conflict rules and mutual exclusion validation preventing duplicate extractions on the same store; verify with `test_task_manager_conflict_prevention`.

## 4. Desktop GUI Server & Frontend

- [x] 4.1 Implement local HTTP/SSE backend in `qexport.gui.server` bound to `127.0.0.1` with dynamic port assignment and per-launch authentication token validation; verify server startup and authentication tests in `tests/unit/test_gui_server.py`.
- [x] 4.2 Build single-page responsive interface (HTML/CSS/JS) with startup data safety status dashboard, plain-language verdict, and SQLite integrity check; verify offline rendering and status accuracy.
- [x] 4.3 Add active task view in GUI with live progress bars, item counts, throughput rates, ETA displays, and interactive Pause / Resume / Cancel buttons connected to SSE stream; verify real-time metric updates.
- [x] 4.4 Implement files panel in GUI listing workbooks with Complete/Partial badges, record statistics, and native actions (Open, Open file location in Windows Explorer, Copy path, Delete old); verify file operations via API tests.
- [x] 4.5 Build step-by-step Qiscus login setup wizard with token validation, token expiration monitoring, and one-click task resumption; verify wizard interaction and resumption flow.
- [x] 4.6 Implement plain-language options dialog and sanitized "Copy diagnostics" feature (strictly redacting authentication tokens); verify diagnostic output contains zero credentials in `test_gui_diagnostics_sanitization`.

## 5. Process Lifecycle, Packaging & Launchers

- [x] 5.1 Implement single-instance lock integration in GUI entry point using `RunLock`, bringing the existing window into focus when a second instance is launched; verify duplicate instance handling.
- [x] 5.2 Implement safe window close interceptor prompting confirmation when background tasks are running (continue in background / stop safely / cancel); verify exit handling behavior.
- [x] 5.3 Update `pyproject.toml` with dependencies; update `install.bat` and desktop shortcut creation to launch the GUI; update `qexport.bat` to keep console menu as fallback; verify `tools/build_zip.py` package builds successfully.
- [x] 5.4 Execute full test suite including end-to-end integration tests for interrupted runs, resume capabilities, and existing CLI commands to ensure zero regressions.
