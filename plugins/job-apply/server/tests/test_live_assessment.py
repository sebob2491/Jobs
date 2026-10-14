"""The live check's test identity takes an employer's assessment with random answers (scripts/live_smoke.py's
take_assessment; the owner's call, Oct 13). Run in a process of its own: live_smoke sets the environment it runs in."""

import functools
import http.server
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from conftest import browser_available

FIXTURES = Path(__file__).parent / "fixtures"
SERVER = Path(__file__).parent.parent

RUN = """
import asyncio, json, sys, types
sys.path.insert(0, "scripts")
import live_smoke
from job_apply import server

async def main(url, *vendors):
    live_smoke.ASSESSMENT_VENDORS.update(vendors)  # (a site taken for an assessment vendor's, for this test)
    await server.browser.goto(url.replace("EMAIL", live_smoke.FAKE_PROFILE["personal"]["email"]))
    run = types.SimpleNamespace(page=await server.browser.page())
    rec = {"company": "Example Store"}
    await live_smoke.take_assessment(run, rec)
    titles = [await p.title() for p in server.browser._ctx.pages if not p.is_closed()]
    print("RESULT " + json.dumps({**rec["assessment"], "titles": titles}))
    await server.close_browser()

asyncio.run(main(*sys.argv[1:]))
"""


def _take(path_query: str, vendors: tuple[str, ...] = ()) -> dict:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    httpd = http.server.ThreadingHTTPServer(("", 0), handler)  # (127.0.0.1, 127.0.0.2 and localhost: three sites)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        url = f"http://127.0.0.1:{port}/site/assessment-start.html" + path_query.replace("PORT", str(port))
        out = subprocess.run([sys.executable, "-c", RUN, url, *vendors], cwd=SERVER, capture_output=True, text=True,
                             encoding="utf-8", timeout=240)
    finally:
        httpd.shutdown()
    line = next((x for x in out.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, out.stdout[-2000:] + out.stderr[-3000:]
    return json.loads(line[len("RESULT "):])


@pytest.mark.skipif(not browser_available(), reason="needs Chromium")
def test_an_assessment_is_taken_with_random_answers_and_the_application_page_comes_back():
    """Circle K's and Taylor Morrison's Workday (run 56): "Take Assessment" opens the vendor's assessment in a new
    tab. Each page's questions are answered at random and its way on pressed (its Submit too, on the vendor's site),
    until the assessment brings the tab back to the application, its Take Assessment gone."""
    # the application at 127.0.0.1, the vendor at localhost: two sites
    # (and one that opens a moment after the press: Circle K's and Taylor Morrison's, run 57)
    for late in ("", "&late=3000"):
        result = _take("?vendor=http://localhost:PORT/site/assessment-vendor.html" + late)
        assert result["end"] == "back on the application", (late, result)
    pressed = [s.get("pressed") for s in result["steps"]]
    assert pressed[:4] == ["Take Assessment", "Start", "Next", "Submit"], result
    assert result["steps"][2]["answered"] == 3 and result["steps"][3]["answered"] == 2, result
    assert "done=1" in result["back_at"], result  # (the application's page, its assessment done)
    # Circle K's question page (run 60): a row of boxes to tick one of, its Next drawn only once one is ticked
    for shape in ("", "=apart"):  # (a group of boxes, or each its own as Circle K's read)
        boxes = _take("?vendor=" + f"http://localhost:PORT/site/assessment-vendor.html?boxes{shape}".replace("?", "%3F")
                      .replace("=", "%3D"))
        assert boxes["end"] == "back on the application", (shape, boxes)
        assert [s.get("pressed") for s in boxes["steps"]][:4] == ["Take Assessment", "Start", "Next", "Submit"], boxes
        assert boxes["steps"][2]["answered"] == 1, (shape, boxes)


@pytest.mark.skipif(not browser_available(), reason="needs Chromium")
def test_an_application_page_the_assessment_hands_back_to_is_never_filled_or_pressed():
    """The review of the live check's assessment taker: an assessment that hands back to another of the application's
    addresses (a page with the applicant's details in its boxes and a Submit) must never be taken for the vendor's:
    nothing filled or pressed there, so the application is never sent."""
    result = _take("?vendor=http://localhost:PORT/site/assessment-vendor.html"
                   "&return=" + "http://127.0.0.2:PORT/site/applicant-review.html?email=EMAIL".replace("&", "%26"))
    assert result["end"] == "back on the application", result
    assert "127.0.0.2" in result["back_at"] and all(s.get("host") != "127.0.0.2" for s in result["steps"]), result
    assert "SENT" not in result["titles"], result


@pytest.mark.skipif(not browser_available(), reason="needs Chromium")
def test_an_assessment_vendors_page_with_the_applicants_details_is_taken_and_no_other_sites():
    """Taylor Morrison's (run 61): The Predictive Index's assessment opens on "Welcome!" with the applicant's name and
    email filled in from the employer, and a Continue, and the taker took it for the application's page. On a known
    assessment vendor's site it's the assessment's: taken. The same page on any other site (an employer's own that
    greets the applicant by name, the review of 0.3.149) is never filled or pressed."""
    vendor = "http://localhost:PORT/site/assessment-vendor.html?welcome&email=EMAIL"
    query = "?vendor=" + vendor.replace("?", "%3F").replace("=", "%3D").replace("&", "%26")
    result = _take(query, vendors=("localhost",))
    assert result["end"] == "back on the application", result  # (its lone "already taken" box never ticked: run 62)
    assert [s.get("pressed") for s in result["steps"]][:4] == ["Take Assessment", "Continue", "Next", "Submit"], result
    other = _take(query)
    assert other["end"] == "it opened no assessment off the application's site", other
    assert other["steps"][-1]["applicants_page"] and len(other["steps"]) == 2, other


@pytest.mark.skipif(not browser_available(), reason="needs Chromium")
def test_an_assessment_page_with_no_way_on_keeps_its_boxes_as_read():
    """Circle K's (run 61): its question page read boxes, none taken for a question, and no way on. What the page's
    boxes were, and the markup around the first, are kept for the next look."""
    vendor = "http://localhost:PORT/site/assessment-vendor.html?boxes=apart&stuck"
    result = _take("?vendor=" + vendor.replace("?", "%3F").replace("=", "%3D").replace("&", "%26"))
    assert result["end"] == "no way on it knows", result
    last = result["steps"][-1]
    assert last["questions"] == [] and last["kinds"] == ["checkbox"], result  # (run 61's record: all it had)
    assert [(f["kind"], f["value"]) for f in last["fields"]] == [("checkbox", "True")] * 4, result
    assert "Always" in last["markup"] and "Never" in last["markup"], result
    grouped = _take("?vendor=" + "http://localhost:PORT/site/assessment-vendor.html?boxes&stuck".replace("?", "%3F")
                    .replace("=", "%3D").replace("&", "%26"))  # (a group of boxes: its markup by its members)
    assert grouped["steps"][-1]["fields"][0]["kind"] == "checkbox_group" and "Always" in grouped["steps"][-1]["markup"], \
        grouped


ACCOUNT = """
import asyncio, json, sys, types
sys.path.insert(0, "scripts")
import live_smoke
from job_apply import server

async def main(url, pressed):
    await server.browser.goto(url)
    page = await server.browser.page()
    for box, value in (("fbclc_userName", "a@example.com"), ("fbclc_emailConf", "a@example.com"),
                       ("fbclc_pwd", "Pw-12345678"), ("fbclc_pwdConf", "Pw-12345678"), ("fbclc_fName", "Jobdesk"),
                       ("fbclc_lName", "Test")):
        await page.fill("#" + box, value)
    await page.select_option("#fbclc_country", "United States")
    await page.click("#dataPrivacyId")
    await page.click("#accept")
    await page.check("#gigya-checkbox-terms")
    await page.click("#fbclc_createAccountButton")  # (the desk's press: refused, a choice left)
    log = [f"pressed \u201c{pressed}\u201d to create your account on SAP SuccessFactors with your saved password"]
    run = types.SimpleNamespace(page=page, log=log)
    did = await live_smoke.finish_account_choices(run)
    kept = await page.evaluate("() => Object.fromEntries(Object.keys(sessionStorage).filter(k => k.startsWith('sf.'))"
                               ".map(k => [k.slice(3), JSON.parse(sessionStorage.getItem(k))]))")
    print("RESULT " + json.dumps({"did": did, "kept": kept, "url": page.url}))
    await server.close_browser()

asyncio.run(main(*sys.argv[1:]))
"""


def _finish_account(pressed: str, more: str = "") -> dict:
    url = (FIXTURES / "site" / "sf-create-account.html").as_uri() + "?gigya&visibility" + more
    out = subprocess.run([sys.executable, "-c", ACCOUNT, url, pressed], cwd=SERVER, capture_output=True, text=True,
                         encoding="utf-8", timeout=240)
    line = next((x for x in out.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, out.stdout[-2000:] + out.stderr[-3000:]
    return json.loads(line[len("RESULT "):])


@pytest.mark.skipif(not browser_available(), reason="needs Chromium")
def test_the_test_identity_makes_a_create_account_choice_the_desk_leaves_to_the_person():
    """Edwards Vacuum's Create Account (run 61): the desk stopped on "Make my profile visible to:", a choice that's
    the person's. The live check's test identity makes it as a person would (a throwaway answer), and presses the
    button the desk pressed to create the account: the account is made. Never another button."""
    result = _finish_account("Submit")
    assert "Make my profile visible to" in result["did"] and result["did"].endswith("pressed “Submit”"), result
    assert result["kept"]["created"] == 1 and result["kept"]["visibility"] is not None, result
    other = _finish_account("Create Account")  # (not the button the page has: nothing done)
    assert other["did"] == "" and "created" not in other["kept"] and other["kept"]["visibility"] is None, other
    alerts = _finish_account("Submit", "&alerts")  # (a required choice of job alerts too: the person's, never made)
    assert alerts["did"] == "" and "created" not in alerts["kept"] and alerts["kept"]["visibility"] is None, alerts


@pytest.mark.skipif(not browser_available(), reason="needs Chromium")
def test_an_assessments_answer_tiles_are_clicked_and_a_word_list_that_wants_more_gets_more():
    """Circle K's (run 62): its statements' answers are tiles to click under "Answers" (nothing read as a box but its
    Auto Advance switch), and the taker found no way on. One is clicked at random, and the page goes on by itself.
    The Predictive Index's word list (run 62) stayed after its Continue: pressed once and stayed, more words are ticked
    (as many as its error asks for, run 63: "Please select 5 more"; else one), and it goes on."""
    def take(mode: str) -> dict:
        vendor = f"http://localhost:PORT/site/assessment-vendor.html?{mode}"
        return _take("?vendor=" + vendor.replace("?", "%3F").replace("=", "%3D").replace("&", "%26"))

    for mode in ("tiles", "tiles=fixed"):  # (fixed: its heading stays "1 Statement", the review of 0.3.150)
        tiles = take(mode)
        assert tiles["end"] == "back on the application", (mode, tiles)
        clicked = [s for s in tiles["steps"] if s.get("clicked")]
        assert len(clicked) == 2 and clicked[0]["tiles"] == ["Strongly disagree", "Disagree", "Neutral", "Agree",
                                                             "Strongly agree"], (mode, tiles)  # (no link, no Help)
        assert all(not s.get("questions") for s in clicked), tiles  # (its Auto Advance switch is no question to answer)
    marks = take("bestworst")  # (Circle K's "most likely / least likely" marks, run 64)
    assert marks["end"] == "back on the application", marks
    assert [s["clicked"].split(" of ")[1] for s in marks["steps"] if s.get("clicked")] == ["3", "3"], marks
    dead = take("tiles=dead")  # (tiles that take no click: three clicks that change nothing end it)
    assert dead["end"] == "the page didn't move on" and sum(1 for s in dead["steps"] if s.get("clicked")) == 3, dead
    words = take("words")
    assert words["end"] == "back on the application", words
    stayed = next((s for s in words["steps"] if s.get("ticked_more")), {})
    assert "Please select at least 2 words." in stayed.get("errors", []) and stayed.get("chosen"), words
    many = take("words=many")  # (as many more as the page asks: PI's "Please select 5 more", run 63)
    assert many["end"] == "back on the application", many
    assert "Please select 3 more words." in next((s for s in many["steps"] if s.get("ticked_more")), {}).get("errors", []), \
        many
