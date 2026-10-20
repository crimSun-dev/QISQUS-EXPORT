# OpenSpec proposal prompt — qexport desktop GUI

Use with: `/opsx:propose` (paste everything below the line).

---

Create an OpenSpec change named `add-qexport-gui` that gives qexport (the Qiscus Omnichannel bulk
exporter in this repo) a graphical interface for non-technical staff, replacing the numbered menu in
`qexport.bat` as the everyday way to use the tool. The CLI stays and keeps working unchanged; the GUI
is a second front end over the same engine.

## Who uses it and what goes wrong today

The users are admissions/marketing staff at Sampoerna University on Windows PCs, installed with
`install.bat` and launched from a desktop shortcut. They are not developers. Real problems seen with
the console version:

- A power cut mid-download left them unsure whether the 6,826 conversations had landed and where
  they were stored. The data was safe in `exports/qexport_<app>.sqlite3`, but nothing told them so.
- The Excel workbook is only written after *every* phase finishes (conversations, then ~1,750
  broadcast campaigns, which takes hours). A run that stops early (expired token, network loss, closed
  window) writes no workbook at all, even though the conversations finished long before.
- Old, incomplete workbooks sit in `exports/` next to good ones with nothing to tell them apart (two
  13:47/13:48 files list all 6,826 conversations but only 261 messages).
- Closing the console window killed the process; one menu action at a time; progress was a
  console bar that vanished with the window.

## What the GUI must do

1. **Dashboard / "is my data safe?"** — On open, show the state of the local store without any
   network call: conversations total/done/pending/failed/skipped, broadcast campaigns likewise,
   messages stored, recipient rows stored, last completed run, store file size, and a
   plain-language verdict ("All 6,826 conversations downloaded", "Download interrupted — 1,359
   campaigns still to fetch, press Continue"). Run SQLite `integrity_check` on demand and after an
   unclean shutdown is detected.
2. **Run several things at once** — download conversations, download broadcast logs, and build a
   workbook as independent tasks that can run in parallel, each with its own progress bar, rate,
   ETA, current item, and Pause / Resume / Cancel. Starting a task that conflicts with a running one
   is prevented with a clear reason, not an error.
3. **Workbook on demand and automatically** — "Build Excel now" works while downloads are running
   (build from a consistent snapshot, e.g. the SQLite backup API, so it never blocks or corrupts the
   live store). Automatically write a workbook when the conversation phase finishes, again when
   broadcasts finish, and a partial one when any run stops early. Each workbook is labelled
   Complete or Partial (with what's missing) in the GUI and in its file name / Export Info sheet.
4. **Files panel** — list every workbook in `exports/` with date, size, conversation/message/
   campaign counts read from its Export Info sheet, and a Complete/Partial badge. Buttons: Open,
   Open file location (Explorer with the file selected), Copy path, Delete old. Also "Open exports
   folder" and "Open data store location".
5. **Login** — guide the existing `qexport setup` flow (copy console snippet, open Qiscus, read
   clipboard) in a wizard; show token status (OK / expired) via `auth-check`; when a task stops with
   an expired token, prompt to refresh login and offer one-click Continue.
6. **Options without flags** — expose the useful CLI options as controls: broadcasts-since date,
   include/skip messages or broadcasts, include/skip the Broadcast Recipients sheet, update only
   changed items (`--since-last-run`), retry failed items, output folder. Request rates go under an
   "Advanced" section with the safe defaults (1.2 / 3.0 / 1.2 req/s) and a warning.
7. **Tracking and history** — an activity log per task (human-readable, with warnings and skipped
   items and the reason Qiscus gave), a run history list (start, end, outcome mapped from the exit
   codes 0/2/3/4/5/6/130 to plain sentences), and a "Copy diagnostics" button that copies log +
   versions + counts (never tokens) for support.

## Not easily prone to errors

- Survive the window being closed or the PC losing power: tasks resume from the checkpoint on next
  launch; the GUI offers "Continue where it stopped". Closing the window while tasks run asks first
  (keep running in the background / stop safely / cancel).
- Only one GUI instance per data store (reuse the OS lock in `RunLock`); a second launch focuses the
  existing window.
- Concurrency must be designed, not assumed. Today `CheckpointStore` uses WAL but sets no
  `busy_timeout`, and `write` derives actions *into* the store. The design must state how parallel
  tasks share the store (single writer thread/queue, or busy_timeout + short transactions) and how
  rate limits are shared: the rooms API and broadcast API are on the same host, so their rate
  limiters must be combined, not added, when both run at once.
- If a workbook is open in Excel when writing, say so and offer Retry, instead of the current
  PermissionError message.
- Never display or log tokens; `.env` stays the credential store.
- All long work off the UI thread; the window never freezes.

## Intuitive to use

- One main screen: status at the top, big primary actions ("Download / Continue", "Update",
  "Build Excel now"), running tasks below, files panel to the side. Plain words, no CLI jargon,
  no exit codes on screen.
- Every error says what happened, whether data is safe, and the one button that fixes it.
- Works at a typical laptop resolution, keyboard accessible, readable in light and dark mode.

## Technical constraints

- Python 3.11+, Windows first (must work from the `install.bat` venv and the desktop shortcut);
  don't break macOS/Linux CLI use.
- Reuse `qexport.orchestrator`, `qexport.store.checkpoint`, `qexport.load.excel` — no duplicated
  export logic. Refactor the orchestrator where needed so phases can run as separate tasks and report
  progress through the existing `Progress` protocol (`phase`, `advance`, `warn`) plus pause/cancel.
- Choose the GUI technology in `design.md` and justify it against install size, offline use, packaging
  via `tools/build_zip.py`, and threading. Recommended starting point: a local web UI served by the
  Python process on 127.0.0.1 (random port, per-launch token) and opened in the default browser or an
  app window, since progress streaming, tables and file lists are straightforward and it adds no heavy
  toolkit. Compare against PySide6 and Tkinter before deciding.
- New dependencies are pinned and added to `pyproject.toml`; `install.bat` and the desktop shortcut
  launch the GUI; `qexport.bat` keeps the menu for fallback.
- Tests: unit tests for the task manager (parallel tasks, pause/cancel, conflict rules), snapshot
  workbook build while a download writes, auto-workbook on phase end and on early stop, workbook
  Complete/Partial detection, and resume after a simulated kill. Existing tests must still pass.

## Out of scope

- Changing what data is downloaded or the workbook's sheets and columns.
- Hosting the tool on a server or multi-user access.
- An HTML export of the data itself.

Produce `proposal.md`, `design.md` (include the concurrency model and a screen layout sketch),
capability specs with testable scenarios, and `tasks.md` ordered so the auto-workbook and
snapshot-build fixes land first, since they are useful even before the GUI exists.
