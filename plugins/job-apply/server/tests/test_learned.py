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
# (the desk knows Next, Continue and the like). The next section is a step with a Submit, which is the review page
PAGE = """<!doctype html><html><head><title>Apply</title></head><body>
<h1 id="h">Your experience</h1>
<div id="one"><p>Tell us about it.</p>
<label for="y">Years of experience</label><input id="y" type="text" value="">
<button type="button" id="{go}" onclick="document.getElementById('one').hidden=true;
  document.getElementById('h').textContent='References';document.getElementById('two').hidden=false">{words}</button>
{extra}</div>
<div id="two" hidden><label for="r">Reference name</label><input id="r" type="text">
<button type="submit">Submit application</button></div>
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


def seed(srv, url, words=WORDS, day=DAY, **kept):
    """What the desk learned at this page on an earlier visit."""
    key = page_key_sync(srv, url)
    assert learned.remember(learned.place_of(url), "company_site", key, words, day)
    return key


def job_at(srv, url, n=0):
    return srv.add_job(url=url + (f"?n={n}" if n else ""), title="Technician", company="Acme")["job"]


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
    assert set(entry) == {"place", "system", "page", "button", "on", "worked", "failed"}
    assert (entry["button"], entry["system"]) == (WORDS, "company_site")
    assert (entry["worked"], entry["failed"]) == (0, 0)
    assert len(entry["page"]) == 16 and entry["place"].startswith("file:") and entry["on"] == time.strftime("%Y-%m-%d")
    for never in ("acme-careers", "apply.html", "Years of experience", "Your experience", "Sam", "4471", "rivera",
                  "example.com", "Tell us about it", "References"):
        assert never not in text, never
    assert r.hand_press == "" and r.hand_stop is None  # (the stop's record is gone, learned or not)


# -------------------------------------------------------------------- 2: no other stop ever teaches
CODES = ["bot_check", "captcha", "account_theirs", "sign_in_refused", "email_code", "security_questions",
         "notice_theirs", "agreement_theirs", "assessment", "your_submit", "cookie_banner"]
NEEDS = {"bot_check": "bot_check", "captcha": "captcha", "email_code": "email_code", "your_submit": "your_submit"}
MOVED = {"url": "https://acme.example/careers/apply", "headings": ["References"], "fields": [], "actions": []}


def _stopped(srv, code="no_step_button"):
    job = srv.add_job(url="https://acme.example/careers/apply", title="Technician", company="Acme")["job"]
    applier = Applier(srv)
    r = Run(job["id"], "Technician", "Acme", status="running", url="https://acme.example/careers/apply")
    applier.runs[job["id"]] = r
    applier._pause(r, NEEDS.get(code, "stuck"), "Over to you.", code=code)
    return applier, r


def _hand(r, code):
    r.hand_stop = {"code": code, "place": learned.place_of(MOVED["url"]), "system": "", "page": "0123456789abcdef",
                   "buttons": [{"text": WORDS}]}
    r.hand_press = WORDS


def test_every_stop_code_there_is_for_the_cases_below_is_a_real_one():
    assert set(CODES) | {"no_step_button"} <= pipeline.STOP_CODES


@pytest.mark.parametrize("code", ["no_step_button", *CODES])
def test_only_a_no_step_button_stop_teaches(srv, job_apply_home, monkeypatch, code):
    """The same press, the same page moved on: kept at a no_step_button stop, at none of the others."""
    monkeypatch.setattr(Applier, "_looks_past", lambda *a: True)
    applier, r = _stopped(srv, code)
    _hand(r, code)
    run(applier._learn_from_hand(r, MOVED, ""))
    kept = learned.lookup(learned.place_of(MOVED["url"]), "0123456789abcdef")
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
    shown = {"button": WORDS, "on": DAY}
    assert r.learned_press == shown and r.public()["learned_press"] == shown
    [entry] = learned.all()
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
        assert r.hand_press == "" and await r.page.evaluate("window.__jdHandOn") is False
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
        assert await r.page.evaluate("window.__jdHandOn") is True
        await r.page.click("#go")
        await asyncio.sleep(0.5)
        return r

    assert over(applier, body).hand_press == ""


def test_the_listener_script_reports_nothing_but_a_buttons_words():
    js = learned.LISTEN_JS
    assert re.findall(r"addEventListener\('(\w+)'", js) == ["click"] and "}, true);" in js  # (the capture phase)
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
