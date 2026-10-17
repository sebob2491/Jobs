from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from job_apply.tracker import Tracker, normalize_url


def test_normalize_url():
    assert normalize_url("https://www.linkedin.com/jobs/view/field-service-engineer-at-example-3900000001/?trk=x&refId=y") == \
        "https://www.linkedin.com/jobs/view/3900000001/"
    assert normalize_url("https://www.linkedin.com/jobs/search/?currentJobId=3900000002&geoId=1") == \
        "https://www.linkedin.com/jobs/view/3900000002/"
    assert normalize_url("https://www.indeed.com/viewjob?jk=a1b2c3&from=serp&vjs=3") == "https://www.indeed.com/viewjob?jk=a1b2c3"
    assert normalize_url("https://jobs.example.com/job/1?utm_source=x&id=2") == "https://jobs.example.com/job/1?id=2"


def test_upsert_update_export(job_apply_home):
    t = Tracker()
    job, created = t.upsert({"url": "https://www.linkedin.com/jobs/view/3900000001/?trk=a", "title": "Field Service Engineer 2",
                             "company": "Lam Research", "ats": "linkedin"})
    assert created and job["status"] == "saved"
    assert Path(job["folder"]).is_dir()
    again, created = t.upsert({"url": "https://www.linkedin.com/jobs/view/3900000001/", "location": "Chandler, AZ"})
    assert not created and again["id"] == job["id"] and again["location"] == "Chandler, AZ"

    t.update(job["id"], status="applied", note="clicked submit")
    got = t.get(job["id"])
    assert got["status"] == "applied" and got["applied_at"]
    assert [e["status"] for e in t.events(job["id"])] == ["saved", "applied"]
    with pytest.raises(ValueError):
        t.update(job["id"], status="ghosted")

    assert t.counts() == {"applied": 1}
    out = job_apply_home / "out.csv"
    assert t.export_csv(out) == 1
    assert "Lam Research" in out.read_text(encoding="utf-8")


def test_log_email_moves_status_forward_once(job_apply_home):
    t = Tracker()
    job, _ = t.upsert({"url": "https://kla.wd1.myworkdayjobs.com/Search/job/x_1", "title": "FSE", "company": "KLA"})
    t.update(job["id"], status="applied")

    out = t.log_email(job["id"], "thr-1", "interview", "Phone screen request", "2026-10-01")
    assert out == {"already_logged": False, "status": "interviewing", "changed": True}
    assert t.log_email(job["id"], "thr-1", "interview")["already_logged"] is True  # same thread again
    # a late "we received your application" doesn't move it backwards
    assert t.log_email(job["id"], "thr-0", "confirmation")["status"] == "interviewing"
    assert t.log_email(job["id"], "thr-2", "offer", "Offer letter")["status"] == "offer"
    assert t.log_email(job["id"], "thr-3", "rejection")["status"] == "offer"  # offer stands
    assert {r["thread_id"] for r in t.logged_threads()} == {"thr-0", "thr-1", "thr-2", "thr-3"}
    notes = [e["note"] for e in t.events(job["id"])]
    assert any("Phone screen request" in n for n in notes)

    other, _ = t.upsert({"url": "https://example.com/jobs/2", "title": "Tech", "company": "Example"})
    assert t.log_email(other["id"], "thr-9", "rejection")["status"] == "rejected"
    with pytest.raises(ValueError):
        t.log_email(other["id"], "thr-10", "ghosted")


def test_one_posting_one_row():
    """The desk's own Workday search gives an address without the language; a link pasted
    from the browser has /en-US/. Two rows meant one could be applied to again."""
    wd = "https://kla.wd1.myworkdayjobs.com/Search/job/Chandler-AZ/Customer-Support-Engineer_2637278"
    assert normalize_url("https://kla.wd1.myworkdayjobs.com/en-US/Search/job/Chandler-AZ/Customer-Support-Engineer_2637278") == wd
    assert normalize_url("https://KLA.wd1.MyWorkdayJobs.com/Search/job/Chandler-AZ/Customer-Support-Engineer_2637278") == wd
    assert normalize_url("https://boards.greenhouse.io/asm/jobs/4954508101?gh_src=x") == \
        "https://job-boards.greenhouse.io/asm/jobs/4954508101"
    # the address still opens the posting: paths keep their case and their slash
    assert normalize_url("https://careers.qorvo.com/job/Chandler-AZ/1421977600/") == \
        "https://careers.qorvo.com/job/Chandler-AZ/1421977600/"


def test_addresses_saved_before_are_brought_up_to_date(job_apply_home):
    t = Tracker()
    pasted = "https://kla.wd1.myworkdayjobs.com/en-US/Search/job/Chandler-AZ/CSE_1"
    found = "https://kla.wd1.myworkdayjobs.com/Search/job/Chandler-AZ/CSE_1"
    other = "https://kla.wd1.myworkdayjobs.com/en-US/Search/job/Phoenix-AZ/CSE_2"
    with t._lock:  # rows as an older version saved them
        for url, status in ((pasted, "applied"), (found, "saved"), (other, "saved")):
            t.conn.execute("INSERT INTO jobs (url, status, created_at, updated_at) VALUES (?, ?, 'x', 'x')", (url, status))
        t.conn.commit()
    t.close()
    t = Tracker()
    assert t.find_by_url(other)["url"] == normalize_url(other)  # brought up to date
    # two rows for one posting from before become one, the one applied to: nothing can reach
    # a copy not yet applied to (the desk's queue upserts a search's listing by its address)
    assert t.find_by_url(pasted)["status"] == "applied" and t.find_by_url(found)["status"] == "applied"
    job, created = t.upsert({"url": found, "title": "Customer Support Engineer"})
    assert not created and job["status"] == "applied"
    assert sorted(j["url"] for j in t.list()) == sorted([found, normalize_url(other)])
    assert any("merged with job" in e["note"] for e in t.events(job["id"]))


def test_the_tracker_is_used_from_several_threads(job_apply_home):
    """The MCP tools run in worker threads, the desk on its event loop: one tracker for both."""
    import threading

    t = Tracker()
    errors: list[Exception] = []

    def add(n: int) -> None:
        try:
            for i in range(20):
                job, _ = t.upsert({"url": f"https://example.com/jobs/{n}-{i}", "title": "Tech"})
                t.update(job["id"], status="in_progress")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=add, args=(n,)) for n in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == [] and t.counts() == {"in_progress": 80}


def test_a_later_email_in_a_thread_already_logged_counts(job_apply_home):
    """A rejection that arrives as a reply under the confirmation is the same Gmail thread."""
    t = Tracker()
    job, _ = t.upsert({"url": "https://example.com/jobs/1", "title": "Tech", "company": "Example"})
    t.update(job["id"], status="applied")
    assert t.log_email(job["id"], "thr-1", "confirmation", "Received", "2026-10-01")["status"] == "applied"
    assert t.log_email(job["id"], "thr-1", "confirmation", "Received", "2026-10-01")["already_logged"]
    out = t.log_email(job["id"], "thr-1", "rejection", "Not moving forward", "2026-10-06")
    assert out == {"already_logged": False, "status": "rejected", "changed": True}
    assert t.logged_threads() == [{"thread_id": "thr-1", "job_id": job["id"], "category": "rejection",
                                   "received_at": "2026-10-06"}]


def test_a_jobs_folder_stays_put_when_its_company_is_filled_in_later(job_apply_home):
    """A job saved without its company, then again with it: the folder keeps its name, so
    its tailored resume and its record of a Submit press aren't left behind."""
    t = Tracker()
    job, _ = t.upsert({"url": "https://example.com/jobs/7", "title": "Field Service Technician"})
    folder = Path(job["folder"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "submission.json").write_text('{"confirmed": false}', encoding="utf-8")
    again, created = t.upsert({"url": "https://example.com/jobs/7", "title": "Field Service Technician",
                               "company": "Northwind Semi"})
    assert not created and again["company"] == "Northwind Semi"
    assert Path(again["folder"]) == folder and (Path(again["folder"]) / "submission.json").exists()
    t.close()


def test_undoing_a_skip_keeps_what_an_email_said_meanwhile(job_apply_home):
    """A confirmation email logged while the job was skipped (the person sent it on the site, then
    pressed Skip) leaves it skipped; Undo then gave back "ready_to_submit", and the desk queued it
    again for a second application. Undo takes in what the employer's emails said."""
    t = Tracker()
    job, _ = t.upsert({"url": "https://example.com/jobs/10", "title": "Tech", "company": "Example"})
    t.update(job["id"], status="ready_to_submit")
    t.update(job["id"], status="skipped", note="skipped in the Job Desk")
    t.log_email(job["id"], "thread-1", "confirmation", "We received your application")
    assert t.get(job["id"])["status"] == "skipped"
    assert t.unskip(job["id"])["status"] == "applied"
    other, _ = t.upsert({"url": "https://example.com/jobs/11", "title": "Tech 2", "company": "Example"})
    t.update(other["id"], status="in_progress")
    t.update(other["id"], status="skipped")
    t.log_email(other["id"], "thread-2", "interview", "Can you talk on Tuesday?")
    assert t.unskip(other["id"])["status"] == "interviewing"
    t.close()


def test_undoing_a_skip_brings_back_the_status_before_it(job_apply_home):
    """The Job Desk's Undo on a skipped job: the status it had before (from its history), with its
    notes and history kept. A job that isn't skipped is left alone."""
    t = Tracker()
    job, _ = t.upsert({"url": "https://example.com/jobs/8", "title": "Tech", "company": "Example"})
    t.update(job["id"], status="in_progress", notes="asked about the night shift", note="opened application")
    t.update(job["id"], status="skipped", note="skipped in the Job Desk")
    t.update(job["id"], status="skipped", note="skipped in the Job Desk")  # pressed twice
    t.update(job["id"], note="a note with no status")
    back = t.unskip(job["id"], note="skip undone")
    assert back is not None and back["status"] == "in_progress" and back["notes"] == "asked about the night shift"
    assert [e["status"] for e in t.events(job["id"])] == ["saved", "in_progress", "skipped", "skipped", "", "in_progress"]
    assert t.unskip(job["id"]) is None and len(t.events(job["id"])) == 6  # not skipped now: nothing changes
    # skipped again later: back to what it was then
    t.update(job["id"], status="ready_to_submit")
    t.update(job["id"], status="skipped")
    assert t.unskip(job["id"])["status"] == "ready_to_submit"
    # one saved as skipped, with nothing before it
    other, _ = t.upsert({"url": "https://example.com/jobs/9", "title": "Tech 2"}, status="skipped")
    assert t.unskip(other["id"])["status"] == "saved"
    with pytest.raises(KeyError):
        t.unskip(999)
    t.close()


def test_two_processes_share_the_tracker_without_locking_each_other_out(tmp_path):
    """The review of Oct 15: the tracker opened SQLite with Python's default wait (5 s), so a second process's write
    (another session's tools) behind a longer one failed with "database is locked". One waits out the other's write,
    for DB_TIMEOUT. The journal stays SQLite's own (the review of 0.3.189: WAL can't live on a network drive or a synced
    folder)."""
    import threading

    from job_apply.tracker import Tracker

    first, second = Tracker(tmp_path / "jobs.db"), Tracker(tmp_path / "jobs.db")
    try:
        assert first.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal"
        assert first.conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 10000
        first.conn.execute("BEGIN IMMEDIATE")  # (a write under way in the first)
        timer = threading.Timer(0.5, lambda: first.conn.execute("ROLLBACK"))
        timer.start()
        second.conn.execute("BEGIN IMMEDIATE")  # (waits for it, then writes: no "database is locked")
        second.conn.execute("ROLLBACK")
        timer.join()
    finally:
        first.close()
        second.close()


# ------------------------------------------------------------- after submit: last heard, follow-up, groups
def _applied(t, name: str, applied_at: str, status: str = "applied") -> int:
    """A job at `status`, applied to at `applied_at` (the tracker's own form: UTC, to the second)."""
    job, _ = t.upsert({"url": f"https://example.com/jobs/{name}", "title": "Technician", "company": name})
    t.update(job["id"], status=status)
    t.conn.execute("UPDATE jobs SET applied_at = ? WHERE id = ?", (applied_at, job["id"]))
    t.conn.commit()
    return job["id"]


def test_last_heard_is_each_jobs_newest_email_read_in_one_query(job_apply_home):
    """The code review's worth building 7 (after submit): each job's newest logged email (its category, when it came,
    that day and its one-line summary) and how many are logged for it, for every job in one query (not one a job).
    Newest is by when it came, not when it was logged: a newsletter logged late isn't news after the interview, and
    received_at is as Claude wrote it (a date, an ISO time, an email's Date header). A blank received_at is the time it
    was logged; a job with no email isn't there."""
    t = Tracker()
    talk, rejected, quiet, unsaid = (_applied(t, n, "2026-09-28T15:00:00+00:00") for n in ("Talk", "No", "Quiet", "Unsaid"))
    t.log_email(talk, "t-1", "confirmation", "We received your application", "2026-09-29")
    t.log_email(talk, "t-2", "interview", "Phone screen request, reply with availability", "2026-10-06T16:30:00Z")
    t.log_email(talk, "t-0", "other", "Talent community newsletter", "Wed, 30 Sep 2026 09:00:00 -0700")
    t.log_email(rejected, "n-1", "confirmation", "Received", "2026-10-01")
    t.log_email(rejected, "n-2", "rejection", "Not moving forward", "2026-10-03")
    t.log_email(unsaid, "u-1", "confirmation", "Thanks for applying")  # (no received_at given)
    logged = t.conn.execute("SELECT logged_at FROM emails WHERE thread_id = 'u-1'").fetchone()[0]

    asked: list[str] = []
    t.conn.set_trace_callback(asked.append)
    try:
        heard = t.last_heard()
    finally:
        t.conn.set_trace_callback(None)
    assert len(asked) == 1 and asked[0].lstrip().upper().startswith("SELECT"), asked
    assert heard[talk] == {"category": "interview", "received_at": "2026-10-06T16:30:00Z", "day": "2026-10-06",
                           "summary": "Phone screen request, reply with availability", "emails": 3}, heard[talk]
    assert (heard[rejected]["category"], heard[rejected]["day"], heard[rejected]["emails"]) == ("rejection", "2026-10-03",
                                                                                                  2), heard[rejected]
    assert heard[unsaid] == {"category": "confirmation", "received_at": logged, "day": logged[:10],
                             "summary": "Thanks for applying", "emails": 1}, heard[unsaid]
    assert quiet not in heard and set(heard) == {talk, rejected, unsaid}
    t.close()


NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def test_a_follow_up_is_due_once_an_application_has_waited_its_days_with_no_reply(job_apply_home):
    """An application applied to settings.follow_up_days days ago or more (10 by default), with no email logged for it,
    is due a follow-up from the day those days ran out; one that hasn't waited that long isn't yet. Any email counts as
    heard, a "we received your application" too (and one logged before it was marked applied: logging it is what marked
    it). One interviewing, with an offer, rejected or withdrawn is never due, nor one not sent in yet. Each falls in one
    group: waiting, heard (an email came and it's no further on), interviews or closed."""
    from job_apply.tracker import standing

    t = Tracker()
    due = _applied(t, "Due", "2026-10-05T12:00:00+00:00")  # 12 days ago
    edge = _applied(t, "Edge", "2026-10-07T12:00:00+00:00")  # 10 days ago to the minute: due today
    early = _applied(t, "Early", "2026-10-08T09:00:00+00:00")  # 9 days ago
    acked = _applied(t, "Acked", "2026-09-01T12:00:00+00:00")
    t.log_email(acked, "a-1", "confirmation", "We received your application", "2026-09-01")
    talking = _applied(t, "Talking", "2026-09-01T12:00:00+00:00", status="interviewing")
    offered = _applied(t, "Offered", "2026-09-01T12:00:00+00:00", status="offer")
    closed = [_applied(t, n, "2026-09-01T12:00:00+00:00", status=n.lower()) for n in ("Rejected", "Withdrawn")]
    ready = _applied(t, "Ready", "", status="ready_to_submit")
    heard = t.last_heard()

    def stands(job_id, days=10):
        return standing(t.get(job_id), heard.get(job_id), days, now=NOW)

    assert stands(due) == {"last_heard": None, "emails": 0, "follow_up_due": "2026-10-15", "group": "waiting"}
    assert stands(edge)["follow_up_due"] == "2026-10-17"
    assert stands(early) == {"last_heard": None, "emails": 0, "follow_up_due": None, "group": "waiting"}
    assert stands(acked) == {"last_heard": {"category": "confirmation", "received_at": "2026-09-01", "day": "2026-09-01",
                                            "summary": "We received your application"},
                             "emails": 1, "follow_up_due": None, "group": "heard"}
    assert [(stands(j)["follow_up_due"], stands(j)["group"]) for j in (talking, offered, *closed, ready)] == [
        (None, "interviews"), (None, "interviews"), (None, "closed"), (None, "closed"), (None, "waiting")]
    # the person's own number of days
    assert stands(due, days=12)["follow_up_due"] == "2026-10-17" and stands(due, days=13)["follow_up_due"] is None
    assert stands(early, days=7)["follow_up_due"] == "2026-10-15"
    # a confirmation logged first, which is what marked the job applied (its applied_at the moment it was logged)
    first, _ = t.upsert({"url": "https://example.com/jobs/first", "title": "Technician", "company": "First"})
    t.update(first["id"], status="ready_to_submit")
    t.log_email(first["id"], "f-1", "confirmation", "Received", "2026-09-01")
    assert t.get(first["id"])["status"] == "applied"
    later = datetime.now(timezone.utc) + timedelta(days=60)
    assert standing(t.get(first["id"]), t.last_heard()[first["id"]], 10, now=later)["follow_up_due"] is None
    assert standing(t.get(first["id"]), None, 10, now=later)["follow_up_due"] is not None  # (with none logged: due)
    # an applied_at changed by hand: one past the calendar's end, or words, is never due (and breaks nothing)
    for said in ("9999-12-31T00:00:00+00:00", "last week", ""):
        assert standing({"status": "applied", "applied_at": said}, None, 10, now=NOW)["follow_up_due"] is None, said
    t.close()


# ------------------------------------------------------------- one posting, one job (the code review's worth building 8)
INDEED = "https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b"
INTEL = "https://intel.wd1.myworkdayjobs.com/External/job/US-Arizona-Chandler/Equipment-Technician_JR0286553"


def _indeed(url=INDEED, **fields):
    """A posting Claude saved from Indeed (the find-jobs skill's add_job, source "indeed")."""
    return {"url": url, "title": "Equipment Technician", "company": "Intel Corporation", "location": "Chandler, AZ",
            "source": "indeed", "ats": "indeed", "apply_url": f"https://www.indeed.com/applystart?jk={url[-16:]}",
            **fields}


def _intels(url=INTEL, **fields):
    """The same posting on the employer's own site (its Workday search, as the desk's Apply saves it)."""
    return {"url": url, "title": "Equipment Technician", "company": "Intel", "location": "US-Arizona-Chandler",
            "ats": "workday", "source": "job desk", "description": "Maintain 300mm tools. " * 20, **fields}


def test_the_employers_posting_joins_the_job_saved_from_indeed(job_apply_home):
    """The same job from Indeed and from the employer's site was two jobs (one address each), and the desk could apply
    to it twice. The employer's posting joins the Indeed job: one job, with its id, status and history, applied for on
    the employer's site (its address and Apply), Indeed's address kept as an alias that finds it."""
    t = Tracker()
    job, created = t.upsert(_indeed())
    assert created
    t.update(job["id"], status="in_progress", note="opened application")
    t.log_email(job["id"], "thr-1", "other", "Thanks for your interest")
    joined, created = t.upsert(_intels())
    assert not created and joined["id"] == job["id"] and len(t.list()) == 1
    assert (joined["url"], joined["apply_url"], joined["ats"], joined["source"]) == (INTEL, "", "workday", "job desk")
    assert joined["status"] == "in_progress" and joined["company"] == "Intel Corporation"  # (kept: only empty ones filled)
    assert joined["description"].startswith("Maintain 300mm tools.")
    assert t.aliases() == {job["id"]: [INDEED]}
    notes = [e["note"] for e in t.events(job["id"])]
    assert notes[:2] == ["added", "opened application"] and len(t.logged_threads()) == 1
    assert notes[-1] == "merged: the same posting on the employer's site (Workday); Indeed's link kept as an alias"
    # Indeed's address again (Claude saving it once more, or the person pasting it): the job, never a new one, and
    # never where it's applied for
    for url in (INDEED, INDEED.replace("viewjob?", "viewjob?from=serp&")):
        again, created = t.upsert(_indeed(url=url, salary="$25 an hour"))
        assert not created and again["id"] == job["id"] and again["url"] == INTEL and again["apply_url"] == ""
        assert t.find_by_url(url)["id"] == job["id"]
    assert again["salary"] == "$25 an hour" and len(t.list()) == 1  # (what the job didn't say is filled in)
    t.close()


def test_an_indeed_posting_after_the_employers_is_kept_as_its_alias(job_apply_home):
    """Indeed's copy of a job saved from the employer's site makes no new job: its address finds that one (the desk's
    paste, ingest_job, a search's `tracked`), which is applied for on the employer's site still."""
    t = Tracker()
    job, _ = t.upsert(_intels(location=""))  # (one not saying where: alike any city's)
    t.update(job["id"], status="ready_to_submit")
    same, created = t.upsert(_indeed(salary="$25 an hour"))
    assert not created and same["id"] == job["id"] and len(t.list()) == 1
    assert (same["url"], same["apply_url"], same["ats"], same["status"]) == (INTEL, "", "workday", "ready_to_submit")
    assert same["salary"] == "$25 an hour" and same["location"] == "Chandler, AZ"
    assert t.find_by_url(INDEED)["id"] == job["id"] and t.aliases() == {job["id"]: [INDEED]}
    assert "the same posting on Indeed" in t.events(job["id"])[-1]["note"]
    again, created = t.upsert(_indeed())
    assert not created and again["id"] == job["id"] and len(t.list()) == 1
    # a LinkedIn copy too: another alias of the one job
    linkedin = "https://www.linkedin.com/jobs/view/3900000123/"
    also, created = t.upsert({"url": linkedin, "title": "Equipment Technician", "company": "Intel", "ats": "linkedin"})
    assert not created and also["id"] == job["id"] and t.aliases() == {job["id"]: [INDEED, linkedin]}
    t.close()


def test_two_postings_from_one_side_stay_two_jobs(job_apply_home):
    """An employer may have two openings alike (two requisitions), and two job boards' copies are each the employer's:
    only a job board's copy and the employer's own posting are ever one job."""
    t = Tracker()
    first, _ = t.upsert(_intels())
    second, created = t.upsert(_intels(url=INTEL.replace("JR0286553", "JR0286554")))
    assert created and second["id"] != first["id"]
    boards = [t.upsert(_indeed(company="Lam Research", title="Field Service Engineer")),
              t.upsert({"url": "https://www.linkedin.com/jobs/view/3900000124/", "company": "Lam Research",
                        "title": "Field Service Engineer", "location": "Chandler, AZ", "ats": "linkedin"})]
    assert all(created for _, created in boards) and len(t.list()) == 4 and t.aliases() == {}
    t.close()


def test_a_posting_alike_more_than_one_job_is_left_apart_and_said(job_apply_home):
    """Two openings alike at the employer's site and Indeed's copy of one of them: which it is can't be told, so it's a
    job of its own, said in its history. And the other way round: the employer's posting for a job two boards' copies
    are alike."""
    t = Tracker()
    one, _ = t.upsert(_intels())
    two, _ = t.upsert(_intels(url=INTEL.replace("JR0286553", "JR0286554")))
    copy, created = t.upsert(_indeed())
    assert t.events(copy["id"])[-1]["note"] == (f"may be the same posting as jobs {one['id']}, {two['id']}, but more "
                                                "than one matches: not merged")
    assert created and len(t.list()) == 3 and t.aliases() == {}
    # a board's copy not saying where, alike employer jobs in two cities
    t.upsert(_intels(url=INTEL.replace("Chandler", "Phoenix") + "x", company="Microchip", location="Phoenix, AZ"))
    t.upsert(_intels(url=INTEL.replace("Chandler", "Tempe") + "x", company="Microchip", location="Tempe, AZ"))
    _, created = t.upsert(_indeed(url="https://www.indeed.com/viewjob?jk=1111111111111111", company="Microchip",
                                  location=""))
    assert created and t.aliases() == {}
    # the employer's posting alike Indeed's and LinkedIn's copies
    t.upsert(_indeed(url="https://www.indeed.com/viewjob?jk=2222222222222222", company="KLA"))
    t.upsert({"url": "https://www.linkedin.com/jobs/view/3900000125/", "company": "KLA Corporation",
              "title": "Equipment Technician", "location": "Chandler, AZ", "ats": "linkedin"})
    kla, created = t.upsert(_intels(url="https://kla.wd1.myworkdayjobs.com/Search/job/Chandler-AZ/ET_1", company="KLA"))
    assert created and t.aliases() == {} and "more than one matches" in t.events(kla["id"])[-1]["note"]
    t.close()


def test_postings_in_different_cities_are_different_jobs(job_apply_home):
    t = Tracker()
    t.upsert(_intels(location="Hillsboro, OR"))
    _, created = t.upsert(_indeed())
    assert created and len(t.list()) == 2 and t.aliases() == {}
    t.close()


def test_employers_names_are_compared_without_their_legal_words():
    from job_apply.tracker import company_key

    assert company_key("Intel Corporation") == company_key("Intel") == "intel"
    assert company_key("The Boeing Company") == company_key("Boeing") == "boeing"
    assert company_key("Applied Materials, Inc.") == company_key("Applied Materials") == "applied materials"
    assert company_key("Microchip Technology Inc") == company_key("MICROCHIP TECHNOLOGY") != company_key("Microchip")
    assert company_key("Acme Holdings, L.L.C.") == "acme"
    assert company_key("Atlas Copco Group") != company_key("Atlas Copco")  # ("Group" stays: a name may need it)
    assert company_key("The Company") == "company"  # (never nothing)


def test_a_titles_requisition_number_is_left_out_when_compared(job_apply_home):
    from job_apply.tracker import city_key, title_key

    for said in ("Field Service Engineer (R0012345)", "Field Service Engineer [JR-2024-001]", "field service engineer",
                 "Field Service Engineer - Req #4567", "Field Service Engineer – JR0286553",
                 "Field  Service Engineer (Job ID: 12345)"):
        assert title_key(said) == "field service engineer", said
    for said in ("Technician (2nd Shift)", "Technician - Level 2", "Technician (Fab 42)"):  # words, not a number
        assert title_key(said) != "technician", said
    for said in ("Chandler, AZ", "Chandler, AZ 85226", "US-Arizona-Chandler", "USA - Arizona - Chandler", "Chandler AZ",
                 "Chandler, Arizona, United States"):
        assert city_key(said) == "chandler", said
    t = Tracker()
    job, _ = t.upsert(_indeed())
    same, created = t.upsert(_intels(title="Equipment Technician (JR0286553)"))
    assert not created and same["id"] == job["id"]
    t.close()


def _older(t, rows):
    """Jobs as a version before one posting was one job saved them: each (url, status, company, title, location,
    source), with an event of its own."""
    ids = []
    with t._lock:
        for url, status, company, title, location, source in rows:
            cur = t.conn.execute("INSERT INTO jobs (url, status, company, title, location, source, created_at, "
                                 "updated_at) VALUES (?, ?, ?, ?, ?, ?, '2026-10-01T00:00:00+00:00', 'x')",
                                 (url, status, company, title, location, source))
            t.conn.execute("INSERT INTO events (job_id, at, status, note) VALUES (?, 'x', ?, 'added')",
                           (cur.lastrowid, status))
            ids.append(cur.lastrowid)
        t.conn.commit()
    return ids


def test_jobs_saved_twice_before_are_made_one_as_the_tracker_opens(job_apply_home):
    """An Indeed job the person applied to, and the employer's posting of it saved since: one job as the tracker opens,
    applied to (the further along), at the employer's address with Indeed's as its alias, with both one's history and
    emails. Each open after changes nothing. Two both begun are left apart (either may be under way at another
    session's desk)."""
    t = Tracker()
    applied, own = _older(t, [(INDEED, "applied", "Intel Corporation", "Equipment Technician", "Chandler, AZ", "indeed"),
                              (INTEL, "saved", "Intel", "Equipment Technician", "", "job desk")])
    t.update(applied, note="I submitted it on Indeed")
    t.log_email(applied, "thr-1", "confirmation", "We received your application")
    t.log_email(own, "thr-2", "other", "Your Intel account")
    begun = _older(t, [("https://www.indeed.com/viewjob?jk=3333333333333333", "in_progress", "KLA", "Technician",
                        "Chandler, AZ", "indeed"),
                       ("https://kla.wd1.myworkdayjobs.com/Search/job/Chandler-AZ/T_1", "in_progress", "KLA",
                        "Technician", "Chandler, AZ", "")])
    t.close()

    t = Tracker()
    job = t.find_by_url(INTEL)
    assert job["id"] == applied and t.get(own) is None and t.find_by_url(INDEED)["id"] == applied
    assert (job["url"], job["status"], job["source"], job["location"]) == (INTEL, "applied", "job desk", "Chandler, AZ")
    assert {r["thread_id"]: r["job_id"] for r in t.logged_threads()} == {"thr-1": applied, "thr-2": applied}
    notes = [e["note"] for e in t.events(applied)]
    assert "I submitted it on Indeed" in notes and notes.count("added") == 2
    assert notes[-1] == (f"merged with job {own}: the same posting on Indeed and the employer's site; Indeed's link "
                         "kept as an alias")
    assert t.merged_into(own) == applied and t.merged_into(applied) is None
    assert all(t.get(i) is not None for i in begun)  # (both begun: left apart)
    before = (t.list(), t.events(applied), t.aliases())
    t.close()
    t = Tracker()  # again: nothing more to do
    assert (t.list(), t.events(applied), t.aliases()) == before
    # and the employer's posting saved again: the one job, applied to, never queued again
    again, created = t.upsert(_intels())
    assert not created and again["id"] == applied and again["status"] == "applied"
    t.close()


def test_a_jobs_folder_is_kept_when_two_jobs_are_made_one(job_apply_home):
    """A job's folder holds its tailored resume and the record of a Submit press (which the desk reads so as never to
    press Submit again): a posting joining the job keeps its folder, though its employer's name is another; and as
    two jobs saved before are made one, the other's folder is moved into the kept one's, nothing written over."""
    t = Tracker()
    job, _ = t.upsert(_indeed(company="", location=""))  # (its folder named without the employer)
    folder = Path(job["folder"])
    (folder / "resume-tailored.docx").write_bytes(b"resume")
    t.update(job["id"], status="in_progress")
    with t._lock:  # (the employer, filled in since by hand)
        t.conn.execute("UPDATE jobs SET company = 'Intel Corporation' WHERE id = ?", (job["id"],))
        t.conn.commit()
    joined, _ = t.upsert(_intels())
    assert joined["id"] == job["id"] and joined["url"] == INTEL
    assert Path(joined["folder"]) == folder and (folder / "resume-tailored.docx").read_bytes() == b"resume"
    # two saved before, each with its folder
    first, second = _older(t, [("https://www.indeed.com/viewjob?jk=4444444444444444", "ready_to_submit", "Lam Research",
                                "Field Service Engineer", "Tempe, AZ", "indeed"),
                               ("https://careers.lamresearch.com/careers/job/1", "saved", "Lam Research",
                                "Field Service Engineer", "Tempe, AZ", "")])
    kept, gone = Path(t.get(first)["folder"]), Path(t.get(second)["folder"])
    for where, said in ((kept, "kept"), (gone, "gone")):
        where.mkdir(parents=True)
        (where / "resume-tailored.docx").write_text(said, encoding="utf-8")
    (gone / "submission.json").write_text('{"confirmed": false}', encoding="utf-8")
    third, fourth = _older(t, [("https://www.indeed.com/viewjob?jk=5555555555555555", "saved", "ASML",
                                "Customer Support Engineer", "Chandler, AZ", "indeed"),
                               ("https://asml.example/careers/job/2", "in_progress", "ASML", "Customer Support Engineer",
                                "Chandler, AZ", "")])
    only = Path(t.get(third)["folder"])  # (the one merged away has the only folder: it becomes the kept one's)
    only.mkdir(parents=True)
    (only / "resume-tailored.docx").write_text("asml", encoding="utf-8")
    t.close()

    t = Tracker()
    assert t.get(second) is None and Path(t.get(first)["folder"]) == kept and not gone.exists()
    assert (kept / "resume-tailored.docx").read_text(encoding="utf-8") == "kept"
    assert (kept / f"from-job-{second}" / "resume-tailored.docx").read_text(encoding="utf-8") == "gone"
    assert (kept / "submission.json").read_text(encoding="utf-8") == '{"confirmed": false}'
    assert t.get(third) is None and not only.exists()
    assert (Path(t.get(fourth)["folder"]) / "resume-tailored.docx").read_text(encoding="utf-8") == "asml"
    t.close()
