"""The desk in a window of its own (the design's A2 and A6): a link out of it opens in the person's usual browser
(/api/open, web addresses only), and a desk run on its own can be stopped from its page (/api/quit, which
job-apply-desk waits on). The page's own Quit button comes with a later Settings page: here a button like it is put
on the page for the test. Made-up employers only: the repo is public."""

import asyncio
import contextlib
import json
import socket
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.responses import PlainTextResponse
from conftest import browser_available, person_browser, run
from test_desk import _client, _fit, _jobs, _keep_queue, _kept_queue, _stopped_desk, _until
from test_desk_buttons import _POLLS, _idle_desk

from job_apply import desk as desk_module
from job_apply import window
from job_apply.desk import Desk, running_desk, watch_path

needs_browser = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

JOB = "https://careers.example.com/jobs/12345?source=desk#apply"
NOT_WEB = "Only web addresses can be opened."
NO_BROWSER = "No browser opened it."
QUIT_ASKS = "Quit the Job Desk? Jobs it is working on stop where they are. The queue is kept for next time."
INSIDE_CLAUDE = "The desk runs inside Claude here: close Claude to stop it."
STOPPED = ["The Job Desk has stopped", "You can close this window. Open Job Desk again to carry on."]
# A button as the Settings page's "This program" card will have it, put on the page for the test
ADD_QUIT = """() => document.body.insertAdjacentHTML("afterbegin",
    '<button data-act="quit">Quit the Job Desk</button>')"""
# The window's own close, and the person's No to the next question (once: the one after is the browser's own again),
# as stand-ins that note they were called
NOTE_CLOSE = "() => { window.close = () => { window.closedItself = (window.closedItself || 0) + 1; }; }"
SAY_NO = """() => { const ask = window.confirm;
    window.confirm = (asked) => { window.askedFirst = asked; window.confirm = ask; return false; }; }"""


@contextlib.asynccontextmanager
async def _page(desk: Desk, address: str, dialogs: list[str] | None = None):
    """The running desk's page at `address`, in a browser of the person's own (a fresh one each time: nothing kept
    from another page); with the calls it makes to the desk's API besides its refresh (method, path, the JSON sent),
    and the confirms and alerts it shows (each accepted) in `dialogs`."""
    calls: list[tuple[str, str, object]] = []

    def record(request) -> None:
        path = urlsplit(request.url).path
        if path.startswith("/api/") and path not in _POLLS:
            calls.append((request.method, path, json.loads(request.post_data) if request.post_data else None))

    async with person_browser() as browser:
        page = await browser.new_page()
        page.on("request", record)
        page.on("dialog", lambda d: (dialogs.append(d.message) if dialogs is not None else None,
                                     asyncio.ensure_future(d.accept())))
        await page.goto(address)
        await page.wait_for_selector("#plugin-version:not(:empty)")  # the page has its state
        yield page, calls
        await browser.close()


def test_api_open_hands_a_web_address_to_the_usual_browser_and_nothing_else(srv, monkeypatch):
    """/api/open opens an http or https address with a host in the person's usual browser, off the desk's own loop.
    Anything else is refused with 400 and opens nothing: a file, another program's kind of link, a path, text with a
    space or a character that doesn't show, one over 2,000 characters, and anything that isn't text. Without the
    page's key it's 403, as every route is."""
    opened: list[tuple] = []

    def usual_browser(*args, **options):
        opened.append((args, options, threading.get_ident()))
        return True

    monkeypatch.setattr(desk_module.webbrowser, "open", usual_browser)
    desk = _idle_desk(srv)
    longest = "https://careers.example.com/jobs?q=" + "q" * (2000 - len("https://careers.example.com/jobs?q="))
    web = [JOB, "http://example.com", "HTTPS://Careers.Example.com/Jobs/1", "https://example.com/jobs/caf\u00e9",
           "http://127.0.0.1:8080/jobs", "https://[2001:db8::1]/jobs", longest]
    not_web = [
        "file:///C:/x", "javascript:alert(1)", "ms-settings:", "", 5, "https://example.com/a b",  # (the design's)
        "https:///jobs/1", "https://", "http:example.com", "https://:443/jobs", "https://?q=1", "http://@/x",  # no host
        longest + "q",  # 2,001 characters
        " https://example.com/", "https://example.com/ ", "https://example.com/\tjobs", "https://example.com/\n",
        "https://exam\x00ple.com/", "https://example.com/\x7f", "https://example.com/\u00a0", "https://example.com/\u2028",
        "\u200bhttps://example.com/", "https://example.com/\u202ecod.exe",  # (neither shows: no telling what's opened)
        "ftp://example.com/x", "//example.com/x", "example.com/jobs", "/jobs/1", "about:blank", "chrome://settings",
        "data:text/html,hello", "mailto:someone@example.com", "shell:startup", "search-ms:query=x",
        "vbscript:msgbox(1)", "view-source:https://example.com/", "C:\\Windows\\System32\\calc.exe",
        "\\\\server\\share\\run.exe", "https:\\\\example.com\\x", "--app=https://example.com/", "-https://example.com/",
        "https://[::1",
        None, True, 1.5, ["https://example.com/"], {"href": "https://example.com/"},
    ]
    assert len(longest) == 2000

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            async with _client(desk) as c:
                for url in web:
                    r = await c.post("/api/open", headers=h, json={"url": url})
                    assert (r.status_code, r.json()) == (200, {"opened": True}), url
                done = list(opened)
                refused = [await c.post("/api/open", headers=h, json={"url": url}) for url in not_web]
                # nothing to open at all, or not the page's kind of request
                refused += [await c.post("/api/open", headers=h, json=body)
                            for body in ({}, {"address": JOB}, [JOB], JOB)]
                refused.append(await c.post("/api/open", headers=h, content=b"not json"))
                for said, r in zip([*not_web, "no url", "another name", "a list", "a string", "not JSON"], refused):
                    assert (r.status_code, r.json()) == (400, {"error": NOT_WEB}), said
                # and never without the page's key, or under another site's name
                for headers in ({}, {"x-desk-token": "a-guess"}, {**h, "host": "careers.example.com"}):
                    assert (await c.post("/api/open", headers=headers, json={"url": JOB})).status_code == 403
                assert (await c.get("/api/open", headers=h)).status_code == 405
                return done, threading.get_ident()
        finally:
            await desk.stop()

    done, loop_thread = run(go())
    assert [args for args, _, _ in done] == [(url,) for url in web]  # (each as it was sent, and nothing with it)
    assert all(options == {} and thread != loop_thread for _, options, thread in done), done
    assert opened == done  # (none of the refused ones opened anything)


def test_api_open_says_so_when_no_browser_took_the_link(srv, monkeypatch):
    """webbrowser.open says False when no browser took the address: /api/open answers 502 then, never that it opened
    it, so the page opens the link the ordinary way."""
    tried: list[str] = []
    monkeypatch.setattr(desk_module.webbrowser, "open", lambda url, *a, **k: tried.append(url) and False)
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                return await c.post("/api/open", headers={"x-desk-token": desk.token}, json={"url": JOB})
        finally:
            await desk.stop()

    r = run(go())
    assert (r.status_code, r.json()) == (502, {"error": NO_BROWSER}) and tried == [JOB]


def _code(address: str) -> str:
    """The one-time code in a window's address (Desk.window_url)."""
    said = parse_qs(urlsplit(address).query)
    assert list(said) == ["c"], address
    return said["c"][0]


def test_a_windows_code_opens_the_page_once_within_a_minute_and_otherwise_is_a_wrong_key(srv, monkeypatch):
    """The page's address with a window's one-time code goes on to the page's own address with its key (and &w=app
    where the window gave it), once. A code used already, one 60 seconds old, a made-up one, and the oldest of more
    than the handful kept get the page as a wrong key does: no redirect, no key. Under another site's name, 403, and
    the code isn't used up."""
    now = [1000.0]
    monkeypatch.setattr(desk_module, "_clock", lambda: now[0])
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                async def get(params: str):
                    r = await c.get("/" + params)
                    return r.status_code, r.headers.get("location"), r.text

                wrong_key = await get(f"?t=a-guess-{desk.token}")
                assert wrong_key[:2] == (200, None)
                said: dict[str, object] = {"wrong key": wrong_key}
                first = _code(desk.window_url())
                said["foreign"] = (await c.get(f"/?c={first}", headers={"host": "careers.example.com"})).status_code
                said["first"] = await get(f"?c={first}")
                said["again"] = await get(f"?c={first}")
                windowed = _code(desk.window_url())
                said["windowed"] = await get(f"?c={windowed}&w=app")
                said["made up"] = await get("?c=a-made-up-code&w=app")
                said["empty"] = await get("?c=")
                said["not ascii"] = await get("?c=caf%C3%A9")
                late, in_time = _code(desk.window_url()), _code(desk.window_url())
                now[0] += 59.9
                said["in time"] = await get(f"?c={in_time}")
                now[0] += 0.1
                said["late"] = await get(f"?c={late}")
                many = [_code(desk.window_url()) for _ in range(desk_module.WINDOW_CODES_MOST + 1)]
                said["oldest of many"] = await get(f"?c={many[0]}")
                said["newest of many"] = await get(f"?c={many[-1]}")
                return said, len(desk._window_codes)
        finally:
            await desk.stop()

    said, kept = run(go())
    wrong_key = said.pop("wrong key")
    assert said.pop("foreign") == 403
    page = (303, desk.url, "")
    assert said == {"first": page, "again": wrong_key, "windowed": (303, desk.url + "&w=app", ""),
                    "made up": wrong_key, "empty": wrong_key, "not ascii": wrong_key, "in time": page,
                    "late": wrong_key, "oldest of many": wrong_key, "newest of many": page}, said
    assert kept <= desk_module.WINDOW_CODES_MOST


def test_api_window_needs_the_pages_key_and_gives_a_new_code_each_time(srv):
    """/api/window, which a second start asks for the window's address: an address with a new one-time code each time,
    and no key in it. Without the page's key, or under another site's name, 403, as every route is."""
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            async with _client(desk) as c:
                refused = [(await c.post("/api/window", headers=headers, json={})).status_code
                           for headers in ({}, {"x-desk-token": "a-guess"}, {**h, "host": "careers.example.com"})]
                got = [await c.post("/api/window", headers=h, json={}) for _ in range(3)]
                not_get = (await c.get("/api/window", headers=h)).status_code
                return refused, [(r.status_code, r.json()) for r in got], not_get
        finally:
            await desk.stop()

    refused, got, not_get = run(go())
    assert refused == [403, 403, 403] and not_get == 405
    assert all(status == 200 and list(body) == ["url"] for status, body in got), got
    addresses = [body["url"] for _, body in got]
    assert all(a.startswith(f"http://127.0.0.1:{desk.port}/?c=") and desk.token not in a for a in addresses), addresses
    assert len({_code(a) for a in addresses}) == 3


def test_a_second_start_opens_with_a_code_or_the_keyed_address_where_the_running_desk_has_no_api_window(srv,
                                                                                                        monkeypatch):
    """A second start opens the running desk's page with the one-time code /api/window gives. A desk from before
    /api/window (Not Found there) has its page opened at the address with its key, as before, rather than not at
    all: in a tab, whatever the profile says, as a window's address goes on the browser's command line, where the key
    must never be (the second look of Nov 9)."""
    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(window, "open_window", lambda url, mode: opened.append((url, mode)) or mode)
    older = [False]
    window_view = Desk.window_view

    async def maybe_older(self, request):  # (an older desk: no such route)
        return PlainTextResponse("Not Found", status_code=404) if older[0] else await window_view(self, request)

    monkeypatch.setattr(Desk, "window_view", maybe_older)
    desk = _idle_desk(srv)

    def second_desk(server):
        raise AssertionError("a second desk was started")

    monkeypatch.setattr(desk_module, "get_desk", second_desk)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            assert await asyncio.to_thread(desk_module.main, ["--port", str(desk.port)]) == 0
            older[0] = True
            async with _client(desk) as c:
                assert (await c.post("/api/window", headers={"x-desk-token": desk.token}, json={})).status_code == 404
            assert await asyncio.to_thread(desk_module.main, ["--port", str(desk.port)]) == 0
        finally:
            await desk.stop()

    run(go())
    assert len(opened) == 2, opened
    (coded, mode), keyed = opened
    assert coded == f"http://127.0.0.1:{desk.port}/?c={_code(coded)}" and mode == "app", coded
    assert keyed == (desk.url, "tab")


@pytest.mark.parametrize("answer", [
    '{"url": "http://127.0.0.1:PORT/?c=abc&t=more"}', '{"url": "http://127.0.0.1:PORT/?c=abc#x"}',
    '{"url": "http://127.0.0.1:1/?c=abc"}', '{"url": "http://careers.example.com/?c=abc"}',
    '{"url": "https://127.0.0.1:PORT/?c=abc"}', '{"url": "http://127.0.0.1:PORT/?c="}',
    '{"url": "http://127.0.0.1:PORT/?c=abc def"}', '{"url": "file:///C:/x?c=abc"}', '{"url": 7}', '{"url": null}',
    '{}', '[]', 'not json', ''])
def test_a_second_start_takes_only_the_running_desks_own_coded_address_from_what_answers_on_its_port(
        answer, monkeypatch):
    """The second look of Nov 9: what answers /api/window on the desk's port is believed only when it is that desk's
    own page with a code and nothing more (the check could be deleted and every test passed). Anything else, another
    host or port, more after the code, another kind of address, no address: no coded address, so the keyed one is
    opened in a tab."""
    import http.server

    class Other(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # (http.server's own name for it)
            body = answer.replace("PORT", str(self.server.server_address[1])).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Other)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert desk_module.window_address(f"http://127.0.0.1:{port}/?t=a-key") is None
    finally:
        server.shutdown()
        server.server_close()


def test_a_second_start_believes_the_running_desks_own_coded_address():
    """...and the one address it does take: the same port's page with a code."""
    import http.server

    class Own(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # (http.server's own name for it)
            body = ('{"url": "http://127.0.0.1:%d/?c=Ab-_9"}' % self.server.server_address[1]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Own)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert desk_module.window_address(f"http://127.0.0.1:{port}/?t=a-key") == f"http://127.0.0.1:{port}/?c=Ab-_9"
    finally:
        server.shutdown()
        server.server_close()


def test_api_quit_is_for_a_desk_run_on_its_own_and_needs_the_pages_key(srv):
    """/api/quit: a desk inside Claude's plugin answers 409, saying how it stops, and isn't stopped. One run on its own
    (the state's `standalone`) says it's quitting and sets what job-apply-desk waits on. Without the page's key, 403."""
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            async with _client(desk) as c:
                assert (await c.get("/api/state", headers=h)).json()["standalone"] is False
                inside = await c.post("/api/quit", headers=h, json={})
                assert (inside.status_code, inside.json()) == (409, {"error": INSIDE_CLAUDE})
                assert not desk.quitting.is_set()
                desk.standalone = True  # (as job-apply-desk sets it)
                assert (await c.get("/api/state", headers=h)).json()["standalone"] is True
                for headers in ({}, {"x-desk-token": "a-guess"}, {**h, "host": "careers.example.com"}):
                    assert (await c.post("/api/quit", headers=headers, json={})).status_code == 403
                assert (await c.get("/api/quit", headers=h)).status_code == 405
                assert not desk.quitting.is_set()
                own = await c.post("/api/quit", headers=h, json={})
                assert (own.status_code, own.json()) == (200, {"quitting": True})
                assert desk.quitting.is_set()
        finally:
            await desk.stop()

    run(go())


@pytest.mark.parametrize("quiet", [True, False], ids=["--no-browser", "its window opened"])
def test_job_apply_desk_runs_until_its_page_quits_it_then_stops_as_ctrl_c_does(srv, monkeypatch, job_apply_home,
                                                                              quiet):
    """job-apply-desk serves the desk as one run on its own (the state says so) and waits. The page's quit ends the
    wait: the desk stops, the browser is closed after it, and the command ends with 0, as with Ctrl+C. The queue is
    kept, as at any restart. Its page opens through open_window, and not at all with --no-browser."""
    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(window, "open_window", lambda url, mode: opened.append((url, mode)) or mode)
    desk = _idle_desk(srv)
    monkeypatch.setattr(desk_module, "get_desk", lambda server: desk)  # (the desk main() serves: this one)
    first, second = _jobs(srv, 2)
    _keep_queue([first, second], paused=True)  # (a queue kept from before: back as the desk starts)
    closed: list[bool] = []
    close = srv.browser.close

    async def close_browser():
        closed.append(desk._server is None)  # (whether the desk had stopped by then)
        await close()

    monkeypatch.setattr(srv.browser, "close", close_browser)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    serving: dict = {}
    start = desk.start

    async def started(*a, **k):  # (main()'s own loop and task: to end it, should this test fail before its quit)
        serving.update(loop=asyncio.get_running_loop(), task=asyncio.current_task())
        return await start(*a, **k)

    desk.start = started
    ended: list[int] = []
    args = ["--port", str(port), *(["--no-browser"] if quiet else [])]
    command = threading.Thread(target=lambda: ended.append(desk_module.main(args)), daemon=True)

    async def go():
        command.start()
        try:
            await _until(lambda: (desk.port is not None and watch_path(desk.port).exists()) or not command.is_alive(),
                         about=lambda: (desk.port, ended))
            h = {"x-desk-token": desk.token}
            async with _client(desk) as c:
                state = (await c.get("/api/state", headers=h)).json()
                assert state["standalone"] is True
                assert state["queued"] == [first, second] and state["paused"]
                await asyncio.sleep(0.5)
                assert command.is_alive() and not ended  # (it waits: nothing ends it but the quit, or Ctrl+C)
                quit_ = await c.post("/api/quit", headers=h, json={})
                assert (quit_.status_code, quit_.json()) == (200, {"quitting": True})
            await asyncio.to_thread(command.join, 30)
            assert not command.is_alive()
            return desk.url
        finally:
            if command.is_alive() and serving:
                serving["loop"].call_soon_threadsafe(serving["task"].cancel)
                await asyncio.to_thread(command.join, 30)

    url = run(go())
    assert ended == [0]
    assert closed == [True]  # (the browser closed once, after the desk had stopped)
    assert running_desk(desk.port) is None and not watch_path(desk.port).exists()
    assert [job["id"] for job in _kept_queue()["jobs"]] == [first, second]
    # (with a one-time code in its address, never the page's key)
    assert [mode for _, mode in opened] == ([] if quiet else ["app"]), opened
    assert all(address == f"http://127.0.0.1:{desk.port}/?c={_code(address)}" and desk.token not in address
               for address, _ in opened), (opened, url)


@needs_browser
def test_quit_the_job_desk_asks_first_then_the_page_stops_refreshing_and_says_only_that(srv):
    """A button with data-act="quit" asks first, in the design's words. A no leaves everything as it was. After a yes
    the desk is told (/api/quit), and the page stops refreshing and shows only that the desk has stopped, then closes
    its window."""
    desk, _ = _stopped_desk(srv, {"Example Fab": dict(status="needs_you", need="stuck")})
    desk.standalone = True  # (as job-apply-desk sets it)
    said: list[str] = []

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _page(desk, desk.url, said) as (page, calls):
                await page.wait_for_function("document.title === '(1) Needs you · Job Desk'")
                await page.evaluate(ADD_QUIT)
                await page.evaluate(NOTE_CLOSE)  # (a page that closed itself would be gone before it's read)
                # a no (the person's Cancel on the question)
                await page.evaluate(SAY_NO)
                await page.click("body > [data-act=quit]")
                asked = await page.evaluate("window.askedFirst")
                await page.wait_for_timeout(300)
                after_no = (list(calls), await page.is_hidden("#stopped"), desk.quitting.is_set(),
                            await page.evaluate("stopped"))
                # a yes
                await page.click("body > [data-act=quit]")
                await page.wait_for_selector("#stopped", state="visible")
                later: list[str] = []
                page.on("request", lambda r: later.append(urlsplit(r.url).path))
                # (the page coming to the front, with a look at the openings due: neither is told to the desk now)
                await page.evaluate("() => { seenSent = 0; window.dispatchEvent(new Event('focus')); }")
                await page.wait_for_timeout(2500)  # (it refreshed every 1.5 s)
                shown = [line.strip() for line in (await page.inner_text("body")).splitlines() if line.strip()]
                return (asked, after_no, list(calls), shown, later, await page.title(),
                        await page.evaluate("window.closedItself"), desk.quitting.is_set())
        finally:
            await desk.stop()

    asked, after_no, calls, shown, later, title, closed, quitting = run(go())
    assert asked == QUIT_ASKS and after_no == ([], True, False, False), (asked, after_no)
    assert said == [QUIT_ASKS], said
    assert calls == [("POST", "/api/quit", {})] and quitting, calls
    assert shown == STOPPED, shown
    assert later == [], later
    assert title == "Job Desk" and closed == 1, (title, closed)


@needs_browser
def test_a_desk_inside_claude_isnt_stopped_from_its_page(srv):
    """A page that asks a desk inside Claude's plugin to quit is told why not, in the desk's words, and carries on."""
    desk = _idle_desk(srv)
    said: list[str] = []

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _page(desk, desk.url, said) as (page, calls):
                await page.evaluate(ADD_QUIT)
                await page.click("body > [data-act=quit]")
                await _until(lambda: len(said) == 2, about=lambda: said)
                async with page.expect_request(lambda r: urlsplit(r.url).path == "/api/state", timeout=10000):
                    pass  # (it still refreshes)
                return list(calls), await page.is_hidden("#stopped"), await page.evaluate("stopped")
        finally:
            await desk.stop()

    calls, hidden, stopped = run(go())
    assert said == [QUIT_ASKS, INSIDE_CLAUDE], said
    assert calls == [("POST", "/api/quit", {})] and hidden and not stopped, (calls, hidden, stopped)
    assert not desk.quitting.is_set()


@needs_browser
def test_a_link_opens_in_the_usual_browser_from_the_desks_own_window_and_as_before_in_a_tab(srv, monkeypatch):
    """With &w=app in the page's address (the desk's own window), a press on a job's title is sent to the desk
    (/api/open, the address as the browser reads it), which opens it in the person's usual browser: no new page
    opens from this one. A link the desk won't open (an address over its limit) opens the ordinary way. Without
    &w=app (a tab), the link opens a new page as before, and the desk isn't asked."""
    opened: list[str] = []
    monkeypatch.setattr(desk_module.webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    desk = _idle_desk(srv)
    spaced = "https://careers.example.com/jobs/1?team=field service"  # (as an employer's data may give it)
    read = "https://careers.example.com/jobs/1?team=field%20service"  # (and as the browser reads it)
    long = "https://careers.example.com/jobs/2?ref=" + "r" * 2100
    desk.listings = [{"company": "Example Fab", "title": "Field Service Engineer", "url": spaced,
                      "location": "Phoenix, AZ", "fit": _fit(80)},
                     {"company": "Example Litho", "title": "Equipment Technician", "url": long,
                      "location": "Chandler, AZ", "fit": _fit(70)}]
    fab, litho = ("#rows li.row:has-text('Example Fab') a.title", "#rows li.row:has-text('Example Litho') a.title")

    async def a_job(route):  # (the employer's site, made up: nothing leaves this computer)
        await route.fulfill(status=200, content_type="text/html", body="<title>A job</title>")

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _page(desk, desk.url + "&w=app#openings") as (page, calls):
                await page.context.route("https://careers.example.com/**", a_job)
                await page.click(fab)
                await _until(lambda: opened, about=lambda: calls)
                await page.wait_for_timeout(500)  # (time for a page to open, were one going to)
                own = (list(calls), list(opened), len(page.context.pages), page.url)
                async with page.context.expect_page() as new:
                    await page.click(litho)
                await (await new.value).wait_for_url(lambda url: url == long)
                refused = (calls[1:], list(opened), [other.url for other in page.context.pages[1:]])
            async with _page(desk, desk.url + "#openings") as (page, calls):
                await page.context.route("https://careers.example.com/**", a_job)
                async with page.context.expect_page() as new:
                    await page.click(fab)
                await (await new.value).wait_for_url(lambda url: url == read)
                await page.wait_for_timeout(300)  # (time for a call to the desk, were one going to be made)
                tab = (list(calls), list(opened), [other.url for other in page.context.pages[1:]])
            return own, refused, tab
        finally:
            await desk.stop()

    own, refused, tab = run(go())
    # its own window: the desk is asked, the usual browser gets the address, and no page opens from this one
    assert own == ([("POST", "/api/open", {"url": read})], [read], 1, desk.url + "&w=app#openings"), own
    # refused by the desk: nothing more handed to the usual browser, and the link's own page opens
    assert refused == ([("POST", "/api/open", {"url": long})], [read], [long]), refused[1:]
    # a tab: no call, nothing handed over, and the link's own page opens
    assert tab == ([], [read], [read]), tab


@needs_browser
def test_a_link_no_browser_took_opens_the_ordinary_way_from_the_desks_own_window(srv, monkeypatch):
    """In the desk's own window, a link the desk handed on but no browser took (/api/open's 502) opens the ordinary
    way, as one the desk refuses does: never a press that does nothing."""
    tried: list[str] = []
    monkeypatch.setattr(desk_module.webbrowser, "open", lambda url, *a, **k: tried.append(url) and False)
    desk = _idle_desk(srv)
    job = "https://careers.example.com/jobs/3"
    desk.listings = [{"company": "Example Fab", "title": "Field Service Engineer", "url": job,
                      "location": "Phoenix, AZ", "fit": _fit(80)}]

    async def a_job(route):  # (the employer's site, made up: nothing leaves this computer)
        await route.fulfill(status=200, content_type="text/html", body="<title>A job</title>")

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _page(desk, desk.url + "&w=app#openings") as (page, calls):
                await page.context.route("https://careers.example.com/**", a_job)
                async with page.context.expect_page() as new:
                    await page.click("#rows li.row:has-text('Example Fab') a.title")
                await (await new.value).wait_for_url(lambda url: url == job)
                return list(calls), [other.url for other in page.context.pages[1:]]
        finally:
            await desk.stop()

    calls, pages = run(go())
    assert calls == [("POST", "/api/open", {"url": job})] and tried == [job], (calls, tried)
    assert pages == [job], pages
