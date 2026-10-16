"""The live check records the words just above each box the desk asked about (scripts/live_smoke.py's _words_above):
what a question with no words of its own asks. Run in a process of its own: live_smoke sets the environment it runs in."""

import json
import subprocess
import sys
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

async def main(url):
    await server.browser.goto(url)
    run = types.SimpleNamespace(page=await server.browser.page(),
                                questions=[{"id": "q1"}, {"id": "q2"}, {"id": "q3"}, {"id": "gone"}, {
                                    "id": "q4", "label": "I understand that checking this box is the legal "
                                    "equivalent of a signature*"}])
    print("RESULT " + json.dumps(await live_smoke._words_above(run)))
    await server.close_browser()

asyncio.run(main(sys.argv[1]))
"""


RUN_PROFILE = """
import json, sys
sys.path.insert(0, "scripts")
import live_smoke
live_smoke.use_role("hr")
print("RESULT " + json.dumps(live_smoke.profile_values()))
"""


def test_the_values_the_review_page_should_show_are_noted_for_the_audit():
    """Each employer's record notes what its review page should show of the applicant (live_audit.py's values
    check): the name, the role's jobs and schools, the degree levels, the other roles' employers; never the email,
    the phone or the address."""
    out = subprocess.run([sys.executable, "-c", RUN_PROFILE], cwd=SERVER, capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    line = next((x for x in out.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, out.stdout[-2000:] + out.stderr[-3000:]
    values = json.loads(line[len("RESULT "):])
    assert values == {"name": "Testy McTestface", "levels": ["bachelor", "high_school"],
                      "work": [{"title": "HR Coordinator", "company": "Example Staffing", "start": "2021-03",
                                "end": "present"}],
                      "education": [{"school": "Arizona State University", "degree": "Bachelor of Science",
                                     "start": "2016", "end": "2020"}],
                      "others": ["Example Bank", "Example Fab"]}, values
    assert "example.com" not in line and "480" not in line and "Arizona Ave" not in line


RUN_BAKE = """
import asyncio, json, sys
sys.path.insert(0, "scripts")
import live_smoke
from job_apply import server

async def main():
    page = await server.browser.page()
    await page.set_content('<form><input id="n" type="text"><input id="p" type="password">'
                           '<input id="s" type="text" data-ja-secret="in"><textarea id="t"></textarea>'
                           '<select id="d"><option>Choose...</option><option>Bachelor\\'s</option></select>'
                           '<input id="c" type="checkbox"><input id="f" type="file"></form>')
    await page.fill("#n", "Testy")
    await page.fill("#p", "not-a-real-password")
    await page.fill("#s", "jd0123456789ab")
    await page.fill("#t", "Maintained equipment.")
    await page.select_option("#d", "Bachelor's")
    await page.check("#c")
    baked = await live_smoke.bake_values(page)
    print("RESULT " + json.dumps({"baked": baked, "html": await page.content()}))
    await server.close_browser()

asyncio.run(main())
"""


@pytest.mark.skipif(not browser_available(), reason="needs Chromium")
def test_a_saved_page_carries_each_boxs_value_but_never_a_password_or_a_security_answer():
    """A React form's boxes (Oracle's, Eightfold's, UKG Pro's: run 107) hold their values as properties, which a saved
    page never carried, so the values audit couldn't read them: before the snapshot each box's value is written into
    the markup (a list's chosen entry and a tick too), never a password's or a security answer's."""
    out = subprocess.run([sys.executable, "-c", RUN_BAKE], cwd=SERVER, capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    line = next((x for x in out.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, out.stdout[-2000:] + out.stderr[-3000:]
    got = json.loads(line[len("RESULT "):])
    html = got["html"]
    assert got["baked"] == 2 and 'value="Testy"' in html and ">Maintained equipment.</textarea>" in html, html
    assert "not-a-real-password" not in html and "jd0123456789ab" not in html, html
    assert '<option selected="">Bachelor\'s</option>' in html and 'type="checkbox" checked=""' in html, html


@pytest.mark.skipif(not browser_available(), reason="needs Chromium")
def test_the_words_above_each_asked_about_box_are_recorded():
    """Taylor Morrison's Workday (run 71) asked about a box labelled only "Acknowledgment*": what it acknowledges
    is the employer's words above it, under its "Terms and Conditions". Never another box's words."""
    url = (FIXTURES / "site" / "workday-terms-acknowledgment.html").as_uri()
    out = subprocess.run([sys.executable, "-c", RUN, url], cwd=SERVER, capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    line = next((x for x in out.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, out.stdout[-2000:] + out.stderr[-3000:]
    gender, terms, middle, gone, signature = json.loads(line[len("RESULT "):])
    assert gender == "Voluntary Disclosures", gender  # (the heading above it: its own label isn't)
    assert terms == ("Terms and Conditions By checking the box below, I certify that the information in my "
                     "application is true and complete."), terms
    assert middle == "" and gone is None, (middle, gone)
    # (and never a box's own label just before it: Arizona State University's signature box, probe 76)
    assert signature == "The terms above apply to everything in this application.", signature
