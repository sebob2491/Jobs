"""The desk without Claude, and an application's status set on the desk (the design's C7, C8 and C9, tests 10 and 11):
a desk run on its own never holds a job for a resume, whatever desk.json says, and its words leave Claude out; and
POST /api/job/{id}/status sets one of five statuses on one of the person's applications, nothing else. Made-up
employers only: the repo is public."""

import asyncio
import json
import time
from pathlib import Path

import pytest
import yaml
from conftest import fixture_url, run

from job_apply import config, letters, pipeline, pipeline_letters
from job_apply.desk import Desk
from job_apply.pipeline import Applier, Run

FIVE = ("applied", "interviewing", "offer", "rejected", "withdrawn")
NOTE = "set on the Job Desk"
NOT_AN_APPLICATION = "That job isn't one of your applications."
NOT_SENT_YET = ("That job hasn't been sent yet. If you sent it on the site yourself, press I submitted it myself on "
                "its card on Home.")


async def _until(check, timeout=60, about=None):
    start = time.monotonic()
    while not check():
        if time.monotonic() - start > timeout:
            raise AssertionError("timed out waiting" + (f": {about()!r}" if about else ""))
        await asyncio.sleep(0.1)


def _client(desk):
    import httpx

    return httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=20, trust_env=False)


def _tailoring_chosen() -> None:
    """"Tailor my resume for each job" on, as desk.json keeps it."""
    (config.home() / "desk.json").write_text(json.dumps({"tailor_resumes": True}), encoding="utf-8")


def _idle_desk(srv, standalone: bool) -> Desk:
    desk = Desk(srv)
    desk.applier.start = lambda: None  # (the queue isn't worked: what's kept and said is what's looked at)
    desk.search.update(status="done", at=time.time())
    desk.standalone = standalone
    return desk


# ------------------------------------------------------------------ C9, test 11: /api/job/{id}/status
def test_api_job_status_sets_each_of_the_five_and_refuses_anything_else(srv):
    """Each of the five, in lower case, on a job in the Applications list: the tracker's status, with the note. A
    sixth value, a value of another shape, a job that isn't an application (or isn't a job), a body that isn't JSON:
    400, nothing changed. Without the page's key (or from another host): 403, nothing changed."""
    t = srv.tracker()
    applied = srv.add_job(url="https://careers.example.com/jobs/1", title="Technician", company="Example Fab")["job"]
    t.update(applied["id"], status="applied")
    saved = srv.add_job(url="https://careers.example.com/jobs/2", title="Technician", company="Example Litho")["job"]
    skipped = srv.add_job(url="https://careers.example.com/jobs/3", title="Technician", company="Example Etch")["job"]
    t.update(skipped["id"], status="skipped")
    # filled, waiting on its review page, never sent
    filled = srv.add_job(url="https://careers.example.com/jobs/4", title="Technician", company="Example Dep")["job"]
    t.update(filled["id"], status="ready_to_submit")
    desk = _idle_desk(srv, standalone=True)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            async with _client(desk) as c:
                path = f"/api/job/{applied['id']}/status"
                for status in (*FIVE[1:], FIVE[0]):
                    r = await c.post(path, headers=h, json={"status": status})
                    assert (r.status_code, r.json()) == (200, {"ok": True, "status": status}), r.text
                    assert t.get(applied["id"])["status"] == status
                    assert t.events(applied["id"])[-1]["status"] == status
                    assert t.events(applied["id"])[-1]["note"] == NOTE
                    # (and the page's Applications list says so)
                    state = (await c.get("/api/state", headers=h)).json()
                    assert [a["status"] for a in state["applications"] if a["id"] == applied["id"]] == [status]
                before = len(t.events(applied["id"]))
                for body in ({"status": "skipped"}, {"status": "saved"}, {"status": "ready_to_submit"},
                             {"status": "Offer"}, {"status": " offer"}, {"status": "hired"}, {"status": ""},
                             {"status": None}, {"status": 3}, {"status": ["offer"]}, {}, ["offer"]):
                    r = await c.post(path, headers=h, json=body)
                    assert r.status_code == 400, (body, r.text)
                r = await c.post(path, headers={**h, "content-type": "application/json"}, content=b"offer")
                assert r.status_code == 400
                for job in (saved, skipped):  # (not in the Applications list)
                    r = await c.post(f"/api/job/{job['id']}/status", headers=h, json={"status": "offer"})
                    assert (r.status_code, r.json()) == (400, {"error": NOT_AN_APPLICATION})
                    assert t.get(job["id"])["status"] == ("saved" if job is saved else "skipped")
                for status in FIVE:  # (filled, never sent: no status, nor a time it was applied at)
                    r = await c.post(f"/api/job/{filled['id']}/status", headers=h, json={"status": status})
                    assert (r.status_code, r.json()) == (400, {"error": NOT_SENT_YET})
                assert t.get(filled["id"])["status"] == "ready_to_submit" and not t.get(filled["id"])["applied_at"]
                for job_id in (999999, 2 ** 63):  # (no such job; past what the tracker can hold)
                    r = await c.post(f"/api/job/{job_id}/status", headers=h, json={"status": "offer"})
                    assert r.status_code == 400, r.text
                for headers in ({}, {"x-desk-token": "a-guess"}, {**h, "host": "careers.example.com"}):
                    r = await c.post(path, headers=headers, json={"status": "offer"})
                    assert r.status_code == 403
                assert (await c.get(path, headers=h)).status_code == 405
                assert t.get(applied["id"])["status"] == "applied" and len(t.events(applied["id"])) == before
        finally:
            await desk.stop()

    run(go())


# ------------------------------------------------------------------ C7 and C9, test 10: no resume to wait for
@pytest.mark.parametrize("standalone", [True, False])
def test_a_job_applied_for_with_tailoring_in_desk_json_waits_for_a_resume_only_inside_claude(srv, monkeypatch,
                                                                                             standalone):
    """desk.json has "Tailor my resume for each job" on. On a desk run on its own nothing writes a resume: the job goes
    on without waiting (its tab opens, and the page's sign-in is its stop); on a desk inside Claude it waits, before its
    tab opens, as it always has."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    _tailoring_chosen()
    job = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    desk = Desk(srv)
    desk.standalone = standalone
    applier = desk.applier
    assert applier.tailor is True  # (the person's choice, as desk.json keeps it)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await _until(lambda: r.status == "needs_you", about=lambda: (r.status, r.reason))
            return r, [x.job_id for x in applier.tailoring()]
        finally:
            await applier.stop()

    r, waiting = run(go())
    if standalone:
        assert r.need == "sign_in" and waiting == [], (r.need, r.reason, r.log)
        assert pipeline._TAILOR_SAYS not in r.reason
    else:
        assert (r.need, r.page, r.reason, waiting) == ("tailor", None, pipeline._TAILOR_SAYS, [job["id"]])


@pytest.mark.parametrize("standalone", [True, False])
def test_a_job_kept_waiting_for_its_resume_over_a_restart_waits_only_inside_claude(srv, standalone):
    """A job queued while tailoring was on, kept in the queue's file as waiting for its resume, and desk.json's switch
    still on: a desk run on its own picks it up as an ordinary queued job, its state saying tailoring is off and none
    waiting; a desk inside Claude has it waiting for its resume again."""
    _tailoring_chosen()
    job = srv.add_job(url="https://careers.example.com/jobs/7", title="Technician", company="Example Fab")["job"]
    (config.home() / "desk-queue.json").write_text(
        json.dumps({"paused": False, "jobs": [{"id": job["id"], "state": "queued", "tailor": True}]}),
        encoding="utf-8")
    desk = _idle_desk(srv, standalone)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                return (await c.get("/api/state", headers={"x-desk-token": desk.token})).json()
        finally:
            await desk.stop()

    state = run(go())
    r = desk.applier.runs[job["id"]]
    if standalone:
        assert (r.status, r.need) == ("queued", ""), (r.status, r.need, r.reason)
        assert ("apply", job["id"]) in desk.applier.tasks
        assert state["settings"]["tailor_resumes"] is False and state["tailoring"] == 0
    else:
        assert (r.status, r.need, r.reason) == ("needs_you", "tailor", pipeline._TAILOR_SAYS)
        assert state["settings"]["tailor_resumes"] is True and state["tailoring"] == 1


def test_a_job_waiting_for_its_resume_goes_on_once_the_desk_is_on_its_own(srv):
    """A job left waiting for its resume (queued while tailoring was on) when the desk turns out to run on its own: it
    goes on, as when the switch is turned off."""
    _tailoring_chosen()
    job = srv.add_job(url="https://careers.example.com/jobs/8", title="Technician", company="Example Fab")["job"]
    desk = Desk(srv)
    desk.applier.start = lambda: None
    waiting = Run(job["id"], "Technician", "Example Fab", status="needs_you", need="tailor",
                  reason=pipeline._TAILOR_SAYS, code="tailor")
    desk.applier.runs[job["id"]] = waiting

    async def go():
        desk.standalone = True

    run(go())
    assert waiting.status == "queued" and ("apply", job["id"]) in desk.applier.tasks
    assert desk.applier.tailoring() == [] and not desk.applier.tailoring_on()


def test_the_settings_route_turning_tailoring_on_holds_nothing_on_a_desk_run_on_its_own(srv):
    """The page's switch is hidden there, but the route still takes the setting: it's kept in desk.json (for a desk
    inside Claude), and here it says, and stays, off: a job applied for after it doesn't wait for a resume."""
    desk = _idle_desk(srv, standalone=True)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            async with _client(desk) as c:
                r = await c.post("/api/settings", headers=h, json={"tailor_resumes": True})
                assert r.status_code == 200 and r.json()["tailor_resumes"] is False
                state = (await c.get("/api/state", headers=h)).json()
                assert state["settings"]["tailor_resumes"] is False
        finally:
            await desk.stop()

    run(go())
    assert json.loads((config.home() / "desk.json").read_text(encoding="utf-8"))["tailor_resumes"] is True
    assert desk.applier.tailor is True and not desk.applier.tailoring_on()


# ------------------------------------------------------------------ the sentences with two forms (Plain wording, C)
@pytest.mark.parametrize("standalone", [True, False])
def test_the_first_cover_letters_stop_names_claude_only_inside_claude(srv, standalone):
    """LETTER_SAYS: both say "If it isn't, press Turn cover letters off."; inside Claude it adds "Or tell Claude in the
    chat what to change." """
    job = srv.add_job(url="https://careers.example.com/jobs/9", title="Technician", company="Example Fab")["job"]
    folder = Path(srv.tracker().get(job["id"])["folder"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / letters.DRAFT).write_text("# A letter\n\nDear Example Fab,\n", encoding="utf-8")
    applier = Applier(srv)
    applier.letters = True
    applier.standalone = standalone
    r = Run(job["id"], "Technician", "Example Fab", status="running")

    async def go():
        return await applier._held_for_first_letter(r)

    assert run(go()) is True
    assert (r.status, r.need, r.code) == ("needs_you", "cover_letter", "cover_letter")
    alone = ("This is the first cover letter the desk wrote for you, and this job waits for your say. If it's right, "
             "press Use it: from then on each job's letter goes in without asking. If it isn't, press Turn cover "
             "letters off.")
    assert pipeline_letters.LETTER_SAYS_ALONE == alone
    if standalone:
        assert r.reason == alone and "Claude" not in r.reason
    else:
        assert r.reason == alone + " Or tell Claude in the chat what to change." == pipeline_letters.LETTER_SAYS


ADD_IT = "Add it in your profile (Settings, Your profile), then press Carry on."
ASK_FOR_JOBS = " Or ask Claude: “add the start and end months of my jobs from my resume to my profile”."
ASK_FOR_SCHOOLS = " Or ask Claude to add it from your resume (only what it says: never a degree you didn't finish)."


NO_DEGREE = (" For a college where you took classes without finishing, your profile says Some college (no degree), "
             "never a degree: answer No to “Did you finish this degree?”.")
AFTER = (" From then on, the desk fills them in from your profile on every application. Or type them into the page in "
         "the desk's browser window instead, then press Carry on.")


@pytest.mark.parametrize("standalone", [True, False])
def test_the_stops_about_past_jobs_and_schools_name_claude_only_inside_claude(srv, standalone):
    """The stops for a past job's and a school's dates (or Degree) the profile lacks say to add them in the profile on
    the desk, never in profile.yaml; inside Claude each adds what to ask Claude. A Degree box adds how a college not
    finished is said (coursework is never a degree). Carry on is asked for once for each thing the person does."""
    path = config.profile_path()
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    profile["work_history"][0].pop("start")
    profile["education_history"][0].pop("start")
    profile["education_history"][0].pop("degree")
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    applier = Applier(srv)
    applier.standalone = standalone
    site = {"url": "https://example.wd1.myworkdayjobs.com/x"}
    work = {"id": "a", "label": "From", "sublabel": "Year", "section": "Work Experience 1", "required": True}
    school = {"id": "b", "label": "From", "sublabel": "Year", "section": "Education 1", "required": True}
    degree = {"id": "c", "label": "Degree*", "section": "Education 1", "required": True, "kind": "listbox"}

    def said(*boxes):
        return applier._entry_gaps(Run(1, "Technician", "Example Litho"), site, list(boxes))[0]

    jobs = ("Workday asks about your past jobs, and your profile doesn't have this for them: Equipment Technician at "
            "Intel (From). " + ADD_IT + ("" if standalone else ASK_FOR_JOBS))

    def schools(what):
        return ("Workday asks about your schools, and your profile doesn't have this for them: Arizona State "
                f"University in Education 1 ({what}). " + ADD_IT + ("" if standalone else ASK_FOR_SCHOOLS))

    assert said(work) == jobs + AFTER
    assert said(school) == schools("From") + AFTER
    assert said(degree) == schools("Degree") + NO_DEGREE + AFTER
    assert said(work, school) == jobs + " " + schools("From") + AFTER
    for each in (said(work), said(school), said(degree), said(work, school)):
        assert "profile.yaml" not in each and "_history" not in each, each
        assert ("Claude" in each) is not standalone, each
        assert "Then press Carry on" not in each and each.count("press Carry on") == each.count("Add it in") + 1


def test_the_sentences_the_same_with_claude_and_without():
    """QUEUE_ELSEWHERE and the profile search's refusal (config.search_profile_text) say what the design says, with
    Claude or without: no Claude Code session, no "ask Claude"."""
    assert pipeline.QUEUE_ELSEWHERE.startswith("Another Job Desk is already running on this computer, and the queue "
                                               "is kept there: ")
    assert "Claude" not in pipeline.QUEUE_ELSEWHERE
    config.profile_path().unlink()  # (no profile yet)
    with pytest.raises(ValueError) as refused:
        config.search_profile_text(places=["Phoenix, AZ"])
    assert str(refused.value) == "Set up your profile first."


def test_the_tailoring_stop_says_in_your_chat_with_claude():
    """_TAILOR_SAYS is said only inside Claude (a desk run on its own never holds a job for a resume): "In your chat
    with Claude", not "In Claude Code"."""
    assert pipeline._TAILOR_SAYS.startswith("Waiting for a resume written for this job. In your chat with Claude, say "
                                            "“tailor my resumes”; ")
    assert "Claude Code" not in pipeline._TAILOR_SAYS
