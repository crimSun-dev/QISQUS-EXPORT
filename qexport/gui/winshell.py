"""The few operating-system actions the GUI needs. Windows first; macOS/Linux get best effort."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

IS_WINDOWS = os.name == "nt"
WINDOW_TITLE = "Qiscus Export"      # the page <title>; app-mode windows show it as the window title


def open_path(path: Path) -> None:
    """Open a file or folder with its default program (Excel for .xlsx, Explorer for folders)."""
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def reveal(path: Path) -> None:
    """Open the folder that holds `path`, with the file selected."""
    path = Path(path).resolve()
    if IS_WINDOWS:
        if not _shell_select(path):
            # Explorer only understands /select,"<path>" with the quotes after the comma. A list
            # argument gets quoted whole ("/select,G:\a b\c.xlsx") whenever the path has a space,
            # and Explorer then silently opens Documents instead. Pass the command line verbatim.
            subprocess.Popen(f'explorer.exe /select,"{path}"')
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


def _shell_select(path: Path) -> bool:
    """SHOpenFolderAndSelectItems: opens (or reuses) the folder window and selects the file,
    with no command-line quoting involved. False if the shell refused."""
    import ctypes
    from ctypes import wintypes

    shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
    ole32.CoInitialize(None)          # this runs on a request thread; the shell call needs COM
    pidl = ctypes.c_void_p()
    try:
        shell32.SHParseDisplayName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_void_p), wintypes.ULONG,
                                               ctypes.POINTER(wintypes.ULONG)]
        if shell32.SHParseDisplayName(str(path), None, ctypes.byref(pidl), 0, None) != 0 or not pidl:
            return False
        shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
                                                       wintypes.DWORD]
        return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0
    except (OSError, AttributeError):
        return False
    finally:
        if pidl:
            ole32.CoTaskMemFree(pidl)
        ole32.CoUninitialize()


def recycle(path: Path) -> None:
    """Move to the Recycle Bin (Windows) so a mistaken delete can be undone; elsewhere, delete."""
    if not IS_WINDOWS:
        path.unlink()
        return
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16),
                    ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]
    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom=str(path.resolve()) + "\0",
                         fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or path.exists():
        raise OSError(f"could not move {path.name} to the Recycle Bin (code {rc}); is it open in Excel?")


def clipboard_set(text: str) -> bool:
    from qexport.cli import _clipboard_set

    return _clipboard_set(text)


def clipboard_get() -> str:
    from qexport.cli import _clipboard_get

    return _clipboard_get()


def _browser_for_app_mode() -> str | None:
    """Edge (on every Windows 10/11 PC) or Chrome, which can open a page as a plain app window."""
    if IS_WINDOWS:
        roots = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        for root in filter(None, roots):
            for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
                p = Path(root) / rel
                if p.exists():
                    return str(p)
        return None
    for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        if found := shutil.which(name):
            return found
    return None


def open_app_window(url: str) -> None:
    """Open the GUI as its own window (no tabs or address bar); fall back to the default browser."""
    exe = _browser_for_app_mode()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1280,880"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    webbrowser.open(url)


def focus_existing_window() -> bool:
    """Bring an open Qiscus Export window to the front. True if one was found.

    The process the user just launched is allowed to hand the foreground to another window,
    which the running GUI server (a background process) is not."""
    if not IS_WINDOWS:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def check(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith(WINDOW_TITLE):
                found.append(hwnd)
                return False
        return True
    user32.EnumWindows(EnumProc(check), 0)
    if not found:
        return False
    SW_RESTORE = 9
    if user32.IsIconic(found[0]):
        user32.ShowWindow(found[0], SW_RESTORE)
    user32.SetForegroundWindow(found[0])
    return True


def message_box(title: str, text: str) -> None:
    """A plain OK dialog, for errors before the window exists (pythonw has no console)."""
    if IS_WINDOWS:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)   # MB_ICONINFORMATION
    else:
        print(f"{title}: {text}", file=sys.stderr)
