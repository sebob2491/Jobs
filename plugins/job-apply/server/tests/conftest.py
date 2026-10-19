import asyncio
import contextlib
import inspect
import os
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from playwright.async_api import Browser, async_playwright

FIXTURES = Path(__file__).parent / "fixtures"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

PROFILE = {
    "personal": {
        "first_name": "Sam",
        "last_name": "Rivera",
        "email": "sam.rivera@example.com",
        "phone": "480-555-0123",
        "phone_country_code": "+1",
        "address": {"line1": "100 W Main St", "city": "Chandler", "state": "AZ", "postal_code": "85225",
                    "country": "United States"},
        "linkedin_url": "https://www.linkedin.com/in/samrivera",
    },
    "documents": {"resume": "RESUME_PLACEHOLDER"},
    "work_authorization": {"authorized_to_work": True, "requires_sponsorship": False, "us_person": True,
                           "over_18": True},
    "history": {"previous_employers": ["Intel"]},
    "preferences": {"willing_to_travel": "Yes, up to 75%", "how_did_you_hear": "LinkedIn",
                    "willing_to_relocate": False},
    "eeo": {"gender": "Decline to self-identify", "veteran": "I don't wish to answer"},
    "answers": [{"match": "clean ?room", "answer": "Yes"}, {"match": "lift", "answer": None}],
    "work_history": [
        {"title": "Equipment Technician", "company": "Intel", "location": "Chandler, AZ", "start": "2021-03",
         "end": "present", "description": "Maintained 300mm etch and deposition tools."},
        {"title": "Maintenance Technician", "company": "Example Fab Services", "location": "Tempe, AZ",
         "start": "Jun 2018", "end": "02/2021", "description": "PMs and troubleshooting on vacuum pumps."},
    ],
    "education_history": [
        {"school": "Arizona State University", "degree": "BS Electrical Engineering", "major": "Electrical Engineering",
         "gpa": 3.4, "start": 2016, "end": 2020},
    ],
    # (account handling off: the tests of the desk leaving Create Account to the person; the
    # manage_accounts tests turn it on. One job at a time: the desk as it was before jobs_at_once, whose tests
    # take jobs in order; the tests of jobs at once set their own)
    "settings": {"submit_mode": "review", "browser_channel": "chromium", "headless": True, "manage_accounts": False,
                 "jobs_at_once": 1},
}


@pytest.fixture(autouse=True)
def job_apply_home(tmp_path, monkeypatch):
    home = tmp_path / "job-apply"
    home.mkdir()
    resume = home / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% test resume\n")
    profile = {**PROFILE, "documents": {"resume": str(resume)}}
    (home / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    monkeypatch.setenv("JOB_APPLY_HOME", str(home))
    monkeypatch.setenv("JOB_APPLY_HEADLESS", "1")
    # a page read with no form is read again a moment later (Oracle's draw theirs late): in tests, 50 ms apart,
    # not 500 (about a minute of the suite); test_inspect_waits_for_a_form_drawn_late keeps the real wait
    from job_apply import browser
    monkeypatch.setattr(browser, "FORM_DRAW_WAIT", 50)
    monkeypatch.setenv("JOB_APPLY_NO_UPDATE_CHECK", "1")  # no look at GitHub for a newer version
    return home


def fixture_url(name: str) -> str:
    return (FIXTURES / name).resolve().as_uri()


def launch_options() -> dict:
    """For tests that start a browser of their own: the same Chromium the app is told to use."""
    exe = os.environ.get("JOB_APPLY_CHROMIUM_PATH")
    return {"executable_path": exe} if exe else {}


def playwright_browsers() -> Path:
    """Where `playwright install` puts its browsers: its own default on each system (on Windows,
    AppData\\Local; looking only in ~/.cache skipped every browser test there)."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"


def browser_available() -> bool:
    if os.environ.get("JOB_APPLY_CHROMIUM_PATH"):
        return True
    return any(playwright_browsers().glob("chromium-*"))


# The desk's waits for pages that draw late (their production values in pipeline.py and browser.py), shortened for the
# tests: a mock site here draws at once or within a few seconds (the longest, 5 s, is workday-account.html's &slow
# refusal), and a test that sits out the production 10 to 30 seconds for a button that never comes spends them for
# nothing (the whole-program review of Oct 15: 87 wait-heavy tests, 562 s of worker time). A test that sets a wait
# itself keeps its own value (its setattr comes after this one), and one timed against the real waits on purpose opts
# out with @pytest.mark.real_waits.
FAST_WAITS = {
    "pipeline": {"LATE_BUTTONS_WAIT": 5, "SIGN_IN_SETTLE": 7, "RESET_SETTLE": 7, "STEP_SAVE_WAIT": 6,
                 "STEP_LOAD_WAIT": 8, "BLANK_PAGE_WAIT": 8, "SIGN_IN_STEP_WAIT": 8, "ACCOUNT_WAIT": 7,
                 "ACCOUNT_STEP_WAIT": 5, "NOTICE_WAIT": 3, "NEW_TAB_WAIT": 3, "ACCOUNT_DRAW_WAIT": 3,
                 # (the Applier's poll and its settles: a mock site answers at once)
                 "POLL_SECONDS": 0.3, "ONCE_SETTLE": 0.2, "REFUSAL_SETTLE": 0.5, "INDICATOR_GONE_SETTLE": 0.5,
                 "LEFT_SIGN_IN_SETTLE": 0.5},
    "browser": {"SETTLE_WAIT": 4, "CONFIRM_WAIT": 5, "UPLOAD_SHOWN_WAIT": 6, "UPLOAD_RELOAD_START": 0.3,
                "FRAME_WAIT": 4, "LOST_FILL_WAIT": 0.3, "SAVED_DRAW_WAIT": 500, "BLOCK_DRAW_WAIT": 1500,
                # (the menu reader's, in ms: a mock's list shows its choices at once, or the test sets its own wait)
                "LIST_OPEN_WAIT": 600, "COMBO_OPEN_WAIT": 300, "LIST_PLACEHOLDER_WAIT": 1200, "PICK_OPEN_WAIT": 600,
                "LIST_REOPEN_WAIT": 900, "LIST_JUMP_WAIT": 500, "LIST_FILTER_WAIT": 800, "LIST_SEARCH_WAIT": 900,
                "LIST_NEW_WAIT": 500},
}


def pytest_configure(config):
    config.addinivalue_line("markers", "real_waits: keep the desk's production waits (a test timed against them)")
    config.addinivalue_line("markers", "own_browser: the desk starts a browser of its own for this test, as the app "
                                       "does, instead of a context in the worker's (see one_browser_per_worker)")
    config.addinivalue_line("markers", "slow: takes 20 s or more on CI (its --durations list): CI starts these first "
                                       "(JOB_APPLY_SLOW_FIRST), and -m \"not slow\" leaves them out of a quicker run")


@pytest.fixture(autouse=True)
def fast_waits(request, monkeypatch):
    if request.node.get_closest_marker("real_waits"):
        return
    from job_apply import browser, pipeline
    for name, value in FAST_WAITS["pipeline"].items():
        monkeypatch.setattr(pipeline, name, value)
    for name, value in FAST_WAITS["browser"].items():
        monkeypatch.setattr(browser, name, value)


# ---------------------------------------------------------------- one browser per worker
# Each test used to start a Playwright and a Chromium of its own: about 1.2 s a test, for some 500 tests (the code
# review of Oct 16). Now each xdist worker (or a run without xdist) starts them once, on an event loop its tests share
# (`loop`), and each test gets a fresh context in that browser: cookies, storage, cache, downloads, viewport and routes
# of its own, as a browser started for it had. What a test leaves running on the loop is cancelled after it, and what
# it leaves open in the browser is closed. A test that needs a browser process of its own (one that crashes a tab,
# which now and then takes the whole browser down with it) says so with @pytest.mark.own_browser: the desk then starts
# one for it, as the app does.


class _Worker:
    """This worker's event loop, Playwright and browser."""
    loop: asyncio.AbstractEventLoop | None = None
    playwright: Any = None
    browser: Any = None
    launched: dict = {}  # the options it was launched with


WORKER = _Worker()


def worker_launch() -> dict:
    """What the worker's browser is launched with: the tests' Chromium, headless, with the desk's own switches (its
    tabs behind the one in front never throttled: BACKGROUND_TABS_RUN)."""
    from job_apply.browser import BACKGROUND_TABS_RUN

    return {"headless": True, "args": list(BACKGROUND_TABS_RUN), **launch_options()}


async def worker_playwright():
    if WORKER.playwright is None:
        WORKER.playwright = await async_playwright().start()
    return WORKER.playwright


async def worker_browser():
    """The worker's browser: started the first time a test asks for it (and again if it went down, or a test has
    another Chromium named for the tests)."""
    if WORKER.browser is None or not WORKER.browser.is_connected() or WORKER.launched != worker_launch():
        if WORKER.browser is not None:
            with contextlib.suppress(Exception):
                await WORKER.browser.close()
        WORKER.launched = worker_launch()
        WORKER.browser = await (await worker_playwright()).chromium.launch(**WORKER.launched)
    return WORKER.browser


def _stop_leftovers(lp: asyncio.AbstractEventLoop) -> None:
    """Cancel what a test left running on the loop, so that none of it runs on into the next test (a loop of each
    test's own, closed after it, stopped it there). A Playwright's connection is left to its stop: the worker's runs on
    for the next test, and the one of a browser of the test's own is stopped as that browser closes (cancelled, the
    close would wait forever for the browser's answer)."""
    left = [t for t in asyncio.all_tasks(lp) if getattr(t.get_coro(), "__qualname__", "") != "Connection.run"]
    for task in left:
        task.cancel()
    if left:
        lp.run_until_complete(asyncio.wait(left, timeout=30))
    stuck = [t for t in left if not t.done()]
    assert not stuck, f"what the test left running wouldn't stop: {stuck}"


@pytest.fixture(scope="session")
def worker_loop():
    lp = WORKER.loop = asyncio.new_event_loop()
    yield lp
    for end in (WORKER.browser and WORKER.browser.close, WORKER.playwright and WORKER.playwright.stop):
        if end:
            with contextlib.suppress(Exception):
                lp.run_until_complete(end())
    WORKER.loop = WORKER.playwright = WORKER.browser = None
    lp.close()


@pytest.fixture
def loop(worker_loop):
    """The worker's event loop (the worker's browser runs on it), for this test."""
    asyncio.set_event_loop(worker_loop)
    yield worker_loop
    _stop_leftovers(worker_loop)
    asyncio.set_event_loop(None)  # (a test that runs a coroutine without asking for a loop fails, as it did)


# (Browser.new_context's options: launch_persistent_context's others are the browser's own)
_CONTEXT_OPTIONS = set(inspect.signature(Browser.new_context).parameters) - {"self"}


class _WorkerPlaywright:
    """`async_playwright()` as the desk's BrowserSession gets it in a test: each launch it asks for is a fresh context
    in the worker's browser, and its stop leaves the worker's Playwright running. A launch the worker's browser can't
    stand in for (another browser or channel, a window shown, launch options of its own) is the real
    launch_persistent_context, in a browser of its own; on a loop other than the worker's (a test's asyncio.run), the
    session gets a Playwright of its own, as before."""

    def __init__(self, opened: list) -> None:
        self.opened = opened
        self.chromium = self

    async def start(self):
        if asyncio.get_running_loop() is not WORKER.loop:
            return await async_playwright().start()
        return self

    async def stop(self) -> None:
        pass  # (the worker's: kept for its next test)

    async def launch_persistent_context(self, **options):
        launch = {k: v for k, v in options.items() if k not in _CONTEXT_OPTIONS and k != "user_data_dir"}
        if launch == worker_launch():
            browser = await worker_browser()
            ctx = await browser.new_context(**{k: v for k, v in options.items() if k in _CONTEXT_OPTIONS})
        else:
            ctx = await (await worker_playwright()).chromium.launch_persistent_context(**options)
        self.opened.append(ctx)
        return ctx


@pytest.fixture(autouse=True)
def one_browser_per_worker(request, monkeypatch):
    if request.node.get_closest_marker("own_browser"):
        yield
        return
    from job_apply import browser
    opened: list = []
    monkeypatch.setattr(browser, "async_playwright", lambda: _WorkerPlaywright(opened))
    yield
    for ctx in opened:  # (any the test left open: srv closes the desk's)
        with contextlib.suppress(Exception):
            WORKER.loop.run_until_complete(ctx.close())


class _PersonsBrowser:
    """The person's own browser, beside the desk's, in the worker's browser: as Browser.new_page has it, each page in
    a fresh context of its own."""

    def __init__(self, browser) -> None:
        self._browser = browser
        self._contexts: list = []

    async def new_page(self, **options):
        ctx = await self._browser.new_context(**options)
        self._contexts.append(ctx)
        return await ctx.new_page()

    async def close(self) -> None:
        while self._contexts:
            await self._contexts.pop().close()


@contextlib.asynccontextmanager
async def person_browser():
    """A browser of the person's own, for a test of the desk's page: fresh, and closed afterwards."""
    if asyncio.get_running_loop() is not WORKER.loop:
        async with async_playwright() as pw:
            yield await pw.chromium.launch(**launch_options())
        return
    person = _PersonsBrowser(await worker_browser())
    try:
        yield person
    finally:
        with contextlib.suppress(Exception):
            await person.close()


@pytest.fixture
def srv(job_apply_home, loop):
    """The MCP server module with a fresh tracker; closes its browser afterwards."""
    from job_apply import server

    server._tracker = None
    server.browser.current_job_id = None
    yield server
    try:
        _stop_leftovers(loop)  # (before the browser and the tracker close under it)
    finally:
        loop.run_until_complete(server.browser.close())
        if server._tracker:
            server._tracker.close()
            server._tracker = None


@pytest.fixture
def smoke_script(tmp_path):
    """scripts/live_smoke.py in a process of its own, short of a browser: `smoke_script(env, *args)` runs it with
    `args`, its --out in tmp_path, and of the test identity's values only those in `env` (its home goes in tmp_path).
    Gives its exit code and all it printed."""
    def smoke(env: dict, *args: str) -> tuple[int, str]:
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(("LIVE_TEST_", "JOB_APPLY_SECRET_", "JOB_APPLY_LIVE_TEST_"))}
        out = subprocess.run([sys.executable, str(SCRIPTS / "live_smoke.py"), *args, "--out", str(tmp_path / "out")],
                             env={**clean, **env, "TMPDIR": str(tmp_path)}, capture_output=True, text=True,
                             timeout=120)
        return out.returncode, out.stdout + out.stderr

    return smoke


@pytest.fixture
def fake_open(monkeypatch) -> list[int]:
    """The Applier's _open with no page: each job (by id) as it would open, noted in the list this gives, and its pass
    ends at once, ready on its review page."""
    from job_apply import pipeline

    opened: list[int] = []

    async def open_(self, r):  # (where the application's page would open)
        opened.append(r.job_id)
        r.status, r.reason = "ready", "Filled and waiting on the review page."
        return False

    monkeypatch.setattr(pipeline.Applier, "_open", open_)
    return opened


@pytest.fixture
def fake_launch(monkeypatch):
    """Playwright as the desk's BrowserSession starts it, with a Chromium that won't start: `fake_launch(error)` puts
    it in place, and each launch_persistent_context's options go in the list it gives before it raises a PlaywrightError
    saying `error`."""
    from job_apply import browser as browser_module

    def launch(error: str) -> list[dict]:
        launched: list[dict] = []

        class Chromium:
            async def launch_persistent_context(self, **kw):
                launched.append(kw)
                raise browser_module.PlaywrightError(error)

        class Playwright:
            chromium = Chromium()

            async def stop(self):
                pass

        class Starter:
            async def start(self):
                return Playwright()

        monkeypatch.setattr(browser_module, "async_playwright", lambda: Starter())
        return launched

    return launch


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def by_label(fields, text):
    return next(f for f in fields if text.lower() in f["label"].lower())


async def check_fixture_extraction(server, html: Path, expect: dict) -> list[str]:
    """Open a saved page and report expected fields that are no longer extracted."""
    await server.browser.goto(html.resolve().as_uri())
    data = await server.browser.inspect(include_dropdown_options=False)
    got = data["fields"]
    problems = []
    texts = {str(a.get("text") or "").strip() for a in data.get("actions") or []}
    problems += [f"missing button {t!r}" for t in expect.get("actions") or [] if t not in texts]
    for want in expect["fields"]:
        same = [f for f in got if f.get("label") == want["label"] and f.get("kind") == want["kind"]]
        if not same:
            problems.append(f"missing {want['kind']} field {want['label']!r}")
        elif "required" in want and not any(bool(f.get("required")) == want["required"] for f in same):
            problems.append(f"required flag changed for {want['label']!r}")
    return problems


def slow_first(items: list) -> list:
    """The same tests, those marked slow first (each group in the order it had)."""
    slow = [item for item in items if item.get_closest_marker("slow")]
    return slow + [item for item in items if not item.get_closest_marker("slow")]


def pytest_collection_modifyitems(config, items):
    """JOB_APPLY_TEST_SHARD=1/6 runs only that part of the suite (CI runs the parts on separate
    machines). A test's part comes from its id, so every machine and xdist worker agrees.

    JOB_APPLY_SLOW_FIRST=1 then puts the part's slow tests first (after the split, so each test stays in its part),
    so that none is handed out last to make the part's tail. CI runs it with xdist's --maxschedchunk=1, and needs it:
    by default xdist starts each worker on a block of the tests in their order (a quarter of its share, about 28 on
    CI), so the first worker would get every slow test and run them one after another. One at a time, each worker
    starts with two tests and gets the next one in line whenever it finishes one."""
    shard = os.environ.get("JOB_APPLY_TEST_SHARD")
    if shard:
        part, parts = (int(x) for x in shard.split("/"))
        mine = [zlib.crc32(item.nodeid.encode()) % parts == part - 1 for item in items]
        config.hook.pytest_deselected(items=[item for item, keep in zip(items, mine) if not keep])
        items[:] = [item for item, keep in zip(items, mine) if keep]
    if os.environ.get("JOB_APPLY_SLOW_FIRST") == "1":
        items[:] = slow_first(items)
