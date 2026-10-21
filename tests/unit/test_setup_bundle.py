"""The single-file installer: it unpacks itself (even copied alone out of the zip) and never
carries credentials or downloaded data."""

import base64
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import build_zip  # noqa: E402


def _bundle(tmp_path) -> Path:
    data = build_zip.payload(build_zip.files())
    return build_zip.build_setup("9.9.9", data, tmp_path / build_zip.SETUP_NAME)


def test_payload_has_the_program_and_nothing_private(tmp_path):
    setup = _bundle(tmp_path)
    text = setup.read_text(encoding="ascii")
    b64 = text[text.rindex(build_zip.MARKER) + len(build_zip.MARKER):]
    names = zipfile.ZipFile(__import__("io").BytesIO(base64.b64decode("".join(b64.split())))).namelist()
    assert "install.bat" in names and "qexport/gui/static/app.js" in names and "qexport/login.py" in names
    assert not any(n == ".env" or n.startswith("exports/") or n.endswith((".sqlite3", ".xlsx")) for n in names)
    assert b"\r\n" in setup.read_bytes() and b"\n" not in setup.read_bytes().replace(b"\r\n", b"")


@pytest.mark.skipif(os.name != "nt", reason="Windows installer")
def test_installer_unpacks_itself_from_a_lone_copy(tmp_path):
    setup = _bundle(tmp_path)
    alone = tmp_path / "zipview"                     # what Explorer does when run from inside a zip
    alone.mkdir()
    lone = alone / setup.name
    lone.write_bytes(setup.read_bytes())
    home = tmp_path / "Qiscus Export"
    (home / "exports").mkdir(parents=True)
    (home / "exports" / "keep.sqlite3").write_text("data")      # an earlier install's data
    (home / ".env").write_text("QISCUS_APP_CODE=x\n")
    (home / "qexport").mkdir()
    (home / "qexport" / "stale_module.py").write_text("")       # removed in the new version
    env = {**os.environ, "QEXPORT_HOME": str(home), "QEXPORT_EXTRACT_ONLY": "1"}
    r = subprocess.run([str(lone)], cwd=alone, env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=120)     # like a double-click: Windows quotes it
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / "install.bat").exists() and (home / "qexport" / "gui" / "server.py").exists()
    assert (home / "exports" / "keep.sqlite3").read_text() == "data" and (home / ".env").exists()
    assert not (home / "qexport" / "stale_module.py").exists()


def test_offline_extras_ride_along_in_the_payload(tmp_path):
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    pyexe = tmp_path / "python-3.12.10-amd64.exe"
    pyexe.write_bytes(b"MZ")
    data = build_zip.payload(build_zip.files(), {f"wheels/{wheel.name}": wheel, f"python/{pyexe.name}": pyexe})
    z = zipfile.ZipFile(__import__("io").BytesIO(data))
    assert z.read("wheels/demo-1.0-py3-none-any.whl") == b"wheel" and "python/python-3.12.10-amd64.exe" in z.namelist()
    bat = (Path(build_zip.ROOT) / "install.bat").read_text(encoding="utf-8")
    assert '--no-index --find-links "%~dp0wheels"' in bat and "pip install --upgrade pip" not in bat
