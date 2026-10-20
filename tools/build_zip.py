"""Build dist/qexport-<version>.zip for copying to another PC.

Never includes credentials (.env), downloaded data (exports/), or the local virtualenv.
Usage:  python tools/build_zip.py
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = ["qexport", "tests", "tools", "pyproject.toml", "README.md", "TDD-qiscus-chat-exporter.md",
           ".env.example", ".gitignore", "install.bat", "qexport.bat", "INSTALL.txt"]
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".venv", "exports", "dist", "build"}
EXCLUDE_SUFFIXES = {".pyc", ".sqlite3", ".lock", ".partial", ".xlsx"}
FORBIDDEN_NAMES = {".env"}


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


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from qexport import __version__

    for bat in ROOT.glob("*.bat"):
        to_crlf(bat)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    target = dist / f"qexport-{__version__}.zip"
    chosen = files()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in chosen:
            z.write(f, Path("qexport") / f.relative_to(ROOT))
    names = zipfile.ZipFile(target).namelist()
    assert not any(n.endswith("/.env") for n in names), "credentials leaked into the zip"
    print(f"{target}  ({len(names)} files, {target.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
