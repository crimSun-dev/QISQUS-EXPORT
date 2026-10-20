"""GUI backend: local server security, state and verdict, files panel, options, login, diagnostics."""

import json
import threading

import httpx
import pytest
import respx
from openpyxl import Workbook

from qexport.api.models import RawComment, RawRoom
from qexport.gui import winshell
from qexport.gui.app import GuiApp, Options, UserError
from qexport.gui.diagnostics import REDACTED, redact
from qexport.gui.files import WorkbookIndex, describe, old_partials, read_export_info
from qexport.gui.server import GuiServer
from qexport.store.checkpoint import CheckpointStore
from tests.conftest import ADMIN_EMAIL, ADMIN_TOKEN, APP, OMNI, SDK, SDK_TOKEN, comment, raw_room, rooms_page

ENV = (f"QISCUS_APP_CODE={APP}\nQISCUS_ADMIN_TOKEN={ADMIN_TOKEN}\nQISCUS_SDK_USER_ID={ADMIN_EMAIL}\n"
       f"QISCUS_SDK_TOKEN={SDK_TOKEN}\nQISCUS_ROLE=admin\n")


@pytest.fixture(autouse=True)
def no_desktop(monkeypatch):
    """Never open Excel/Explorer/browsers or touch the real clipboard from tests."""
    calls = []
    for name in ("open_path", "reveal", "open_app_window"):
        monkeypatch.setattr(winshell, name, lambda *a, _n=name: calls.append((_n, a)))
    monkeypatch.setattr(winshell, "recycle", lambda p: (calls.append(("recycle", (p,))), p.unlink()))
    clip = {"text": ""}
    monkeypatch.setattr(winshell, "clipboard_set", lambda t: clip.update(text=t) or True)
    monkeypatch.setattr(winshell, "clipboard_get", lambda: clip["text"])
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: True)
    for k in [k for k in __import__("os").environ if k.startswith("QISCUS_")]:
        monkeypatch.delenv(k)
    return {"calls": calls, "clip": clip}


def _seed_store(root, norm, n=3, done=None):
    s = CheckpointStore(root / "exports" / f"qexport_{APP}.sqlite3", APP)
    rooms = [norm.room(RawRoom.model_validate(raw_room(i))) for i in range(1, n + 1)]
    s.upsert_rooms_page(rooms, None)
    s.mark_enumeration_done()
    s.mark_broadcast_enumeration_done()
    for r in rooms[: n if done is None else done]:
        rid = int(r.room_id)
        s.commit_room(r.room_id, [norm.message(RawComment.model_validate(
            comment(rid * 10, rid, email=r.customer_identifier, text="Halo")), r)], None)
    s.close()


def _workbook(path, info: dict):
    wb = Workbook()
    wb.active.title = "Leads"
    ws = wb.create_sheet("Export Info")
    ws.append(["Item", "Value"])
    for k, v in info.items():
        ws.append([k, v])
    wb.save(path)
    return path


@pytest.fixture
def gui(tmp_path, norm):
    (tmp_path / "exports").mkdir()
    app = GuiApp(tmp_path)
    yield app
    app.close()


@pytest.fixture
def served(gui):
    gui.start()
    srv = GuiServer(gui)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.port}"
    yield srv, base
    srv.shutdown()
    srv.server_close()


# --------------------------------------------------------------------------- server

def test_gui_server_requires_token_and_local_host(served):
    srv, base = served
    assert httpx.get(f"{base}/api/state").status_code == 401
    assert httpx.post(f"{base}/api/build", json={}).status_code == 401
    assert httpx.get(f"{base}/api/state", headers={"X-QX-Token": "nope"}).status_code == 401
    assert httpx.get(f"{base}/api/state", headers={"X-QX-Token": srv.token, "Host": "evil.example"}).status_code == 403
    r = httpx.get(f"{base}/api/state", headers={"X-QX-Token": srv.token})
    assert r.status_code == 200 and r.json()["verdict"]["level"] == "empty"
    # POST must carry the header: a ?t= query (what a link or form could do) is not enough
    assert httpx.post(f"{base}/api/build?t={srv.token}", json={}).status_code == 401


def test_gui_server_binds_loopback_on_a_random_port(served):
    srv, _ = served
    assert srv.server_address[0] == "127.0.0.1" and srv.port > 0
    assert srv.url.startswith(f"http://127.0.0.1:{srv.port}/#t=")


def test_gui_server_serves_the_page_without_secrets(served):
    srv, base = served
    page = httpx.get(f"{base}/")
    assert page.status_code == 200 and "<title>Qiscus Export</title>" in page.text
    assert srv.token not in page.text
    assert "default-src 'self'" in page.headers["Content-Security-Policy"]
    assert httpx.get(f"{base}/app.js").status_code == 200
    assert httpx.get(f"{base}/../.env").status_code == 404
    assert httpx.get(f"{base}/schema.sql").status_code == 404


def test_event_stream_sends_state(served):
    srv, base = served
    with httpx.stream("GET", f"{base}/api/events?t={srv.token}", timeout=5) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data: "):
                assert "verdict" in json.loads(line[6:])
                break


def test_user_errors_come_back_as_plain_messages(served):
    srv, base = served
    r = httpx.post(f"{base}/api/download", json={}, headers={"X-QX-Token": srv.token})
    assert r.status_code == 400 and "Sign in to Qiscus first" in r.json()["error"]


# --------------------------------------------------------------------------- dashboard

def test_dashboard_reads_existing_store_without_login(gui, norm):
    _seed_store(gui.root, norm, n=4, done=4)
    gui.start()
    st = gui.state()
    assert not st["logged_in"] and st["app_code"] == APP
    assert st["verdict"]["level"] == "ok"
    assert st["verdict"]["headline"].startswith("All 4 conversations downloaded")
    assert st["blocked"]                     # downloads need a login


def test_interrupted_store_offers_continue(gui, norm):
    _seed_store(gui.root, norm, n=5, done=2)
    gui.start()
    v = gui.state()["verdict"]
    assert v["level"] == "warn" and v["action"] == "download"
    assert "3 conversations" in v["detail"] and "Continue download" in v["detail"]


def test_leftover_lock_triggers_integrity_check(gui, norm):
    _seed_store(gui.root, norm, n=2)
    (gui.root / "exports" / f"qexport_{APP}.lock").write_text("12345")   # killed run
    gui.start()
    assert gui.unclean_shutdown
    import time
    for _ in range(200):
        if gui.integrity["state"] != "checking":
            break
        time.sleep(0.02)
    assert gui.integrity["state"] == "ok"
    assert any("checked and is fine" in n for n in gui.state()["verdict"]["notes"])


def test_store_held_by_console_run_blocks_downloads_but_not_reading(gui, norm, tmp_path):
    import subprocess
    import sys
    _seed_store(gui.root, norm, n=2)
    lock = gui.root / "exports" / f"qexport_{APP}.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; from pathlib import Path; "
         "from qexport.store.checkpoint import RunLock\n"
         "with RunLock(Path(sys.argv[1])):\n print('locked', flush=True); time.sleep(20)", str(lock)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        gui.start()
        st = gui.state()
        assert st["other_window"] and "another qexport window" in st["blocked"]
        assert st["store"]["rooms_total"] == 2
    finally:
        holder.kill()
        holder.wait()


# --------------------------------------------------------------------------- files

def test_export_info_reader_and_badges(tmp_path):
    good = _workbook(tmp_path / "a.xlsx", {"Tool": "qexport 1.1.0", "Completeness": "Complete",
                                           "Conversations": 6826, "Messages": 81429, "Broadcast jobs": 1747,
                                           "Broadcast recipient rows": 557000})
    old = _workbook(tmp_path / "b.xlsx", {"Tool": "qexport 1.0.2", "Data as of (UTC)": "incomplete run",
                                          "Conversations": 6826, "Messages": 261})
    assert read_export_info(good)["Completeness"] == "Complete"
    g, o = describe(good), describe(old)
    assert g.complete and (g.conversations, g.messages, g.campaigns, g.recipients) == (6826, 81429, 1747, 557000)
    assert o.complete is False and "before the download finished" in o.completeness
    (tmp_path / "junk.xlsx").write_bytes(b"not a zip")
    assert describe(tmp_path / "junk.xlsx").complete is None


def test_index_caches_and_drops_deleted_files(tmp_path):
    p = _workbook(tmp_path / "x.xlsx", {"Completeness": "Complete"})
    idx = WorkbookIndex(tmp_path / ".idx.json")
    assert [w.name for w in idx.list([tmp_path])] == ["x.xlsx"]
    assert WorkbookIndex(tmp_path / ".idx.json").list([tmp_path])[0].complete     # from cache
    p.unlink()
    assert idx.list([tmp_path]) == []


def test_delete_old_never_touches_complete_or_newest_files(tmp_path):
    import os
    names = []
    for i, label in enumerate(["Partial (x)", "Complete", "Partial (y)", "Partial (z)"]):
        p = _workbook(tmp_path / f"f{i}.xlsx", {"Completeness": label})
        os.utime(p, (1_700_000_000 + i * 60, 1_700_000_000 + i * 60))
        names.append(p.name)
    wbs = WorkbookIndex(tmp_path / ".i.json").list([tmp_path])
    assert [w.name for w in old_partials(wbs)] == ["f0.xlsx"]       # only the partial older than the complete one
    only_partials = [w for w in wbs if not w.complete]
    assert [w.name for w in old_partials(only_partials)] == ["f2.xlsx", "f0.xlsx"]   # newest kept


def test_delete_old_keeps_the_fullest_partial_even_with_equal_timestamps(tmp_path):
    import os
    for name, msgs in (("a_1347.xlsx", 261), ("a_1348.xlsx", 261), ("a_2157.xlsx", 81429)):
        p = _workbook(tmp_path / name, {"Completeness": "Partial (x)", "Messages": msgs})
        os.utime(p, (1_700_000_000, 1_700_000_000))          # copied files: same mtime
    wbs = WorkbookIndex(tmp_path / ".i.json").list([tmp_path])
    assert [w.name for w in old_partials(wbs)] == ["a_1348.xlsx", "a_1347.xlsx"]


def test_file_actions_only_accept_listed_files(gui, no_desktop):
    p = _workbook(gui.exports / "qiscus_export_x.xlsx", {"Completeness": "Complete"})
    gui.refresh()
    gui.open_file(p.name)
    gui.reveal_file(p.name)
    assert [c[0] for c in no_desktop["calls"]] == ["open_path", "reveal"]
    with pytest.raises(UserError):
        gui.open_file("..\\..\\Windows\\notepad.exe")
    r = gui.delete_files([p.name])
    assert r["deleted"] == [p.name] and not p.exists()


# --------------------------------------------------------------------------- options

def test_options_are_validated_in_plain_words():
    o = Options()
    with pytest.raises(UserError, match="at least one"):
        o.updated({"include_messages": False, "include_broadcasts": False})
    with pytest.raises(UserError, match="2026-09-01"):
        o.updated({"broadcasts_since": "last week"})
    with pytest.raises(UserError, match="between"):
        o.updated({"rooms_rps": 50})
    with pytest.raises(UserError, match="full folder path"):
        o.updated({"workbook_dir": "relative/dir"})
    assert o.updated({"rooms_rps": "0.8", "broadcasts_since": "2026-09-01"}).rooms_rps == 0.8


def test_options_persist_and_feed_task_settings(gui, tmp_path):
    (gui.root / ".env").write_text(ENV, encoding="utf-8")
    gui.start()
    gui.set_options({"broadcasts_since": "2026-09-01", "messages_rps": 2.0, "include_recipients": False})
    assert GuiApp(gui.root).options.messages_rps == 2.0
    from qexport.tasks import TaskKind
    ctx = gui._context(TaskKind.BROADCASTS)
    assert str(ctx.settings.broadcasts_since) == "2026-09-01" and ctx.settings.messages_rps == 2.0
    assert ctx.include_recipients is False and ctx.store_path.parent == gui.exports


# --------------------------------------------------------------------------- login

@respx.mock
def test_login_wizard_reads_clipboard_saves_env_and_checks(gui, no_desktop):
    respx.post(f"{OMNI}/api/v2/customer_rooms").mock(return_value=httpx.Response(200, json=rooms_page([], None)))
    respx.get(f"{SDK}/api/v2/sdk/my_profile").mock(return_value=httpx.Response(200, json={}))
    gui.start()
    assert gui.state()["login"]["state"] == "missing"
    snippet = gui.login_start()
    assert no_desktop["clip"]["text"] == snippet["snippet"]
    no_desktop["clip"]["text"] = "Copied!\n" + ENV          # what the console snippet leaves behind
    r = gui.login_read_clipboard()
    assert r["state"] == "ok" and not r["not_admin"]
    assert ADMIN_TOKEN in (gui.root / ".env").read_text(encoding="utf-8")
    assert gui.state()["logged_in"] and gui.manager.blocked_reason is None


@respx.mock
def test_sign_in_window_connects_without_the_console(gui, monkeypatch):
    import time as time_mod

    from qexport import login as login_mod
    respx.post(f"{OMNI}/api/v2/customer_rooms").mock(return_value=httpx.Response(200, json=rooms_page([], None)))
    respx.get(f"{SDK}/api/v2/sdk/my_profile").mock(return_value=httpx.Response(200, json={}))
    seen = {}

    def fake_browser_login(profile_dir, on_status=None, **k):
        seen["profile"] = profile_dir
        on_status("A Qiscus window opened. Sign in there with your admin account.")
        return {"QISCUS_APP_CODE": APP, "QISCUS_ADMIN_TOKEN": ADMIN_TOKEN, "QISCUS_SDK_USER_ID": ADMIN_EMAIL,
                "QISCUS_SDK_TOKEN": SDK_TOKEN, "QISCUS_ROLE": "admin"}
    monkeypatch.setattr(login_mod, "browser_login", fake_browser_login)
    monkeypatch.setattr(login_mod, "find_browser", lambda: "edge.exe")
    gui.auto_signin = True
    gui.start()                                       # no login saved: sign-in starts by itself
    for _ in range(500):
        if gui.login.get("state") == "ok":
            break
        time_mod.sleep(0.01)
    assert gui.login["state"] == "ok" and gui.state()["logged_in"]
    assert seen["profile"] == gui.exports / ".qexport_signin"
    assert ADMIN_TOKEN in (gui.root / ".env").read_text(encoding="utf-8")


def test_sign_in_window_closed_early_says_so(gui, monkeypatch):
    import time as time_mod

    from qexport import login as login_mod

    def closed(*a, **k):
        raise login_mod.BrowserLoginError("The Qiscus window was closed before signing in finished.")
    monkeypatch.setattr(login_mod, "browser_login", closed)
    gui.start()
    gui.login_browser_start()
    for _ in range(300):
        if gui.login["state"] != "signing_in":
            break
        time_mod.sleep(0.01)
    assert gui.login["state"] == "signin_failed" and "closed before" in gui.login["message"]
    assert not (gui.root / ".env").exists()


def test_login_with_missing_values_explains_what_to_do(gui):
    gui.start()
    with pytest.raises(UserError, match="missing QISCUS_SDK_TOKEN"):
        gui.login_save(f"QISCUS_APP_CODE={APP}\nQISCUS_ADMIN_TOKEN=x\nQISCUS_SDK_USER_ID=y\n")


# --------------------------------------------------------------------------- lifecycle

def test_second_launch_hands_over_to_the_running_window(served, no_desktop, monkeypatch):
    from qexport.gui import server as server_mod
    from qexport.store.checkpoint import RunLock
    srv, _ = served
    exports = srv.app.exports
    (exports / server_mod.INSTANCE_FILE).write_text(json.dumps({"port": srv.port, "token": srv.token}))
    focused = []
    monkeypatch.setattr(winshell, "focus_existing_window", lambda: focused.append(1) or False)
    with RunLock(exports / server_mod.INSTANCE_LOCK):            # the first instance holds this
        rc = server_mod.main(srv.app.root, open_window=True)
    assert rc == 0 and focused == [1]
    opened = [c for c in no_desktop["calls"] if c[0] == "open_app_window"]
    assert opened and f":{srv.port}/#t={srv.token}" in opened[0][1][0]   # no window left: opens one


def test_close_choices(served):
    srv, base = served
    hdr = {"X-QX-Token": srv.token}
    r = httpx.post(f"{base}/api/quit", json={"mode": "background"}, headers=hdr)
    assert r.status_code == 200 and not srv.app.exit_requested.is_set()     # keeps running
    r = httpx.post(f"{base}/api/quit", json={"mode": "stop"}, headers=hdr)
    assert r.status_code == 200 and srv.app.exit_requested.is_set()


def test_app_stops_by_itself_when_no_window_and_nothing_runs(gui, monkeypatch):
    import time as time_mod
    gui.start()
    gui.last_heartbeat = time_mod.monotonic() - 120         # window gone two minutes ago
    for _ in range(300):
        if gui.exit_requested.is_set():
            break
        time_mod.sleep(0.02)
    assert gui.exit_requested.is_set()


# --------------------------------------------------------------------------- diagnostics

def test_gui_diagnostics_sanitization(gui):
    (gui.root / ".env").write_text(ENV, encoding="utf-8")
    gui.start()
    gui.note("test", f"request failed with token {ADMIN_TOKEN} for 6281234567890")
    gui.note("test", f"QISCUS_SDK_TOKEN={SDK_TOKEN}")
    text = gui.diagnostics()
    assert ADMIN_TOKEN not in text and SDK_TOKEN not in text
    assert "6281234567890" not in text and REDACTED in text
    assert "qexport" in text and "== Data store" in text


def test_redact_catches_token_shapes_without_known_values():
    out = redact("QISCUS_ADMIN_TOKEN=abc123xyz\nQiscus-Secret-Key: s3cr3tvalue\nX-QX-Token: tok")
    assert "abc123xyz" not in out and "s3cr3tvalue" not in out and out.count(REDACTED) == 3
