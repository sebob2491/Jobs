"""Learning from the person: the words of a button they press at a "no step button" stop, kept for the same
employer's page and pressed there next time (learned.py, pipeline._may_learn, _press_learned, _learn_from_hand)."""

import asyncio
import json
import re
import time
from pathlib import Path

import pytest
from conftest import browser_available, run

import job_apply.pipeline as pipeline
from job_apply import config, learned
from job_apply.pipeline import Applier, Run, _may_learn

needs_browser = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

# A step of an application with no step button of the desk's own: its way on is "Go to next section", a plain button
# (the desk knows Next, Continue and the like). The next section is a step with a box and Next, and after it comes a
# page with a Submit, which is the review page
PAGE = """<!doctype html><html><head><title>Apply</title></head><body>
<h1 id="h">Your experience</h1>
<div id="one"><p>Tell us about it.</p>
<label for="y">Years of experience</label><input id="y" type="text" value="">
<button type="button" id="{go}" onclick="document.getElementById('one').hidden=true;
  document.getElementById('h').textContent='References';document.getElementById('two').hidden=false">{words}</button>
{extra}</div>
<div id="two" hidden><label for="r">Reference name</label><input id="r" type="text">
<button type="button" onclick="document.getElementById('two').hidden=true;
  document.getElementById('h').textContent='Review';document.getElementById('three').hidden=false">Next</button></div>
<div id="three" hidden><p>Look it over.</p><button type="submit">Submit application</button></div>
</body></html>"""


def site(folder: Path, words="Go to next section", extra="") -> str:
    """A stand-in application page in `folder`; its address."""
    folder.mkdir(parents=True, exist_ok=True)
    page = folder / "apply.html"
    page.write_text(PAGE.format(go="go", words=words, extra=extra), encoding="utf-8")
    return page.resolve().as_uri()


async def until(condition, timeout=60, about=None):
    start = time.monotonic()
    while not condition():
        if time.monotonic() - start > timeout:
            raise AssertionError("timed out waiting" + (f": {about()!r}" if about else ""))
        await asyncio.sleep(0.2)


def state(r):
    return lambda: (r.status, r.need, r.code, r.reason, r.log[-8:])


def learned_file(home: Path) -> Path:
    return home / "learned.json"


FORM_PAGE = """<!doctype html><html><head><title>Apply</title></head><body>
<h1>Your experience</h1><form onsubmit="return false">
<label for="y">Years of experience</label><input id="y" type="text" value="">
<button type="submit" id="go">Go to next section</button></form></body></html>"""
DAY = "2026-10-03"
WORDS = "Go to next section"
SAID = "pressed \u201cGo to next section\u201d, as you did here on Oct 3"


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)


def over(applier, body):
    """Run `body(applier)` with the applier started, and stop it after."""
    async def go():
        applier.start()
        try:
            return await body(applier)
        finally:
            await applier.stop()
    return run(go())


async def settle(r, listening=True):
    """Until the run stops for the person (and, where the desk listens, until it does)."""
    await until(lambda: r.status not in ("queued", "running"), about=state(r))
    if listening:
        await until(lambda: r.hand_listening, about=state(r))


async def carry_on(applier, r):
    applier.resume(r.job_id)
    await until(lambda: r.status not in ("queued", "running"), about=state(r))


def page_key_sync(srv, url):
    async def go():
        await srv.browser.goto(url)
        return pipeline._learn_page_key(await srv.inspect_form(include_dropdown_options=False))
    return run(go())


def seed(srv, url, words=WORDS, day=DAY, company="Acme"):
    """What the desk learned at this page of this employer's on an earlier visit."""
    key = page_key_sync(srv, url)
    assert learned.remember(learned.place_key(url, company), "company_site", key, words, day)
    return key


def job_at(srv, url, n=0, company="Acme"):
    return srv.add_job(url=url + (f"?n={n}" if n else ""), title="Technician", company=company)["job"]


# -------------------------------------------------------------------- 1: a press that moves the step on is kept
@needs_browser
def test_a_press_at_a_no_step_button_stop_that_moves_the_step_on_is_kept(srv, job_apply_home, quick, tmp_path):
    url = site(tmp_path / "acme-careers")
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job_at(srv, url)["id"])
        await settle(r)
        assert (r.status, r.code) == ("needs_you", "no_step_button")
        # the person types in the box, clicks in it and on the page's words, then presses the button
        await r.page.fill("#y", "Sam Rivera 4471 sam.rivera@example.com")
        await r.page.click("#y")
        await r.page.click("p")
        await asyncio.sleep(0.5)
        assert r.hand_press == ""  # (nothing for a box, its typing or a paragraph)
        await r.page.click("#go")
        await until(lambda: r.hand_press, about=state(r))
        assert r.hand_press == WORDS
        await carry_on(applier, r)
        return r

    r = over(applier, body)
    assert r.status == "ready", (r.status, r.reason, r.log)
    text = learned_file(job_apply_home).read_text(encoding="utf-8")
    [entry] = json.loads(text)
    assert set(entry) == {"place", "system", "page", "button", "on", "worked", "failed", "employer"}
    assert (entry["button"], entry["system"], entry["employer"]) == (WORDS, "company_site", "Acme")
    assert (entry["worked"], entry["failed"]) == (0, 0)
    assert len(entry["page"]) == 16 and entry["place"].startswith("file:") and entry["on"] == time.strftime("%Y-%m-%d")
    assert entry["place"].endswith("|acme")  # (the employer, as the tracker names it: one host serves many)
    for never in ("acme-careers", "apply.html", "Years of experience", "Your experience", "Sam", "4471", "rivera",
                  "example.com", "Tell us about it", "References"):
        assert never not in text, never
    assert r.hand_press == "" and r.hand_stop is None  # (the stop's record is gone, learned or not)


# -------------------------------------------------------------------- 2: no other stop ever teaches
CODES = ["bot_check", "captcha", "account_theirs", "sign_in_refused", "email_code", "security_questions",
         "notice_theirs", "agreement_theirs", "assessment", "your_submit", "cookie_banner"]
NEEDS = {"bot_check": "bot_check", "captcha": "captcha", "email_code": "email_code", "your_submit": "your_submit"}
# (the step the press led to: a further one of the application, a box to fill on it)
MOVED = {"url": "https://acme.example/careers/apply", "headings": ["References"],
         "fields": [{"id": "r", "label": "Reference name", "kind": "text"}], "actions": [{"id": "n", "text": "Next"}]}


def _stopped(srv, code="no_step_button"):
    job = srv.add_job(url="https://acme.example/careers/apply", title="Technician", company="Acme")["job"]
    applier = Applier(srv)
    r = Run(job["id"], "Technician", "Acme", status="running", url="https://acme.example/careers/apply")
    applier.runs[job["id"]] = r
    applier._pause(r, NEEDS.get(code, "stuck"), "Over to you.", code=code)
    return applier, r


def _hand(r, code, words=WORDS):
    r.hand_stop = {"code": code, "place": learned.place_key(MOVED["url"], "Acme"), "system": "",
                   "page": "0123456789abcdef", "buttons": [{"text": words}]}
    r.hand_press, r.hand_heard = words, {words.casefold()}


def _going_on(r):
    """The run as the first look of the pass after Carry on finds it."""
    r.status, r.need, r.reason = "running", "", "Working on it"


PLACE = learned.place_key(MOVED["url"], "Acme")


def test_every_stop_code_there_is_for_the_cases_below_is_a_real_one():
    assert set(CODES) | {"no_step_button"} <= pipeline.STOP_CODES


@pytest.mark.parametrize("code", ["no_step_button", *CODES])
def test_only_a_no_step_button_stop_teaches(srv, job_apply_home, monkeypatch, code):
    """The same press, the same page moved on: kept at a no_step_button stop, at none of the others."""
    monkeypatch.setattr(Applier, "_looks_past", lambda *a: True)
    applier, r = _stopped(srv, code)
    _hand(r, code)
    _going_on(r)
    run(applier._learn_from_hand(r, MOVED, ""))
    kept = learned.lookup(PLACE, "0123456789abcdef")
    assert (kept is not None) == (code == "no_step_button")
    assert r.hand_stop is None and r.hand_press == ""


@pytest.mark.parametrize("code", CODES)
def test_a_stop_of_another_kind_forgets_what_the_last_one_heard(srv, job_apply_home, code):
    """A "no step button" stop's press, heard, then the run stops for a bot check or the like: the press is gone, and
    what the person does at that stop is never heard."""
    applier, r = _stopped(srv)
    _hand(r, "no_step_button")
    applier._pause(r, NEEDS.get(code, "stuck"), "Over to you.", code=code)
    assert r.hand_stop is None and r.hand_press == ""


# -------------------------------------------------------------------- 3: _may_learn
def _b(text, **kind):
    return {"text": text, **kind}


@pytest.mark.parametrize("words, kind", [
    ("Submit", {"is_submit": True}), ("Submit application", {}), ("Finish", {}), ("Send", {}),
    ("I agree", {}), ("Accept and continue", {}), ("I understand", {}), ("Acknowledge", {}),
    ("Sign up for job alerts", {}), ("Join our Talent Community", {}),
    ("Accept all cookies", {"cookie": True}), ("Got it", {"cookie": True}),
    ("Continue to account", {"sign_in_form": True}), ("Go to next section", {"account_form": True}),
    ("Go to next section", {"site_nav": True}), ("Go to next section", {"aside": True}),
    ("Go to next section", {"disabled": True}), ("Go to next section", {"form_submit": True}),
    ("Log in to go on", {}), ("Create your account", {}), ("Register", {}), ("Verify", {}), ("Captcha", {}),
    ("Give consent", {}), ("", {}), ("   ", {}),
])
def test_may_learn_refuses(words, kind):
    assert not _may_learn(words, [_b(words, **kind), _b("Back")])


def test_may_learn_refuses_two_buttons_with_the_same_words_and_one_the_page_doesnt_have():
    assert not _may_learn("Go to next section", [_b("Go to next section"), _b("  go TO next   section ")])
    assert not _may_learn("Go to next section", [_b("Back")])
    assert not _may_learn("Go to next section", [])


def test_may_learn_accepts_a_plain_step_button_and_a_forms_step_words():
    assert _may_learn("Go to next section", [_b("Go to next section"), _b("Back"), _b("Cancel")])
    assert _may_learn("  go to  NEXT section", [_b("Go to next section")])
    # (a form's own button, but its words are a step's)
    assert _may_learn("Next \u203a", [_b("Next \u203a", form_submit=True)])
    assert not _may_learn("Go to next section", [_b("Go to next section", form_submit=True)])


# -------------------------------------------------------------------- 4: the cookie-banner stop's code
@needs_browser
def test_a_cookie_banner_the_desk_wont_accept_stops_with_a_code_of_its_own(srv, job_apply_home, quick, tmp_path,
                                                                          monkeypatch):
    assert "cookie_banner" in pipeline.STOP_CODES
    page = tmp_path / "banner.html"
    page.write_text("<html><body><h1>Your details</h1><input aria-label='Phone'><button>Next</button></body></html>",
                    encoding="utf-8")
    real = srv.click

    async def refused(target, *a, **kw):
        return {"clicked": False, "blocked": "Cookie and privacy banners aren't accepted for the user."}
    monkeypatch.setattr(srv, "click", refused)
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job_at(srv, page.resolve().as_uri())["id"])
        await until(lambda: r.status not in ("queued", "running"), about=state(r))
        return r, await r.page.evaluate("typeof window.__jdHand")

    r, bound = over(applier, body)
    monkeypatch.setattr(srv, "click", real)
    assert (r.status, r.code) == ("needs_you", "cookie_banner"), (r.status, r.code, r.reason)
    assert "cookie or privacy banner is in the way" in r.reason
    assert r.hand_stop is None and bound == "undefined"  # (never listened to: a banner's button isn't a step's)


# -------------------------------------------------------------------- 5: next visit, the desk presses it
@needs_browser
def test_next_visit_the_desk_presses_the_button_the_person_pressed(srv, job_apply_home, quick, tmp_path):
    url = site(tmp_path / "acme")
    seed(srv, url)
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job_at(srv, url)["id"])
        await until(lambda: r.status not in ("queued", "running"), about=state(r))
        return r

    r = over(applier, body)
    assert r.status == "ready", (r.status, r.code, r.reason, r.log)
    assert SAID in r.log
    [entry] = learned.all()
    # (and the id of the entry, for the page's Don't do that again)
    shown = {"button": WORDS, "on": DAY, "id": learned.entry_id(entry["place"], entry["page"])}
    assert r.learned_press == shown and r.public()["learned_press"] == shown
    assert (entry["worked"], entry["failed"], entry["on"]) == (1, 0, DAY)


# -------------------------------------------------------------------- 6: and only there
def _not_used(r):
    assert (r.status, r.code) == ("needs_you", "no_step_button"), (r.status, r.code, r.reason, r.log)
    assert not any("as you did here" in line for line in r.log), r.log
    assert r.learned_press is None


@needs_browser
@pytest.mark.parametrize("case", ["another place", "another page shape", "a greyed button", "two buttons"])
def test_a_learned_press_is_not_made_where_it_was_not_learned(srv, job_apply_home, quick, tmp_path, case):
    url = site(tmp_path / "acme")
    key = seed(srv, url)
    if case == "another place":
        url = site(tmp_path / "other-employer")
    elif case == "another page shape":
        page = tmp_path / "acme" / "apply.html"
        page.write_text(page.read_text(encoding="utf-8").replace("Your experience", "Your history"), encoding="utf-8")
    elif case == "a greyed button":
        page = tmp_path / "acme" / "apply.html"
        page.write_text(page.read_text(encoding="utf-8").replace('id="go"', 'id="go" disabled'), encoding="utf-8")
    else:
        site(tmp_path / "acme", extra="<button type='button'>Go to next section</button>")
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job_at(srv, url)["id"])
        await until(lambda: r.status not in ("queued", "running"), about=state(r))
        return r

    r = over(applier, body)
    _not_used(r)
    [entry] = learned.all()  # (not forgotten, nor counted: it may work another time)
    assert (entry["page"], entry["worked"], entry["failed"]) == (key, 0, 0)


# -------------------------------------------------------------------- 7: a learned button that is a Submit now
@needs_browser
def test_a_learned_button_that_is_a_form_submit_now_is_not_pressed_and_is_forgotten(srv, job_apply_home, quick,
                                                                                    tmp_path):
    url = site(tmp_path / "acme")
    seed(srv, url)
    (tmp_path / "acme" / "apply.html").write_text(FORM_PAGE, encoding="utf-8")
    # (the same headings and boxes: the page's key is the same)
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job_at(srv, url)["id"])
        await until(lambda: r.status not in ("queued", "running"), about=state(r))
        return r, await r.page.evaluate("document.getElementById('y').value")

    r, _ = over(applier, body)
    _not_used(r)
    assert learned.all() == []


# -------------------------------------------------------------------- 8: two failures drop it
@needs_browser
def test_a_learned_press_that_moves_nothing_is_counted_and_dropped_at_the_second(srv, job_apply_home, quick, tmp_path):
    url = site(tmp_path / "acme")
    seed(srv, url)
    page = tmp_path / "acme" / "apply.html"
    page.write_text(page.read_text(encoding="utf-8").replace("onclick=", "data-x="), encoding="utf-8")  # (does nothing)
    applier = Applier(srv)

    async def body(applier):
        out = []
        for n in (1, 2):
            r = applier.enqueue(job_at(srv, url, n)["id"])
            await settle(r, listening=False)
            out.append((r.code, [e["failed"] for e in learned.all()]))
        return out

    assert over(applier, body) == [("no_step_button", [1]), ("no_step_button", [])]


def test_failed_drops_the_entry_at_the_second_and_forget_drops_it(job_apply_home):
    assert learned.remember("acme.example", "workday", "0123456789abcdef", WORDS, DAY)
    learned.failed("acme.example", "0123456789abcdef")
    learned.worked("acme.example", "0123456789abcdef")
    [e] = learned.all()
    assert (e["worked"], e["failed"]) == (1, 1)
    learned.failed("acme.example", "0123456789abcdef")
    assert learned.lookup("acme.example", "0123456789abcdef") is None
    learned.remember("acme.example", "workday", "0123456789abcdef", WORDS, DAY)
    assert learned.forget("acme.example", "0123456789abcdef") and not learned.forget("acme.example", "0123456789abcdef")


def test_the_file_reads_tolerantly_and_keeps_the_newest_500(job_apply_home):
    path = learned_file(job_apply_home)
    for text in ("", "not json", "{}", "[1, 2]", '[{"place": "x"}]'):
        path.write_text(text, encoding="utf-8")
        assert learned.all() == [] and learned.lookup("x", "0123456789abcdef") is None
        learned.failed("x", "0123456789abcdef")  # (never raises)
    assert not learned.remember("", "", "0123456789abcdef", WORDS, DAY)
    assert not learned.remember("x", "", "short", WORDS, DAY)
    assert not learned.remember("x", "", "0123456789abcdef", "   ", DAY)
    assert learned.remember("x", "", "0123456789abcdef", "w" * 90, DAY) and len(learned.all()[0]["button"]) == 60
    for n in range(520):
        learned.remember(f"h{n}.example", "", f"{n:016x}", WORDS, f"2026-01-{1 + n % 28:02d}" if n < 260 else DAY)
    kept = learned.all()
    assert len(kept) == 500 and all(e["on"] == DAY for e in kept if e["place"] == "h519.example")
    assert not any(e["on"].startswith("2026-01-0") and e["place"] in ("h0.example", "h28.example") for e in kept)


# -------------------------------------------------------------------- 9: the setting off, and 11: the live check
@needs_browser
@pytest.mark.parametrize("how", ["setting", "never_submit"])
def test_with_learning_off_nothing_is_heard_kept_or_used(srv, job_apply_home, quick, tmp_path, monkeypatch, how):
    url = site(tmp_path / "acme")
    applier = Applier(srv)
    if how == "setting":
        applier.learn_from_me = False
    else:
        monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")  # (the live check's)

    async def body(applier):
        r = applier.enqueue(job_at(srv, url)["id"])
        await settle(r, listening=False)
        assert await r.page.evaluate("typeof window.__jdHand") == "undefined"  # (no binding on the tab)
        await r.page.click("#go")
        await asyncio.sleep(0.5)
        assert r.hand_press == "" and r.hand_stop is None
        await carry_on(applier, r)
        return r

    r = over(applier, body)
    assert r.status == "ready", (r.status, r.reason)
    assert not learned_file(job_apply_home).exists()


@needs_browser
def test_with_the_setting_off_a_learned_press_is_not_used_and_the_file_is_left_alone(srv, job_apply_home, quick,
                                                                                    tmp_path):
    url = site(tmp_path / "acme")
    seed(srv, url)
    before = learned_file(job_apply_home).read_text(encoding="utf-8")
    applier = Applier(srv)
    applier.learn_from_me = False

    async def body(applier):
        r = applier.enqueue(job_at(srv, url)["id"])
        await until(lambda: r.status not in ("queued", "running"), about=state(r))
        return r

    _not_used(over(applier, body))
    assert learned_file(job_apply_home).read_text(encoding="utf-8") == before


# -------------------------------------------------------------------- 10: what the listener hears
@needs_browser
def test_the_listener_hears_only_a_buttons_words_and_only_while_the_stop_lasts(srv, job_apply_home, quick, tmp_path):
    url = site(tmp_path / "acme")
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job_at(srv, url)["id"])
        await settle(r)
        await r.page.click("#y")
        await r.page.keyboard.type("hello there")
        await r.page.keyboard.press("Enter")
        await r.page.click("h1")
        await asyncio.sleep(0.5)
        assert r.hand_press == ""  # (a box, keys, typing, a heading)
        # the stop ends in the page's flag too, and a report that came when none was waiting is ignored
        await applier._stop_hearing(r)
        await r.page.click("#go")
        await asyncio.sleep(0.5)
        assert r.hand_press == "" and await r.page.evaluate("typeof window.__jdHandOff") == "undefined"
        return r

    r = over(applier, body)
    assert r.hand_press == ""


@needs_browser
def test_a_press_heard_while_the_run_is_at_a_stop_of_another_kind_is_ignored(srv, job_apply_home, quick, tmp_path):
    url = site(tmp_path / "acme")
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job_at(srv, url)["id"])
        await settle(r)
        applier._pause(r, "bot_check", "Over to you.", code="bot_check")  # (the page's flag is still on)
        r.hand_stop = {"code": "no_step_button", "place": "x", "system": "", "page": "0" * 16, "buttons": []}
        assert await r.page.evaluate("typeof window.__jdHandOff") == "function"  # (the page's listener still on)
        await r.page.click("#go")
        await asyncio.sleep(0.5)
        return r

    assert over(applier, body).hand_press == ""


def test_the_listener_script_reports_nothing_but_a_buttons_words():
    js = learned.LISTEN_JS
    assert re.findall(r"addEventListener\('(\w+)'", js) == ["click"]
    assert "addEventListener('click', heard, true)" in js  # (the capture phase)
    for banned in ("clientX", "clientY", "pageX", "location", "href", "key", "cookie", "storage", "fetch"):
        assert banned not in js, banned
    assert js.count("window.__jdHand(") == 1  # (one report, of the words)


# -------------------------------------------------------------------- 12: what is never shown or sent
@needs_browser
def test_what_the_desk_heard_is_in_no_public_state_report_or_stop_record(srv, job_apply_home, quick, tmp_path):
    from job_apply import report

    url = site(tmp_path / "acme")
    seed(srv, url)
    job = job_at(srv, url)
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job["id"])
        await until(lambda: r.status not in ("queued", "running"), about=state(r))
        return r

    r = over(applier, body)
    assert SAID in r.log
    r.hand_press, r.hand_stop = "Zzyzx button", {"code": "no_step_button", "place": "zzyzx.example", "buttons": []}
    shown = json.dumps(r.public())
    folder = tmp_path / "stop"
    folder.mkdir()
    (folder / "snapshot.json").write_text("{}", encoding="utf-8")
    facts = json.dumps(report.stop_facts(r))
    written = report.write_stop(folder, report.stop_facts(r))
    assert written is not None and written.name == "stop.json"
    made = report.build({"id": r.job_id, "title": "Technician", "company": "Acme", "url": url}, r,
                        config.Profile.load())
    texts = [shown, facts, written.read_text(encoding="utf-8"), made["preview"]]
    for text in texts:
        assert "Zzyzx" not in text and "zzyzx" not in text and "hand_press" not in text and "hand_stop" not in text
    assert "learned_press" in shown


# -------------------------------------------------------------------- 13: the two routes
def test_the_routes_list_and_forget_what_was_learned_and_want_the_pages_key(srv, job_apply_home):
    from test_desk_settings import _calls, _desk

    learned.remember("acme.wd1.myworkdayjobs.com/acme", "workday", "0123456789abcdef", WORDS, DAY)
    learned.remember("file:0123456789abcdef", "", "fedcba9876543210", "Next section", "2026-10-04")
    desk = _desk(srv)
    [(code, got)] = _calls(desk, [("GET", "/api/learned", None)])
    assert code == 200
    first, second = got["learned"]
    assert set(first) == {"id", "employer", "system", "button", "on", "worked"}
    assert (first["employer"], first["system"], first["button"], first["on"], first["worked"]) == (
        "an application saved on this computer", "", "Next section", "2026-10-04", 0)
    assert (second["employer"], second["system"]) == ("acme.wd1.myworkdayjobs.com/acme", "Workday")
    assert "page" not in json.dumps(got) and "0123456789abcdef" not in json.dumps(got)
    out = _calls(desk, [("POST", "/api/learned/forget", {"id": second["id"]}), ("GET", "/api/learned", None),
                        ("POST", "/api/learned/forget", {"id": second["id"]}), ("POST", "/api/learned/forget", {})])
    assert out[0] == (200, {"forgotten": True}) and [e["button"] for e in out[1][1]["learned"]] == ["Next section"]
    assert out[2] == (200, {"forgotten": False}) and out[3][0] == 400
    for method, path, body in (("GET", "/api/learned", None), ("POST", "/api/learned/forget", {"id": first["id"]})):
        assert _calls(desk, [(method, path, body)], token=False)[0][0] == 403
    assert len(learned.all()) == 1  # (the refused one forgot nothing)


def test_the_setting_is_kept_in_desk_json_and_on_in_the_state(srv, job_apply_home):
    from test_desk_settings import _calls, _desk

    desk = _desk(srv)
    [(c0, s0)] = _calls(desk, [("GET", "/api/state", None)])
    assert s0["settings"]["learn_from_me"] is True
    out = _calls(desk, [("POST", "/api/settings", {"learn_from_me": False})])
    assert out[0] == (200, out[0][1]) and out[0][1]["learn_from_me"] is False
    assert json.loads((job_apply_home / "desk.json").read_text(encoding="utf-8"))["learn_from_me"] is False
    desk2 = _desk(srv)
    desk2._load()
    assert desk2.applier.learn_from_me is False


# ================================================================= the review of the learned press (its eight faults)
# -------------------------------------------------------------------- 1: a learned press never sends
TABLE = {
    # the step words, alone or with others: allowed
    "Next": True, "Continue": True, "Save and Continue": True, "Save & Continue": True, "Go to next section": True,
    "Review": True, "Next: Voluntary Disclosures": True, "Proceed": True, "Continue to next step": True,
    "Next ›": True,
    # an end, a send, an agreement, a way back or out, something added, another site's sign-in, an account, an answer
    "Proceed to step 3": False, "Review and Submit": False, "Done": False, "Finish": False,
    "Complete application": False, "Place my application": False, "Send": False, "Apply": False, "Confirm": False,
    "I agree": False, "Accept all": False, "Back": False, "Go back": False, "Previous step": False, "Cancel": False,
    "Skip this step": False, "Delete": False, "Add another": False, "Upload and continue": False,
    "Start assessment": False, "Continue with Google": False, "Continue with LinkedIn": False,
    "Continue as Sam Rivera": False, "Continue as guest": False, "Sign in": False, "Sign in and continue": False,
    "Create account": False, "Register and continue": False, "I'm not a robot": False, "Continue to my profile": False,
    "Yes, continue": False, "OK": False, "Subscribe and continue": False, "Save": False, "Done, next": False,
    "Next to job alerts": False, "Confirm and continue": False, "Finish and review": False,
    # the second review's: an end in other words, and words that leave the application
    "Continue and Finalize": False, "Proceed to finalise": False, "Continue to final step": False,
    "Next, Lodge application": False, "Next: eSignature": False, "Proceed with application": False,
    "Next job": False, "Go to next job": False, "Next opening": False, "Go to my applications": False,
    "Go to home": False, "Go to dashboard": False, "Continue later": False, "Save for later and continue": False,
    "Continue and logout": False, "Leave and continue": False, "Continue without saving": False,
    "Continue anyway": False, "Proceed anyway": False, "Continue to Video Interview": False,
    "Proceed to interview scheduling": False, "Forward to a friend": False, "Next position": False,
    "Continue to search": False, "Close and continue": False, "Continue to all steps": False,
    "Continue to list": False, "Next menu": False, "Share and continue": False, "Continue to survey": False,
    # look-alike and invisible characters, and a button not in English
    "Continue to \u0405ubmit": False, "Sub\u200bmit and continue": False, "Sub\u00admit and continue": False,
    "Continuer": False, "Weiter \u00bb": False, "Siguiente paso": False, "N\u0435xt": False,
    "Next \u00e9tape": False, "Continue \u2192 \u00e9tape suivante": False,
    # the third review's: more words that leave the application, and a mark laid over a letter
    "Go to careers": False, "Continue browsing": False, "Continue to careers site": False, "Go to portal": False,
    "Go to end": False, "Continue to last step": False, "Continue to verification": False,
    "Continue to Su\u0301bmit": False, "Ne\u0302xt": False,
    # no step word at all, nothing refused either: the allow-list alone keeps these out
    "Learn more": False, "Details": False, "Help": False, "Print": False, "Edit": False,
    "Show more": False, "Expand": False, "Hide": False, "Change": False, "Update": False, "Select": False,
    "Choose": False, "Go": False, "Proceeding": False, "Nextdoor": False, "Reviewed": False, "Stepped": False,
}


@pytest.mark.parametrize("words", list(TABLE))
def test_may_learn_allows_a_steps_words_and_nothing_that_ends_sends_or_leaves_the_step(words):
    """Allowed, not merely not refused: the words must say they go on to a step, and say nothing of an end, a send, a
    way back or out, something added, another site's sign-in, an account or an answer (the review: a plain "Done" that
    sent the application was learned, and pressed at the next job there)."""
    assert _may_learn(words, [_b(words), _b("Back")]) is TABLE[words], words


@pytest.mark.parametrize("words", ["Continue to step 2", "Next for jo@example.org", "Continue: resume_final.pdf",
                                   "Continue to careers.example", "Continue on to the next part of this form",
                                   "Next, Rivera", "Continue in Chandler", "Next for Sam"])
def test_may_learn_refuses_words_that_may_be_the_persons_own(words):
    """A number, an email, a file's or a site's name, a sentence, or a word of the person's name, email or city (the
    test profile's: Sam Rivera, sam.rivera@..., Chandler)."""
    assert not _may_learn(words, [_b(words)])
    assert not _may_learn("Continue to Tempe", [_b("Continue to Tempe")], personal={"tempe"})
    assert _may_learn("Continue to Tempe", [_b("Continue to Tempe")], personal={"chandler"})


@pytest.mark.parametrize("how", ["confirmation", "ended", "submit pressed", "two buttons pressed"])
def test_a_press_is_not_kept_after_a_confirmation_an_end_a_submit_or_with_two_buttons_pressed(srv, job_apply_home,
                                                                                              monkeypatch, how):
    """The same press as kept at test_only_a_no_step_button_stop_teaches, but the page after it says the application
    went in; the run ended (marked applied, or skipped); a Submit was pressed for it; or the person pressed two
    different buttons at the stop (the last of them may not be the one that moved the step on)."""
    monkeypatch.setattr(Applier, "_looks_past", lambda *a: True)
    applier, r = _stopped(srv)
    _hand(r, "no_step_button")
    _going_on(r)
    text = ""
    if how == "confirmation":
        text = "Thank you for applying. Your application has been received. We will be in touch."
    elif how == "ended":
        r.status = "submitted"
    elif how == "submit pressed":
        r.pressed_before = True
    else:
        r.hand_heard = {"add another", WORDS.casefold()}
    run(applier._learn_from_hand(r, MOVED, text))
    assert learned.lookup(PLACE, "0123456789abcdef") is None


# A step whose way on is "Continue to next step", and pressing it sends the application: the page thanks the person
SENDS = """<!doctype html><html><head><title>Apply</title></head><body>
<h1 id="h">Your experience</h1>
<label for="y">Years of experience</label><input id="y" type="text" value="3">
<button type="button" id="go" onclick="document.body.innerHTML=document.getElementById('thanks').innerHTML"
>Continue to next step</button>
<template id="thanks"><h1>Thank you for applying</h1><p>Your application has been received.</p></template>
</body></html>"""


@needs_browser
def test_a_learned_press_after_which_the_page_says_it_went_in_stops_for_the_person_to_check(srv, job_apply_home, quick,
                                                                                           tmp_path):
    folder = tmp_path / "acme"
    folder.mkdir()
    (folder / "apply.html").write_text(SENDS, encoding="utf-8")
    url = (folder / "apply.html").resolve().as_uri()
    seed(srv, url, words="Continue to next step")
    job = job_at(srv, url)
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job["id"])
        await until(lambda: r.status not in ("queued", "running"), about=state(r))
        return r

    r = over(applier, body)
    assert (r.status, r.need, r.code) == ("needs_you", "check_submit", "check_submit"), (r.code, r.reason, r.log)
    assert "“Continue to next step”, as you did here before" in r.reason and "I submitted it myself" in r.reason
    assert "the page now says \u201capplication has been received\u201d" in r.reason  # (the confirmation's own words)
    assert r.pressed_before and learned.all() == []  # (never pressed again, there or anywhere)
    assert srv.tracker().get(job["id"])["status"] != "applied"


@needs_browser
@pytest.mark.parametrize("how", ["never_submit", "dry_run"])
def test_a_learned_press_is_never_made_in_practice_mode(srv, job_apply_home, quick, tmp_path, monkeypatch, how):
    """JOB_APPLY_NEVER_SUBMIT=1 (the live check, its test identity) or submit_mode: dry_run: what was learned is left
    as it is, and not pressed."""
    url = site(tmp_path / "acme")
    seed(srv, url)
    before = learned_file(job_apply_home).read_text(encoding="utf-8")
    if how == "never_submit":
        monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    else:
        monkeypatch.setattr(config.Settings, "dry_run", property(lambda s: True))
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job_at(srv, url)["id"])
        await until(lambda: r.status not in ("queued", "running"), about=state(r))
        return r

    r = over(applier, body)
    assert not any("as you did here" in line for line in r.log), r.log
    assert r.learned_press is None and r.status != "submitted"
    assert learned_file(job_apply_home).read_text(encoding="utf-8") == before


# -------------------------------------------------------------------- 2: another employer on the same host
@needs_browser
def test_a_press_learned_for_one_employer_is_not_made_for_another_on_the_same_host(srv, job_apply_home, quick,
                                                                                   tmp_path):
    """Two employers on one job system's host, their pages of the same shape (jobs.smartrecruiters.com's, say): what
    was learned at Acme's is never pressed at Bolt's."""
    url = site(tmp_path / "shared")
    seed(srv, url, company="Acme")
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job_at(srv, url, company="Bolt Industries")["id"])
        await until(lambda: r.status not in ("queued", "running"), about=state(r))
        return r

    _not_used(over(applier, body))
    assert [e["worked"] for e in learned.all()] == [0]


def test_where_a_press_is_kept_names_the_employer_and_nothing_with_none(job_apply_home):
    url = "https://jobs.smartrecruiters.com/AcmeCorp/123-technician"
    assert learned.place_key(url, "Acme, Inc.") == learned.place_of(url) + "|acmeinc"
    assert learned.place_key(url, "Bolt") != learned.place_key(url, "Acme")
    assert learned.place_key(url, "") == "" and learned.place_key(url, " ,. ") == ""
    assert learned.place_key("", "Acme") == ""
    assert learned.shown_place("acme.example|acme") == "acme.example"


@pytest.mark.parametrize("where", ["another host", "another employer"])
def test_a_press_is_not_kept_where_the_page_after_it_is_another_place(srv, job_apply_home, monkeypatch, where):
    """The page the person's press led to is at another place, or the job is another employer's (the place check in
    _learn_from_hand: taken out, this fails)."""
    monkeypatch.setattr(Applier, "_looks_past", lambda *a: True)
    applier, r = _stopped(srv)
    _hand(r, "no_step_button")
    _going_on(r)
    moved = MOVED
    if where == "another host":
        moved = {**MOVED, "url": "https://other.example/careers/apply"}
    else:
        r.company = "Bolt"
    run(applier._learn_from_hand(r, moved, ""))
    assert learned.all() == []


# -------------------------------------------------------------------- 3: only a button's visible words are heard
LISTENED = """<!doctype html><html><body><h1>Your experience</h1>
<button type="button" id="icon" aria-label="Sam Rivera resume.pdf" title="sam.rivera@example.com"><svg width="9"
 height="9"></svg></button>
<a id="link" href="#" title="Continue as Sam Rivera"><img alt="" width="9" height="9"></a>
<input type="submit" id="sub" value="Next step" onclick="return false">
<button type="button" id="go">Go to next section</button></body></html>"""


async def _tab(srv, url):
    await srv.browser.goto(url)
    return srv.browser.current_tab


@needs_browser
def test_the_listener_hears_a_buttons_visible_words_and_never_an_aria_label_or_title(srv, tmp_path):
    page = tmp_path / "listened.html"
    page.write_text(LISTENED, encoding="utf-8")
    heard: list[str] = []

    async def go():
        tab = await _tab(srv, page.resolve().as_uri())
        await learned.listen(tab, None, lambda source, words: heard.append(words))
        for which in ("#icon", "#link", "#sub", "#go"):
            await tab.click(which)
        await asyncio.sleep(0.3)

    run(go())
    assert heard == ["Next step", "Go to next section"]


# -------------------------------------------------------------------- 4: the press that moved the step on
@needs_browser
def test_two_different_buttons_pressed_at_the_stop_teach_nothing(srv, job_apply_home, quick, tmp_path):
    url = site(tmp_path / "acme", extra="<button type='button' id='add'>Add another</button>")
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job_at(srv, url)["id"])
        await settle(r)
        await r.page.click("#add")
        await until(lambda: r.hand_press, about=state(r))
        await r.page.click("#go")
        await until(lambda: r.hand_press == WORDS, about=state(r))
        await carry_on(applier, r)
        return r

    r = over(applier, body)
    assert r.status == "ready", (r.status, r.reason)
    assert not learned_file(job_apply_home).exists() or learned.all() == []


# A first step the desk goes on from by itself (its Next), then a step whose only way on, "Go to next section", leads
# back to the first: a learned press that goes nowhere new
CIRCLE = """<!doctype html><html><head><title>Apply</title></head><body>
<div id="a"><h1>Your details</h1><label for="fn">First name</label><input id="fn" type="text">
<button type="button" id="next" onclick="document.getElementById('a').hidden=true;
  document.getElementById('b').hidden=false">Next</button></div>
<div id="b" hidden><h1>Your experience</h1><label for="y">Years of experience</label><input id="y" type="text">
<button type="button" id="go" onclick="document.getElementById('b').hidden=true;
  document.getElementById('a').hidden=false">Go to next section</button></div>
</body></html>"""


@needs_browser
def test_a_learned_press_that_leads_back_to_a_step_already_gone_through_counts_as_failed(srv, job_apply_home, quick,
                                                                                         tmp_path):
    folder = tmp_path / "acme"
    folder.mkdir()
    (folder / "apply.html").write_text(CIRCLE, encoding="utf-8")
    url = (folder / "apply.html").resolve().as_uri()
    key = learned.page_key(["Your experience"], ["Years of experience"])
    assert learned.remember(learned.place_key(url, "Acme"), "company_site", key, WORDS, DAY)
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job_at(srv, url)["id"])
        await until(lambda: r.status not in ("queued", "running"), about=state(r))
        return r

    r = over(applier, body)
    assert SAID in r.log, r.log
    assert (r.status, r.code) == ("needs_you", "no_step_button"), (r.status, r.code, r.reason, r.log)
    [entry] = learned.all()
    assert (entry["worked"], entry["failed"]) == (0, 1)


# -------------------------------------------------------------------- 5: never in another site's frame
@needs_browser
def test_the_listener_goes_into_the_pages_own_frames_and_never_another_sites(srv, tmp_path):
    """A frame of the page's own origin is heard; one of another origin (another port: another site) never is."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def server(pages):
        class Site(BaseHTTPRequestHandler):
            def do_GET(self):
                body = pages.get(self.path.split("?")[0], "").encode("utf-8")
                self.send_response(200 if body else 404)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=site.serve_forever, daemon=True).start()
        return site

    other = server({"/frame": "<button id='x'>Next over there</button>"})
    own_pages: dict[str, str] = {"/own": "<button id='y'>Next in here</button>"}
    own = server(own_pages)
    own_pages["/apply"] = (f"<h1>Apply</h1><iframe id='o' src='http://127.0.0.1:{other.server_address[1]}/frame'>"
                           "</iframe><iframe id='s' src='/own'></iframe><button id='z'>Next on the page</button>")
    heard: list[str] = []

    async def go():
        tab = await _tab(srv, f"http://127.0.0.1:{own.server_address[1]}/apply")
        await tab.wait_for_load_state("load")
        await learned.listen(tab, None, lambda source, words: heard.append(words))
        put = {f.url.rsplit("/", 1)[-1]: await f.evaluate("typeof window.__jdHandOff") for f in tab.frames}
        assert put == {"apply": "function", "own": "function", "frame": "undefined"}, put  # (never into another site's)
        await tab.frame_locator("#o").locator("#x").click()
        await tab.frame_locator("#s").locator("#y").click()
        await tab.click("#z")
        await asyncio.sleep(0.3)

    try:
        run(go())
    finally:
        other.shutdown()
        own.shutdown()
    assert heard == ["Next in here", "Next on the page"]


# -------------------------------------------------------------------- 6: a page with nothing to know it by
def test_a_page_with_no_headings_and_no_labels_has_no_key():
    assert pipeline._learn_page_key({"headings": [], "fields": []}) == ""
    assert pipeline._learn_page_key({"headings": [" "], "fields": [{"label": ""}]}) == ""
    assert pipeline._learn_page_key({"headings": [], "fields": [{"label": "Years"}]}) != ""


BARE = """<!doctype html><html><head></head><body>
<div id="one"><input id="y" type="text" value="">
<button type="button" id="go" onclick="document.getElementById('one').hidden=true;
  document.getElementById('two').hidden=false">Go to next section</button></div>
<div id="two" hidden><h1>References</h1><label for="r">Reference name</label><input id="r" type="text">
<button type="submit">Submit application</button></div>
</body></html>"""


@needs_browser
def test_a_press_on_a_page_with_no_headings_and_no_labels_is_not_kept(srv, job_apply_home, quick, tmp_path):
    folder = tmp_path / "acme"
    folder.mkdir()
    (folder / "apply.html").write_text(BARE, encoding="utf-8")
    url = (folder / "apply.html").resolve().as_uri()
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job_at(srv, url)["id"])
        await settle(r, listening=False)
        assert (r.status, r.code) == ("needs_you", "no_step_button"), state(r)()
        assert r.hand_stop is None and not r.hand_listening  # (nothing to know the page by: not listened to)
        await r.page.click("#go")
        await asyncio.sleep(0.5)
        await carry_on(applier, r)
        return r

    over(applier, body)
    assert not learned_file(job_apply_home).exists() or learned.all() == []


# -------------------------------------------------------------------- 7: what's left in the page
@needs_browser
def test_the_listener_is_taken_out_of_the_page_when_the_stop_ends(srv, tmp_path):
    page = tmp_path / "listened.html"
    page.write_text(LISTENED, encoding="utf-8")
    heard: list[str] = []

    async def go():
        tab = await _tab(srv, page.resolve().as_uri())
        await learned.listen(tab, None, lambda source, words: heard.append(words))
        await learned.listen(tab, tab, lambda source, words: heard.append(words))  # (a second stop: one listener)
        await tab.click("#go")
        await asyncio.sleep(0.3)
        await learned.mute(tab)
        left = await tab.evaluate("[typeof window.__jdHandOff, typeof window.__jdHandOn, typeof window.__jdHandHears,"
                                  " typeof window.__jdHand]")
        await tab.click("#go")
        await asyncio.sleep(0.3)
        return left

    left = run(go())
    assert left == ["undefined", "undefined", "undefined", "function"]  # (the binding stays: Playwright keeps it)
    assert heard == [WORDS]


@needs_browser
def test_with_the_setting_turned_off_at_a_stop_a_tab_already_listening_reports_nothing(srv, job_apply_home, quick,
                                                                                       tmp_path):
    url = site(tmp_path / "acme")
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job_at(srv, url)["id"])
        await settle(r)
        applier.set_learn_from_me(False)
        await r.page.click("#go")
        await asyncio.sleep(0.5)
        return r

    assert over(applier, body).hand_press == ""


# -------------------------------------------------------------------- 8: a click of the desk's or Claude's tools
@needs_browser
def test_a_click_of_the_tools_in_the_paused_tab_teaches_nothing(srv, job_apply_home, quick, tmp_path):
    url = site(tmp_path / "acme", extra="<button type='button' id='more'>Continue reading</button>")
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job_at(srv, url)["id"])
        await settle(r)
        assert srv.browser.use_tab(r.page)
        got = await srv.click("Continue reading")  # (Claude's click tool, at the stop)
        assert got.get("clicked") is True, got
        await asyncio.sleep(0.5)
        assert r.hand_press == "" and r.hand_heard == set()
        await r.page.click("#more")  # (the person's own press there is heard)
        await until(lambda: r.hand_press, about=state(r))
        return r

    assert over(applier, body).hand_press == "Continue reading"


# ============================================================== the second review: positive evidence of a further step
# -------------------------------------------------------------------- 1: no further step shown, nothing kept or counted
@pytest.mark.parametrize("after", ["thanks in other words", "only a search box", "a sign-in form", "a step's button"])
def test_a_press_is_kept_only_where_the_page_after_it_shows_a_further_step(srv, job_apply_home, monkeypatch, after):
    """"Not a confirmation" is no proof a press sent nothing ("Application sent", a thanks in another language): a press
    is kept only where the page after it shows a box of the application's to fill, or a step's button."""
    monkeypatch.setattr(Applier, "_looks_past", lambda *a: True)
    applier, r = _stopped(srv)
    _hand(r, "no_step_button")
    _going_on(r)
    page = {"url": MOVED["url"], "headings": ["Application sent"], "fields": [], "actions": []}
    if after == "only a search box":
        page = {**page, "headings": ["Open positions"], "fields": [{"id": "q", "label": "Search jobs", "kind": "text"}]}
    elif after == "a sign-in form":
        page = {**page, "headings": ["Welcome back"], "fields": [{"id": "e", "label": "Email", "kind": "text"},
                                                                 {"id": "p", "label": "Password", "kind": "password"}]}
    elif after == "a step's button":
        page = {**page, "headings": ["Voluntary disclosures"], "actions": [{"id": "n", "text": "Next"}],
                "fields": [{"id": "v", "label": "Veteran status", "kind": "select"}]}
    run(applier._learn_from_hand(r, page, "Application sent"))
    assert (learned.lookup(PLACE, "0123456789abcdef") is not None) == (after == "a step's button")


def test_what_counts_as_a_further_step():
    box = {"id": "r", "label": "Reference name", "kind": "text", "required": True}
    nxt = {"text": "Next"}
    assert pipeline._further_step({"fields": [box], "actions": [nxt]}, "")
    assert pipeline._further_step({"fields": [{**box, "required": False}], "actions": [{"text": "Continue"}]}, "")
    assert pipeline._further_step({"actions": [{"text": "Save and Continue"}]}, "")
    assert pipeline._further_step({"headings": ["Current step 3 of 6"]}, "")
    assert not pipeline._further_step({"headings": ["Current step 6 of 6"]}, "")
    # a box with only a Submit under it (the form's last page, or a survey after it went in), or with no way on at all
    assert not pipeline._further_step({"fields": [box], "actions": [{"text": "Submit", "is_submit": True}]}, "")
    assert not pipeline._further_step({"fields": [box]}, "")
    assert not pipeline._further_step({"actions": [nxt]}, "")  # (a way on with nothing to fill beside it)
    for flag in ("site_nav", "disabled", "is_submit", "same_page", "aside"):
        assert not pipeline._further_step({"fields": [box], "actions": [{**nxt, flag: True}]}, ""), flag
    assert not pipeline._further_step({"fields": [box], "actions": [{"text": "Back to jobs"}]}, "")
    sign_in = {"text": "Sign in", "sign_in_form": True}
    assert not pipeline._further_step({"fields": [box], "actions": [nxt, sign_in]}, "")
    assert not pipeline._further_step({"fields": [box, {"id": "p", "label": "Password", "kind": "password"}],
                                       "actions": [nxt]}, "")
    assert not pipeline._further_step({"fields": [{"id": "k", "label": "Keywords", "kind": "text"}],
                                       "actions": [nxt]}, "")


@pytest.mark.parametrize("page, text", [
    ({"headings": ["Want to hear about roles like this?"], "fields": [{"id": "e", "label": "Email", "kind": "email"}],
      "actions": [{"text": "Continue"}]}, ""),
    ({"fields": [{"id": "k", "label": "Keep me posted about new roles", "kind": "checkbox"}],
      "actions": [{"text": "Next"}]}, ""),
    ({"headings": ["Join our mailing list"], "fields": [{"id": "e", "label": "Email", "kind": "email"}],
      "actions": [{"text": "Continue"}]}, ""),
    ({"fields": [{"id": "c", "label": "Comments", "kind": "textarea"}], "actions": [{"text": "Continue"}]},
     "Thank you for applying. Please complete the following survey."),
    ({"headings": ["Step 1 of 3"]}, "Thank you for your application. Please continue to check your email."),
], ids=["roles like this", "keep me posted", "mailing list", "a thank-you and a survey", "a thank-you and steps"])
def test_a_sign_up_in_other_words_or_a_thank_you_that_asks_for_more_is_no_further_step(page, text):
    assert not pipeline._further_step(page, text)
    how = {"fields": [{"id": "h", "label": "How did you hear about us?", "kind": "select"}],
           "actions": [{"text": "Next"}]}
    assert pipeline._further_step(how, "")  # (an application's own question is still one)


ALERT_BOX = {"id": "e", "label": "Email", "kind": "email", "required": True}
SURVEY = [{"id": "q1", "label": "How was applying today?", "kind": "radio", "required": True}]


@pytest.mark.parametrize("page", [
    {"headings": ["You're all set", "Get job alerts"], "fields": [ALERT_BOX], "actions": [{"text": "Subscribe"}]},
    {"headings": ["You're all set"], "fields": [{**ALERT_BOX, "aside": True}],
     "actions": [{"text": "Subscribe", "aside": True}, {"text": "Continue"}]},
    {"headings": ["You're all set", "Get job alerts"], "fields": [ALERT_BOX],
     "actions": [{"text": "Subscribe"}, {"text": "Continue"}]},
    {"headings": ["You're all set"], "fields": [], "actions": [{"text": "Continue"}]},
    {"headings": ["Next steps"], "fields": [], "actions": [{"text": "Continue"}]},
    {"headings": ["You're all set"], "fields": [{"id": "g", "label": "Name", "kind": "text", "disabled": True}],
     "actions": [{"text": "Continue"}]},
    {"headings": ["You're all set", "One quick question"], "fields": SURVEY,
     "actions": [{"text": "Submit", "is_submit": True}]},
], ids=["a job-alert box", "a job-alert box aside", "a job-alert box and Continue", "a bare Continue",
        "next steps and Continue", "a greyed box", "a survey"])
def test_a_page_that_may_follow_a_send_is_no_further_step(page):
    """The third review: a thank-you in words the desk doesn't know, with a job-alert sign-up, a bare Continue or a
    greyed-out box beside it, is not a step of the application (the desk's own _mid_application decides)."""
    assert not pipeline._further_step(page, "You're all set. We'll be in touch.")


# A step whose way on, "Continue to next step", sends the application: the page after says so in words confirmations()
# doesn't know
SENT_UNSAID = SENDS.replace("<h1>Thank you for applying</h1><p>Your application has been received.</p>",
                            "<h1>Application sent</h1>")


@needs_browser
def test_a_learned_press_after_which_the_page_shows_no_further_step_stops_for_the_person_to_check(
        srv, job_apply_home, quick, tmp_path):
    assert SENT_UNSAID != SENDS
    folder = tmp_path / "acme"
    folder.mkdir()
    (folder / "apply.html").write_text(SENT_UNSAID, encoding="utf-8")
    url = (folder / "apply.html").resolve().as_uri()
    seed(srv, url, words="Continue to next step")
    job = job_at(srv, url)
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job["id"])
        await until(lambda: r.status not in ("queued", "running"), about=state(r))
        return r

    r = over(applier, body)
    assert (r.status, r.need, r.code) == ("needs_you", "check_submit", "check_submit"), (r.code, r.reason, r.log)
    assert "shows no further step of this application" in r.reason and "I submitted it myself" in r.reason
    assert r.pressed_before and learned.all() == []
    assert srv.tracker().get(job["id"])["status"] != "applied"


SENT_WITH_ALERTS = SENDS.replace(
    "<h1>Thank you for applying</h1><p>Your application has been received.</p>",
    "<h1>You're all set</h1><p>We'll be in touch.</p><h2>Get job alerts</h2><label for='e'>Email</label>"
    "<input id='e' type='email' required><button type='button'>Subscribe</button>"
    "<button type='button'>Continue</button>")


@needs_browser
def test_a_learned_press_that_leads_to_a_thanks_with_a_job_alert_box_stops_for_the_person_to_check(
        srv, job_apply_home, quick, tmp_path):
    assert SENT_WITH_ALERTS != SENDS
    folder = tmp_path / "acme"
    folder.mkdir()
    (folder / "apply.html").write_text(SENT_WITH_ALERTS, encoding="utf-8")
    url = (folder / "apply.html").resolve().as_uri()
    seed(srv, url, words="Continue to next step")
    job = job_at(srv, url)
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job["id"])
        await until(lambda: r.status not in ("queued", "running"), about=state(r))
        return r

    r = over(applier, body)
    assert (r.status, r.need, r.code) == ("needs_you", "check_submit", "check_submit"), (r.code, r.reason, r.log)
    assert r.pressed_before and learned.all() == []
    assert not any("Subscribe" in line or "job alert" in line.lower() and "filled" in line for line in r.log), r.log


# A step whose way on leads to a real further step (a box to fill and Next)
GOES_ON = SENDS.replace(
    "<h1>Thank you for applying</h1><p>Your application has been received.</p>",
    "<h1>References</h1><label for='r'>Reference name</label><input id='r' type='text' required>"
    "<button type='button'>Next</button>")


@needs_browser
@pytest.mark.parametrize("own", [True, False], ids=["this job's page", "another job's page"])
def test_a_learned_press_counts_only_where_the_page_after_it_is_still_this_jobs(srv, job_apply_home, quick, tmp_path,
                                                                                monkeypatch, own):
    folder = tmp_path / "acme"
    folder.mkdir()
    (folder / "apply.html").write_text(GOES_ON, encoding="utf-8")
    url = (folder / "apply.html").resolve().as_uri()
    seed(srv, url, words="Continue to next step")
    job = job_at(srv, url)
    if not own:
        real = Applier._own_place
        # (after the learned press, the page is taken as another job's)
        monkeypatch.setattr(Applier, "_own_place",
                            lambda self, run, u, o=None: False if run.learned_press else real(self, run, u, o))
    applier = Applier(srv)

    async def body(applier):
        r = applier.enqueue(job["id"])
        await until(lambda: r.status not in ("queued", "running"), about=state(r))
        return r

    r = over(applier, body)
    if own:
        assert r.code != "check_submit" and not r.pressed_before, (r.code, r.reason, r.log)
        assert [e["worked"] for e in learned.all()] == [1]
    else:
        assert (r.status, r.code) == ("needs_you", "check_submit"), (r.code, r.reason, r.log)
        assert r.pressed_before and learned.all() == []


# -------------------------------------------------------------------- 3: characters that show nothing
def test_the_words_drop_characters_that_show_nothing():
    assert learned.words_of("Sub​mit") == "Submit" and learned.words_of("Sub­mit") == "Submit"
    assert learned.words_of("​Go to next⁠ section﻿") == WORDS


# -------------------------------------------------------------------- 4: a script's click, another site's frame
@needs_browser
def test_a_click_made_by_the_pages_script_is_not_heard(srv, tmp_path):
    page = tmp_path / "listened.html"
    page.write_text(LISTENED, encoding="utf-8")
    heard: list[str] = []

    async def go():
        tab = await _tab(srv, page.resolve().as_uri())
        await learned.listen(tab, None, lambda source, words: heard.append(words))
        await tab.evaluate("document.getElementById('go').click()")  # (isTrusted false)
        await asyncio.sleep(0.3)
        await tab.click("#go")
        await asyncio.sleep(0.3)

    run(go())
    assert heard == [WORDS]


@needs_browser
def test_a_report_from_another_sites_frame_is_dropped(srv, tmp_path):
    """The binding is reachable from every frame of the tab: another site's frame calling it is never heard."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def server(pages):
        class Site(BaseHTTPRequestHandler):
            def do_GET(self):
                body = pages.get(self.path.split("?")[0], "").encode("utf-8")
                self.send_response(200 if body else 404)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=site.serve_forever, daemon=True).start()
        return site

    other = server({"/frame": "<p>an embed</p>"})
    own_pages: dict[str, str] = {"/own": "<p>ours</p>"}
    own = server(own_pages)
    own_pages["/apply"] = (f"<h1>Apply</h1><iframe id='o' src='http://127.0.0.1:{other.server_address[1]}/frame'>"
                           "</iframe><iframe id='s' src='/own'></iframe>")
    heard: list[str] = []

    async def go():
        tab = await _tab(srv, f"http://127.0.0.1:{own.server_address[1]}/apply")
        await tab.wait_for_load_state("load")
        await learned.listen(tab, None, lambda source, words: heard.append(words))
        for frame in tab.frames:
            if frame is not tab.main_frame:
                said = "From " + ("ours" if frame.url.endswith("/own") else "there")
                await frame.evaluate("(w) => window.__jdHand && window.__jdHand(w)", said)
        await tab.evaluate("window.__jdHand('From the page')")
        await asyncio.sleep(0.3)

    try:
        run(go())
    finally:
        other.shutdown()
        own.shutdown()
    assert sorted(heard) == ["From ours", "From the page"]


# -------------------------------------------------------------------- 5: the person's other names, employers, schools
def test_may_learn_refuses_the_persons_other_names_employers_and_schools(job_apply_home):
    import yaml

    path = job_apply_home / "profile.yaml"
    prof = yaml.safe_load(path.read_text(encoding="utf-8"))
    prof["personal"].update({"middle_name": "Jo", "preferred_name": "Sammy"})
    prof["history"]["previous_employers"] = ["Intel", "Microchip Technology"]
    path.write_text(yaml.safe_dump(prof), encoding="utf-8")
    for words in ("Next for Jo", "Next, Sammy", "Continue to Intel", "Continue to Microchip", "Next: Arizona",
                  "Continue to University", "Next for Fab"):
        assert not _may_learn(words, [_b(words)]), words
    for words in ("Next for Al", "Continue to Tech"):  # (two letters and not a name; a word that's part of one)
        assert _may_learn(words, [_b(words)]), words


# -------------------------------------------------------------------- 6: the checks of _learn_from_hand, each alone
@pytest.mark.parametrize("which", ["practice mode turned on", "the same page", "not past the stop"])
def test_each_check_of_learn_from_hand_alone_keeps_a_press_out(srv, job_apply_home, monkeypatch, which):
    """The press of test_only_a_no_step_button_stop_teaches, with one thing otherwise: practice mode turned on during
    the stop (_learning_on), the page after it of the same shape as the stop's (its key), or the tab not past the stop
    as _looks_past tells it (its look the same as the page it paused on)."""
    applier, r = _stopped(srv)
    _hand(r, "no_step_button")
    _going_on(r)
    if which == "practice mode turned on":
        monkeypatch.setattr(Applier, "_looks_past", lambda *a: True)
        monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    elif which == "the same page":
        monkeypatch.setattr(Applier, "_looks_past", lambda *a: True)
        r.hand_stop["page"] = pipeline._learn_page_key(MOVED)
    else:
        r.pause_sig = pipeline._page_sig(MOVED)
    run(applier._learn_from_hand(r, MOVED, ""))
    assert learned.all() == []


# -------------------------------------------------------------------- the list names the employer, not its site
def test_the_list_names_the_employer_where_the_press_was_kept_with_one(srv, job_apply_home):
    from test_desk_settings import _calls, _desk

    learned.remember("jobs.example|acme", "", "0123456789abcdef", WORDS, DAY, "Acme,\n  Inc.")
    learned.remember("jobs.example|other", "", "fedcba9876543210", WORDS, "2026-10-04")
    learned.worked("jobs.example|acme", "0123456789abcdef")  # (a count keeps the name)
    desk = _desk(srv)
    [(_, got)] = _calls(desk, [("GET", "/api/learned", None)])
    assert {e["employer"]: e["worked"] for e in got["learned"]} == {"Acme, Inc.": 1, "jobs.example": 0}


def test_an_employers_name_that_isnt_one_is_left_out_and_a_long_one_cut(job_apply_home):
    learned.remember("jobs.example|acme", "", "0123456789abcdef", WORDS, DAY, "A" * 200)
    [kept] = learned.all()
    assert kept["employer"] == "A" * learned.NAME_LONGEST
    path = config.home() / learned.LEARNED_FILE
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows[0]["employer"] = ["not", "a", "name"]
    path.write_text(json.dumps(rows), encoding="utf-8")
    assert "employer" not in learned.all()[0]
