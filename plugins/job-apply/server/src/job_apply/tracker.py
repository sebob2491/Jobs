"""SQLite application tracker."""

from __future__ import annotations

import builtins
import csv
import functools
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import config

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


def _progress(status: str) -> int:
    """How far along an application is: applied (and after) beyond ready, beyond started."""
    return 3 if status in _DONE else {"ready_to_submit": 2, "in_progress": 1}.get(status, 0)

_JOB_FIELDS = [
    "url", "apply_url", "title", "company", "location", "ats", "source", "external_id",
    "salary", "employment_type", "posted_at", "description",
]


_TRACKING_PARAMS = {"trk", "trackingid", "refid", "src", "source", "gh_src", "lever-source", "from", "ref"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _locked(method):
    """One call at a time: the MCP tools run in worker threads, the desk on its event loop."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class Tracker:
    def __init__(self, path: Path | None = None):
        self.path = path or config.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self._renormalize()

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
            for table in ("events", "emails"):
                self.conn.execute(f"UPDATE {table} SET job_id = ? WHERE job_id = ?", (keep["id"], drop["id"]))
            fill = {k: drop[k] for k in _JOB_FIELDS if k != "url" and drop[k] and not keep[k]}
            self.conn.execute("DELETE FROM jobs WHERE id = ?", (drop["id"],))
            sets = ", ".join(f"{k} = ?" for k in ["url", *fill])
            self.conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (url, *fill.values(), keep["id"]))
            self.conn.execute("INSERT INTO events (job_id, at, status, note) VALUES (?, ?, '', ?)",
                              (keep["id"], _now(), f"merged with job {drop['id']}: the same posting"))
        self.conn.commit()

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
        """Insert a job, or refresh empty fields of an existing one. Returns (job, created)."""
        url = normalize_url(posting["url"])
        existing = self.conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
        now = _now()
        values = {k: str(posting.get(k) or "") for k in _JOB_FIELDS}
        values["url"] = url
        if existing:
            updates = {k: v for k, v in values.items() if v and not existing[k]}
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                self.conn.execute(
                    f"UPDATE jobs SET {sets}, updated_at = ? WHERE id = ?",
                    (*updates.values(), now, existing["id"]),
                )
                self.conn.commit()
            return self.get(existing["id"]), False
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
        self.conn.commit()
        job = self.get(cur.lastrowid)
        Path(job["folder"]).mkdir(parents=True, exist_ok=True)
        return job, True

    @_locked
    def get(self, job_id: int, with_description: bool = True) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row(row, with_description)

    @_locked
    def find_by_url(self, url: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE url = ?", (normalize_url(url),)).fetchone()
        return self._row(row)

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
