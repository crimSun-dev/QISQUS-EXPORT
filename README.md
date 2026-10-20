# qexport — Qiscus Omnichannel bulk exporter

Pulls **every conversation, every broadcast campaign, and every broadcast recipient** from the
Qiscus Omnichannel dashboard (app `enckp-1dh7o4rkopdtmh5`, Sampoerna University WhatsApp) into one
Excel workbook, then derives **what each recipient actually did**: tapped *Build My University Plan*,
filled the intake form, asked a question, or replied. That's the gap the Qiscus broadcast log
(name / phone / status / failure reason only) leaves open.

Design: [`TDD-qiscus-chat-exporter.md`](TDD-qiscus-chat-exporter.md). This build adds the broadcast and
action layers asked for in the 2026-10-16 screen recording (see "What changed vs the TDD" below).

## Install on a new PC (no command line)

1. Copy `dist\qexport-1.1.0.zip` to the PC and unzip it somewhere permanent (e.g. `Documents\qexport`).
2. Double-click **`install.bat`**. It:
   - finds Python 3.11+, or installs Python 3.12 with `winget` (falls back to opening python.org);
   - creates a private environment in `.venv` and installs qexport into it;
   - runs `qexport setup`, which puts the console snippet on the clipboard, opens Qiscus, and reads
     the login values back once you've run the snippet (see `INSTALL.txt` for the click-by-click);
   - adds two desktop shortcuts: **Qiscus Export** (the window) and **Qiscus Export (menu)** (the
     old numbered console menu, kept as a fallback).
3. Use **Qiscus Export** (or `qexport-app.bat`). See "The window" below.

Re-running `install.bat` repairs or updates the install and keeps `.env` and `exports\`.
`qexport.bat <command>` runs any command directly, e.g. `qexport.bat status`.

To build the zip after changing the code: `python tools/build_zip.py` (it never includes `.env`,
`exports\` or `.venv`).

## The window (`qexport gui`)

One screen for everything the menu did, plus running things side by side:

- **Is my data safe?** On opening, without going online, it reads the local data file and says in
  one sentence what is downloaded and what is left (e.g. *All 6,826 conversations downloaded;
  758 of 1,747 sent broadcast campaigns downloaded*), with the one button that fits: Continue
  download, Build Excel now, or Retry failed items. If the last session was cut off (power cut,
  killed window), it runs SQLite's integrity check before allowing new downloads.
- **Tasks.** Downloading conversations, downloading broadcast campaigns and building the Excel file
  are separate tasks that can run at the same time, each with progress, speed, time left, and
  Pause / Resume / Cancel. Pause and cancel take effect after the item in progress, so nothing
  half-saved is left behind. Conversations and broadcasts share the Qiscus server's request limit,
  so running both doesn't go faster than one would.
- **Excel files.** Every workbook in `exports\`, newest first, marked **Complete** or **Partial** (with
  what's missing), with counts, Open, Show in folder, Copy path and Delete (to the Recycle Bin).
  *Delete old partial files* never removes a Complete file, the newest file, or the fullest one.
- **Automatic Excel files.** One when conversations finish, one when broadcasts finish, and a
  partial one when a download stops early (login expired, network down, cancelled). Builds use a
  snapshot of the data, so they never slow down or block a download that's running.
- **Login.** A three-step guide (open Qiscus → run the snippet → read it back), the login status
  at the top, and, when a login expires mid-download, *Refresh login* then *Continue downloads*.
- **Options** in plain words (what to download, campaign start date, recipients sheet, Excel
  folder; request speeds under Advanced), **Copy diagnostics** (no tokens or phone numbers), an
  activity log and a history of past runs.

It runs locally: a small web page served by qexport on `127.0.0.1` and opened as an app window in
Edge (or Chrome, or the default browser). Only one copy runs per folder; opening it again brings
the existing window forward. Closing the window while something runs offers *keep going in the
background* or *stop safely*; with no window and nothing running, it stops by itself. While a console
run (`qexport export`) is using the data, the window shows its progress but won't start downloads.
Log file: `exports\qexport_gui.log`.

## Install (developers)

```powershell
cd "G:\Draven's Very Important Files\Workspace\SIR REY PROJECTS\QISQUS"
pip install -e ".[dev]"
```

Python 3.11+. On Windows the `tzdata` dependency is required for `Asia/Jakarta`.

## Credentials

The tool reuses the dashboard session of an **admin** account. Log in at
`multichannel.qiscus.com`, open DevTools → Console, and run:

```js
(() => { const d = JSON.parse(localStorage.getItem('qismo-data'));
  console.log(`QISCUS_APP_CODE=${d.user.app.app_code}\nQISCUS_ADMIN_TOKEN=${d.user.authentication_token}\nQISCUS_SDK_USER_ID=${d.details.sdk_user.id}\nQISCUS_SDK_TOKEN=${d.details.sdk_user.token}\nQISCUS_ROLE=${d.user.type_as_string}`); })()
```

Paste the output into `.env` in this folder (template: `.env.example`). `.env` is git-ignored.
Or run `qexport setup`, which does the copying for you.
The admin token is a session token: **logging out of the dashboard invalidates it**. If a run stops
with exit code 2, refresh `.env` the same way and run `qexport resume`.

```powershell
qexport auth-check        # OK / FAILED for each API
```

## Run

```powershell
qexport export                       # everything: ~1.5–2.5 h the first time (rate-limited, resumable)
qexport status                       # progress of the checkpoint
qexport resume                       # continue after Ctrl+C, token expiry, network loss
qexport resume --retry-skipped       # also retry items that failed 5 times
qexport export --since-last-run      # later runs: only conversations/campaigns that changed
qexport write-xlsx                   # rebuild the workbook from local data, no network
```

Useful options:

| Option | Effect |
|---|---|
| `--broadcasts-since 2026-09-01` | Only campaigns created on/after a date (there are ~1,750 campaigns / ~557k recipient rows in total) |
| `--no-broadcasts` / `--no-messages` | Skip one half of the export |
| `--no-recipients-sheet` | Leave the very large *Broadcast Recipients* sheet out of the workbook (data stays in SQLite) |
| `--limit-rooms N` / `--limit-jobs N` | Small test runs |
| `--rooms-rps`, `--messages-rps`, `--broadcast-rps` | Request rates (defaults 1.2 / 3.0 / 1.2 per second) |
| `--out PATH` | Output folder or `.xlsx` path (default `exports/`) |

Exit codes: `0` complete · `3` partial (some items skipped, listed in *Export Info*) · `2` token expired
(resumable) · `4` rate limited (resumable) · `5` Qiscus changed its API · `130` interrupted (resumable).

Downloaded data lives in `exports/qexport_<app>.sqlite3`. Every room/campaign is committed in one
transaction, so a crash loses at most the item in flight.

`export` writes a workbook as soon as conversations are done (before the long broadcast phase) and
again at the end; if it stops early it writes a partial one from what was downloaded. Workbooks
whose data is incomplete are named `…_PARTIAL.xlsx`, and *Export Info → Completeness* says what is
missing. Campaigns that haven't been sent yet don't count as missing. If the target file is open in
Excel, the finished workbook waits as `.partial` and qexport asks you to close Excel and retry.

## The workbook

| Sheet | One row per | Answers |
|---|---|---|
| **Leads** | conversation (customer) | Who are they (intake-form fields: name, grade, email, school, city, majors, parents), how far did they get (**Lead Stage**), what did they click, how many questions, which broadcasts reached them |
| **Actions** | customer action | *What did they do and which campaign caused it*: `button_clicked`, `form_submitted`, `question`, `replied`, `attachment_sent`, with the campaign and how it was attributed |
| **Campaigns** | broadcast job | Funnel per campaign: recipients → sent / delivered / read / failed → responders → clicked → forms → questions, with rates |
| **Broadcast Recipients** | recipient of a campaign | The Qiscus log (phone, status, failure reason) **plus** `Clicked Button`, `Responded`, `First Response` |
| **Conversations** | conversation | Message counts by sender, first/last message |
| **Messages** | message | The full transcript, oldest first per conversation |
| **Export Info** | — | Run metadata, counts, definitions, skipped items |

Filter *Actions* by `Action = button_clicked` and `Campaign = Qya_First_BM` to get exactly who tapped
*Build My University Plan* from that blast.

### How attribution works

A button tap arrives as a customer message whose `extras.context.id` is the WhatsApp message id
(`wamid`) of the broadcast it answers. The broadcast recipient log carries the same `wamid`, so the
tap is tied to the exact campaign (`button_context`). A WhatsApp reply that quotes the broadcast is
equally exact (`reply_context`). Other messages are credited to the most recent broadcast that phone
received within 7 days (`last_touch`, configurable with `QISCUS_ATTRIBUTION_WINDOW_DAYS`).

### Lead Stage

`form_completed` (≥ 60 % of the 10 intake fields) › `form_started` › `clicked` › `question_received` ›
`replied` › `read` › `delivered` › `sent` › `failed` › `no_response`.

Form fields are read from labelled lines (`Name: …`, `Nama: …`, `Kelas: …`) or, when the customer
answers the bot's template line by line without labels, matched to the template's order (only
when the message contains an email or phone number, to avoid mistaking ordinary chat for a form).

## Privacy

The workbook and the SQLite file contain personal data of prospective students and their parents
(names, phone numbers, emails, schools). Indonesia's PDP Law (UU 27/2022) applies. Keep them on the
admissions team's approved storage only; don't put them on shared drives or forward them by email
without approval. Delete old exports when they're no longer needed. Tokens never appear in logs or
the workbook.

## What changed vs the TDD

- **Broadcast layer added.** `POST /api/v3/admin/broadcast_jobs` (campaigns) and
  `GET /api/v2/admin/broadcast_logs/{job}?pagination=cursor` (recipients) were found in the dashboard's
  outbound bundle and verified live. Status enums come from the same bundle.
- **Action layer added** (`transform/actions.py`): button taps, form submissions, questions, replies,
  campaign attribution, lead stage. New sheets: Leads, Actions, Campaigns, Broadcast Recipients.
- **New sender type `broadcast`.** All agent, auto-reply and broadcast messages come from the one
  admin SDK account; broadcasts are told apart by `extras.broadcast_message`. Bot detection only fires
  if Qiscus marks a sender as a bot; auto-replies currently show as `agent`.
- **Incremental runs** use a "top-up" walk from the newest item until a page has no changes, instead
  of re-walking all 69 room pages.

## Tests

```powershell
pytest
```

All HTTP is mocked with respx; payload shapes mirror live responses captured on 2026-10-20.
