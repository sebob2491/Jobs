"""The desk says when a newer job-apply has been published, and how to get it."""

import httpx
import pytest
from conftest import browser_available, launch_options, run

from job_apply import config, updates
from job_apply.desk import Desk


def test_versions_are_compared_as_numbers():
    assert updates.newer("0.3.10", "0.3.9") and updates.newer("0.4.0", "0.3.70")
    assert not updates.newer("0.3.70", "0.3.70") and not updates.newer("0.3.69", "0.3.70")
    assert not updates.newer("", "0.3.70") and not updates.newer("0.3.71", "") and not updates.newer("v1", "0.3.70")


def test_the_published_manifest_is_the_repositorys_own():
    assert updates.manifest_url() == \
        "https://raw.githubusercontent.com/sebob2491/Jobs/main/plugins/job-apply/.claude-plugin/plugin.json"


def _github(monkeypatch, respond):
    """httpx.AsyncClient answering every request with respond(request); the requests seen."""
    seen = []
    real = httpx.AsyncClient

    def handler(request):
        seen.append(str(request.url))
        return respond(request)

    monkeypatch.setattr(updates.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("JOB_APPLY_NO_UPDATE_CHECK", "0")
    monkeypatch.setattr(config, "plugin_version", lambda: "0.3.70")
    return seen


def test_a_newer_published_version_is_said_with_how_to_get_it(monkeypatch, loop):
    seen = _github(monkeypatch, lambda r: httpx.Response(200, json={"name": "job-apply", "version": "0.3.71"}))
    check = updates.UpdateCheck()
    run(check.refresh())
    assert check.notice() == {"latest": "0.3.71", "current": "0.3.70", "commands": updates.UPDATE_COMMANDS}
    run(check.refresh())  # not looked at again within CHECK_EVERY
    assert len(seen) == 1


@pytest.mark.parametrize("respond", [lambda r: httpx.Response(200, json={"version": "0.3.70"}),
                                     lambda r: httpx.Response(200, json={"version": "0.3.69"}),
                                     lambda r: httpx.Response(503, text="unavailable"),
                                     lambda r: httpx.Response(200, text="<html>not a manifest</html>")])
def test_nothing_is_said_for_the_same_or_an_older_version_or_a_failed_look(monkeypatch, respond, loop):
    _github(monkeypatch, respond)
    check = updates.UpdateCheck()
    run(check.refresh())
    assert check.notice() is None


def test_no_look_at_all_when_turned_off(monkeypatch, loop):
    seen = _github(monkeypatch, lambda r: httpx.Response(200, json={"version": "0.3.71"}))
    monkeypatch.setenv("JOB_APPLY_NO_UPDATE_CHECK", "1")
    check = updates.UpdateCheck()
    run(check.refresh())
    assert seen == [] and check.notice() is None


@pytest.mark.skipif(not browser_available(), reason="no browser")
def test_the_desk_page_says_a_newer_version_is_out(srv, monkeypatch):
    from playwright.async_api import async_playwright

    monkeypatch.setattr(config, "plugin_version", lambda: "0.3.70")
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.updates.latest = "0.3.71"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#notices .notice")
                text = await page.inner_text("#notices")
                await browser.close()
                return text
        finally:
            await desk.stop()

    text = run(go())
    assert "job-apply 0.3.71 is out (you have 0.3.70)" in text, text
    assert "claude plugin marketplace update sebob-jobs" in text and "claude plugin update job-apply@sebob-jobs" in text
