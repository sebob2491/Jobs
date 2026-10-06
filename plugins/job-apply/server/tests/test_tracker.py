from pathlib import Path

import pytest

from job_apply.tracker import Tracker, normalize_url


def test_normalize_url():
    assert normalize_url("https://www.linkedin.com/jobs/view/field-service-engineer-at-lam-4402341490/?trk=x&refId=y") == \
        "https://www.linkedin.com/jobs/view/4402341490/"
    assert normalize_url("https://www.linkedin.com/jobs/search/?currentJobId=4402347426&geoId=1") == \
        "https://www.linkedin.com/jobs/view/4402347426/"
    assert normalize_url("https://www.indeed.com/viewjob?jk=a1b2c3&from=serp&vjs=3") == "https://www.indeed.com/viewjob?jk=a1b2c3"
    assert normalize_url("https://jobs.example.com/job/1?utm_source=x&id=2") == "https://jobs.example.com/job/1?id=2"


def test_upsert_update_export(job_apply_home):
    t = Tracker()
    job, created = t.upsert({"url": "https://www.linkedin.com/jobs/view/4402341490/?trk=a", "title": "Field Service Engineer 2",
                             "company": "Lam Research", "ats": "linkedin"})
    assert created and job["status"] == "saved"
    assert Path(job["folder"]).is_dir()
    again, created = t.upsert({"url": "https://www.linkedin.com/jobs/view/4402341490/", "location": "Chandler, AZ"})
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
