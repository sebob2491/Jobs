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
