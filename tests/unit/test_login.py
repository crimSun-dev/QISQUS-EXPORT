"""Browser sign-in: a real Edge/Chrome window on a fake dashboard that stores a session later."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from qexport import login
from qexport.login import BrowserLoginError, browser_login

SESSION = {"user": {"authentication_token": "adm-tok-123456", "type_as_string": "admin",
                    "app": {"app_code": "test-app-code"}},
           "details": {"sdk_user": {"id": "test-app_admin@qismo.com", "token": "sdk-tok-654321"}}}
PAGE = f"""<!doctype html><title>fake dashboard</title><p>signing in…</p>
<script>setTimeout(() => localStorage.setItem('qismo-data', JSON.stringify({json.dumps(SESSION)})), 1500);</script>"""


@pytest.fixture
def fake_dashboard():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/qiscus/"
    srv.shutdown()


@pytest.mark.skipif(login.find_browser() is None, reason="needs Edge or Chrome")
def test_browser_login_picks_up_the_session_and_closes_the_window(fake_dashboard, tmp_path):
    seen = []
    creds = browser_login(tmp_path / "profile", url=fake_dashboard, timeout_s=60, on_status=seen.append)
    assert creds == {"QISCUS_APP_CODE": "test-app-code", "QISCUS_ADMIN_TOKEN": "adm-tok-123456",
                     "QISCUS_SDK_USER_ID": "test-app_admin@qismo.com", "QISCUS_SDK_TOKEN": "sdk-tok-654321",
                     "QISCUS_ROLE": "admin"}
    assert seen[0].startswith("A Qiscus window opened") and seen[-1].startswith("Signed in")


def test_closing_the_window_early_is_reported(monkeypatch, tmp_path):
    class Gone:
        def poll(self):
            return 0
    monkeypatch.setattr(login.subprocess, "Popen", lambda *a, **k: Gone())
    with pytest.raises(BrowserLoginError, match="closed before"):
        browser_login(tmp_path / "p", browser="fake-browser.exe", timeout_s=5)


def test_no_browser_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(login, "find_browser", lambda: None)
    with pytest.raises(BrowserLoginError, match="Edge or Google Chrome"):
        browser_login(tmp_path / "p")
