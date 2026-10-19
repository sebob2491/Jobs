"""The Job Desk's history: a line for each time a pass at a job ends (at its review page, sent, a stop for the person,
failed, skipped, cut short by the desk stopping, or marked as applied), kept in the person's own folder (HISTORY_FILE),
and what some days of it come to (the desk's "This week" window, and Claude's desk_history tool).

A line holds the job (its number, employer, title and the application's host), when the pass began and ended and how
many minutes it took (waits for the site and the inbox included), how it ended (and, for a stop, its kind and its code:
pipeline.STOP_CODES), the desk's own words for why (cut short: short_reason), the steps it pressed through, how many
times the desk's rounds took the person to the job and they pressed Resume for it, and Jobs at once. Answers, codes
and passwords are kept out of it as far as the desk can tell: the reason has the run's answers, codes and secrets, the
profile's words, email addresses, phone numbers, runs of digits and an address's tokens taken out, should a page's
words it quotes show one.

Lines are appended one at a time; past HISTORY_MAX lines the file is cut back to its last HISTORY_KEEP, whole or not at
all (its lines counted once, then kept count of: _COUNTS). A line that won't read (cut short, changed by hand) is
passed over. A write that fails (a folder that can't be written, a full disk) is said once in the log, and never stops
a job."""

from __future__ import annotations

import json
import logging
import math
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
# How a pass ended: at its review page; sent by the desk; waiting on the person (with its kind of stop: "need", and why:
# "code");
# failed; skipped (by the person or the desk); cut short by the desk stopping; marked as applied (the person's "I
# submitted it", or the tracker saying so already, or the site saying it went in: sent, but not by the desk)
OUTCOMES = ("ready", "submitted", "needs_you", "failed", "skipped", "stopped", "marked_applied")
FIELDS = ("job_id", "company", "title", "host", "started", "ended", "minutes", "outcome", "need", "code", "reason",
          "pages", "rounds", "resumes", "jobs_at_once")
HIDE_PREFIX = 40  # an answer too long to be quoted whole is also hidden by its start, this long

_said: set[str] = set()  # (what went wrong with the file, said once in the log)
# The lines in each history file, by its path: counted once (on the first line written after the desk starts, or after
# a count that couldn't be made), then one more for each line written, so the file is read only when it's cut back
_COUNTS: dict[str, int] = {}


def history_path() -> Path:
    return config.home() / HISTORY_FILE


def stamp(t: float) -> str:
    """A time as the history keeps it: this computer's local time with its offset, to the second."""
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def when(entry: dict[str, Any]) -> float | None:
    """When a line's pass ended, or None for one that doesn't say (in a form that reads, and that this computer's clock
    can say: a time before 1970 without its offset can't be on some)."""
    said = entry.get("ended")
    if not isinstance(said, str):
        return None
    try:
        t = datetime.fromisoformat(said.strip().replace("Z", "+00:00"))
        if t.tzinfo is None:  # (written without its offset: this computer's time)
            t = t.astimezone()
        seconds = t.timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    return seconds if math.isfinite(seconds) else None


_URL_REST = re.compile(r"(https?://[^\s?#\"'”]+)[?#][^\s\"'”]*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_DIGITS = re.compile(r"\b\d{5,}\b")  # an emailed code, a ZIP, a phone's digits run together


def _whole(value: str, ends: bool = True) -> re.Pattern[str]:
    """`value` as a whole word or phrase: not inside a longer word (a word character at either end must border on none;
    with `ends` False, only its start: a long answer's beginning, cut mid-word)."""
    start = r"(?<!\w)" if re.match(r"\w", value) else ""
    end = r"(?!\w)" if ends and re.search(r"\w$", value) else ""
    return re.compile(start + re.escape(value) + end)


def short_reason(text: Any, hide: Iterable[str] = ()) -> str:
    """The desk's reason for a stop as a line keeps it: on one line, cut to about REASON_CHARS, without `hide` (the
    run's answers, codes, the desk's secrets and the profile's words, as whole words or phrases, 4 characters or more;
    one longer than REASON_CHARS by its first HIDE_PREFIX too), an email address, a phone number, a run of five digits
    or more, or what follows an address's path (a reset link's token, a session's): the desk's own words, and a page's
    only as far as they name nothing typed, as far as can be told."""
    said = " ".join(str(text or "").split())
    values = {" ".join(v.split()) for v in hide if isinstance(v, str)}
    for value in sorted((v for v in values if len(v) >= 4), key=len, reverse=True):
        # (looked for as plain words first: a pattern for each of a big profile's values, every line, took ~70 ms)
        if value in said:
            said = _whole(value).sub("…", said)
        if len(value) > REASON_CHARS and value[:HIDE_PREFIX] in said:
            said = _whole(value[:HIDE_PREFIX], ends=False).sub("…", said)
    said = _URL_REST.sub(r"\1", said)
    said = _DIGITS.sub("…", _PHONE.sub("…", _EMAIL.sub("…", said)))
    return said if len(said) <= REASON_CHARS else said[:REASON_CHARS - 1].rstrip() + "…"


def _once(what: str) -> None:
    if what not in _said:
        _said.add(what)
        logging.getLogger(__name__).warning("couldn't %s the Job Desk's history (%s)", what, HISTORY_FILE,
                                            exc_info=True)


def _count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def append(entry: dict[str, Any]) -> bool:
    """Add a line, and cut the file back once it's past HISTORY_MAX lines. False when it couldn't be written: said once
    in the log, and nothing else is the worse for it."""
    key = ""
    try:
        path = history_path()
        key = str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        _COUNTS.pop(key, None)  # (what's in it now isn't known: counted again at the next line)
        _once("write")
        return False
    try:
        _COUNTS[key] = _COUNTS[key] + 1 if key in _COUNTS else _count(path)
        if _COUNTS[key] > HISTORY_MAX:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > HISTORY_MAX:  # (another desk's lines counted in too: it's the file that says)
                config.write_text_atomic(path, "".join(line + "\n" for line in lines[-HISTORY_KEEP:]))
            _COUNTS[key] = _count(path)
    except Exception:  # (written: kept longer than it should be, and counted again at the next line)
        _COUNTS.pop(key, None)
        _once("cut back")
    return True


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


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _number(value: Any) -> float | None:
    """A count or a number of minutes as a line has it, or None for one that isn't (changed by hand: Infinity, say)."""
    return float(value) if _finite(value) and value >= 0 else None


def _plain(entry: dict[str, Any]) -> dict[str, Any]:
    """A line as the window lists it: its known fields, only those that are plain words or finite numbers (a line
    changed by hand never breaks the window's JSON)."""
    return {k: entry[k] for k in FIELDS if k in entry and (isinstance(entry[k], str) or _finite(entry[k]))}


def summary(days: int = 7, recent: int = 20, now: float | None = None) -> dict[str, Any]:
    """What the last `days` days of the history come to: the jobs filled up to their review page (or sent by the desk),
    sent by the desk, marked as applied, failed and skipped; the stops for the person by kind; the passes the desk's
    stopping cut short; the minutes spent on the jobs (in all, and the median of a job's: waits included, and jobs at
    once overlap); the employers it stopped at most (the top 5, with the kind of stop each most often was); and the
    newest `recent` lines, newest first. `any`: whether there's any history at all."""
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
        "reviewed": jobs("ready", "submitted"), "submitted": jobs("submitted"), "marked": jobs("marked_applied"),
        "failed": jobs("failed"), "skipped": jobs("skipped"), "stopped": jobs("stopped"),
        "pauses": dict(pauses.most_common()),
        "minutes": round(sum(worked), 1), "median_minutes": round(statistics.median(worked), 1) if worked else None,
        "timed_jobs": len(worked),
        "stops": [{"company": where, "times": sum(kinds.values()), "kind": kinds.most_common(1)[0][0],
                   "kinds": dict(kinds.most_common())} for where, kinds in top],
        "recent": [_plain(e) for _, e in reversed(window[-recent:])] if recent > 0 else [],
    }
