"""The Job Desk's own setup, its "Set up your profile" window (profile_setup.py and desk.py's /api/profile/*): the
resume saved, read with an Anthropic API key (a fake one here: no network) into the form's empty boxes only, and the
form saved into profile.yaml with every other line and comment kept, a school not finished never counted as a degree.
Made-up people only."""

import base64
import io
import json
import zipfile

import httpx
import pytest
import yaml
from conftest import browser_available, person_browser, run
from test_letters import KEY, FakeAnthropic

from job_apply import config, letters, profile_setup
from job_apply.desk import Desk

PDF = b"%PDF-1.4\n% a made-up resume\n"
# Claude's reading of a made-up resume, as structured outputs give it
DRAFT = {"first_name": "Sam", "last_name": "Rivera", "email": "sam.rivera@example.com", "phone": "480-555-0123",
         "city": "Chandler", "state": "AZ", "postal_code": "85225",
         "jobs": [{"company": "Example Fab", "title": "Equipment Technician", "start": "2021-03", "end": "present"},
                  {"company": "Example Pumps", "title": "Maintenance Technician", "start": "Jun 2018",
                   "end": "2021-02"}],
         "schools": [{"school": "Example Community College", "degree": "Associate of Applied Science",
                      "field": "Electronics", "start": "2016", "end": "2018"},
                     {"school": "Example State University", "degree": "Bachelor of Science",
                      "field": "Electrical Engineering", "start": "2018", "end": "2019"}]}


@pytest.fixture
def template_home(job_apply_home):
    """A profile as setup starts it: the plugin's template, comments and all, and nothing of the person's in it."""
    config.profile_path().write_text(config.TEMPLATE_PROFILE.read_text(encoding="utf-8"), encoding="utf-8")
    return job_apply_home


@pytest.fixture
def fake_api(job_apply_home, monkeypatch):
    import anthropic
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    FakeAnthropic.asked, FakeAnthropic.answers, FakeAnthropic.keys, FakeAnthropic.clients = [], [], [], []
    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAnthropic)
    return FakeAnthropic


def _point_at_resume(home) -> None:
    """The made-up resume PDF in the home folder, and the profile's documents.resume naming it."""
    (home / "resume.pdf").write_bytes(PDF)
    config.profile_path().write_text(_text().replace("resume: ~/.job-apply/resume.pdf",
                                                     f"resume: {(home / 'resume.pdf').as_posix()}"),
                                     encoding="utf-8")


def _text() -> str:
    return config.profile_path().read_text(encoding="utf-8")


def _data() -> dict:
    return yaml.safe_load(_text())


# ------------------------------------------------------------------ set_yaml_value, three levels deep
def test_a_value_three_levels_deep_is_set_in_place_and_every_comment_kept():
    text = config.TEMPLATE_PROFILE.read_text(encoding="utf-8")
    out = config.set_yaml_value(text, ("personal", "address", "city"), "Chandler")
    assert yaml.safe_load(out)["personal"]["address"]["city"] == "Chandler"
    assert out.replace('    city: "Chandler"\n', "    city: ~\n") == text  # (only that line changed)
    out = config.set_yaml_value(out, ("personal", "address", "state"), "AZ")
    assert '    state: "AZ"                   # "AZ" or "Arizona" both work\n' in out
    # (the same key name elsewhere is never the one changed: personal.address.line1, not some other line1)
    data = yaml.safe_load(config.set_yaml_value(out, ("personal", "first_name"), "Sam"))
    assert data["personal"]["first_name"] == "Sam" and data["personal"]["address"]["city"] == "Chandler"


def test_a_section_three_levels_deep_that_isnt_there_is_added_where_it_belongs():
    text = "# mine\npersonal:\n  first_name: Sam   # what they go by\nsettings:\n  headless: false\n"
    out = config.set_yaml_value(text, ("personal", "address", "city"), "Chandler")
    assert yaml.safe_load(out) == {"personal": {"first_name": "Sam", "address": {"city": "Chandler"}},
                                   "settings": {"headless": False}}
    assert "# what they go by" in out and out.startswith("# mine\n")
    out = config.set_yaml_value("settings:\n  headless: false\n", ("personal", "address", "city"), "Reno")
    assert yaml.safe_load(out) == {"settings": {"headless": False}, "personal": {"address": {"city": "Reno"}}}
    # an address with keys of its own, and one written under a deeper indent: the new key goes in with them
    text = "personal:\n    address:\n        line1: 1 Main St\n    email: a@example.com\n"
    out = config.set_yaml_value(text, ("personal", "address", "city"), "Reno")
    assert yaml.safe_load(out)["personal"] == {"address": {"line1": "1 Main St", "city": "Reno"},
                                               "email": "a@example.com"}


def test_three_levels_laid_out_another_way_is_never_half_changed():
    for text in ("personal: {address: {city: Reno}}\n", "personal:\n  address: {city: Reno}\n",
                 "personal:\n  address: Reno\n", "personal: [a]\n"):
        with pytest.raises(ValueError):
            config.set_yaml_value(text, ("personal", "address", "city"), "Chandler")
    with pytest.raises(ValueError):
        config.set_yaml_value("a: 1\n", ("a", "b", "c", "d"), 1)


def test_a_list_of_entries_is_written_on_one_line_and_read_back_whole():
    text = config.TEMPLATE_PROFILE.read_text(encoding="utf-8")
    jobs = [{"title": "Equipment Technician", "company": "Example Fab", "start": "2021-03", "end": "present",
             "description": "Kept \"etch\" tools running.\nNights."}]
    out = config.set_yaml_value(text, ("work_history",), jobs)
    assert yaml.safe_load(out)["work_history"] == jobs
    assert "#  - title: Equipment Technician" in out  # (the template's example, a comment, kept)
    out = config.set_yaml_value(out, ("education", "degrees_earned"), [{"level": "bachelor", "field": "Finance"}])
    assert yaml.safe_load(out)["education"]["degrees_earned"] == [{"level": "bachelor", "field": "Finance"}]
    assert "# e.g. [{level: bachelor, field: Finance}]; level is one of" in out


# ------------------------------------------------------------------ the form, as the profile fills it
def test_the_form_is_the_profiles_own_answers_and_nothing_preset(template_home):
    form = profile_setup.form_of(config.Profile.load())
    assert form["personal"]["first_name"] == "" and form["personal"]["country"] == "United States"
    # (the template's over_18: true isn't the person's answer: every work authorization question starts unanswered)
    assert set(form["work_authorization"].values()) == {None}
    assert form["settings"] == {"accept_cookies": False, "manage_accounts": True, "accept_notices": True}
    assert form["jobs"] == [] and form["schools"] == []


def test_the_form_carries_the_profiles_jobs_and_schools(job_apply_home):
    """The test profile's (conftest): its jobs' dates in the form's shape, and its school's Yes or No unanswered (no
    degrees_earned says it was finished)."""
    form = profile_setup.form_of(config.Profile.load())
    assert form["jobs"][1] == {"title": "Maintenance Technician", "company": "Example Fab Services",
                               "location": "Tempe, AZ", "start": "2018-06", "end": "2021-02", "from": 1}
    assert form["schools"][0]["finished"] is None and form["schools"][0]["level"] == "bachelor"
    assert form["work_authorization"]["authorized_to_work"] is True and form["work_authorization"]["over_18"] is True


# ------------------------------------------------------------------ saving
FORM = {
    "personal": {"first_name": "Sam", "last_name": "Rivera", "email": "sam.rivera@example.com",
                 "phone": "480-555-0123", "line1": "100 W Main St", "city": "Chandler", "state": "AZ",
                 "postal_code": "85225", "country": "United States"},
    "work_authorization": {"authorized_to_work": True, "requires_sponsorship": False, "us_citizen": True,
                           "us_person": True, "over_18": True},
    "jobs": [{"title": "Equipment Technician", "company": "Example Fab", "location": "Chandler, AZ",
              "start": "2021-03", "end": "present"}],
    "schools": [{"school": "Example State University", "degree": "Bachelor of Science",
                 "field": "Electrical Engineering", "start": "2018", "end": "2019", "finished": False,
                 "level": "bachelor"},
                {"school": "Example Community College", "degree": "Associate of Applied Science",
                 "field": "Electronics", "start": "2016", "end": "2018", "finished": True, "level": "associate"}],
    "settings": {"accept_cookies": False, "manage_accounts": False, "accept_notices": True},
}


def test_the_form_is_saved_with_every_comment_kept_and_a_degree_not_finished_never_counted(template_home):
    out = profile_setup.save_form(json.loads(json.dumps(FORM)))
    text, data = _text(), _data()
    assert data["personal"]["first_name"] == "Sam" and data["personal"]["address"]["city"] == "Chandler"
    assert data["personal"]["address"]["postal_code"] == "85225"
    assert data["work_authorization"] == {**data["work_authorization"], "authorized_to_work": True,
                                          "requires_sponsorship": False, "us_citizen": True, "us_person": True,
                                          "over_18": True}
    assert data["work_history"] == [{"title": "Equipment Technician", "company": "Example Fab",
                                     "location": "Chandler, AZ", "start": "2021-03", "end": "present"}]
    assert data["experience"]["current_title"] == "Equipment Technician"
    # the university not finished: "Some college (no degree)", never a degree; the associate's is the highest
    assert data["education_history"][0]["degree"] == "Some college (no degree)"
    assert data["education_history"][0]["major"] == "Electrical Engineering"
    assert data["education_history"][1]["degree"] == "Associate of Applied Science"
    assert data["education"]["degrees_earned"] == [{"level": "associate", "field": "Electronics"}]
    assert data["education"]["highest_degree"] == "Associate's Degree"
    assert data["settings"]["manage_accounts"] is False and data["settings"]["accept_cookies"] is False
    # comments kept
    for comment in ("# true/false — legally authorized to work in the US", '# "AZ" or "Arizona" both work',
                    "# Notices and attestations: the desk agrees for you", "#  - title: Equipment Technician"):
        assert comment in text, comment
    assert "work_history" in out["saved"] and "experience.current_company" in out["saved"]
    assert "settings.accept_cookies" not in out["saved"]  # (unchanged: the template's off already)
    assert all(m.startswith("documents.resume") for m in out["missing"])  # (no resume picked in this test)
    # what the desk's answers make of it: no to a Bachelor's, yes to an Associate's
    from job_apply import autofill
    prof = config.Profile.load()
    assert autofill._has_degree(prof, {}, "Do you have a Bachelor's degree?") != "Yes"
    assert autofill._has_degree(prof, {}, "Do you have an Associate's degree?") == "Yes"


def test_no_degree_finished_is_some_college_and_degrees_earned_stays_empty(template_home):
    form = {"schools": [{**FORM["schools"][0]}]}
    profile_setup.save_form(form)
    data = _data()
    assert data["education"]["degrees_earned"] == [] and data["education"]["highest_degree"] == "Some college"
    assert data["education_history"][0]["degree"] == "Some college (no degree)"


def test_saving_changes_only_what_the_form_changed(job_apply_home):
    """The form as the profile fills it, saved unchanged (its school's Yes or No answered as degrees_earned already
    says): only the consents the profile didn't state are written (the person's choice, seen and kept), then nothing.
    One box changed: that line alone; a job's description, which the form doesn't show, kept."""
    profile = yaml.safe_load(_text())
    profile["education"] = {"degrees_earned": [{"level": "bachelor", "field": "Electrical Engineering"}],
                            "highest_degree": "Bachelor's Degree"}
    config.profile_path().write_text(yaml.safe_dump(profile), encoding="utf-8")
    before = _text()
    form = profile_setup.form_of(config.Profile.load())
    assert form["schools"][0]["finished"] is True
    assert profile_setup.save_form(form)["saved"] == ["settings.accept_cookies", "settings.accept_notices"]
    assert _data() == {**profile, "settings": {**profile["settings"], "accept_cookies": False, "accept_notices": True}}
    profile = yaml.safe_load(_text())
    before = _text()
    form = profile_setup.form_of(config.Profile.load())
    assert profile_setup.save_form(form)["saved"] == [] and _text() == before
    form["personal"]["city"] = "Tempe"
    form["jobs"][0]["end"] = "2026-09"
    out = profile_setup.save_form(form)
    assert out["saved"] == ["personal.address.city", "work_history"]
    after = _data()
    assert after["personal"]["address"]["city"] == "Tempe"
    assert after["work_history"][0] == {**profile["work_history"][0], "end": "2026-09"}  # (its description kept)
    assert after["work_history"][1]["description"] == "PMs and troubleshooting on vacuum pumps."
    assert {k: v for k, v in after.items() if k not in ("personal", "work_history")} == \
        {k: v for k, v in profile.items() if k not in ("personal", "work_history")}


@pytest.mark.parametrize("change, said", [
    ({"personal": {"email": "sam at example"}}, "email"),
    ({"personal": {"first_name": ""}}, "first name"),
    ({"personal": {"phone": "call me"}}, "phone"),
    ({"work_authorization": {"authorized_to_work": "yes"}}, "Yes or No"),
    ({"jobs": [{"title": "Tech", "company": "Example Fab", "start": "March 2021"}]}, "2021-03"),
    ({"jobs": [{"title": "Tech", "company": ""}]}, "company"),
    ({"schools": [{"school": "Example State University", "degree": "BS", "finished": None, "level": "bachelor"}]},
     "finished"),
    ({"schools": [{"school": "Example State University", "finished": True, "level": ""}]}, "finished there"),
    ({"schools": [{"school": "Example State University", "finished": True, "level": "phd"}]}, "from the list"),
    ({"settings": {"manage_accounts": "on"}}, "on or off"),
    ({"personal": {"city": "x" * 200}}, "too long"),
])
def test_a_value_that_cant_be_right_is_refused_and_nothing_is_written(job_apply_home, change, said):
    before = _text()
    with pytest.raises(ValueError, match=said):
        profile_setup.save_form({**FORM, **change, "personal": {**FORM["personal"], **change.get("personal", {})}})
    assert _text() == before


# ------------------------------------------------------------------ the desk's endpoints
def _client(desk):
    return httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=20, trust_env=False)


def _with_desk(srv, drive):
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                return await drive(c, {"x-desk-token": desk.token})
        finally:
            await desk.stop()

    return run(go())


def _docx() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p>Sam Rivera</w:p></w:document>")
    return out.getvalue()


def test_the_resume_picked_on_the_page_is_saved_and_the_profile_points_at_it(srv, template_home):
    async def drive(c, h):
        b64 = base64.b64encode(PDF).decode()
        assert (await c.post("/api/profile/resume", json={"name": "r.pdf", "data": b64})).status_code == 403
        assert (await c.get("/api/profile/form")).status_code == 403
        r = await c.post("/api/profile/resume", headers=h, json={"name": "My Resume.PDF", "data": b64})
        assert r.status_code == 200, r.text
        assert (template_home / "resume.pdf").read_bytes() == PDF
        assert config.expand(_data()["documents"]["resume"]) == template_home / "resume.pdf"
        assert "# default resume (PDF or DOCX)" in _text()
        form = (await c.get("/api/profile/form", headers=h)).json()
        assert form["form"]["resume"] == {"path": str(template_home / "resume.pdf"), "found": True}
        assert form["can_read"] is False and KEY not in json.dumps(form)
        r = await c.post("/api/profile/resume", headers=h,
                         json={"name": "resume.docx", "data": base64.b64encode(_docx()).decode()})
        assert r.status_code == 200 and (template_home / "resume.docx").is_file()
        assert config.expand(_data()["documents"]["resume"]) == template_home / "resume.docx"
        before = _text()
        for body, said in (({"name": "resume.txt", "data": b64}, "PDF or a Word"),
                           ({"name": "resume.pdf", "data": base64.b64encode(b"<html>").decode()}, "isn't a PDF"),
                           ({"name": "resume.docx", "data": b64}, "isn't a Word"),
                           ({"name": "resume.pdf", "data": "not base64!"}, "come through"),
                           ({"name": "resume.pdf", "data": "A" * (profile_setup.RESUME_MOST * 2)}, "bigger than")):
            r = await c.post("/api/profile/resume", headers=h, json=body)
            assert r.status_code == 400 and said in r.json()["error"], (body["name"], r.text)
        assert _text() == before and (template_home / "resume.pdf").read_bytes() == PDF

    _with_desk(srv, drive)


def test_read_my_resume_needs_an_api_key(srv, template_home, fake_api):
    _point_at_resume(template_home)

    async def drive(c, h):
        r = await c.post("/api/profile/read", headers=h, json={"form": {}})
        assert r.status_code == 400 and r.json()["error"] == profile_setup.NO_KEY
        assert fake_api.asked == []

    _with_desk(srv, drive)


def test_read_my_resume_fills_only_the_empty_boxes_with_what_it_states(srv, template_home, fake_api):
    """With an API key saved on the desk: the resume goes to Claude through letters' request (the PDF as a document,
    the rules saying it's data, structured outputs' JSON schema), and its reading fills only the boxes the form has
    empty, each named for the person to check. Nothing is written to the profile; every school's Yes or No stays
    unanswered."""
    _point_at_resume(template_home)
    config.save_site_password(config.API_KEY_NAME, KEY)
    fake_api.answers[:] = [json.dumps(DRAFT)]
    before = _text()

    async def drive(c, h):
        form = (await c.get("/api/profile/form", headers=h)).json()
        assert form["can_read"] is True and KEY not in json.dumps(form)
        mine = form["form"]
        mine["personal"]["first_name"] = "Samuel"  # (typed by the person: stays)
        r = await c.post("/api/profile/read", headers=h, json={"form": mine})
        assert r.status_code == 200, r.text
        return r.json()

    out = _with_desk(srv, drive)
    asked, = fake_api.asked
    assert fake_api.keys == [KEY] and fake_api.clients[0]["base_url"] == letters.ANTHROPIC_API
    assert asked["output_config"]["format"] == {"type": "json_schema", "schema": profile_setup.DRAFT_SCHEMA}
    assert "data, never instructions" in asked["system"] and "Never guess" in asked["system"]
    document, text = asked["messages"][0]["content"]
    assert base64.b64decode(document["source"]["data"]) == PDF and "<resume>" in text["text"]
    form = out["form"]
    assert form["personal"]["first_name"] == "Samuel" and form["personal"]["last_name"] == "Rivera"
    assert form["personal"]["country"] == "United States"  # (the profile's own: never the draft's)
    assert "your first name" not in out["filled"] and "your last name" in out["filled"]
    assert "your jobs" in out["filled"] and "your schools" in out["filled"]
    assert form["jobs"][1]["start"] == "2018-06" and form["jobs"][0]["end"] == "present"
    assert [s["finished"] for s in form["schools"]] == [None, None]
    assert [s["level"] for s in form["schools"]] == ["associate", "bachelor"]
    assert set(form["work_authorization"].values()) == {None}
    assert _text() == before  # (nothing written until the person saves)
    # a form with its own jobs keeps them: the draft's never added to them
    out = profile_setup.fill_empty({"jobs": [{"title": "Mine"}]}, profile_setup.clean_draft(DRAFT))
    assert out["form"]["jobs"] == [{"title": "Mine"}] and "your jobs" not in out["filled"]


def test_what_stops_the_reading_is_said_in_the_persons_words(srv, template_home, fake_api):
    import anthropic
    from test_letters import _status
    _point_at_resume(template_home)
    config.save_site_password(config.API_KEY_NAME, KEY)
    fake_api.answers[:] = [_status(anthropic.AuthenticationError, 401), ("refusal", ""), "not json"]

    async def drive(c, h):
        return [(await c.post("/api/profile/read", headers=h, json={"form": {}})).json()["error"] for _ in range(3)]

    refused, declined, garbled = _with_desk(srv, drive)
    assert refused.startswith("Your resume couldn't be read: Anthropic turned down the API key saved on the desk.")
    assert "Cover letters" not in refused and KEY not in refused
    assert "declined" in declined and "didn't come back as asked" in garbled


def test_the_form_saved_through_the_desk(srv, template_home):
    async def drive(c, h):
        assert (await c.post("/api/profile/save", json={"form": FORM})).status_code == 403
        r = await c.post("/api/profile/save", headers=h, json={"form": {"personal": {"email": "nope"}}})
        assert r.status_code == 400 and "email" in r.json()["error"]
        r = await c.post("/api/profile/save", headers=h, json={"form": FORM})
        assert r.status_code == 200, r.text
        return r.json()

    out = _with_desk(srv, drive)
    assert "personal.address.city" in out["saved"] and _data()["personal"]["address"]["city"] == "Chandler"


# ------------------------------------------------------------------ the page
@pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")
def test_the_page_walks_a_new_person_through_setting_up(srv, template_home, fake_api, tmp_path):
    """A profile fresh from the template: Set up your profile opens by itself. The person picks their resume, has it
    read (a fake API), checks the boxes it filled, answers the work authorization questions and each school's "Did
    you finish", keeps the consents, and saves: the profile is written and the What and where window comes up."""
    config.save_site_password(config.API_KEY_NAME, KEY)
    fake_api.answers[:] = [json.dumps(DRAFT)]
    resume = tmp_path / "Sam Rivera resume.pdf"
    resume.write_bytes(PDF)
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#setup[open]")
                assert "Step 1 of 5" in await page.inner_text("#setup-steps")
                await page.set_input_files("#setup-file", str(resume))
                await page.wait_for_selector("#setup [data-act=setup-read]:not([disabled])")
                assert (template_home / "resume.pdf").read_bytes() == PDF
                await page.click("#setup [data-act=setup-read]")
                await page.wait_for_selector("#setup-filled:not([hidden])")
                said = await page.inner_text("#setup-filled")
                assert "your first name" in said and "your schools" in said, said
                assert await page.input_value("[data-sf=last_name]") == "Rivera"
                assert await page.input_value("[data-sf=country]") == "United States"
                # nothing preset: each work authorization question unanswered until picked
                assert await page.locator("#setup-auth input:checked").count() == 0
                for key, answer in (("authorized_to_work", "yes"), ("requires_sponsorship", "no"),
                                    ("us_citizen", "yes"), ("us_person", "yes"), ("over_18", "yes")):
                    await page.check(f"[data-sf-auth={key}][value={answer}]")
                await page.click("#setup-next")
                assert await page.locator("#setup-schools .setup-row").count() == 2
                assert await page.locator("#setup-schools input:checked").count() == 0
                await page.check("[data-k=finished][data-i='0'][value=yes]")
                await page.check("[data-k=finished][data-i='1'][value=no]")
                assert "Some college (no degree)" in await page.inner_text("#setup-schools")
                await page.click("[data-setup-drop=jobs][data-i='1']")  # (a job they'd leave out)
                assert await page.locator("#setup-jobs .setup-row").count() == 1
                await page.click("#setup-next")
                assert await page.is_checked("[data-sf-consent=manage_accounts]")
                assert not await page.is_checked("[data-sf-consent=accept_cookies]")
                await page.click("#setup-save")
                await page.wait_for_selector("#area[open]")
                assert not await page.locator("#setup").evaluate("d => d.open")
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    data = _data()
    assert data["personal"]["first_name"] == "Sam" and data["personal"]["address"]["postal_code"] == "85225"
    assert data["work_authorization"]["requires_sponsorship"] is False
    assert [j["company"] for j in data["work_history"]] == ["Example Fab"]
    assert data["education"]["degrees_earned"] == [{"level": "associate", "field": "Electronics"}]
    assert data["education_history"][1]["degree"] == "Some college (no degree)"
    assert data["education"]["highest_degree"] == "Associate's Degree"
    assert config.Profile.load().missing_required() == []
