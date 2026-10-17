"""The Job Desk's history: a line for each time a pass at a job ends (at its review page, sent, a stop for the person,
failed, skipped, or cut short by the desk stopping), kept in the person's own folder (HISTORY_FILE), and what some days
of it come to (the desk's "This week" window, and Claude's desk_history tool).

A line holds the job (its number, employer, title and the application's host), when the pass began and ended and how
many minutes it took, how it ended (and, for a stop, its kind), the desk's own words for why (cut short, with anything
that could be an answer, a code, an email address or an address's tokens taken out: short_reason), the steps it pressed
through, how many times the desk's rounds took the person to the job and they pressed Resume for it, and Jobs at once.
Never an answer, a code, a password or a page's form.

Lines are appended one at a time; past HISTORY_MAX lines the file is cut back to its last HISTORY_KEEP, whole or not at
all. A line that won't read (cut short, changed by hand) is passed over. A write that fails (a folder that can't be
written, a full disk) is said once in the log, and never stops a job."""

from __future__ import annotations

import json
import logging
import re
import statistics
import time
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

HISTORY_FILE = "runs.jsonl"
HISTORY_MAX = 5000  # lines past which the file is cut back
HISTORY_KEEP = 4000  # and how many of the newest it keeps then
REASON_CHARS = 200
# How a pass ended: at its review page; sent; waiting on the person (with its kind of stop: "need"); failed; skipped (by
# the person or the desk); cut short by the desk stopping
OUTCOMES = ("ready", "submitted", "needs_you", "failed", "skipped", "stopped")
FIELDS = ("job_id", "company", "title", "host", "started", "ended", "minutes", "outcome", "need", "reason", "pages",
          "rounds", "resumes", "jobs_at_once")
_MIN_LINE = 50  # bytes: no line written is shorter, so a file smaller than HISTORY_MAX of them is never counted

_said: set[str] = set()  # (what went wrong with the file, said once in the log)


def history_path() -> Path:
    return config.home() / HISTORY_FILE


def stamp(t: float) -> str:
    """A time as the history keeps it: this computer's local time with its offset, to the second."""
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def when(entry: dict[str, Any]) -> float | None:
    """When a line's pass ended, or None for one that doesn't say (in a form that reads)."""
    said = entry.get("ended")
    if not isinstance(said, str):
        return None
    try:
        t = datetime.fromisoformat(said.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:  # (written without its offset: this computer's time)
        t = t.astimezone()
    return t.timestamp()


_URL_REST = re.compile(r"(https?://[^\s?#\"'”]+)[?#][^\s\"'”]*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_DIGITS = re.compile(r"\b\d{5,}\b")  # an emailed code, a ZIP, a phone's digits run together


def short_reason(text: Any, hide: Iterable[str] = ()) -> str:
    """The desk's reason for a stop as a line keeps it: on one line, cut to about REASON_CHARS, without `hide` (the
    run's answers, codes and the desk's secrets), an email address, a run of five digits or more, or what follows an
    address's path (a reset link's token, a session's): the desk's own words, and a page's only as far as they name
    nothing typed."""
    said = " ".join(str(text or "").split())
    for value in sorted({v.strip() for v in hide if isinstance(v, str) and len(v.strip()) >= 4}, key=len, reverse=True):
        said = said.replace(value, "…")
    said = _DIGITS.sub("…", _EMAIL.sub("…", _URL_REST.sub(r"\1", said)))
    return said if len(said) <= REASON_CHARS else said[:REASON_CHARS - 1].rstrip() + "…"


def _once(what: str) -> None:
    if what not in _said:
        _said.add(what)
        logging.getLogger(__name__).warning("couldn't %s the Job Desk's history (%s)", what, HISTORY_FILE, exc_info=True)


def append(entry: dict[str, Any]) -> bool:
    """Add a line, and cut the file back once it's past HISTORY_MAX lines. False when it couldn't be written: said once
    in the log, and nothing else is the worse for it."""
    try:
        path = history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        _once("write")
        return False
    try:
        _trim(path)
    except Exception:  # (written: kept longer than it should be, and cut back at the next line)
        _once("cut back")
    return True


def _trim(path: Path) -> None:
    if path.stat().st_size < HISTORY_MAX * _MIN_LINE:
        return
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) > HISTORY_MAX:
        config.write_text_atomic(path, "".join(line + "\n" for line in lines[-HISTORY_KEEP:]))


def read() -> list[dict[str, Any]]:
    """Every line that reads, oldest first: one cut short or changed by hand is passed over, and a file that can't be
    read at all is none (said once in the log)."""
    try:
        text = history_path().read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except Exception:
        _once("read")
        return []
    entries = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def summary(days: int = 7, recent: int = 20, now: float | None = None) -> dict[str, Any]:
    """What the last `days` days of the history come to: the jobs filled up to their review page (or sent), sent,
    failed and skipped; the stops for the person by kind; the passes the desk's stopping cut short; the minutes spent
    on the jobs (in all, and the median of a job's); the employers it stopped at most (the top 5, with the kind of stop
    each most often was); and the newest `recent` lines, newest first. `any`: whether there's any history at all."""
    now = time.time() if now is None else now
    since = now - days * 86400
    entries = read()
    window = [(t, e) for e in entries if (t := when(e)) is not None and t >= since]
    window.sort(key=lambda pair: pair[0])

    def job(i: int, e: dict[str, Any]) -> Any:
        return e["job_id"] if type(e.get("job_id")) is int else ("line", i)  # (a line with no number: a job of its own)

    def jobs(*outcomes: str) -> int:
        return len({job(i, e) for i, (_, e) in enumerate(window) if e.get("outcome") in outcomes})

    pauses: Counter[str] = Counter()
    minutes: dict[Any, float] = {}
    stops: dict[str, Counter[str]] = {}
    for i, (_, e) in enumerate(window):
        outcome = e.get("outcome")
        kind = (str(e.get("need") or "") or "other") if outcome == "needs_you" else "failed" if outcome == "failed" \
            else ""
        if outcome == "needs_you":
            pauses[kind] += 1
        if kind:
            where = " ".join(str(e.get("company") or e.get("host") or "").split()) or "(no employer named)"
            stops.setdefault(where, Counter())[kind] += 1
        if (m := _number(e.get("minutes"))) is not None:
            minutes[job(i, e)] = minutes.get(job(i, e), 0.0) + m
    worked = sorted(m for m in minutes.values() if m > 0)
    top = sorted(stops.items(), key=lambda kv: -sum(kv[1].values()))[:5]  # (ties: the one seen first stays first)
    return {
        "days": days, "since": stamp(since), "any": bool(entries), "lines": len(window),
        "jobs": len({job(i, e) for i, (_, e) in enumerate(window)}),
        "reviewed": jobs("ready", "submitted"), "submitted": jobs("submitted"),
        "failed": jobs("failed"), "skipped": jobs("skipped"), "stopped": jobs("stopped"),
        "pauses": dict(pauses.most_common()),
        "minutes": round(sum(worked), 1), "median_minutes": round(statistics.median(worked), 1) if worked else None,
        "timed_jobs": len(worked),
        "stops": [{"company": where, "times": sum(kinds.values()), "kind": kinds.most_common(1)[0][0],
                   "kinds": dict(kinds.most_common())} for where, kinds in top],
        "recent": [{k: e[k] for k in FIELDS if k in e} for _, e in reversed(window[-recent:])] if recent > 0 else [],
    }
