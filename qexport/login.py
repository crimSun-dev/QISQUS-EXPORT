"""Sign in to Qiscus in a real browser window and pick the login up automatically.

Opens the Qiscus dashboard in its own Edge/Chrome window (separate profile, remote debugging on
127.0.0.1). The user signs in on Qiscus's own page; once the dashboard has stored its session
(localStorage 'qismo-data', the same values the console snippet copies), we read it over the
DevTools protocol and close the window. The password never passes through qexport."""

from __future__ import annotations

import json
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import websocket

from qexport.cli import CRED_KEYS

QISCUS_DASHBOARD = "https://multichannel.qiscus.com/"

# Runs inside the dashboard page. Null until the dashboard has finished storing the session
# (the SDK user arrives a moment after the admin token).
READ_SESSION_JS = """(() => {
  try {
    const d = JSON.parse(localStorage.getItem('qismo-data') || 'null');
    const u = d && d.user, sdk = d && d.details && d.details.sdk_user;
    if (!u || !u.authentication_token || !u.app || !u.app.app_code || !sdk || !sdk.id || !sdk.token) return null;
    return JSON.stringify({
      QISCUS_APP_CODE: u.app.app_code, QISCUS_ADMIN_TOKEN: u.authentication_token,
      QISCUS_SDK_USER_ID: String(sdk.id), QISCUS_SDK_TOKEN: sdk.token,
      QISCUS_ROLE: u.type_as_string || 'admin'});
  } catch (e) { return null; }
})()"""


class BrowserLoginError(Exception):
    """Sign-in did not finish; the message says why in plain words."""


def find_browser() -> str | None:
    from qexport.gui.winshell import _browser_for_app_mode

    return _browser_for_app_mode()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _targets(port: int) -> list[dict]:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=2)
        return r.json() if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return []


def _evaluate(ws_url: str, expression: str, timeout: float = 5) -> object:
    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": expression, "returnByValue": True}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return ((msg.get("result") or {}).get("result") or {}).get("value")
    finally:
        ws.close()


def _close_browser(port: int, proc: subprocess.Popen) -> None:
    try:
        info = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2).json()
        ws = websocket.create_connection(info["webSocketDebuggerUrl"], timeout=3, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
        ws.close()
        proc.wait(timeout=5)
    except Exception:   # noqa: BLE001 - fall back to ending the process
        proc.terminate()


def read_session(port: int) -> dict[str, str] | None:
    """Login values from any open dashboard page, or None if nobody is signed in yet."""
    for t in _targets(port):
        if t.get("type") != "page" or "qiscus" not in (t.get("url") or ""):
            continue
        try:
            value = _evaluate(t["webSocketDebuggerUrl"], READ_SESSION_JS)
        except Exception:   # noqa: BLE001 - page navigating / reloading; try again next round
            continue
        if isinstance(value, str):
            creds = json.loads(value)
            if all(creds.get(k) for k in CRED_KEYS):
                return creds
    return None


def browser_login(profile_dir: Path, *, url: str = QISCUS_DASHBOARD, timeout_s: float = 900,
                  on_status: Callable[[str], None] | None = None,
                  browser: str | None = None) -> dict[str, str]:
    """Open the sign-in window and wait until the user is signed in. Returns the QISCUS_* values.

    The profile is kept, so the next refresh is usually instant (still signed in there)."""
    say = on_status or (lambda _m: None)
    exe = browser or find_browser()
    if exe is None:
        raise BrowserLoginError("Couldn't find Microsoft Edge or Google Chrome on this PC.")
    profile_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    proc = subprocess.Popen(
        [exe, f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
         f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check",
         "--disable-features=Translate", "--window-size=1100,820", f"--app={url}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    say("A Qiscus window opened. Sign in there with your admin account.")
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise BrowserLoginError("The Qiscus window was closed before signing in finished.")
            creds = read_session(port)
            if creds:
                say("Signed in. Connecting…")
                return creds
            time.sleep(1.0)
        raise BrowserLoginError("Signing in took too long. Try again.")
    finally:
        if proc.poll() is None:
            _close_browser(port, proc)
