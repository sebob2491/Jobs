"""Whether a newer job-apply has been published, and the desk's Update button (design F2).

Three kinds of copy:
- `installed`: the Windows installer's (bin/uvw.exe beside the plugin's folder). Newer is the newest Release of the
  repository the installed plugin.json names, once it holds both JobDeskSetup.exe and its .sha256. Updating downloads
  that installer, checks its SHA-256, and starts it silently, for the person alone; the desk then stops.
- `plugin`: inside Claude, with a `claude` command found. Newer is plugin.json on the repository's default branch.
  Updating runs Claude's two update commands.
- `manual`: anything else (a copy run from the source code). Newer is as for a plugin copy; it can't update itself.

What is read is looked at when the desk starts, every 6 hours after, and when the page asks (at most once a minute).
Nothing about the person goes with it: plain downloads of public files. A failed read (offline, GitHub down) says
nothing, and is tried again next time. JOB_APPLY_NO_UPDATE_CHECK=1 turns it off.

Where a download comes from is decided by the installed plugin.json alone: of the Release's answer only its version is
used, once it is digits and dots, and the tag and file names are built again from it. Downloads are https only, with
redirects only to https, and stop at MAX_DOWNLOAD while they stream. Nothing is started before the SHA-256 matches.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from . import config, letters, window

CHECK_EVERY = 6 * 3600  # seconds between looks by itself
CHECK_AT_MOST = 60  # seconds between looks the page asks for (Check now)
# How a person updates: the marketplace this repository publishes, and the plugin in it
UPDATE_COMMANDS = ["claude plugin marketplace update sebob-jobs", "claude plugin update job-apply@sebob-jobs"]
COMMAND_TIMEOUT = 120  # seconds each of the two commands may take
TAG_PREFIX = "job-apply-v"  # a Release's tag: this plus the version (the installer workflow's)
INSTALLER = "JobDeskSetup.exe"
CHECKSUM = INSTALLER + ".sha256"
MAX_DOWNLOAD = 300 * 1024 * 1024  # bytes: an installer bigger than this is refused
MAX_SMALL = 1024 * 1024  # bytes: the Release's answer, plugin.json, the checksum file
MAX_REDIRECTS = 5
# Inno Setup's: progress window and errors shown, for the person alone (never an administrator), no restart, and the
# desk opened again afterwards (JobDeskSetup.iss: Relaunch). /LOG= follows, with a path under `updates`
INSTALLER_ARGS = ("/SILENT", "/CURRENTUSER", "/NORESTART", "/RELAUNCH=1")
UNDER_WAY = ("downloading", "installing", "updating")
UPDATED_TO = "updated-to"  # (in `updates`: the version an installer was started for, said once by the desk it opens)

# The sentences: 409s from /api/update/start, and `error` when `progress` is "failed"
NOT_NEWER = "There's no newer version to update to."
MANUAL = "This copy isn't one the desk can update by itself."
ALREADY = "An update is already under way."
FILLING = "Jobs are being filled. Wait for them to finish, or pause the queue."
NOT_WHOLE = "The download didn't arrive whole."
NOT_DOWNLOADED = "The new version couldn't be downloaded. The desk needs the internet for that."
WONT_START = "The installer couldn't be started."
CLAUDE_FAILED = "Claude couldn't update the plugin."

_VERSION = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,9}){0,5}")  # digits and dots, and nothing else (ASCII digits only)
_REPOSITORY = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?")
_CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  JobDeskSetup\.exe\n?")


class UpdateFailed(Exception):
    """An update that stopped, with the one plain sentence the page says."""


def repository() -> tuple[str, str] | None:
    """The owner and name of the repository the installed plugin.json names, or None."""
    try:
        repo = json.loads((config.PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
                          .read_text(encoding="utf-8"))["repository"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    m = _REPOSITORY.fullmatch(str(repo).strip())
    if not m or m[1] in (".", "..") or m[2] in (".", ".."):
        return None
    return m[1], m[2]


def manifest_url() -> str | None:
    """The published manifest: plugin.json on the default branch of the repository it names."""
    repo = repository()
    if repo is None:
        return None
    return f"https://raw.githubusercontent.com/{repo[0]}/{repo[1]}/main/plugins/job-apply/.claude-plugin/plugin.json"


def release_url() -> str | None:
    """The newest Release of the repository the installed plugin.json names (GitHub's "latest")."""
    repo = repository()
    return None if repo is None else f"https://api.github.com/repos/{repo[0]}/{repo[1]}/releases/latest"


def download_url(version: str, name: str) -> str | None:
    """A Release's file, from the installed plugin.json's repository and the tag built from `version` alone."""
    repo = repository()
    if repo is None or _parts(version) is None or name not in (INSTALLER, CHECKSUM):
        return None
    return f"https://github.com/{repo[0]}/{repo[1]}/releases/download/{TAG_PREFIX}{version}/{name}"


def _parts(version: str) -> tuple[int, ...] | None:
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        return None
    return tuple(int(p) for p in version.split("."))


def newer(latest: str, current: str) -> bool:
    """Is `latest` a later version than `current`? 0.3.10 is after 0.3.9."""
    a, b = _parts(latest), _parts(current)
    return a is not None and b is not None and a > b


def installed_copy() -> bool:
    """The Windows installer's copy: its bin\\uvw.exe beside the plugin's folder."""
    return (config.PLUGIN_ROOT.parent / "bin" / "uvw.exe").is_file()


def updates_folder() -> Path:
    """Where downloaded installers and their logs are kept: `updates` in the desk's folder."""
    return config.home() / "updates"


def _no_window() -> dict[str, Any]:
    """Windows: a command run without a console window of its own (the desk itself may have none)."""
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)} if sys.platform == "win32" else {}


class UpdateCheck:
    """The newest published version, as last read, and the update under way. `transport` (httpx), `popen` (starting
    the installer) and `run` (Claude's two commands) are the network's and the computer's: tests hand in their own."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None,
                 popen: Callable[..., Any] | None = None, run: Callable[..., Any] | None = None) -> None:
        self.transport = transport
        self.popen = popen or subprocess.Popen
        self.run = run or subprocess.run
        self.latest: str | None = None
        self.checked_at = 0.0  # when the last look succeeded
        self._tried_at = 0.0  # when the last look began (its pace)
        self.progress: str | None = None
        self.error: str | None = None
        self.output = ""  # what Claude's commands said last (kept here, written nowhere)
        self.updated_to: str | None = None  # (an installed copy just updated to this: noted)
        self._kinds: dict[bool, tuple[str, str | None]] = {}
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ which copy, and what's newer
    def _kind(self, standalone: bool) -> tuple[str, str | None]:
        if standalone not in self._kinds:
            if installed_copy():
                self._kinds[standalone] = ("installed", None)
            else:
                claude = None if standalone else letters.find_claude()
                self._kinds[standalone] = ("plugin", claude) if claude else ("manual", None)
        return self._kinds[standalone]

    def kind(self, standalone: bool = False) -> str:
        """installed, plugin or manual (the module's docstring)."""
        return self._kind(standalone)[0]

    def under_way(self) -> bool:
        return self.progress in UNDER_WAY

    def _client(self) -> httpx.AsyncClient:
        # (redirects followed by _fetch, to https only)
        options: dict[str, Any] = {"timeout": 30, "follow_redirects": False}
        if self.transport is not None:
            options["transport"] = self.transport
        return httpx.AsyncClient(**options)

    async def refresh(self, standalone: bool = False, now: bool = False) -> None:
        """Look for a newer version: by itself every CHECK_EVERY, or `now` (the page's Check now), though never more
        than once every CHECK_AT_MOST."""
        if os.environ.get("JOB_APPLY_NO_UPDATE_CHECK") == "1":
            return
        if time.time() - self._tried_at < (CHECK_AT_MOST if now else CHECK_EVERY):
            return
        installed = self.kind(standalone) == "installed"
        url = release_url() if installed else manifest_url()
        if url is None:
            return
        self._tried_at = time.time()
        try:
            async with self._client() as client:
                answer = json.loads(await self._read(client, url, MAX_SMALL))
            version = self._release_version(answer) if installed else str(answer["version"])
        except Exception:  # offline, or an answer that isn't one: nothing said, looked at again later
            return
        if version is not None and _parts(version) is not None:
            self.latest = version
            self.checked_at = time.time()

    @staticmethod
    def _release_version(answer: Any) -> str | None:
        """The version a Release is for: its tag's, when the tag is TAG_PREFIX and digits and dots, and the Release
        holds both the installer and its checksum. Nothing else of the answer is used."""
        if not isinstance(answer, dict) or not isinstance(answer.get("tag_name"), str):
            return None
        tag = answer["tag_name"]
        version = tag[len(TAG_PREFIX):] if tag.startswith(TAG_PREFIX) else ""
        assets = answer.get("assets")
        names = {a.get("name") for a in assets if isinstance(a, dict)} if isinstance(assets, list) else set()
        if _parts(version) is None or not {INSTALLER, CHECKSUM} <= names:
            return None
        return version

    def state(self, standalone: bool = False) -> dict[str, Any]:
        """The state's `update`: always these eight keys."""
        current = config.plugin_version()
        return {"current": current, "latest": self.latest,
                "newer": bool(self.latest and newer(self.latest, current)), "kind": self.kind(standalone),
                "checked_at": self.checked_at, "progress": self.progress,
                "error": self.error if self.progress == "failed" else None, "commands": UPDATE_COMMANDS}

    # ------------------------------------------------------------ downloads
    async def _fetch(self, client: httpx.AsyncClient, url: str, limit: int, take: Callable[[bytes], None]) -> None:
        """GET `url` over https, following redirects only to https, handing each piece to `take`; past `limit` bytes
        (said ahead, or as it streams) it stops. Raises UpdateFailed(NOT_DOWNLOADED) on anything else."""
        for _ in range(MAX_REDIRECTS + 1):
            if urlsplit(url).scheme != "https":
                raise UpdateFailed(NOT_DOWNLOADED)
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                if response.status_code != 200:
                    raise UpdateFailed(NOT_DOWNLOADED)
                said = response.headers.get("content-length", "")
                if said.isdigit() and int(said) > limit:
                    raise UpdateFailed(NOT_DOWNLOADED)
                got = 0
                async for piece in response.aiter_bytes():
                    got += len(piece)
                    if got > limit:
                        raise UpdateFailed(NOT_DOWNLOADED)
                    take(piece)
                return
        raise UpdateFailed(NOT_DOWNLOADED)

    async def _read(self, client: httpx.AsyncClient, url: str, limit: int) -> bytes:
        body = bytearray()
        await self._fetch(client, url, limit, body.extend)
        return bytes(body)

    # ------------------------------------------------------------ the Update button
    def why_not(self, standalone: bool, filling: bool) -> str | None:
        """Why /api/update/start can't start an update now (its 409's sentence), or None."""
        if self.under_way():
            return ALREADY
        if self.kind(standalone) == "manual":
            return MANUAL
        if not self.state(standalone)["newer"]:
            return NOT_NEWER
        if filling:
            return FILLING
        return None

    def start(self, standalone: bool, *, filling: Callable[[], bool], pause: Callable[[], Callable[[], None]],
              quit: Callable[[], None]) -> None:
        """Begin the update for this copy's kind (why_not said nothing against it). It goes on by itself."""
        if self.kind(standalone) == "installed":
            self.progress, self.error = "downloading", None
            work = self.update_installed(filling=filling, pause=pause, quit=quit)
        else:
            self.progress, self.error = "updating", None
            work = self.update_plugin()
        self._task = asyncio.get_running_loop().create_task(work)

    def _failed(self, sentence: str) -> None:
        self.progress, self.error = "failed", sentence

    async def update_installed(self, *, filling: Callable[[], bool], pause: Callable[[], Callable[[], None]],
                               quit: Callable[[], None]) -> None:
        """Update an installed copy, in F2's order; any failure stops it, changes nothing, and says why.
        `filling`: is a job being filled; `pause`: the Pause queue button's pause, giving what undoes it; `quit`: stop
        the desk as Quit does."""
        self.progress, self.error = "downloading", None
        version = self.latest or ""
        folder = updates_folder()
        saved: Path | None = None
        undo: Callable[[], None] | None = None
        try:
            if not newer(version, config.plugin_version()):  # (and digits and dots: newer checks both)
                raise UpdateFailed(NOT_NEWER)
            saved = folder / f"{INSTALLER[:-4]}-{version}.exe"
            # 1. the two addresses: the installed plugin.json's repository and the version alone
            checksum_url, installer_url = download_url(version, CHECKSUM), download_url(version, INSTALLER)
            if checksum_url is None or installer_url is None:
                raise UpdateFailed(NOT_DOWNLOADED)
            folder.mkdir(parents=True, exist_ok=True)
            # 2. the checksum, then the installer (into a .part file: only a whole, matching one becomes the .exe)
            part = saved.with_name(saved.name + ".part")
            digest = hashlib.sha256()
            try:
                async with self._client() as client:
                    line = (await self._read(client, checksum_url, 1024)).decode("ascii", errors="replace")
                    expected = _CHECKSUM_LINE.fullmatch(line)
                    if expected is None:
                        raise UpdateFailed(NOT_WHOLE)
                    part.unlink(missing_ok=True)
                    with part.open("xb") as f:
                        def take(piece: bytes) -> None:
                            digest.update(piece)
                            f.write(piece)

                        await self._fetch(client, installer_url, MAX_DOWNLOAD, take)
                # 3. the SHA-256 must match, or the file goes
                if not hmac.compare_digest(digest.hexdigest(), expected[1]):
                    raise UpdateFailed(NOT_WHOLE)
                os.replace(part, saved)
            finally:
                part.unlink(missing_ok=True)
            # 4. still later than this copy (it may have been updated meanwhile)
            if not newer(version, config.plugin_version()):
                raise UpdateFailed(NOT_NEWER)
            # 5. the queue paused, as Pause queue does (and never while a job is being filled)
            if filling():
                raise UpdateFailed(FILLING)
            undo = pause()
            for older in folder.glob(f"{INSTALLER[:-4]}-*.exe"):
                if older != saved:
                    older.unlink(missing_ok=True)
            # 6. the installer, on its own, with exactly these arguments, never through a shell
            self.progress = "installing"
            try:
                self.popen([str(saved), *INSTALLER_ARGS, f"/LOG={folder / f'install-{version}.log'}"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           **window.detached())
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                raise UpdateFailed(WONT_START) from e
            with contextlib.suppress(OSError):
                (folder / UPDATED_TO).write_text(version, encoding="utf-8")
        except UpdateFailed as e:
            self._fail_installed(saved, undo, str(e))
            return
        except Exception:  # (offline, a disk that's full, anything else: never left "under way")
            self._fail_installed(saved, undo, NOT_DOWNLOADED)
            return
        # 7. the desk stops, as Quit does
        quit()

    def _fail_installed(self, saved: Path | None, undo: Callable[[], None] | None, sentence: str) -> None:
        if undo is not None:
            undo()
        if saved is not None:
            with contextlib.suppress(OSError):
                saved.unlink(missing_ok=True)
        self._failed(sentence)

    async def update_plugin(self) -> None:
        """Update a plugin copy: Claude's two commands, with the claude found, one after the other, no shell, each
        COMMAND_TIMEOUT at most."""
        self.progress, self.error = "updating", None
        claude = self._kind(False)[1]
        if claude is None:
            self._failed(CLAUDE_FAILED)
            return
        loop = asyncio.get_running_loop()
        for command in UPDATE_COMMANDS:
            args = [claude, *command.split()[1:]]
            try:
                done = await loop.run_in_executor(None, functools.partial(
                    self.run, args, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=COMMAND_TIMEOUT, env=letters.child_env(), **_no_window()))
            except Exception as e:  # (it won't start, it ran too long: never left "under way")
                self.output = type(e).__name__
                self._failed(CLAUDE_FAILED)
                return
            self.output = f"{done.stdout or ''}{done.stderr or ''}"[-2000:]
            if done.returncode != 0:
                self._failed(CLAUDE_FAILED)
                return
        self.progress = "done"

    def note_updated(self) -> None:
        """At the desk's start: an installer this desk's update started, now done (this copy is its version), is
        noted for the page to say once. The note goes either way."""
        marker = updates_folder() / UPDATED_TO
        try:
            version = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return
        with contextlib.suppress(OSError):
            marker.unlink()
        if _parts(version) is not None and version == config.plugin_version():
            self.updated_to = version
