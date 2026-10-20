"""The workbooks in the exports folder: what each one holds and whether it is complete.

Counts come from each file's Export Info sheet, read straight from the .xlsx zip (openpyxl writes
inline strings, so that one small sheet is all that needs parsing) and cached by size + mtime."""

from __future__ import annotations

import json
import logging
import threading
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

log = logging.getLogger("qexport.gui")

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
       "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}


def read_export_info(path: Path) -> dict[str, str]:
    """Item -> Value from the workbook's Export Info sheet ({} if it has none)."""
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rid = next((s.get(f"{{{_NS['r']}}}id") for s in wb.iterfind("m:sheets/m:sheet", _NS)
                    if s.get("name") == "Export Info"), None)
        if rid is None:
            return {}
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels.iterfind("rel:Relationship", _NS) if r.get("Id") == rid)
        target = target.lstrip("/")
        sheet = ET.fromstring(z.read(target if target.startswith("xl/") else f"xl/{target}"))
        shared: list[str] | None = None
        out: dict[str, str] = {}
        for row in sheet.iterfind("m:sheetData/m:row", _NS):
            cells = []
            for c in list(row.iterfind("m:c", _NS))[:2]:
                t = c.get("t")
                if t == "inlineStr":
                    cells.append("".join(x.text or "" for x in c.iterfind(".//m:t", _NS)))
                elif t == "s":
                    if shared is None:
                        shared = _shared_strings(z)
                    v = c.find("m:v", _NS)
                    cells.append(shared[int(v.text)] if v is not None and v.text else "")
                else:
                    v = c.find("m:v", _NS)
                    cells.append(v.text if v is not None and v.text is not None else "")
            if len(cells) == 2 and cells[0]:
                out.setdefault(cells[0], cells[1])
        return out


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(t.text or "" for t in si.iterfind(".//m:t", _NS)) for si in root.iterfind("m:si", _NS)]


@dataclass
class WorkbookInfo:
    name: str
    path: str
    size: int
    modified: str                 # ISO, local time
    complete: bool | None         # None = could not tell (unreadable / not a qexport file)
    completeness: str
    conversations: int | None = None
    messages: int | None = None
    campaigns: int | None = None
    recipients: int | None = None
    tool: str = ""


def _int(v: str | None) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except ValueError:
        return None


def describe(path: Path) -> WorkbookInfo:
    st = path.stat()
    base = dict(name=path.name, path=str(path), size=st.st_size,
                modified=datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"))
    try:
        info = read_export_info(path)
    except Exception as e:   # noqa: BLE001 - corrupt / foreign / still being written
        log.info("could not read %s: %s", path.name, e)
        return WorkbookInfo(**base, complete=None, completeness="Could not read this file")
    if not info:
        return WorkbookInfo(**base, complete=None, completeness="Not a qexport workbook")
    if "Completeness" in info:
        completeness = info["Completeness"]
        complete = completeness == "Complete"
    else:
        # Made before 1.1: no label, so judge by whether that download had finished.
        skipped = _int(info.get("Skipped / failed items")) or 0
        if info.get("Data as of (UTC)") == "incomplete run":
            complete, completeness = False, "Partial (made before the download finished)"
        elif skipped:
            complete, completeness = False, f"Partial ({skipped:,} items failed)"
        else:
            complete, completeness = True, "Complete"
    if path.stem.endswith("_PARTIAL"):
        complete = False
    recip = info.get("Broadcast recipient rows")
    return WorkbookInfo(
        **base, complete=complete, completeness=completeness,
        conversations=_int(info.get("Conversations")), messages=_int(info.get("Messages")),
        campaigns=_int(info.get("Broadcast jobs")), recipients=_int(recip), tool=info.get("Tool", ""))


class WorkbookIndex:
    """Cached WorkbookInfo per file, persisted next to the workbooks (hidden JSON)."""

    def __init__(self, cache_path: Path) -> None:
        self._cache_path = cache_path
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        try:
            self._cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    def list(self, folders: list[Path]) -> list[WorkbookInfo]:
        seen: dict[str, Path] = {}
        for folder in folders:
            if folder.is_dir():
                for p in folder.glob("*.xlsx"):
                    if not p.name.startswith("~$"):           # Excel's own lock files
                        seen.setdefault(str(p.resolve()), p)
        out, dirty = [], False
        for key, p in seen.items():
            try:
                st = p.stat()
            except OSError:
                continue
            stamp = f"{st.st_size}:{st.st_mtime_ns}"
            with self._lock:
                hit = self._cache.get(key)
            if hit and hit.get("_stamp") == stamp:
                out.append(WorkbookInfo(**{k: v for k, v in hit.items() if k != "_stamp"}))
                continue
            info = describe(p)
            with self._lock:
                self._cache[key] = {**asdict(info), "_stamp": stamp}
            dirty = True
            out.append(info)
        with self._lock:
            for key in [k for k in self._cache if k not in seen]:
                del self._cache[key]
                dirty = True
        if dirty:
            self._save()
        out.sort(key=lambda w: w.modified, reverse=True)
        return out

    def _save(self) -> None:
        try:
            with self._lock:
                data = json.dumps(self._cache, ensure_ascii=False)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(self._cache_path)
        except OSError:
            pass


def _data(w: WorkbookInfo) -> int:
    return (w.messages or 0) + (w.recipients or 0)


def old_partials(workbooks: list[WorkbookInfo]) -> list[WorkbookInfo]:
    """Partial workbooks that a newer, fuller one supersedes. Never a Complete file. Without a
    Complete file, the newest partial and the one holding the most data are both kept."""
    newest_first = sorted(workbooks, key=lambda w: (w.modified, w.name), reverse=True)
    latest_complete = next((w for w in newest_first if w.complete), None)
    partials = [w for w in newest_first if w.complete is False]
    if latest_complete is not None:
        return [w for w in partials if (w.modified, w.name) < (latest_complete.modified, latest_complete.name)
                and _data(w) <= _data(latest_complete)]
    if not partials:
        return []
    keep = {partials[0].name, max(partials, key=_data).name}
    return [w for w in partials if w.name not in keep]
