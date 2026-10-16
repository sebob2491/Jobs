"""The live check looks at a pick-only list the desk read as showing no choices (scripts/live_smoke.py's
pick_list_probe: Edwards Vacuum's Protected Veteran, run 113). Run in a process of its own: live_smoke sets the
environment it runs in."""

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
    live_smoke.PICK_PROBE_AT = (0.5, 3)
    await server.browser.goto(url)
    data = await server.browser.inspect(include_dropdown_options=False)
    box = next(f for f in data["fields"] if "Veteran" in (f.get("label") or ""))
    run = types.SimpleNamespace(page=await server.browser.page(), questions=[
        {"id": box["id"], "label": box["label"], "error": "ValueError: its list showed no choices"},
        {"id": "x", "label": "Another", "error": "ValueError: nothing in its list matched 'x'"}])
    print("RESULT " + json.dumps(await live_smoke.pick_list_probe(run)))
    await server.close_browser()

asyncio.run(main(*sys.argv[1:]))
"""


@pytest.mark.skipif(not browser_available(), reason="needs Chromium")
def test_a_pick_list_that_showed_no_choices_is_looked_at_over_time():
    """Its list before a click, a moment and a few seconds after it, and after a second opening: here a list the page
    draws only once its choices came (&nolist, 1.5 s), absent at first and holding its choices later. Only the lists
    that showed no choices are looked at, and nothing is picked."""
    url = (FIXTURES / "site" / "sf-select-slow.html").as_uri() + "?slow=1500&nolist"
    out = subprocess.run([sys.executable, "-c", RUN, url], cwd=SERVER, capture_output=True, text=True,
                         encoding="utf-8", timeout=240)
    line = next((x for x in out.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, out.stdout[-2000:] + out.stderr[-3000:]
    probes = json.loads(line[len("RESULT "):])
    assert [p["label"] for p in probes] == ["Pre-Offer : Are you a Protected Veteran?"], probes
    seen = probes[0]
    assert "error" not in seen, seen
    assert seen["before"]["box"] is None and not seen["after_0.5s"]["menu"]["options"], seen
    assert seen["after_3s"]["roles"].get("option", 0) >= 4 and seen["reopened_3s"]["roles"].get("option", 0) >= 4, seen
    assert seen["after_3s"]["value"] == "", seen  # (nothing picked)
