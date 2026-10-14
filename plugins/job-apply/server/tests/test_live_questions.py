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
