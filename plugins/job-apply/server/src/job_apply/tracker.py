"""SQLite application tracker."""

from __future__ import annotations

import builtins
import contextlib
import csv
import functools
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from collections.abc import Callable, Iterable
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import config
from .ats import ATS_NAMES, JOB_BOARDS, detect_ats, job_board

STATUSES = [
    "saved",  # ingested, not started
    "in_progress",  # form partly filled
    "ready_to_submit",  # everything filled, waiting on a person
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "withdrawn",
    "skipped",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    apply_url TEXT DEFAULT '',
    title TEXT DEFAULT '',
    company TEXT DEFAULT '',
    location TEXT DEFAULT '',
    ats TEXT DEFAULT '',
    source TEXT DEFAULT '',
    external_id TEXT DEFAULT '',
    salary TEXT DEFAULT '',
    employment_type TEXT DEFAULT '',
    posted_at TEXT DEFAULT '',
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'saved',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    applied_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS emails (
    thread_id TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    summary TEXT DEFAULT '',
    received_at TEXT DEFAULT '',
    logged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    status TEXT DEFAULT '',
    note TEXT DEFAULT ''
);
-- a job's other addresses: a job board's (Indeed's) for a job kept at the employer's own site (Tracker.upsert)
CREATE TABLE IF NOT EXISTS aliases (
    url TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS aliases_job ON aliases(job_id);
-- jobs merged into another, one posting saved twice: the desk's kept queue may still name them (Tracker.merged_into)
CREATE TABLE IF NOT EXISTS merged (
    id INTEGER PRIMARY KEY,
    into_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE
);
-- jobs that may be one posting (alike, a job board's copy and the employer's) but weren't made one: the desk asks the
-- person before applying to one while the other is begun or done (Tracker.maybe_same). Each way round; `cleared` once
-- the person said Apply anyway for job_id
CREATE TABLE IF NOT EXISTS maybe_same (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    other_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    cleared INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (job_id, other_id)
);
-- openings and jobs the person said they're not interested in (the desk's Not interested): hidden from the desk's
-- openings and never queued, until their Undo. By address (most openings aren't saved as jobs), and, for one that's a
-- job, by its number on each of its addresses: the mark is the job's, whichever site its posting is read from
-- (NotInterested). With the employer, the title and the reason given (NOT_INTERESTED_REASONS, or none). "company" lowers
-- that employer's other openings (recommend.not_interested_in). (job_id isn't a reference: a job merged into another
-- passes its marks on, _fold). The opening's place too: one marked before its copy on another site was saved is that
-- job's when the tracker finds them the same posting (Tracker.not_interested_marks)
CREATE TABLE IF NOT EXISTS not_interested (
    url TEXT PRIMARY KEY,
    job_id INTEGER,
    company TEXT DEFAULT '',
    title TEXT DEFAULT '',
    location TEXT DEFAULT '',
    req_ids TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS not_interested_job ON not_interested(job_id);
"""
# The reasons the desk's Not interested offers (each optional)
NOT_INTERESTED_REASONS = ("title", "company", "location", "pay", "other")


class NotInterested:
    """The person's Not interested marks, read once (Tracker.not_interested_marks), to check openings and jobs against:
    an opening is marked when its job (by its number) is, or any of the job's addresses (its own, where it's applied
    for, its other sites': aliases), or the opening's own address. So a job marked on Indeed stays marked once the
    employer's posting is found and made one with it, and the other way round. `matched`: openings marked before they
    were a job, by address, each with the job saved since that the tracker finds the same posting (same_posting): the
    Indeed copy of an employer's posting marked on the desk is marked too."""

    def __init__(self, rows: list[dict[str, Any]], matched: dict[str, int] | None = None):
        self.by_url = {r["url"]: r for r in rows}
        self.matched = matched or {}
        self.by_job = {**{job_id: self.by_url[u] for u, job_id in self.matched.items()},
                       **{r["job_id"]: r for r in rows if r.get("job_id") is not None}}

    def __bool__(self) -> bool:
        return bool(self.by_url)

    def mark(self, url: str | None = None, job: dict[str, Any] | None = None,
             aliases: Iterable[str] = ()) -> dict[str, Any] | None:
        """The mark on this opening (its address) or job (the tracker's row, and its other addresses), if any."""
        if job is not None and job.get("id") in self.by_job:
            return self.by_job[job["id"]]
        addresses = [url, *((job.get("url"), job.get("apply_url")) if job is not None else ()), *aliases]
        return next((self.by_url[u] for u in {normalize_url(a) for a in addresses if a} if u in self.by_url), None)

# What an employer email means for the application's status.
EMAIL_CATEGORIES = {
    "confirmation": "applied",  # "we received your application"
    "assessment": "interviewing",  # online test, questionnaire, HireVue
    "interview": "interviewing",
    "offer": "offer",
    "rejection": "rejected",
    "other": None,
}
_PIPELINE = ["saved", "in_progress", "ready_to_submit", "applied", "interviewing", "offer"]
_DONE = {"applied", "interviewing", "offer", "rejected", "withdrawn"}
# a job the person sent in (or is about to): listed under the desk's My applications with where it stands
APPLICATION_STATUSES = ("ready_to_submit", "applied", "interviewing", "offer", "rejected", "withdrawn")
# My applications' groups, in the page's order (standing): sent with nothing heard yet, an email came and it's no further
# on, interviewing or an offer, rejected or withdrawn
GROUPS = ("waiting", "heard", "interviews", "closed")


def _progress(status: str) -> int:
    """How far along an application is: applied (and after) beyond ready, beyond started."""
    return 3 if status in _DONE else {"ready_to_submit": 2, "in_progress": 1}.get(status, 0)

_JOB_FIELDS = [
    "url", "apply_url", "title", "company", "location", "ats", "source", "external_id",
    "salary", "employment_type", "posted_at", "description",
]
# where a job is applied for: the employer's own, once a job board's copy of its posting is one job with it, never
# filled in from the board's
_PLACE_FIELDS = ("url", "apply_url", "ats", "source", "external_id")


_TRACKING_PARAMS = {"trk", "trackingid", "refid", "src", "source", "gh_src", "lever-source", "from", "ref"}


def _clock() -> datetime:
    """Now, for what's worked out from the time (standing's follow-up): a test pins it."""
    return datetime.now(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _moment(said: Any) -> datetime | None:
    """A time as the tracker or Claude wrote it: ISO (a date alone, or a time with its offset: the tracker's own), or an
    email's Date header ("Wed, 01 Oct 2026 09:30:00 -0700"); one without an offset is taken as UTC, as the tracker
    keeps its own. None for words it can't read."""
    said = str(said or "").strip()
    if not said:
        return None
    try:
        t = datetime.fromisoformat(said[:-1] + "+00:00" if said.endswith("Z") else said)
    except ValueError:
        try:
            t = parsedate_to_datetime(said)
        except (TypeError, ValueError, IndexError, OverflowError):  # (3.10 says TypeError for words that aren't one)
            return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def standing(job: dict[str, Any], heard: dict[str, Any] | None, follow_up_days: int,
             now: datetime | None = None) -> dict[str, Any]:
    """Where an application stands since it was sent in (the desk's My applications, and list_jobs), from the job and
    its newest email (`heard`: Tracker.last_heard's, None for none):
    - `last_heard`: that email (its category, when it came, the day it came, and its one-line summary), or None;
    - `emails`: how many are logged for the job;
    - `follow_up_due`: for one applied to `follow_up_days` days ago or more with no email logged for it (a "we received
      it" counts as heard, and so does one logged before it was marked applied: logging it is what marked it), the day
      it became due; None otherwise, and always for one interviewing, with an offer, or closed;
    - `group`: one of GROUPS: closed (rejected or withdrawn), interviews (interviewing or an offer), heard (an email
      came, and it's no further on), or waiting (sent in, or ready to be, with nothing heard)."""
    status = job.get("status")
    group = ("closed" if status in ("rejected", "withdrawn") else "interviews" if status in ("interviewing", "offer")
             else "heard" if heard else "waiting")
    due = None
    applied = _moment(job.get("applied_at")) if status == "applied" and not heard else None
    with contextlib.suppress(OverflowError):  # (a date changed by hand, past the calendar's end, is never due)
        when = applied + timedelta(days=follow_up_days) if applied is not None else None
        if when is not None and (now or _clock()) >= when:
            due = when.date().isoformat()
    return {"last_heard": {k: heard[k] for k in ("category", "received_at", "day", "summary")} if heard else None,
            "emails": heard["emails"] if heard else 0, "follow_up_due": due, "group": group}


def normalize_url(url: str) -> str:
    """One address per posting, so the same one isn't saved twice (and applied to twice):
    tracking params, the host's case, Workday's language segment ("/en-US/") and
    Greenhouse's older board address all go. The address still opens the posting."""
    url = url.strip()
    m = re.search(r"linkedin\.com/jobs/view/(?:[\w-]*?-)?(\d{6,})", url) or re.search(
        r"linkedin\.com/.*currentJobId=(\d{6,})", url
    )
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    m = re.search(r"indeed\.com/.*[?&]jk=([0-9a-f]+)", url)
    if m:
        return f"https://www.indeed.com/viewjob?jk={m.group(1)}"
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _TRACKING_PARAMS
             and not k.lower().startswith("utm_")]
    host, path = parts.netloc.lower(), parts.path
    if re.search(r"myworkdayjobs\.com$|myworkdaysite\.com$", host):
        path = re.sub(r"^/[a-z]{2}-[A-Za-z]{2}(?=/)", "", path)  # the same posting in another language
    if host == "boards.greenhouse.io" and re.match(r"/[\w-]+/jobs/\d+", path):
        host = "job-boards.greenhouse.io"
    return urlunsplit(parts._replace(scheme=parts.scheme.lower(), netloc=host, path=path, query=urlencode(query),
                                     fragment=""))


# ----------------------------------------------------------- one posting, one job, whichever site it was saved from
# Words a company's name ends in that don't tell one employer from another ("Intel Corporation" is Intel's). Not
# "Group", which some employers' names need to tell them from another ("Atlas Copco Group" stays)
_LEGAL_WORDS = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "holdings",
                "plc", "lp", "llp"}
# a requisition number at a title's end, in brackets or after a dash: "(R0012345)", "[JR-2024-001]", "- Req #4567";
# never words ("(2nd Shift)", "- Level 2")
_REQ_ID = (r"(?:(?:req(?:uisition)?|job|jr|id|r)\.?\s*(?:id|no|number)?\.?\s*[#:]?\s*)?"
           r"[a-z]{0,3}[-_]?\d{3,}(?:[-_]\d+)*[a-z]{0,3}")
_TITLE_REQ = re.compile(rf"(?:\s*\[\s*{_REQ_ID}\s*\]|\s*\(\s*{_REQ_ID}\s*\)|\s+[-–—]\s*{_REQ_ID})\s*$", re.I)


@functools.lru_cache(maxsize=8192)
def company_key(name: str) -> str:
    """An employer's name as two sites agree on it: casefolded, without punctuation, a leading "The" or the legal
    words it ends in ("The Boeing Company" and "Boeing", "Applied Materials, Inc." and "Applied Materials")."""
    words = re.sub(r"[\W_]+", " ", re.sub(r"[.'’]", "", name.casefold())).split()
    if len(words) > 1 and words[0] == "the":
        words = words[1:]
    while len(words) > 1 and words[-1] in _LEGAL_WORDS:
        words.pop()
    return " ".join(words)


@functools.lru_cache(maxsize=8192)
def title_key(title: str) -> str:
    """A job's title as two sites agree on it: casefolded, without punctuation or a requisition number at its end (kept
    apart: _req_ids)."""
    said = title.casefold().strip()
    while (bare := _TITLE_REQ.sub("", said)) != said:
        said = bare
    return " ".join(re.sub(r"[\W_]+", " ", said).split())


@functools.lru_cache(maxsize=8192)
def city_key(location: str) -> str | None:
    """The city a job's place names, casefolded: "Chandler, AZ 85226", Workday's "US-Arizona-Chandler", "US, AZ,
    Chandler" and "Chandler AZ" are all "chandler". The first place of several ("Phoenix, AZ; Austin, TX"). "" for no
    place given; None for one naming no city that can be read (a ZIP code alone, "3 Locations"), which matches none."""
    said = location.strip()
    if not said:
        return ""
    if re.fullmatch(r"(?i)(?:\d+|multiple|various)\s+locations?", said):
        return None
    parts = [p.strip() for p in re.split(r"[,;|(]", said)]
    workday = re.match(r"(?i)(?:us|usa)\s*-\s*[^,;-]+?\s*-\s*([^,;]+)$", said)
    if workday:
        city = workday.group(1)
    elif len(parts) >= 3 and re.fullmatch(r"(?i)us|usa|united states(?: of america)?", parts[0]):
        city = parts[2]  # ("US, AZ, Chandler")
    else:
        city = parts[0]
    city = re.sub(r"[\s-]+[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?$", "", city.strip())  # (its state, without a comma)
    return " ".join(re.sub(r"[\W\d_]+", " ", city.casefold()).split()) or None


def _key(company: Any, title: Any, location: Any) -> tuple[str, str, str | None]:
    """(a listing's words may be anything a hand-edited file holds: read as text, as same_posting does)"""
    return company_key(str(company or "")), title_key(str(title or "")), city_key(str(location or ""))


def _alike(a: tuple[str, str, str | None], b: tuple[str, str, str | None]) -> bool:
    """The same employer and title (each known), in the same city or with one of them giving no place at all. A place
    naming no city that can be read is never alike any."""
    if not (a[0] and a[1]) or a[:2] != b[:2] or a[2] is None or b[2] is None:
        return False
    return a[2] == b[2] or not a[2] or not b[2]


# a requisition number in a job board's copy of a posting: one labelled as one ("Req ID: R12345", "Requisition 4567",
# "Job Number: 2024-118", "Posting ID 88123", "Req # 5678"), or a bare JR or REQ number of 5 digits or more
# ("JR0286553"); never another number its words name ("Fanuc R2000 robots": the review of 0.3.206)
_REQ_IN_TEXT = re.compile(
    r"(?i)\b(?:req(?:uisition)?\.?\s*(?:id|no\.?|number|#)|requisition|job\s*(?:id|no\.?|number|#)"
    r"|posting\s*(?:id|no\.?|number|#))\s*[:#]?\s*([a-z]{0,4}[-_]?\d{3,}(?:[-_]\d+)*)\b"
    r"|\b((?:jr|req)[-_]?\d{5,})\b")
_REQ_LIKE = re.compile(r"(?i)[a-z]{0,4}[-_ #:]*\d{3,}(?:[-_]\d+)*[a-z]{0,3}")


def _req_id(said: str) -> str:
    """A requisition number as two sites agree on it: its digits, without leading zeros ("JR0286553" is "286553")."""
    digits = "".join(re.findall(r"\d", said))
    return digits.lstrip("0") or ("0" if digits else "")


@functools.lru_cache(maxsize=8192)
def _title_ids(title: str) -> frozenset[str]:
    said, ids = title.casefold().strip(), set()
    while m := _TITLE_REQ.search(said):
        ids.add(_req_id(m.group(0)))
        said = said[:m.start()]
    return frozenset(i for i in ids if i)


def _req_ids(title: str | None, external_id: str | None, description: str | None, url: str,
             board: str | None) -> frozenset[str]:
    """The requisition numbers a job gives: at its title's end, its external id when that's one (never a job board's
    own number for its listing, which is in its address), and, for a job board's copy, any its text names."""
    ids = set(_title_ids(title or ""))
    external = (external_id or "").strip()
    if _REQ_LIKE.fullmatch(external) and not (board and _req_id(external) in url):
        ids.add(_req_id(external))
    if board:
        ids |= {_req_id(next(g for g in m.groups() if g)) for m in _REQ_IN_TEXT.finditer(description or "")}
    return frozenset(i for i in ids if i)


def _differ(a: frozenset[str], b: frozenset[str]) -> bool:
    """Two jobs whose requisition numbers tell them apart: each gives one, and none is the other's."""
    return bool(a and b and not a & b)


# Merged without asking only when saved within this many days of each other: a job board's copy found later is the
# employer's repost, as often as not
MERGE_DAYS = 30
_UNBEGUN = ("saved", "skipped")


class _Job(NamedTuple):
    """A job as postings are matched (Tracker._postings): its id (0 for a posting not saved yet), match key, the job
    board it was saved from (None: the employer's own site), status, when it was saved, and whether it holds a job
    board's copy already (an alias)."""
    id: int
    key: tuple[str, str, str | None]
    board: str | None
    status: str
    created: datetime | None
    aliased: bool


def _kept_apart(own: _Job, own_ids: frozenset[str], board: _Job, board_ids: frozenset[str],
                alias_only: bool = False) -> str | None:
    """Why the employer's job and a job board's copy alike it aren't made one without asking, or None when they are:
    saved more than MERGE_DAYS apart (the employer's repost, as often as not), or the board's copy begun or done (it may
    have been sent in there; one done is the person's), and as the tracker opens the employer's too. A board's copy
    saved now (`alias_only`) only becomes an alias of the employer's job, which keeps its status; so where that job is
    begun or done, or holds a job board's copy already, only when the two name the same requisition number (the review
    of 0.3.206: another opening alike Intel's JR1 applied to, on Indeed a week later, took its "applied")."""
    if own.created is None or board.created is None or abs(own.created - board.created) > timedelta(days=MERGE_DAYS):
        return f"as they were saved more than {MERGE_DAYS} days apart"
    same_number = bool(own_ids & board_ids)
    unsure = ", and no requisition number shows it's this posting"
    for job in ([own] if alias_only else [board, own]):
        if job.status not in _UNBEGUN and not (alias_only and same_number):
            return f"as job {job.id} is {job.status.replace('_', ' ')}{unsure if alias_only else ''}"
    if own.aliased and not same_number:
        return f"as job {own.id} has a job board's copy already{unsure}"
    return None


def _rank(row: sqlite3.Row) -> tuple[int, bool, int]:
    """Which of two rows for one posting is kept: the one furthest along, then a skipped one (the person said no to
    that job), then the older."""
    return _progress(row["status"]), row["status"] == "skipped", -row["id"]


@functools.lru_cache(maxsize=8192)
def _board(url: str, source: str) -> str | None:
    """The job board a job was saved from (ats.JOB_BOARDS), by its address or the source it was saved with
    ("indeed"); None for one saved from the employer's own site."""
    board = job_board(url)
    if board:
        return board
    said = set(re.findall(r"[a-z]+", (source or "").casefold()))
    return next((b for b in JOB_BOARDS if b in said), None)


def _ats_of(values: dict[str, str]) -> str:
    """The job system a posting is applied on, as postings.finalize names it: its Apply's, else its own address's."""
    applied_on = detect_ats(values.get("apply_url")) if values.get("apply_url") else "company_site"
    return applied_on if applied_on != "company_site" else detect_ats(values.get("url"))


def _move_folder(src: Path, dst: Path, gone_id: int) -> None:
    """What's in a merged job's folder (a tailored resume, the record of a Submit press) moved into the kept job's: the
    folder itself, where the kept job has none or an empty one; else each thing in it, one whose name the kept job's
    has already going under "from-job-<id>" ("from-job-<id>-2", and on, where that has it too): never written over,
    and the move always finishes. The folder emptied goes."""
    if src == dst:
        return
    if not any(src.iterdir()):
        src.rmdir()
        return
    if not dst.exists() or dst.is_dir() and not any(dst.iterdir()):
        if dst.exists():
            dst.rmdir()
        src.rename(dst)
        return
    for child in sorted(src.iterdir()):
        target, n = dst / child.name, 1
        while target.exists():
            target = dst / (f"from-job-{gone_id}" if n == 1 else f"from-job-{gone_id}-{n}") / child.name
            n += 1
        target.parent.mkdir(exist_ok=True)
        child.rename(target)
    src.rmdir()


def _locked(method):
    """One call at a time: the MCP tools run in worker threads, the desk on its event loop."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


# seconds a write waits for another process's to finish before it gives up
DB_TIMEOUT = 15

# The schema's version, kept in the file's user_version (Tracker._migrate). A release that only adds a column to _SCHEMA
# needs nothing more: _migrate compares the columns on every open and adds what the file lacks. One that must change
# rows too adds a step here and raises SCHEMA_VERSION to it. Version 1 is the schema of 0.3.210, when versions began.
# (An index in _SCHEMA on a column a release adds would be made before _migrate adds it, and fail on an older file:
# make such an index in a MIGRATIONS step. A step uses conn.execute, never executescript, which would commit the
# transaction _migrate holds the step in.)
SCHEMA_VERSION = 1
MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = []



def damaged(said: str) -> bool:
    """Whether SQLite's words say the file itself is damaged (not a database, malformed), which moving it aside
    mends, rather than busy (another program's write, a journal left by a crash that the next ordinary open rolls
    back) or a fault of the code's: only then is the person told to move it (the review of 0.3.210)."""
    return bool(re.search(r"not a database|malformed|file is encrypted", said, re.I))

class Tracker:
    def __init__(self, path: Path | None = None):
        self.path = path or config.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # (two processes may share the tracker, Claude's tools and a second session's: one waits out the other's write
        # instead of failing at once with "database is locked", the review of Oct 15. The journal stays SQLite's own,
        # not WAL: a home on a network drive or a synced folder (JOB_APPLY_HOME) can't hold WAL's shared memory, and a
        # synced copy would miss what's still in its log, the review of 0.3.189)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=DB_TIMEOUT)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self._renormalize()
        self._merge_copies()
        self._gather_folders()

    def _migrate(self) -> None:
        """Bring a tracker.db from an older release up to this one's schema. CREATE TABLE IF NOT EXISTS makes a table
        that's missing but never adds a column to one that's there, so a column _SCHEMA has and the file lacks is
        added here on every open that finds one (with its default; a NOT NULL one without a default gets ''), then
        each step of MIGRATIONS past the file's user_version runs, once. Looking only reads, so a file with nothing
        missing is only read (the review of Oct 18: a jobs table without applied_at opened and read, then failed its
        first write). The work is one transaction under the write lock, looked at again once the lock is held: two
        sessions opening one old file at once add each column once, and a step that fails leaves the file as it was."""
        want = sqlite3.connect(":memory:")
        try:
            want.executescript(_SCHEMA)
            if not self._missing(want) and self.conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
                return
            with self._lock:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    for alter in self._missing(want):
                        self.conn.execute(alter)
                    version = self.conn.execute("PRAGMA user_version").fetchone()[0]
                    for step, migrate in MIGRATIONS:
                        if step > version:
                            migrate(self.conn)
                    if version < SCHEMA_VERSION:
                        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    self.conn.commit()
                except BaseException:
                    self.conn.rollback()
                    raise
        finally:
            want.close()

    def _missing(self, want: sqlite3.Connection) -> list[str]:
        """The ALTER TABLE statements that give this file the columns `want` (_SCHEMA, made in memory) has and it
        lacks. (PRAGMA table_info says nothing of REFERENCES, UNIQUE or CHECK: a column added to a table that's there
        has none of them.)"""
        alters = []
        for (table,) in want.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for _, name, kind, notnull, default, _ in want.execute(f"PRAGMA table_info({table})"):
                if name in have:
                    continue
                if default is None and notnull:
                    default = "''"
                extra = f" DEFAULT {default}" if default is not None else ""
                alters.append(f"ALTER TABLE {table} ADD COLUMN {name} {kind}{extra}")
        return alters

    def _renormalize(self) -> None:
        """Bring addresses saved before normalize_url knew a variant up to date. Two rows
        that turn out to be one posting become one, so nothing (the desk's queue, a search's
        listing, ingest_job) can reach the copy not yet applied to and apply again: the row
        furthest along is kept, with the other's history and emails moved onto it."""
        for row in self.conn.execute("SELECT id, url FROM jobs").fetchall():
            url = normalize_url(row["url"])
            if url == row["url"]:
                continue
            other = self.conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
            if other is None:
                self.conn.execute("UPDATE jobs SET url = ? WHERE id = ?", (url, row["id"]))
                continue
            this = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            keep, drop = sorted((this, other), key=_rank, reverse=True)
            self._fold(keep["id"], drop["id"])
            fill = {k: drop[k] for k in _JOB_FIELDS if k != "url" and drop[k] and not keep[k]}
            self.conn.execute("DELETE FROM jobs WHERE id = ?", (drop["id"],))
            sets = ", ".join(f"{k} = ?" for k in ["url", *fill])
            self.conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (url, *fill.values(), keep["id"]))
            self.conn.execute("INSERT INTO events (job_id, at, status, note) VALUES (?, ?, '', ?)",
                              (keep["id"], _now(), f"merged with job {drop['id']}: the same posting"))
        for alias in self.conn.execute("SELECT url FROM aliases").fetchall():  # (and the other addresses kept)
            url = normalize_url(alias["url"])
            if url == alias["url"]:
                continue
            if self.conn.execute("SELECT 1 FROM jobs WHERE url = ? UNION SELECT 1 FROM aliases WHERE url = ?",
                                 (url, url)).fetchone():
                self.conn.execute("DELETE FROM aliases WHERE url = ?", (alias["url"],))  # (a job has it already)
            else:
                self.conn.execute("UPDATE aliases SET url = ? WHERE url = ?", (url, alias["url"]))
        self.conn.commit()

    def _fold(self, keep: int, drop: int) -> None:
        """A job about to go, one posting with `keep`: its history, emails, other addresses and maybe-the-same links moved
        onto that one, and noted as merged into it (Tracker.merged_into), with any merged into it before."""
        for table in ("events", "emails", "aliases", "not_interested"):
            self.conn.execute(f"UPDATE {table} SET job_id = ? WHERE job_id = ?", (keep, drop))
        self.conn.execute("UPDATE OR IGNORE maybe_same SET job_id = ? WHERE job_id = ?", (keep, drop))
        self.conn.execute("UPDATE OR IGNORE maybe_same SET other_id = ? WHERE other_id = ?", (keep, drop))
        self.conn.execute("DELETE FROM maybe_same WHERE job_id = other_id")
        self.conn.execute("UPDATE merged SET into_id = ? WHERE into_id = ?", (keep, drop))
        self.conn.execute("INSERT OR REPLACE INTO merged (id, into_id) VALUES (?, ?)", (drop, keep))

    def _postings(self) -> dict[tuple[str, str], builtins.list[_Job]]:
        """Every job whose employer and title are known, by them (company_key, title_key), as postings are matched."""
        aliased = {r[0] for r in self.conn.execute("SELECT DISTINCT job_id FROM aliases")}
        out: dict[tuple[str, str], builtins.list[_Job]] = {}
        for row in self.conn.execute("SELECT id, url, source, company, title, location, status, created_at FROM jobs"):
            key = _key(row["company"], row["title"], row["location"])
            if key[0] and key[1]:
                out.setdefault((key[0], key[1]), []).append(
                    _Job(row["id"], key, _board(row["url"], row["source"] or ""), row["status"],
                         _moment(row["created_at"]), row["id"] in aliased))
        return out

    def _ids(self, job: _Job, known: dict[int, frozenset[str]]) -> frozenset[str]:
        """A saved job's requisition numbers (_req_ids), read once a match (`known`)."""
        if job.id not in known:
            row = self.conn.execute("SELECT title, external_id, description, url FROM jobs WHERE id = ?",
                                    (job.id,)).fetchone()
            known[job.id] = _req_ids(row["title"], row["external_id"], row["description"], row["url"], job.board)
        return known[job.id]

    def _match(self, new: _Job, new_ids: frozenset[str], group: builtins.list[_Job],
               known: dict[int, frozenset[str]]) -> tuple[_Job | None, builtins.list[_Job], str]:
        """The jobs from the other side that this posting (`new`) may be, alike it (_alike) with no requisition number
        telling them apart (_differ): job boards' copies for the employer's own posting, and the employer's own jobs
        holding a board's copy already (an alias: that copy may be this posting's); the employer's jobs for a board's
        copy. With the one it may be made one with, if any: the only one, a board's copy for the employer's posting
        (two of the employer's own are never one: they may be two openings), alike no other from this posting's side
        (Intel's JR1 and JR2 both alike Indeed's copy: which it's a copy of can't be told); or the only one naming the
        same requisition number, which no other from this posting's side names too. And why there's none."""
        def same(a: _Job, a_ids: frozenset[str], b: _Job) -> bool:
            return a.id != b.id and _alike(a.key, b.key) and not _differ(a_ids, self._ids(b, known))

        mine = [j for j in group if j.id != new.id and (j.board is None) == (new.board is None)]
        if new.board is None:
            theirs = [j for j in group if (j.board is not None or j.aliased) and same(new, new_ids, j)]
        else:
            theirs = [j for j in group if j.board is None and same(new, new_ids, j)]
        if not theirs:
            return None, theirs, ""
        named = [j for j in theirs if new_ids & self._ids(j, known)]
        pick = named if len(named) == 1 else theirs
        if len(pick) == 1 and new.board is None and pick[0].board is None:
            return None, theirs, f"as job {pick[0].id} is another of the employer's postings, with a job board's copy"
        one = pick[0]
        if pick is named:  # (the same number: only another naming it too is a rival)
            rivals = [j for j in mine if new_ids & self._ids(one, known) & self._ids(j, known)]
        else:
            rivals = [j for j in mine if same(one, self._ids(one, known), j)]
        if len(pick) > 1 or rivals:
            return None, theirs, "as more than one matches"
        return one, theirs, ""

    def _link(self, job_id: int, others: builtins.list[int], why: str, now: str) -> None:
        """Note jobs that may be one posting but aren't made one (_match, _kept_apart), each way round: the desk asks the
        person before applying to one while another is begun or done (Tracker.maybe_same). Said in the job's history
        the first time. Nothing written for links already there (the tracker opening with nothing new takes no write
        lock: another session may be writing)."""
        there = {(r[0], r[1]) for r in self.conn.execute(
            "SELECT job_id, other_id FROM maybe_same WHERE job_id = ? OR other_id = ?", (job_id, job_id))}
        added = [o for o in others if (job_id, o) not in there]
        for a, b in [(job_id, o) for o in added] + [(o, job_id) for o in others if (o, job_id) not in there]:
            self.conn.execute("INSERT OR IGNORE INTO maybe_same (job_id, other_id) VALUES (?, ?)", (a, b))
        if added:
            said = f"maybe the same as job{'s' if len(added) > 1 else ''} {', '.join(map(str, added))}: kept apart, {why}"
            self.conn.execute("INSERT INTO events (job_id, at, status, note) VALUES (?, ?, '', ?)", (job_id, now, said))
            logging.getLogger(__name__).info("job %s %s", job_id, said)

    def _merge_copies(self) -> None:
        """Jobs saved before a posting was one job whichever site it came from (Tracker.upsert), as the tracker opens: a
        job board's copy and the employer's own posting made one where upsert would make them one now (_match: each the
        other's only match, no requisition number telling them apart), both untouched (saved or skipped: nothing under
        way or sent at either) and saved within MERGE_DAYS of each other (_kept_apart). The one kept is the skipped one
        (the person said no to that job), else the older, with the employer's address and where it's applied for, the
        board's address kept as an alias, and the other's history, emails and folder moved onto it (_gather_folders).
        The others alike are noted as maybe the same (_link), for the desk to ask before applying. Run at every open:
        one read of the jobs, and nothing to do once they're one."""
        postings, now = self._postings(), _now()
        known: dict[int, frozenset[str]] = {}
        done: set[int] = set()
        for group in postings.values():
            if all(j.board is None and not j.aliased for j in group) or all(j.board is not None for j in group):
                continue
            for own in group:
                if own.board is not None or own.id in done:
                    continue
                one, theirs, why = self._match(own, self._ids(own, known), group, known)
                if one is not None and one.id not in done:
                    why = _kept_apart(own, self._ids(own, known), one, self._ids(one, known)) or ""
                    if not why:
                        done |= {own.id, one.id}
                        self._merge_pair(one.id, own.id, one.board or "")
                        continue
                others = [j.id for j in theirs if j.id not in done]
                if others:
                    self._link(own.id, others, why, now)
        self.conn.commit()

    def _merge_pair(self, board_id: int, own_id: int, board: str) -> None:
        """A job board's copy (board_id) and the employer's own posting (own_id) made one job (_merge_copies)."""
        theirs, own = (self.conn.execute("SELECT * FROM jobs WHERE id = ?", (i,)).fetchone() for i in (board_id, own_id))
        keep, drop = sorted((theirs, own), key=_rank, reverse=True)
        sets: dict[str, Any] = {k: own[k] for k in _PLACE_FIELDS}
        sets.update({k: drop[k] for k in (*_JOB_FIELDS, "applied_at", "notes")
                     if k not in _PLACE_FIELDS and drop[k] and not keep[k]})
        if keep["notes"] and drop["notes"] and drop["notes"] != keep["notes"]:  # (the person's own notes: both kept)
            sets["notes"] = f"{keep['notes']}\n{drop['notes']}"
        sets["created_at"] = min(keep["created_at"], drop["created_at"])
        self._fold(keep["id"], drop["id"])
        self.conn.execute("DELETE FROM jobs WHERE id = ?", (drop["id"],))  # (first: its address goes to the one kept)
        self.conn.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                          (*sets.values(), keep["id"]))
        self.conn.execute("INSERT OR REPLACE INTO aliases (url, job_id) VALUES (?, ?)", (theirs["url"], keep["id"]))
        name = JOB_BOARDS.get(board, "the job board")
        self.conn.execute("INSERT INTO events (job_id, at, status, note) VALUES (?, ?, '', ?)",
                          (keep["id"], _now(), f"merged with job {drop['id']}: the same posting on {name} and the "
                                               f"employer's site; {name}'s link kept as an alias"))

    def _gather_folders(self) -> None:
        """A merged job's folder (a tailored resume, the record of a Submit press, which the desk reads so as never to
        press Submit again) moved into the kept job's (_move_folder): as the tracker opens after the merge, and at
        each open after for one that couldn't be moved then (a file open in another program). A Submit press it records
        with no confirmation is noted in the kept job's history too ("pressed Submit; ..."), which the desk reads as
        well (Applier._press_recorded), wherever the record goes."""
        gone = {r["id"]: r["into_id"] for r in self.conn.execute("SELECT id, into_id FROM merged")}
        base = config.applications_dir()
        if not gone or not base.is_dir():
            return
        for folder in sorted(base.iterdir()):
            named = re.match(r"(\d+)-", folder.name)
            if not named or int(named.group(1)) not in gone or not folder.is_dir():
                continue
            gone_id = int(named.group(1))
            into = self.conn.execute("SELECT id, company, title FROM jobs WHERE id = ?", (gone[gone_id],)).fetchone()
            if into is None:
                continue
            try:
                record = json.loads((folder / "submission.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = None
            said = f"pressed Submit; recorded for job {gone_id}, merged into this one: it may have gone in"
            if isinstance(record, dict) and not record.get("confirmed") and not self.conn.execute(
                    "SELECT 1 FROM events WHERE job_id = ? AND note = ?", (into["id"], said)).fetchone():
                self.conn.execute("INSERT INTO events (job_id, at, status, note) VALUES (?, ?, '', ?)",
                                  (into["id"], _now(), said))
                self.conn.commit()
            try:
                _move_folder(folder, self.job_dir(into["id"], into["company"], into["title"]), gone_id)
            except OSError as e:
                logging.getLogger(__name__).warning("job %s's folder %s wasn't moved into job %s's (%s): tried again at "
                                                    "the next start", gone_id, folder.name, into["id"], e)

    def _at(self, url: str) -> sqlite3.Row | None:
        """The job at this address (normalized), or with it among its other addresses (aliases)."""
        return (self.conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
                or self.conn.execute("SELECT jobs.* FROM aliases JOIN jobs ON jobs.id = aliases.job_id "
                                     "WHERE aliases.url = ?", (url,)).fetchone())

    @_locked
    def close(self) -> None:
        self.conn.close()

    def _row(self, row: sqlite3.Row | None, with_description: bool = True) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        if not with_description:
            d.pop("description", None)
        d["folder"] = str(self.job_dir(d["id"], d.get("company", ""), d.get("title", "")))
        return d

    def job_dir(self, job_id: int, company: str = "", title: str = "") -> Path:
        """The job's folder: the one it already has, whatever its name (a company filled in
        later mustn't leave its tailored resume and submit record behind), else a new name."""
        base = config.applications_dir()
        existing = sorted(p for p in base.glob(f"{job_id:04d}-*") if p.is_dir()) if base.is_dir() else []
        if existing:
            return existing[0]
        slug = re.sub(r"[^a-z0-9]+", "-", f"{company} {title}".lower()).strip("-")[:60]
        return base / f"{job_id:04d}-{slug or 'job'}"

    @_locked
    def upsert(self, posting: dict[str, Any], status: str | None = None) -> tuple[dict[str, Any], bool]:
        """Insert a job, or refresh empty fields of an existing one. Returns (job, created).

        One posting is one job, whichever site it was saved from (the code review's worth building 8: the same job from
        Indeed and from the employer's site was two, and the desk could apply to it twice): a posting at an address
        the tracker doesn't know joins the job saved from the other side that it surely is (_decide, _join), its
        address kept as one of the job's own (aliases), which finds the job from then on. One it may be, but not surely,
        is saved as a job of its own, noted as maybe the same (_link): the desk asks before applying to it while the
        other is begun or done."""
        url = normalize_url(posting["url"])
        existing = self._at(url)
        now = _now()
        values = {k: str(posting.get(k) or "") for k in _JOB_FIELDS}
        values["url"] = url
        board = _board(url, values["source"])
        if existing:
            updates = {k: v for k, v in values.items() if v and not existing[k]}
            if board and _board(existing["url"], existing["source"] or "") is None:
                # a job board's copy of a job kept at the employer's site: never where it's applied for
                updates = {k: v for k, v in updates.items() if k not in _PLACE_FIELDS}
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                self.conn.execute(
                    f"UPDATE jobs SET {sets}, updated_at = ? WHERE id = ?",
                    (*updates.values(), now, existing["id"]),
                )
                self.conn.commit()
            return self.get(existing["id"]), False
        one, maybe, why = self._decide(values, board, now, self._postings(), {})
        if one is not None:
            return self._join(one.id, values, board, now), False
        status = status or "saved"
        cols = ", ".join(_JOB_FIELDS)
        marks = ", ".join("?" for _ in _JOB_FIELDS)
        cur = self.conn.execute(
            f"INSERT INTO jobs ({cols}, status, created_at, updated_at) VALUES ({marks}, ?, ?, ?)",
            (*[values[k] for k in _JOB_FIELDS], status, now, now),
        )
        self.conn.execute(
            "INSERT INTO events (job_id, at, status, note) VALUES (?, ?, ?, ?)",
            (cur.lastrowid, now, status, "added"),
        )
        if maybe and cur.lastrowid is not None:  # (kept apart, and said: the desk asks before applying)
            self._link(cur.lastrowid, [j.id for j in maybe], why, now)
        self.conn.commit()
        job = self.get(cur.lastrowid)
        Path(job["folder"]).mkdir(parents=True, exist_ok=True)
        return job, True

    def _decide(self, values: dict[str, str], board: str | None, now: str,
                postings: dict[tuple[str, str], builtins.list[_Job]],
                known: dict[int, frozenset[str]]) -> tuple[_Job | None, builtins.list[_Job], str]:
        """For a posting not saved yet: the job it's made one with, if any (_match: surely the same posting; and
        _kept_apart: for the employer's posting, a board's copy untouched and saved within MERGE_DAYS; for a board's
        copy, an employer's job saved within MERGE_DAYS, untouched and holding no board's copy yet, or else naming the
        same requisition number); the jobs it may be the same as; and why it isn't made one with them."""
        key = _key(values["company"], values["title"], values["location"])
        new = _Job(0, key, board, "saved", _moment(now), False)
        new_ids = _req_ids(values["title"], values["external_id"], values["description"], values["url"], board)
        one, theirs, why = self._match(new, new_ids, postings.get((key[0], key[1]), []), known)
        if one is not None:
            one_ids = self._ids(one, known)
            why = (_kept_apart(one, one_ids, new, new_ids, alias_only=True) if board
                   else _kept_apart(new, new_ids, one, one_ids)) or ""
        return (None if why else one), theirs, why

    @_locked
    def linked(self, job_id: int) -> builtins.list[int]:
        """Every job noted as maybe the same posting as this one (Tracker.maybe_same), Apply anyway or not: the desk
        never has two of them under way at once."""
        return [r[0] for r in self.conn.execute("SELECT other_id FROM maybe_same WHERE job_id = ? ORDER BY other_id",
                                                (job_id,))]

    def _join(self, job_id: int, values: dict[str, str], board: str | None, now: str) -> dict[str, Any]:
        """A posting at a new address joins the job saved from the other side that it is (upsert). The job keeps its
        id, status, history, emails and folder (job_dir finds a job's folder by its id, and its employer and title are
        only filled in where empty). The employer's own posting gives a job board's job its address and where it's
        applied for (apply_url, ats, source, external_id), the board's address kept as an alias; a job board's copy only
        becomes an alias of the employer's job. Either way the posting fills in what the job didn't say."""
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        sets: dict[str, Any] = {k: v for k, v in values.items() if k not in _PLACE_FIELDS and v and not row[k]}
        if board:
            alias = values["url"]
            note = (f"the same posting on {JOB_BOARDS[board]}: its link kept as an alias (applied for on the "
                    "employer's site)")
        else:
            alias, theirs = row["url"], _board(row["url"], row["source"] or "")
            sets.update({k: values[k] for k in _PLACE_FIELDS})
            sets["ats"] = values["ats"] or _ats_of(values)
            note = (f"merged: the same posting on the employer's site ({ATS_NAMES.get(sets['ats'], sets['ats'])}); "
                    f"{JOB_BOARDS.get(theirs or '', 'the job board')}'s link kept as an alias")
        sets["updated_at"] = now
        self.conn.execute(f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), job_id))
        self.conn.execute("INSERT OR REPLACE INTO aliases (url, job_id) VALUES (?, ?)", (alias, job_id))
        self.conn.execute("INSERT INTO events (job_id, at, status, note) VALUES (?, ?, '', ?)", (job_id, now, note))
        self.conn.commit()
        return self.get(job_id)

    @_locked
    def get(self, job_id: int, with_description: bool = True) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row(row, with_description)

    @_locked
    def find_by_url(self, url: str) -> dict[str, Any] | None:
        """The job at this address, or with it among its other addresses (a job board's, for one kept at the
        employer's site)."""
        return self._row(self._at(normalize_url(url)))

    @_locked
    def aliases(self) -> dict[int, builtins.list[str]]:
        """Each job's other addresses (a job board's, for one kept at the employer's site), by job, as they came."""
        out: dict[int, builtins.list[str]] = {}
        for row in self.conn.execute("SELECT url, job_id FROM aliases ORDER BY rowid"):
            out.setdefault(row["job_id"], []).append(row["url"])
        return out

    @_locked
    def same_posting(self, postings: builtins.list[dict[str, Any]]) -> builtins.list[dict[str, Any] | None]:
        """For each of these postings (the desk's openings, from employers' own sites, at addresses the tracker doesn't
        know yet), the job saved from the other side that it surely is (_decide: a job Claude saved from Indeed, say),
        or None: the desk lists the two as one, and Apply makes them one job (upsert). None too for two of them alike
        the same job: which it is can't be told."""
        if not postings:  # (the page asks every 1.5 s: no read of the jobs for none)
            return []
        saved, now = self._postings(), _now()
        known: dict[int, frozenset[str]] = {}
        found = []
        for p in postings:
            values = {k: str(p.get(k) or "") for k in _JOB_FIELDS}
            values["url"] = normalize_url(values["url"])
            one, _, _ = self._decide(values, _board(values["url"], values["source"]), now, saved, known)
            found.append(one.id if one is not None else None)
        return [self._row(self.conn.execute("SELECT * FROM jobs WHERE id = ?", (i,)).fetchone(), with_description=False)
                if i is not None and found.count(i) == 1 else None for i in found]

    @_locked
    def merged_into(self, job_id: int) -> int | None:
        """The job a job no longer here was merged into (one posting saved twice), or None."""
        row = self.conn.execute("SELECT into_id FROM merged WHERE id = ?", (job_id,)).fetchone()
        return row["into_id"] if row else None

    @_locked
    def maybe_same(self, job_id: int) -> builtins.list[dict[str, Any]]:
        """The jobs this one may be the same posting as (alike, from the other site, but not made one: upsert), unless
        the person said Apply anyway for it, each with the job board it was saved from (`board`, None for the
        employer's own site). The desk asks before applying to it while one of them is begun or done."""
        rows = self.conn.execute("SELECT jobs.* FROM maybe_same JOIN jobs ON jobs.id = maybe_same.other_id "
                                 "WHERE maybe_same.job_id = ? AND NOT maybe_same.cleared ORDER BY jobs.id", (job_id,))
        return [{**(self._row(r, with_description=False) or {}), "board": _board(r["url"], r["source"] or "")}
                for r in rows.fetchall()]

    @_locked
    def clear_maybe_same(self, job_id: int, others: builtins.list[int]) -> None:
        """The person's Apply anyway: this job isn't one of these it may be the same as (the ones its card named: for
        this job only, and for those alone; another begun later is asked about again, and they're asked about still,
        should one of them be started). Said in its history."""
        wanted = [int(o) for o in others]
        if not wanted:
            return
        marks = ", ".join("?" for _ in wanted)
        others = [r[0] for r in self.conn.execute(
            f"SELECT other_id FROM maybe_same WHERE job_id = ? AND NOT cleared AND other_id IN ({marks})",
            (job_id, *wanted))]
        if not others:
            return
        self.conn.execute(f"UPDATE maybe_same SET cleared = 1 WHERE job_id = ? AND other_id IN ({marks})",
                          (job_id, *wanted))
        self.conn.execute("INSERT INTO events (job_id, at, status, note) VALUES (?, ?, '', ?)",
                          (job_id, _now(), f"Apply anyway: not the same as job{'s' if len(others) > 1 else ''} "
                                           f"{', '.join(map(str, others))}, as the person said"))
        self.conn.commit()

    @_locked
    def list(self, status: str | None = None, company: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        q = "SELECT * FROM jobs WHERE 1=1"
        args: list[Any] = []
        if status:
            q += " AND status = ?"
            args.append(status)
        if company:
            q += " AND company LIKE ?"
            args.append(f"%{company}%")
        q += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        return [self._row(r, with_description=False) for r in self.conn.execute(q, args)]  # type: ignore[misc]

    @_locked
    def update(
        self,
        job_id: int,
        status: str | None = None,
        notes: str | None = None,
        apply_url: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"No job with id {job_id}")
        if status and status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}; use one of {', '.join(STATUSES)}")
        now = _now()
        sets: dict[str, Any] = {"updated_at": now}
        if status:
            sets["status"] = status
            if status == "applied" and not job["applied_at"]:
                sets["applied_at"] = now
        if notes is not None:
            sets["notes"] = notes
        if apply_url:
            sets["apply_url"] = apply_url
        self.conn.execute(
            f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
            (*sets.values(), job_id),
        )
        if status or note:
            self.conn.execute(
                "INSERT INTO events (job_id, at, status, note) VALUES (?, ?, ?, ?)",
                (job_id, now, status or "", note),
            )
        self.conn.commit()
        return self.get(job_id)

    @_locked
    def unskip(self, job_id: int, note: str = "skip undone") -> dict[str, Any] | None:
        """Undo a skip: the job goes back to the status it had before it was skipped, read from
        its history (every status change is in it; "saved" when none came before), moved on by any
        employer email logged for it (a confirmation: "applied"), with its notes and history kept.
        None, and nothing changed, for a job that isn't skipped."""
        job = self.get(job_id, with_description=False)
        if job is None:
            raise KeyError(f"No job with id {job_id}")
        if job["status"] != "skipped":
            return None
        row = self.conn.execute("SELECT status FROM events WHERE job_id = ? AND status NOT IN ('', 'skipped') "
                                "ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        status = row["status"] if row and row["status"] in STATUSES else "saved"
        # and what the employer's emails said, which a skipped job doesn't take in (log_email): one that
        # confirmed the application, sent on the site before the Skip, makes it "applied", so the desk never
        # queues it for a second one
        for email in self.conn.execute("SELECT category FROM emails WHERE job_id = ? ORDER BY logged_at", (job_id,)):
            target = EMAIL_CATEGORIES.get(email["category"])
            if target == "rejected":
                status = status if status in ("offer", "withdrawn") else "rejected"
            elif target and status in _PIPELINE and _PIPELINE.index(target) > _PIPELINE.index(status):
                status = target
        return self.update(job_id, status=status, note=note)

    def _the_job(self, url: str | None = None, job_id: int | None = None) -> sqlite3.Row | None:
        """The job an address is (its own, or among its other addresses), or a job number names (or the job it was
        merged into, one posting saved twice)."""
        if job_id is not None:
            row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None and (into := self.merged_into(job_id)) is not None:
                row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (into,)).fetchone()
            return row
        return self._at(normalize_url(url)) if url else None

    def _addresses(self, job: sqlite3.Row) -> builtins.list[str]:  # (in here `list` is Tracker.list)
        """Every address a job is at: its own, where it's applied for, and its other sites' (aliases)."""
        own = [job["url"], job["apply_url"]] + [r[0] for r in self.conn.execute(
            "SELECT url FROM aliases WHERE job_id = ?", (job["id"],))]
        return list(dict.fromkeys(normalize_url(u) for u in own if u))

    @_locked
    def set_not_interested(self, url: str, company: str = "", title: str = "", reason: str = "",
                           job_id: int | None = None, location: str = "", external_id: str = "",
                           description: str = "") -> None:
        """The person said Not interested in the opening at this address (the desk's Not interested), for `reason` (one
        of NOT_INTERESTED_REASONS, or none), kept until undone. The mark is the job's when the opening is one (the job at
        the address, or `job_id`: the one the desk's row shows, saved from another site): on its number and each of its
        addresses, and said in its history. With the opening's place and the requisition numbers it gives (its external
        id, its title's, a job board's text: _req_ids, as a posting is matched), for a job saved since that is surely
        the same posting (not_interested_marks)."""
        if reason and reason not in NOT_INTERESTED_REASONS:
            raise ValueError(f"reason should be one of {', '.join(NOT_INTERESTED_REASONS)}, or none")
        now, url = _now(), normalize_url(url)
        ids = ",".join(sorted(_req_ids(title, external_id, description, url, _board(url, ""))))
        job = self._the_job(job_id=job_id) if job_id is not None else self._the_job(url=url)
        for address in dict.fromkeys([url, *(self._addresses(job) if job is not None else [])]):
            self.conn.execute("INSERT OR REPLACE INTO not_interested (url, job_id, company, title, location, req_ids, "
                              "reason, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (address, job["id"] if job is not None else None, company or "", title or "",
                               location or "", ids, reason or "", now))
        if job is not None:
            self.conn.execute("INSERT INTO events (job_id, at, status, note) VALUES (?, ?, '', ?)",
                              (job["id"], now, "not interested" + (f" ({reason})" if reason else "")))
        self.conn.commit()

    @_locked
    def undo_not_interested(self, url: str, job_id: int | None = None,
                            listed: Iterable[dict[str, Any]] = ()) -> bool:
        """The person's Undo on Not interested: the opening shows again, and its job (`job_id`: the one the desk's row
        is, saved from another site; or the job at the address), whichever of its addresses it was marked at, on its
        number, or as an opening the same posting marked before the job was saved. Whether anything was marked."""
        url = normalize_url(url)
        job = self._the_job(job_id=job_id) if job_id is not None else self._the_job(url=url)
        if job is None:  # (an opening marked with the job its row showed, saved from another site: by its mark)
            row = self.conn.execute("SELECT job_id FROM not_interested WHERE url = ?", (url,)).fetchone()
            job = self._the_job(job_id=row["job_id"]) if row is not None and row["job_id"] is not None else None
        same = self.not_interested_marks(listed).matched if job is not None else {}
        addresses = [url, *(self._addresses(job) if job is not None else []),
                     *(u for u, j in same.items() if job is not None and j == job["id"])]
        marks = ", ".join("?" for _ in addresses)
        gone = self.conn.execute(f"DELETE FROM not_interested WHERE url IN ({marks})"
                                 + (" OR job_id = ?" if job is not None else ""),
                                 (*addresses, *([job["id"]] if job is not None else []))).rowcount
        if gone and job is not None:
            self.conn.execute("INSERT INTO events (job_id, at, status, note) VALUES (?, ?, '', ?)",
                              (job["id"], _now(), "not interested undone"))
        self.conn.commit()
        return bool(gone)

    @_locked
    def not_interested(self) -> dict[str, dict[str, Any]]:
        """The addresses marked Not interested (normalized), each with its job's number (None for an opening not saved
        as one), company, title, reason ("" for none) and when (`at`). One job marked has a line for each of its
        addresses: not_interested_marks checks openings and jobs against them."""
        rows = self.conn.execute("SELECT url, job_id, company, title, location, req_ids, reason, at FROM not_interested "
                                 "ORDER BY at")
        return {r["url"]: {"job_id": r["job_id"],
                           **{k: r[k] or "" for k in ("company", "title", "location", "req_ids", "reason", "at")}}
                for r in rows}

    @_locked
    def not_interested_marks(self, listed: Iterable[dict[str, Any]] = ()) -> NotInterested:
        """The person's Not interested marks, read once, to check many openings against (the desk's list). An opening
        marked before it was a job (a loose mark) is linked to a job saved since only when that job is surely the same
        posting (_sure_link: the same requisition number, or neither naming one and each the other's only match among
        the jobs and the openings the desk lists, `listed`); otherwise it marks its own address alone."""
        rows = [{"url": u, **said} for u, said in self.not_interested().items()]
        loose = [r for r in rows if r["job_id"] is None]
        matched: dict[str, int] = {}
        if loose:
            saved: dict[tuple[str, str], builtins.list[_Job]] = self._postings()
            known: dict[int, frozenset[str]] = {}
            # the openings that could be any job's posting: the desk's (by address), and the loose marks' own
            openings = {normalize_url(o["url"]): _key(o.get("company"), o.get("title"), o.get("location"))
                        for o in listed if isinstance(o.get("url"), str)}
            for r in loose:
                openings.setdefault(r["url"], _key(r["company"], r["title"], r["location"]))
            for r in loose:
                one = self._sure_link(r, saved, known, openings)
                if one is not None:
                    matched[r["url"]] = one
        return NotInterested(rows, matched)

    def _sure_link(self, mark: dict[str, Any], saved: dict[tuple[str, str], builtins.list[_Job]],
                   known: dict[int, frozenset[str]], openings: dict[str, tuple[str, str, str | None]]) -> int | None:
        """The job a loose mark is surely the same posting as, or None. A job from the other side (a job board's copy
        for an employer's posting), alike it (_alike), and either (a) the two name a requisition number, the same one,
        and no other job names it; or (b) neither names one, it's the mark's only such job, and the job is alike no
        other job on the mark's side and no other opening (the desk's, or another loose mark's): each the other's only
        match. Numbers that differ never link, nor does one named on one side only, nor anything ambiguous: an Intel
        listing's JR1 marked is never JR2's Indeed copy."""
        key, board = _key(mark["company"], mark["title"], mark["location"]), _board(mark["url"], "")
        mine = frozenset(i for i in str(mark.get("req_ids") or "").split(",") if i)
        group = saved.get((key[0], key[1]), [])
        side = [j for j in group if _alike(key, j.key)]
        theirs = [j for j in side if j.board is not None or j.aliased] if board is None else \
            [j for j in side if j.board is None]
        if mine:  # (a)
            named = [j for j in theirs if mine & self._ids(j, known)]
            return named[0].id if len(named) == 1 else None
        if len(theirs) != 1 or self._ids(theirs[0], known):  # (b): its only one, naming no number either
            return None
        job = theirs[0]
        others = [j for j in group if j.id != job.id and _alike(job.key, j.key) and (j.board is None) == (board is None)]
        if others or any(u != mark["url"] and _alike(k, job.key) for u, k in openings.items()):
            return None
        return job.id

    @_locked
    def marked_not_interested(self, url: str | None = None, job_id: int | None = None,
                              listed: Iterable[dict[str, Any]] = ()) -> dict[str, Any] | None:
        """The Not interested mark on an opening (by its address) or a job (by its number), or None: the job it is
        (_the_job) marked by its number or at any of its addresses, or the address itself marked, or a loose mark surely
        its posting (`listed`: the openings the desk lists, which make one ambiguous: not_interested_marks). What the
        desk's Apply, its queue and Claude's tools go by."""
        marks = self.not_interested_marks(listed)
        if not marks:
            return None
        job = self._the_job(url=url, job_id=job_id)
        return marks.mark(url, dict(job) if job is not None else None, self._addresses(job) if job is not None else ())

    @_locked
    def log_email(self, job_id: int, thread_id: str, category: str, summary: str = "",
                  received_at: str = "") -> dict[str, Any]:
        """Record an employer email once and move the status forward if it says so.
        Never moves an application backwards (a late confirmation email doesn't undo
        an interview), and a rejection doesn't override an offer. A later message in a
        thread already logged (the rejection under the confirmation) is recorded too."""
        if category not in EMAIL_CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(EMAIL_CATEGORIES)}")
        job = self.get(job_id, with_description=False)
        if job is None:
            raise KeyError(f"No job with id {job_id}")
        seen = self.conn.execute("SELECT category, received_at FROM emails WHERE thread_id = ?", (thread_id,)).fetchone()
        if seen and seen["category"] == category and (not received_at or seen["received_at"] == received_at):
            return {"already_logged": True, "status": job["status"], "changed": False}
        self.conn.execute(  # one row per thread: its latest message
            "INSERT OR REPLACE INTO emails (thread_id, job_id, category, summary, received_at, logged_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (thread_id, job_id, category, summary, received_at, _now()),
        )
        self.conn.commit()
        current, target = job["status"], EMAIL_CATEGORIES[category]
        new = current
        if target == "rejected":
            new = current if current in ("offer", "withdrawn") else "rejected"
        elif target and current in _PIPELINE and _PIPELINE.index(target) > _PIPELINE.index(current):
            new = target
        note = f"email ({category}{', ' + received_at if received_at else ''}): {summary}".strip()
        if new != current:
            self.update(job_id, status=new, note=note)
        else:
            self.update(job_id, note=note)
        return {"already_logged": False, "status": new, "changed": new != current}

    @_locked
    def logged_threads(self, since_days: int | None = None) -> builtins.list[dict[str, Any]]:  # (in here `list` is Tracker.list)
        q = "SELECT thread_id, job_id, category, received_at FROM emails"
        args: list[Any] = []
        if since_days is not None:
            q += " WHERE logged_at >= datetime('now', ?)"
            args.append(f"-{int(since_days)} days")
        return [dict(r) for r in self.conn.execute(q + " ORDER BY logged_at DESC", args)]

    @_locked
    def last_heard(self) -> dict[int, dict[str, Any]]:
        """Each job's newest logged email, by job: its `category`, `received_at` (when it came; when it was logged, where
        that wasn't given), the `day` it came as YYYY-MM-DD (in the time's own offset; the day it was logged, where
        received_at doesn't read as a time), its one-line `summary`, and how many `emails` are logged for the job. One
        query for every job. Newest is by when it came, not when it was logged: a confirmation logged late isn't news
        after the interview. (Picked here, not in SQL: received_at is as Claude wrote it, a date, an ISO time or an
        email's Date header, which don't sort as words.)"""
        rows = self.conn.execute("SELECT rowid AS n, job_id, category, summary, received_at, logged_at FROM emails")
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        newest: dict[int, tuple[Any, sqlite3.Row, datetime | None]] = {}
        counts: dict[int, int] = {}
        for r in rows:
            came = _moment(r["received_at"]) or _moment(r["logged_at"])
            key = (came or oldest, _moment(r["logged_at"]) or oldest, r["n"])
            counts[r["job_id"]] = counts.get(r["job_id"], 0) + 1
            if r["job_id"] not in newest or key > newest[r["job_id"]][0]:
                newest[r["job_id"]] = (key, r, came)
        return {jid: {"category": r["category"], "received_at": r["received_at"] or r["logged_at"],
                      "day": came.date().isoformat() if came else "", "summary": r["summary"] or "",
                      "emails": counts[jid]}
                for jid, (_, r, came) in newest.items()}

    @_locked
    def events(self, job_id: int) -> builtins.list[dict[str, Any]]:
        rows = self.conn.execute("SELECT at, status, note FROM events WHERE job_id = ? ORDER BY id", (job_id,))
        return [dict(r) for r in rows]

    @_locked
    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def export_csv(self, path: Path) -> int:
        rows = self.list(limit=100000)
        cols = ["id", "status", "company", "title", "location", "ats", "source", "url", "apply_url",
                "salary", "applied_at", "updated_at", "notes"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        return len(rows)
