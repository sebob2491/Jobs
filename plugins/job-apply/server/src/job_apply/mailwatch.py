"""Claude's watch for the Job Desk's mail requests (settings.email_codes, with no working email app password on the
desk): waits until a job on the desk is waiting on an emailed code or link that Claude may read through the Gmail
connector, or about 19 minutes with none, then says so in a line and ends. Claude runs it in the background (its Bash
tool wakes Claude as it ends), then calls mail_requests().

    uv run --project <plugin>/server job-apply-wait-mail --desk <the desk's watch file>

The command open_job_desk() and mail_requests() give names the running desk's watch file (desk.watch_path: its port
and key, readable by the person alone), so no key is ever on the command line. It answers only whether there's
something to do: never which job, nor anything of the person's mail.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

from . import config

WAIT = 19 * 60  # seconds the desk is asked to wait at most (pipeline.MAIL_WATCH_MAX)
SLACK = 60  # and seconds more this waits on its answer


def _newest_desk() -> Path | None:
    """The newest running desk's watch file, when none is named."""
    found = sorted(config.home().glob("desk_watch_*.json"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def watch(desk: Path | None, wait: int = WAIT) -> tuple[int, str]:
    """One wait: its exit status and its line."""
    path: Path | None = desk
    try:
        if path is None:
            path = _newest_desk()  # (one gone as it's looked at: as none at all)
        if path is None:
            raise FileNotFoundError("no watch file")
        info = json.loads(path.read_text(encoding="utf-8"))
        port, token = int(info["port"]), str(info["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return 1, ("The Job Desk isn't open (it has no watch file), so there's nothing to watch: stop watching, and "
                   "start again after open_job_desk().")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/mail/wait?wait={wait}",
                                     headers={"x-desk-token": token})
    # (straight to this computer's own desk: never through a proxy the environment names)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=wait + SLACK) as answer:
            said = json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return 1, (f"The Job Desk on port {port} turned the watch down ({e.code}): it isn't the desk that wrote "
                   f"{path.name}. Stop watching, and start again after open_job_desk().")
    except (OSError, ValueError) as e:
        return 1, (f"The Job Desk isn't open on port {port} any more ({type(e).__name__}): stop watching, and start "
                   "again after open_job_desk().")
    if not isinstance(said, dict):
        return 1, "The Job Desk's answer didn't read. Call mail_requests()."
    if said.get("due"):
        return 0, "A job on the Job Desk is waiting on an emailed code or link: call mail_requests()."
    if said.get("watch"):
        return 0, "Nothing yet. Call mail_requests(), then run this watch again in the background."
    return 0, f"Nothing left to watch ({said.get('why') or 'the desk says so'}). Call mail_requests() to confirm."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Wait for the Job Desk's next mail request (for Claude's Gmail watch)")
    ap.add_argument("--desk", type=Path, help="the desk's watch file (open_job_desk() names it)")
    ap.add_argument("--wait", type=int, default=WAIT, help="seconds to wait at most (1 to %(default)s)")
    args = ap.parse_args(argv)
    status, line = watch(args.desk, max(1, min(WAIT, args.wait)))
    print(line, flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
