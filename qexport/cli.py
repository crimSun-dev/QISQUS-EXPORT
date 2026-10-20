from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, \
    TimeElapsedColumn, TimeRemainingColumn
from rich.table import Table

from qexport.api.client import QiscusClient
from qexport.config import Settings, load_settings
from qexport.extract.broadcasts import BroadcastEnumerator, BroadcastLogFetcher
from qexport.extract.messages import MessageFetcher
from qexport.extract.rooms import RoomEnumerator
from qexport.load.excel import ExcelWriter, ExportReport, WorkbookLockedError, finish_locked_workbook
from qexport.orchestrator import (
    EXIT_AUTH, EXIT_OK, ExportOrchestrator, NullProgress, RunResult, default_out_path,
)
from qexport.store.checkpoint import CheckpointStore, RunLock, StoreAppMismatchError, StoreLockedError
from qexport.transform.normalize import Normalizer

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Export Qiscus conversations, broadcasts and recipient actions to one .xlsx.")
console = Console()


class RichProgress:
    def __init__(self) -> None:
        self._bar = Progress(
            SpinnerColumn(), TextColumn("[bold]{task.description}"), BarColumn(), MofNCompleteColumn(),
            TextColumn("{task.fields[info]}"), TimeElapsedColumn(), TimeRemainingColumn(),
            console=console, transient=False)
        self._task = None
        self._warnings = 0

    def __enter__(self):
        self._bar.start()
        return self

    def __exit__(self, *exc):
        self._bar.stop()

    def phase(self, name: str, total: int | None) -> None:
        if self._task is not None:
            self._bar.update(self._task, completed=self._bar.tasks[self._task].completed)
        self._task = self._bar.add_task(name, total=total, info="")

    def advance(self, n: int = 1, **info: object) -> None:
        if self._task is None:
            return
        text = " ".join(f"{k}={v}" for k, v in info.items())
        self._bar.update(self._task, advance=n, **({"info": text} if text else {}))

    def item(self, label: str) -> None: ...

    def warn(self, msg: str) -> None:
        self._warnings += 1
        if self._warnings <= 25:
            self._bar.console.print(f"[yellow]warning:[/] {msg}")
        elif self._warnings == 26:
            self._bar.console.print("[yellow]further warnings suppressed; see `qexport status`[/]")


def _settings(**overrides) -> Settings:
    try:
        return load_settings(**{k: v for k, v in overrides.items() if v is not None})
    except ValidationError as e:
        missing = ", ".join("QISCUS_" + str(err["loc"][0]).upper() for err in e.errors())
        console.print(f"[red]Not logged in:[/] {missing} missing or invalid in .env. "
                      "Run `qexport setup` (menu option 7).")
        raise typer.Exit(EXIT_AUTH)


def _store_path(s: Settings) -> Path:
    return s.out_dir / f"qexport_{s.app_code}.sqlite3"


def _open_store(s: Settings) -> CheckpointStore:
    try:
        return CheckpointStore(_store_path(s), s.app_code)
    except StoreAppMismatchError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)


def _build(s: Settings, store: CheckpointStore, progress: RichProgress
           ) -> tuple[QiscusClient, ExportOrchestrator]:
    norm = Normalizer(s.display_tz)
    client = QiscusClient(s)
    orch = ExportOrchestrator(
        s, client, store,
        RoomEnumerator(client, store, norm, s.page_limit),
        MessageFetcher(client, norm, s.page_limit),
        ExcelWriter(norm),
        progress,
        BroadcastEnumerator(client, store, norm, s.page_limit, s.broadcasts_since),
        BroadcastLogFetcher(client, norm, s.page_limit),
    )
    return client, orch


def _print_report(r: ExportReport) -> None:
    t = Table(title="Export written", show_header=False)
    t.add_row("File", str(r.path.resolve()))
    t.add_row("Completeness", r.completeness)
    t.add_row("Conversations", f"{r.rooms:,}")
    t.add_row("Messages", f"{r.messages:,}")
    t.add_row("Customer actions", f"{r.actions:,}")
    t.add_row("Broadcast campaigns", f"{r.broadcast_jobs:,}")
    t.add_row("Broadcast recipients", f"{r.broadcast_recipients:,}")
    t.add_row("Sheets", ", ".join(r.sheets))
    t.add_row("Truncated cells", str(r.truncated_cells))
    t.add_row("Skipped items", str(len(r.skipped_rooms)))
    t.add_row("Write time", f"{r.duration_s:.1f}s")
    console.print(t)


def _retry_locked(err: WorkbookLockedError) -> ExportReport | None:
    """The workbook is finished but its target is open in Excel: let the user close it and retry."""
    for _ in range(5):
        console.print(f"[yellow]{err.target.name} is open in another program (Excel?).[/] "
                      "Close it, then press Enter to save the new workbook (or type s to skip).")
        try:
            answer = typer.prompt("", default="", show_default=False, prompt_suffix="")
        except Exception:   # no console (EOF) / Ctrl+C
            answer = "s"
        if answer.strip().lower().startswith("s"):
            break
        try:
            return finish_locked_workbook(err)
        except WorkbookLockedError as again:
            err = again
    console.print(f"[red]Not saved over {err.target.name}.[/] The new workbook is complete at "
                  f"[bold]{err.partial.resolve()}[/]; rename it to .xlsx to open it.")
    return None


def _finish(result: RunResult) -> None:
    for r in result.reports[:-1]:
        _print_report(r)
    if result.report:
        _print_report(result.report)
    if result.message:
        style = "green" if result.exit_code == EXIT_OK else "yellow" if result.exit_code == 3 else "red"
        console.print(f"[{style}]{result.message}[/]")
    raise typer.Exit(result.exit_code)


def _run(out: Optional[Path], since_last_run: bool, limit_rooms: Optional[int],
         limit_jobs: Optional[int], rooms_rps: Optional[float], messages_rps: Optional[float],
         broadcast_rps: Optional[float], no_xlsx: bool, no_broadcasts: bool, no_messages: bool,
         no_recipients_sheet: bool, broadcasts_since: Optional[datetime], retry_skipped: bool = False) -> None:
    s = _settings(rooms_rps=rooms_rps, messages_rps=messages_rps, broadcast_rps=broadcast_rps,
                  broadcasts_since=broadcasts_since.date() if broadcasts_since else None)
    s.out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with RunLock(_store_path(s).with_suffix(".lock")):
            store = _open_store(s)
            if retry_skipped:
                console.print(f"Reset {store.reset_failed()} failed/skipped item(s) to pending.")
            progress = RichProgress()
            client, orch = _build(s, store, progress)
            out_path = None
            if out is not None:
                out_path = out if out.suffix.lower() == ".xlsx" else out / default_out_path(s).name
            try:
                with progress:
                    result = orch.run(
                        since_last_run=since_last_run, write_xlsx=not no_xlsx, limit_rooms=limit_rooms,
                        limit_jobs=limit_jobs, include_messages=not no_messages,
                        include_broadcasts=not no_broadcasts,
                        include_recipients_sheet=not no_recipients_sheet, out_path=out_path)
            except WorkbookLockedError as e:
                report = _retry_locked(e)
                if report is None:
                    raise typer.Exit(1)
                result = RunResult(EXIT_OK if report.complete else 3, report, reports=(report,))
            except PermissionError as e:
                console.print(f"[red]Could not write the workbook ({e}).[/] Close it in Excel, then run "
                              "`qexport write-xlsx`. Downloaded data is safe in the .sqlite3 store.")
                raise typer.Exit(1)
            finally:
                st = client.stats
                console.print(f"[dim]HTTP requests: {st.requests:,}  429s: {st.http_429}  retries: {st.retries}[/]")
                client.close()
                store.close()
    except StoreLockedError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)
    _finish(result)


OutOpt = typer.Option(None, "--out", help="Output directory or .xlsx path (default ./exports/)")
SinceOpt = typer.Option(False, "--since-last-run", help="Only re-download conversations/campaigns that changed")
LimitRoomsOpt = typer.Option(None, "--limit-rooms", help="Fetch at most N pending conversations (testing)")
LimitJobsOpt = typer.Option(None, "--limit-jobs", help="Fetch at most N pending broadcast campaigns (testing)")
RoomsRpsOpt = typer.Option(None, "--rooms-rps", help="Conversation-list requests per second")
MsgRpsOpt = typer.Option(None, "--messages-rps", help="Message requests per second")
BcRpsOpt = typer.Option(None, "--broadcast-rps", help="Broadcast API requests per second")
NoXlsxOpt = typer.Option(False, "--no-xlsx", help="Download only; don't write the workbook")
NoBcOpt = typer.Option(False, "--no-broadcasts", help="Skip broadcast campaigns and recipient logs")
NoMsgOpt = typer.Option(False, "--no-messages", help="Skip conversations (broadcasts only)")
NoRecipOpt = typer.Option(False, "--no-recipients-sheet",
                          help="Leave the (large) Broadcast Recipients sheet out of the workbook")
BcSinceOpt = typer.Option(None, "--broadcasts-since", formats=["%Y-%m-%d"],
                          help="Only campaigns created on/after this date (YYYY-MM-DD)")


@app.command("auth-check")
def auth_check() -> None:
    """Verify both credential pairs with one cheap call each."""
    s = _settings()
    client = QiscusClient(s)
    try:
        status = client.ping()
    finally:
        client.close()
    for api, ok in status.items():
        label = {"omni": "Omnichannel API (admin token)", "sdk": "Chat SDK API (SDK token)"}[api]
        console.print(f"{'[green]OK[/]' if ok else '[red]FAILED[/]'}  {label}")
    raise typer.Exit(EXIT_OK if all(status.values()) else EXIT_AUTH)


@app.command()
def export(out: Optional[Path] = OutOpt, since_last_run: bool = SinceOpt,
           limit_rooms: Optional[int] = LimitRoomsOpt, limit_jobs: Optional[int] = LimitJobsOpt,
           rooms_rps: Optional[float] = RoomsRpsOpt, messages_rps: Optional[float] = MsgRpsOpt,
           broadcast_rps: Optional[float] = BcRpsOpt, no_xlsx: bool = NoXlsxOpt,
           no_broadcasts: bool = NoBcOpt, no_messages: bool = NoMsgOpt,
           no_recipients_sheet: bool = NoRecipOpt, broadcasts_since: Optional[datetime] = BcSinceOpt) -> None:
    """Download everything (resumable) and write the workbook.

    Exit codes: 0 complete | 3 partial (skipped items) | 2 auth expired (resumable)
    | 4 rate limited (resumable) | 5 API changed / refused a request (resumable)
    | 6 network down (resumable) | 130 interrupted."""
    _run(out, since_last_run, limit_rooms, limit_jobs, rooms_rps, messages_rps, broadcast_rps,
         no_xlsx, no_broadcasts, no_messages, no_recipients_sheet, broadcasts_since)


@app.command()
def resume(retry_skipped: bool = typer.Option(False, "--retry-skipped", help="Retry failed/skipped items too"),
           out: Optional[Path] = OutOpt, limit_rooms: Optional[int] = LimitRoomsOpt,
           limit_jobs: Optional[int] = LimitJobsOpt, rooms_rps: Optional[float] = RoomsRpsOpt,
           messages_rps: Optional[float] = MsgRpsOpt, broadcast_rps: Optional[float] = BcRpsOpt,
           no_xlsx: bool = NoXlsxOpt, no_broadcasts: bool = NoBcOpt, no_messages: bool = NoMsgOpt,
           no_recipients_sheet: bool = NoRecipOpt) -> None:
    """Continue an interrupted export from its checkpoint. Same exit codes as `export`."""
    _run(out, False, limit_rooms, limit_jobs, rooms_rps, messages_rps, broadcast_rps,
         no_xlsx, no_broadcasts, no_messages, no_recipients_sheet, None, retry_skipped=retry_skipped)


@app.command()
def status() -> None:
    """Show checkpoint progress."""
    s = _settings()
    if not _store_path(s).exists():
        console.print("No export started yet. Run `qexport export`.")
        raise typer.Exit(EXIT_OK)
    store = _open_store(s)
    try:
        p = store.progress()
        t = Table(title=f"qexport checkpoint - {s.app_code}")
        t.add_column("")
        t.add_column("Total", justify="right")
        t.add_column("Done", justify="right")
        t.add_column("Pending", justify="right")
        t.add_column("Failed", justify="right")
        t.add_column("Skipped", justify="right")
        t.add_row("Conversations" + ("" if p.enumeration_done else " (listing incomplete)"),
                  f"{p.rooms_total:,}", f"{p.done:,}", f"{p.pending:,}", f"{p.failed:,}", f"{p.skipped:,}")
        t.add_row("Broadcast campaigns" + ("" if p.broadcast_enumeration_done else " (listing incomplete)"),
                  f"{p.jobs_total:,}", f"{p.jobs_done:,}", f"{p.jobs_pending:,}", f"{p.jobs_failed:,}",
                  f"{p.jobs_skipped:,}")
        console.print(t)
        console.print(f"Messages stored: {p.messages:,}   Broadcast recipient rows: {p.broadcast_logs:,}")
        for kind, ident, err in store.skipped()[:20]:
            console.print(f"[yellow]{kind} {ident}[/]: {err}")
    finally:
        store.close()


@app.command("write-xlsx")
def write_xlsx(out: Optional[Path] = OutOpt, no_recipients_sheet: bool = NoRecipOpt) -> None:
    """Rebuild the workbook from the local store only (no network)."""
    s = _settings()
    store = _open_store(s)
    norm = Normalizer(s.display_tz)
    out_path = None
    if out is not None:
        out_path = out if out.suffix.lower() == ".xlsx" else out / default_out_path(s).name
    orch = ExportOrchestrator(s, None, store, None, None, ExcelWriter(norm), NullProgress())  # type: ignore[arg-type]
    try:
        with console.status("Snapshotting data, deriving actions and writing workbook…"):
            report = orch.write(out_path, include_recipients=not no_recipients_sheet)
    except WorkbookLockedError as e:
        report = _retry_locked(e)
        if report is None:
            raise typer.Exit(1)
    except PermissionError as e:
        console.print(f"[red]Could not write the workbook ({e}).[/] Close it in Excel and run again.")
        raise typer.Exit(1)
    finally:
        store.close()
    _print_report(report)


CRED_KEYS = ("QISCUS_APP_CODE", "QISCUS_ADMIN_TOKEN", "QISCUS_SDK_USER_ID", "QISCUS_SDK_TOKEN")
CONSOLE_SNIPPET = (
    "(() => { const d = JSON.parse(localStorage.getItem('qismo-data')); "
    "const t = `QISCUS_APP_CODE=${d.user.app.app_code}\\nQISCUS_ADMIN_TOKEN=${d.user.authentication_token}"
    "\\nQISCUS_SDK_USER_ID=${d.details.sdk_user.id}\\nQISCUS_SDK_TOKEN=${d.details.sdk_user.token}"
    "\\nQISCUS_ROLE=${d.user.type_as_string}`; copy(t); "
    "return 'Copied! Go back to the qexport window and press Enter.'; })()"
)


def parse_credentials(text: str) -> dict[str, str]:
    """Pull QISCUS_*=value pairs out of pasted console output (tolerates quotes and junk lines)."""
    import re

    found: dict[str, str] = {}
    for m in re.finditer(r"(QISCUS_[A-Z_]+)\s*=\s*[\"']?([^\s\"'`\\]+)", text):
        value = m.group(2)
        if "$" in value or "{" in value:   # the snippet itself is still on the clipboard
            continue
        found[m.group(1)] = value
    return found


def write_env(path: Path, creds: dict[str, str]) -> None:
    """Write credentials to .env, keeping any other settings already in the file."""
    kept: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0].strip()
            if key not in creds:
                kept.append(line)
    lines = [f"{k}={v}" for k, v in creds.items()] + kept
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _clipboard_get() -> str:
    import subprocess

    try:
        # stdin=DEVNULL: a child that inherits stdin swallows what the user types/pastes next.
        r = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
        return r.stdout or ""
    except Exception:
        return ""


def _clipboard_set(text: str) -> bool:
    import subprocess

    try:
        subprocess.run(["clip"], input=text.encode("utf-16-le"), check=True, timeout=15,
                       capture_output=True)
        return True
    except Exception:
        return False


def _collect_credentials() -> dict[str, str] | None:
    """Read the console output from the clipboard, or let the user paste it. Three attempts."""
    for _ in range(3):
        typer.prompt("Press Enter when the console says 'Copied!'", default="", show_default=False)
        creds = parse_credentials(_clipboard_get())
        if all(creds.get(k) for k in CRED_KEYS):
            return creds
        console.print("[yellow]Couldn't find the login values on the clipboard.[/] "
                      "Paste the console output here instead, then press Enter on an empty line:")
        pasted: list[str] = []
        while True:
            line = typer.prompt("", default="", show_default=False, prompt_suffix="")
            if not line.strip():
                break
            pasted.append(line)
        creds = parse_credentials("\n".join(pasted))
        if all(creds.get(k) for k in CRED_KEYS):
            return creds
        missing = ", ".join(k for k in CRED_KEYS if not creds.get(k))
        console.print(f"[red]Still missing: {missing}.[/] Run the snippet again in the Qiscus tab.")
    return None


@app.command()
def setup(env_file: Path = typer.Option(Path(".env"), "--env-file", help="Where to save credentials"),
          no_browser: bool = typer.Option(False, "--no-browser", help="Don't open the Qiscus dashboard")) -> None:
    """Guided credential setup: copies the console snippet, reads the values back, writes .env."""
    import webbrowser

    console.rule("[bold]qexport - connect to Qiscus")
    copied = _clipboard_set(CONSOLE_SNIPPET)
    console.print(
        "1. In [bold]Chrome[/], log in to [cyan]https://multichannel.qiscus.com[/] with an [bold]admin[/] account.\n"
        "2. Press [bold]F12[/] and open the [bold]Console[/] tab.\n"
        "   (First time only: Chrome may ask you to type [bold]allow pasting[/] and press Enter.)\n"
        f"3. Paste the snippet ({'already on your clipboard' if copied else 'printed below'}) and press Enter.\n"
        "   It copies your login values back to the clipboard.\n"
        "4. Come back here and press Enter.\n")
    if not copied:
        console.print(CONSOLE_SNIPPET, soft_wrap=True, highlight=False)
        console.print()
    if not no_browser:
        webbrowser.open("https://multichannel.qiscus.com/")

    try:
        creds = _collect_credentials()
    except Exception as e:
        # Ctrl+C / end of input. Older Typer raises click's Abort, newer raises its own;
        # match by name so neither version needs importing.
        if type(e).__name__ != "Abort" and not isinstance(e, EOFError):
            raise
        creds = None
    if creds is None:
        console.print("\n[red]Setup not finished.[/] Run `qexport setup` again when you're ready.")
        raise typer.Exit(EXIT_AUTH)

    creds ={k: creds[k] for k in (*CRED_KEYS, "QISCUS_ROLE") if creds.get(k)}
    write_env(env_file, creds)
    console.print(f"Saved credentials to [bold]{env_file.resolve()}[/]")
    if creds.get("QISCUS_ROLE", "admin") != "admin":
        console.print("[yellow]This account is not an admin; broadcast export may be refused.[/]")

    s = _settings(_env_file=env_file)
    client = QiscusClient(s)
    try:
        status_ = client.ping()
    finally:
        client.close()
    for api, ok in status_.items():
        label = {"omni": "Omnichannel API (admin token)", "sdk": "Chat SDK API (SDK token)"}[api]
        console.print(f"{'[green]OK[/]' if ok else '[red]FAILED[/]'}  {label}")
    if not all(status_.values()):
        console.print("[red]Login check failed.[/] Make sure you're logged in, then run `qexport setup` again.")
        raise typer.Exit(EXIT_AUTH)
    console.print("[green]Connected. You're ready to export.[/]")


@app.command()
def gui(no_window: bool = typer.Option(False, "--no-window", help="Print the address instead of opening a window")
        ) -> None:
    """Open the Qiscus Export window: downloads, Excel files and progress in one place."""
    from qexport.gui.server import main as gui_main

    raise typer.Exit(gui_main(Path.cwd(), open_window=not no_window))


def main() -> None:   # pragma: no cover
    if os.name == "nt":
        os.environ.setdefault("PYTHONUTF8", "1")
    app()


if __name__ == "__main__":   # pragma: no cover
    main()
