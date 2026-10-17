"""SQLite application tracker."""

from __future__ import annotations

import builtins
import contextlib
import csv
import functools
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
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
"""

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
    """A job's title as two sites agree on it: casefolded, without punctuation or a requisition number at its end."""
    said = title.casefold().strip()
    while (bare := _TITLE_REQ.sub("", said)) != said:
        said = bare
    return " ".join(re.sub(r"[\W_]+", " ", said).split())


@functools.lru_cache(maxsize=8192)
def city_key(location: str) -> str:
    """The city a job's place names, casefolded: "Chandler, AZ 85226", Workday's "US-Arizona-Chandler" and
    "Chandler AZ" are all "chandler". The first place of several ("Phoenix, AZ; Austin, TX")."""
    said = location.strip()
    workday = re.match(r"(?i)(?:us|usa)\s*-\s*[^,;-]+?\s*-\s*([^,;]+)$", said)
    city = workday.group(1) if workday else re.split(r"[,;|(]", said)[0]
    city = re.sub(r"[\s-]+[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?$", "", city.strip())  # (its state, without a comma)
    return " ".join(re.sub(r"[\W\d_]+", " ", city.casefold()).split())


def _key(company: str | None, title: str | None, location: str | None) -> tuple[str, str, str]:
    return company_key(company or ""), title_key(title or ""), city_key(location or "")


def _alike(a: tuple[str, str, str], b: tuple[str, str, str]) -> bool:
    """The same employer and title (each known), in the same city or with one of them not saying where."""
    return bool(a[0] and a[1]) and a[:2] == b[:2] and (a[2] == b[2] or not a[2] or not b[2])


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
    has already going under "from-job-<id>" (never written over). The folder emptied goes."""
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
        target = dst / child.name
        if target.exists():
            target = dst / f"from-job-{gone_id}" / child.name
            if target.exists():
                continue  # (left where it is, and said: the folder stays)
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
        self._renormalize()
        self._merge_copies()
        self._gather_folders()

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
            keep, drop = sorted((this, other), key=lambda r: (_progress(r["status"]), -r["id"]), reverse=True)
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
        """A job about to go, one posting with `keep`: its history, emails and other addresses moved onto that one, and
        noted as merged into it (Tracker.merged_into), with any merged into it before."""
        for table in ("events", "emails", "aliases"):
            self.conn.execute(f"UPDATE {table} SET job_id = ? WHERE job_id = ?", (keep, drop))
        self.conn.execute("UPDATE merged SET into_id = ? WHERE into_id = ?", (keep, drop))
        self.conn.execute("INSERT OR REPLACE INTO merged (id, into_id) VALUES (?, ?)", (drop, keep))

    def _postings(self) -> dict[tuple[str, str], builtins.list[tuple[sqlite3.Row, tuple[str, str, str], str | None]]]:
        """Every job whose employer and title are known, by them (company_key, title_key): each with its match key
        (_key) and the job board it was saved from (None: the employer's own site)."""
        out: dict[tuple[str, str], builtins.list[tuple[sqlite3.Row, tuple[str, str, str], str | None]]] = {}
        for row in self.conn.execute("SELECT id, url, source, company, title, location, status FROM jobs"):
            key = _key(row["company"], row["title"], row["location"])
            if key[0] and key[1]:
                out.setdefault(key[:2], []).append((row, key, _board(row["url"], row["source"] or "")))
        return out

    @staticmethod
    def _counterpart(key: tuple[str, str, str], board: str | None, postings: dict[Any, Any],
                     own_id: int | None = None) -> tuple[sqlite3.Row | None, builtins.list[int]]:
        """The job saved from the other side that is this posting (`key`, saved from `board`, or from the employer's own
        site for None): a job board's copy for the employer's own posting, or the employer's for a board's. (It, []) for
        exactly one, alike (_alike) and alike no other from this posting's side; (None, the ids of those it might be)
        when more than one might be, either way round; (None, []) for none. Two from the same side are never one
        posting: an employer may have two openings alike, and two job boards' copies are each the employer's."""
        group = postings.get(key[:2], [])
        theirs = [(r, k) for r, k, b in group if (b is None) != (board is None) and r["id"] != own_id and _alike(key, k)]
        if len(theirs) != 1:
            return None, [r["id"] for r, _ in theirs]
        same, same_key = theirs[0]
        rivals = [r["id"] for r, k, b in group if (b is None) == (board is None) and r["id"] != own_id
                  and _alike(same_key, k)]
        return (None, [same["id"], *rivals]) if rivals else (same, [])

    def _merge_copies(self) -> None:
        """Jobs saved before a posting was one job whichever site it came from (Tracker.upsert): a job board's copy
        and the employer's own posting, alike (_counterpart: each the other's one match) become one job, as the tracker
        opens. The one furthest along is kept (an Indeed job applied to and the employer's still saved make one job,
        applied to), with the employer's address and where it's applied for, the board's address kept as an alias, and
        the other's history and emails moved onto it (its folder too: _gather_folders). Two both begun (in progress)
        are left as they are: either may be under way at another session's desk. Run at every open: one read of the
        jobs, and nothing to do once they're one."""
        log = logging.getLogger(__name__)
        postings = self._postings()
        done: set[int] = set()
        for group in postings.values():
            if all(b is None for _, _, b in group) or all(b is not None for _, _, b in group):
                continue
            for row, key, board in group:
                if board is None or row["id"] in done:
                    continue
                same, maybe = self._counterpart(key, board, postings, own_id=row["id"])
                if maybe:
                    log.info("job %s may be the same posting as jobs %s, but more than one matches: not merged",
                             row["id"], maybe)
                if same is None or same["id"] in done:
                    continue
                if row["status"] == same["status"] == "in_progress":
                    log.info("jobs %s and %s are one posting, both begun: not merged", row["id"], same["id"])
                    continue
                done |= {row["id"], same["id"]}
                self._merge_pair(row["id"], same["id"], board)
        self.conn.commit()

    def _merge_pair(self, board_id: int, own_id: int, board: str) -> None:
        """A job board's copy (board_id) and the employer's own posting (own_id) made one job (_merge_copies)."""
        theirs, own = (self.conn.execute("SELECT * FROM jobs WHERE id = ?", (i,)).fetchone() for i in (board_id, own_id))
        keep, drop = sorted((theirs, own), key=lambda r: (_progress(r["status"]), -r["id"]), reverse=True)
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
        self.conn.execute("INSERT INTO events (job_id, at, status, note) VALUES (?, ?, '', ?)",
                          (keep["id"], _now(), f"merged with job {drop['id']}: the same posting on {JOB_BOARDS[board]} "
                                               f"and the employer's site; {JOB_BOARDS[board]}'s link kept as an alias"))

    def _gather_folders(self) -> None:
        """A merged job's folder (a tailored resume, the record of a Submit press, which the desk reads so as never to
        press Submit again) moved into the kept job's (_move_folder): as the tracker opens after the merge, and at
        each open after for one that couldn't be moved then (a file open in another program)."""
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
        the tracker doesn't know joins the job saved from the other side that it's alike (_counterpart, _join), its
        address kept as one of the job's own (aliases), which finds the job from then on."""
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
        same, maybe = self._counterpart(_key(values["company"], values["title"], values["location"]), board,
                                        self._postings())
        if same is not None:
            return self._join(same["id"], values, board, now), False
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
        if maybe:  # (left apart, and said: which is this posting can't be told)
            said = f"may be the same posting as jobs {', '.join(map(str, maybe))}, but more than one matches: not merged"
            self.conn.execute("INSERT INTO events (job_id, at, status, note) VALUES (?, ?, '', ?)",
                              (cur.lastrowid, now, said))
            logging.getLogger(__name__).info("job %s %s", cur.lastrowid, said)
        self.conn.commit()
        job = self.get(cur.lastrowid)
        Path(job["folder"]).mkdir(parents=True, exist_ok=True)
        return job, True

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
        know yet), the job saved from the other side that it is (_counterpart: a job Claude saved from Indeed, say),
        or None: the desk lists the two as one, and Apply makes them one job (upsert). None too for two of them alike
        the same job: which it is can't be told."""
        if not postings:  # (the page asks every 1.5 s: no read of the jobs for none)
            return []
        known = self._postings()
        found = []
        for p in postings:
            same, _ = self._counterpart(_key(p.get("company"), p.get("title"), p.get("location")),
                                        _board(normalize_url(str(p.get("url") or "")), str(p.get("source") or "")), known)
            found.append(same["id"] if same is not None else None)
        return [self._row(self.conn.execute("SELECT * FROM jobs WHERE id = ?", (i,)).fetchone(), with_description=False)
                if i is not None and found.count(i) == 1 else None for i in found]

    @_locked
    def merged_into(self, job_id: int) -> int | None:
        """The job a job no longer here was merged into (one posting saved twice), or None."""
        row = self.conn.execute("SELECT into_id FROM merged WHERE id = ?", (job_id,)).fetchone()
        return row["into_id"] if row else None

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
