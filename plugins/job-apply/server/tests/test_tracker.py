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
    assert "Lam Research" in out.read_text()


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
