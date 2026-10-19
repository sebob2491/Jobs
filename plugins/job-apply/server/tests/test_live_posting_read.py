"""The live check reads a posting as the desk does (scripts/live_smoke.py's read_posting). Run in a process of its
own: live_smoke sets the environment it runs in."""

import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).parent.parent

RUN = """
import asyncio, sys
sys.path.insert(0, "scripts")
import live_smoke
from job_apply import server
from job_apply.postings import FetchError, Posting

async def over_http(url):
    if "thin" in url:
        return Posting(url=url, title="Loading", description="Loading...")
    raise FetchError(f"HTTP 405 from {url}")

async def in_a_frame(url):
    return Posting(url=url, title="Field Service Technician", description="Repair tools.")

async def in_a_tab(url):
    if "down" in url:
        raise RuntimeError("the page didn't open")
    return "<html><head><title>Line Technician</title></head><body><main>" + "Keep the line running. " * 20 + "</main></body></html>"

live_smoke.fetch_posting = over_http
server.read_icims_posting = in_a_frame
server.browser.background_html = in_a_tab

async def main():
    print("RESULT", (await live_smoke.read_posting("https://careers-example.icims.com/jobs/1/technician/job")).title)
    print("RESULT", (await live_smoke.read_posting("https://careers.example.com/jobs/thin")).title)
    try:
        await live_smoke.read_posting("https://careers.example.com/jobs/down")
    except FetchError as e:
        print("RESULT", e)

asyncio.run(main())
"""  # noqa: E501


def test_an_icims_posting_is_read_in_a_tab_as_the_desk_reads_it():
    """Run 116: every iCIMS posting the search check read (11 employers, Jibe sites' too) failed with HTTP 405, as
    iCIMS turns plain requests away, so the check said nothing of how they read: the desk reads them in a background
    tab (from the frame its posting is drawn in), as it reads a page that builds itself with script. One read neither
    way says both."""
    out = subprocess.run([sys.executable, "-c", RUN], cwd=SERVER, capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    lines = [x[len("RESULT "):] for x in out.stdout.splitlines() if x.startswith("RESULT ")]
    assert lines == ["Field Service Technician", "Line Technician", "HTTP 405 from https://careers.example.com/jobs/down;"
                     " in a tab: RuntimeError: the page didn't open"], \
        out.stdout[-2000:] + out.stderr[-3000:]
