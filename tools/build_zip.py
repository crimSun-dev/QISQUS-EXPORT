"""Build the files to hand to another PC:

  dist/Install Qiscus Export.bat   one self-contained file: double-click it (even straight from
                                   inside the zip) and it unpacks qexport, installs it and opens it
  dist/qexport-<version>.zip       that installer plus INSTALL.txt, for sending around

Never includes credentials (.env), downloaded data (exports/), or the local virtualenv.
Usage:  python tools/build_zip.py
"""

from __future__ import annotations

import base64
import io
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = ["qexport", "tests", "tools", "pyproject.toml", "README.md", "TDD-qiscus-chat-exporter.md",
           ".env.example", ".gitignore", "install.bat", "qexport.bat", "qexport-app.bat", "INSTALL.txt"]
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".venv", "exports", "dist", "build"}
EXCLUDE_SUFFIXES = {".pyc", ".sqlite3", ".lock", ".partial", ".xlsx"}
FORBIDDEN_NAMES = {".env"}
SETUP_NAME = "Install Qiscus Export.bat"
MARKER = "::QXPAYLOAD" + "::"          # split so this file never contains the marker itself

# The PowerShell finds the marker at run time from two halves, so it never matches its own text.
SETUP_SCRIPT = r"""@echo off
rem ===========================================================================
rem  Qiscus Export installer. Double-click to install or update; your login and
rem  downloaded data are kept. Everything qexport needs is packed in this file.
rem ===========================================================================
setlocal EnableExtensions
chcp 65001 >nul <nul
title Qiscus Export setup
set "QX_SELF=%~f0"
if not defined QEXPORT_HOME set "QEXPORT_HOME=%LOCALAPPDATA%\Qiscus Export"
echo.
echo  Installing Qiscus Export {version} into:
echo    %QEXPORT_HOME%
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $m='::QXPAYLOAD'+'::'; $t=[IO.File]::ReadAllText($env:QX_SELF); $i=$t.LastIndexOf($m); if ($i -lt 0) {{ throw 'installer is damaged (no payload)' }}; $b=$t.Substring($i+$m.Length) -replace '[^A-Za-z0-9+/=]',''; $z=Join-Path $env:TEMP ('qexport-'+[guid]::NewGuid().ToString()+'.zip'); [IO.File]::WriteAllBytes($z,[Convert]::FromBase64String($b)); $d=$env:QEXPORT_HOME; New-Item -ItemType Directory -Force -Path $d | Out-Null; $code=Join-Path $d 'qexport'; if (Test-Path $code) {{ Remove-Item -Recurse -Force $code }}; Expand-Archive -Path $z -DestinationPath $d -Force; Remove-Item $z"
if errorlevel 1 (
  echo.
  echo  Could not unpack Qiscus Export. If the antivirus blocked it, allow it and try again.
  pause
  exit /b 1
)
if defined QEXPORT_EXTRACT_ONLY exit /b 0
call "%QEXPORT_HOME%\install.bat"
exit /b %errorlevel%
"""


def to_crlf(path: Path) -> None:
    """cmd.exe needs CRLF line endings; LF-only .bat files break labels and goto."""
    data = path.read_bytes()
    fixed = re.sub(rb"(?<!\r)\n", b"\r\n", data)
    if fixed != data:
        path.write_bytes(fixed)


def files() -> list[Path]:
    out: list[Path] = []
    for item in INCLUDE:
        p = ROOT / item
        if not p.exists():
            continue
        candidates = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file())
        for f in candidates:
            rel = f.relative_to(ROOT)
            if f.name in FORBIDDEN_NAMES or EXCLUDE_PARTS & set(rel.parts) or f.suffix in EXCLUDE_SUFFIXES:
                continue
            if any(part.endswith(".egg-info") for part in rel.parts):
                continue
            out.append(f)
    return out


def payload(chosen: list[Path]) -> bytes:
    """The program as a zip with install.bat at its root."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in chosen:
            z.write(f, f.relative_to(ROOT).as_posix())
    names = zipfile.ZipFile(io.BytesIO(buf.getvalue())).namelist()
    assert not any(n == ".env" or n.endswith("/.env") for n in names), "credentials leaked into the payload"
    assert "install.bat" in names
    return buf.getvalue()


def build_setup(version: str, data: bytes, target: Path) -> Path:
    b64 = base64.b64encode(data).decode("ascii")
    lines = [b64[i:i + 120] for i in range(0, len(b64), 120)]
    text = SETUP_SCRIPT.format(version=version) + "\r\n" + MARKER + "\r\n" + "\r\n".join(lines) + "\r\n"
    target.write_bytes(re.sub(r"(?<!\r)\n", "\r\n", text).encode("ascii"))
    return target


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from qexport import __version__

    for bat in ROOT.glob("*.bat"):
        to_crlf(bat)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    chosen = files()
    setup = build_setup(__version__, payload(chosen), dist / SETUP_NAME)
    target = dist / f"qexport-{__version__}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(setup, SETUP_NAME)
        z.write(ROOT / "INSTALL.txt", "INSTALL.txt")
    print(f"{setup}  ({setup.stat().st_size / 1024:.0f} KB, {len(chosen)} files packed)")
    print(f"{target}  ({target.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
