"""Whether a newer job-apply has been published, for the desk to say so: the plugin's manifest
on its repository's default branch, read when the desk starts and every few hours after.
Nothing about the person goes with it: it's a plain download of a public file. A failed
read (offline, GitHub down) says nothing, and is tried again next time.
JOB_APPLY_NO_UPDATE_CHECK=1 turns it off."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx

from . import config

CHECK_EVERY = 6 * 3600  # seconds between looks
# How a person updates: the marketplace this repository publishes, and the plugin in it
UPDATE_COMMANDS = ["claude plugin marketplace update sebob-jobs", "claude plugin update job-apply@sebob-jobs"]


def manifest_url() -> str | None:
    """The published manifest: plugin.json on the default branch of the repository it names."""
    try:
        repo = json.loads((config.PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["repository"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    m = re.fullmatch(r"https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?", str(repo).strip())
    if not m:
        return None
    return f"https://raw.githubusercontent.com/{m[1]}/{m[2]}/main/plugins/job-apply/.claude-plugin/plugin.json"


def _parts(version: str) -> tuple[int, ...] | None:
    if not re.fullmatch(r"\d+(\.\d+)*", version or ""):
        return None
    return tuple(int(p) for p in version.split("."))


def newer(latest: str, current: str) -> bool:
    """Is `latest` a later version than `current`? 0.3.10 is after 0.3.9."""
    a, b = _parts(latest), _parts(current)
    return a is not None and b is not None and a > b


class UpdateCheck:
    """The newest published version, as last read."""

    def __init__(self) -> None:
        self.latest: str | None = None
        self.checked_at = 0.0

    async def refresh(self) -> None:
        if os.environ.get("JOB_APPLY_NO_UPDATE_CHECK") == "1" or time.time() - self.checked_at < CHECK_EVERY:
            return
        url = manifest_url()
        if url is None:
            return
        self.checked_at = time.time()
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                response = await client.get(url)
            response.raise_for_status()
            version = str(response.json()["version"])
        except Exception:  # offline, or a manifest that isn't one: nothing said, looked at again later
            return
        if _parts(version) is not None:
            self.latest = version

    def notice(self) -> dict[str, Any] | None:
        """What the desk page shows: the newer version and how to get it, or None."""
        current = config.plugin_version()
        if self.latest and newer(self.latest, current):
            return {"latest": self.latest, "current": current, "commands": UPDATE_COMMANDS}
        return None
