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

async def main(url):
    await server.browser.goto(url)
    run = types.SimpleNamespace(page=await server.browser.page())
    rec = {"company": "Example Store"}
    await live_smoke.take_assessment(run, rec)
    print("RESULT " + json.dumps(rec["assessment"]))
    await server.close_browser()

asyncio.run(main(sys.argv[1]))
"""


@pytest.mark.skipif(not browser_available(), reason="needs Chromium")
def test_an_assessment_is_taken_with_random_answers_and_the_application_page_comes_back():
    """Circle K's and Taylor Morrison's Workday (run 56): "Take Assessment" opens the vendor's assessment in a new
    tab. Each page's questions are answered at random and its way on pressed (its Submit too, off the application's
    site), until the assessment brings the tab back to the application, its Take Assessment gone."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        # the application at 127.0.0.1, the vendor at localhost: two sites
        url = (f"http://127.0.0.1:{port}/site/assessment-start.html"
               f"?vendor=http://localhost:{port}/site/assessment-vendor.html")
        out = subprocess.run([sys.executable, "-c", RUN, url], cwd=SERVER, capture_output=True, text=True,
                             encoding="utf-8", timeout=240)
    finally:
        httpd.shutdown()
    line = next((x for x in out.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, out.stdout[-2000:] + out.stderr[-3000:]
    result = json.loads(line[len("RESULT "):])
    assert result["end"] == "back on the application", result
    pressed = [s.get("pressed") for s in result["steps"]]
    assert pressed[:4] == ["Take Assessment", "Start", "Next", "Submit"], result
    assert result["steps"][2]["answered"] == 3 and result["steps"][3]["answered"] == 2, result
    assert "done=1" in result["back_at"], result  # (the application's page, its assessment done)
