"""The Job Desk's own window: its page in the person's browser as an "app" window, with no tabs and no address bar.

The browser is the person's own Edge or Chrome (on Linux, Chromium too), on their own profile there, started with the
page's address and a window size and nothing else, ever: no --user-data-dir, no --remote-debugging-port, no switch that
turns a protection off. It is never the browser the desk fills applications in (browser.py, ~/.job-apply/browser), and
the desk's automation never attaches to it. Where there's no such browser, or it won't start, the page opens in a tab
of the person's usual browser, as it did before.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

WINDOW_SIZE = "1280,860"  # the window's width and height as it first opens
# Linux: the first of these found on the PATH
LINUX_BROWSERS = ("google-chrome", "google-chrome-stable", "microsoft-edge", "microsoft-edge-stable", "chromium",
                  "chromium-browser")
MAC_APPLICATIONS = Path("/Applications")  # where macOS keeps its programs
# Windows' flags for a program started on its own, with no console of the desk's and none of its Ctrl+C (subprocess
# has them on Windows alone; these are Windows' own numbers for them)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


def _system() -> str:
    """This computer's system, as the browsers are looked for on it: "windows", "macos", or "linux" for any other."""
    if sys.platform == "win32":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def _windows_browsers() -> list[Path]:
    """Where Edge and Chrome are installed on Windows, in the order they're looked for: Edge (every Windows has it)
    under Program Files (x86), then Program Files; Chrome under Program Files, Program Files (x86), then the person's
    own Local AppData (installed for them alone). A folder the environment doesn't name in full is left out: never a
    program under whatever folder the desk happened to start in."""
    edge = Path("Microsoft", "Edge", "Application", "msedge.exe")
    chrome = Path("Google", "Chrome", "Application", "chrome.exe")
    x86, files, local = (os.environ.get(name) or "" for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"))
    return [Path(folder) / program
            for folder, program in ((x86, edge), (files, edge), (files, chrome), (x86, chrome), (local, chrome))
            if folder and Path(folder).is_absolute()]


def _mac_browsers() -> list[Path]:
    """Chrome, then Edge, where macOS keeps them."""
    return [MAC_APPLICATIONS / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
            MAC_APPLICATIONS / "Microsoft Edge.app" / "Contents" / "MacOS" / "Microsoft Edge"]


def find_browser() -> str | None:
    """The program of a browser that can show the desk in a window of its own: the first that's there of the ones
    looked for on this system, or None."""
    system = _system()
    if system == "linux":
        return next((found for name in LINUX_BROWSERS if (found := shutil.which(name))), None)
    places = _windows_browsers() if system == "windows" else _mac_browsers()
    # (isfile: a folder that can't be looked into is no browser there, never an error)
    return next((str(place) for place in places if os.path.isfile(place)), None)


def detached() -> dict[str, Any]:
    """Popen's options for a program started on its own, never waited for: it outlasts the desk, and nothing of the
    desk's reaches it (its console, its Ctrl+C)."""
    if _system() == "windows":
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def open_window(url: str, mode: str) -> str:
    """Open the desk's page at `url` and say how: "app", in a window of its own (its address ending `&w=app`, which
    tells the page so), or "tab", in a tab of the person's usual browser, as before: when `mode`
    (settings.desk_window) is "tab", when there's no browser for a window, or when the one found won't start."""
    browser = None if mode == "tab" else find_browser()
    if browser is not None:
        # on its own, never waited for: the window outlasts the desk, and nothing of the desk's reaches it (its
        # console, its Ctrl+C, what it prints)
        try:
            # these two arguments and no other, ever (the module's rule, above)
            subprocess.Popen([browser, f"--app={url}&w=app", f"--window-size={WINDOW_SIZE}"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             **detached())
            return "app"
        except OSError:
            pass  # (found, but it won't start: a tab instead)
    webbrowser.open(url)
    return "tab"


def _folder_opener() -> list[str]:
    """The program that shows a folder in the system's file manager, outside Windows (which has os.startfile)."""
    return ["open"] if _system() == "macos" else ["xdg-open"]


def open_folder(folder: Path) -> bool:
    """Show `folder` in the system's file manager: Windows' own (os.startfile, which opens a folder in Explorer), or
    macOS' open and Linux's xdg-open, given the folder's whole path as their one argument. Never through a shell.
    Whether it started."""
    target = folder.resolve()
    if not target.is_dir():
        return False
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # (Windows alone has it: Explorer opens the folder)
        else:
            subprocess.Popen([*_folder_opener(), str(target)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True
