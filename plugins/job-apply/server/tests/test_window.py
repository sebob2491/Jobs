"""The Job Desk's own window (window.py, the design's A1): which browser shows it, how that browser is started, the
profile's settings.desk_window that chooses between a window and a tab, and the two places the desk opens its page.
No browser is started here but the tests' own: Popen and the person's usual browser are stand-ins."""

import asyncio
import logging
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml
from conftest import browser_available, person_browser, run
from test_desk import _client, _set_profile_settings

from job_apply import config, window
from job_apply import desk as desk_module
from job_apply.desk import Desk

URL = "http://127.0.0.1:8765/?t=a-made-up-key"
EDGE = Path("Microsoft", "Edge", "Application", "msedge.exe")
CHROME = Path("Google", "Chrome", "Application", "chrome.exe")
# the page's tab icon, as the design's mockup draws it
LOGO = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><defs><linearGradient id="g" x1="0" y1="0" x2="1" '
        'y2="1"><stop offset="0" stop-color="#6366f1"/><stop offset="1" stop-color="#4338ca"/></linearGradient></defs>'
        '<rect width="32" height="32" rx="8" fill="url(#g)"/><path d="M9 16.5l4.5 4.5L23 11" fill="none" stroke="#fff" '
        'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def _program(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _only(places: list[Path], there: list[Path]) -> None:
    """Of `places`, programs at `there` and nothing at the others."""
    for place in places:
        if place in there:
            _program(place)
        else:
            place.unlink(missing_ok=True)


def test_this_computers_system_is_the_one_python_says():
    assert window._system() == {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def test_on_windows_edge_comes_before_chrome_each_where_its_installer_puts_it(monkeypatch, tmp_path):
    """Edge under Program Files (x86), then Program Files; Chrome under Program Files, Program Files (x86), then the
    person's Local AppData: the first that's there, and none when none is."""
    monkeypatch.setattr(window, "_system", lambda: "windows")
    x86, files, local = tmp_path / "Program Files (x86)", tmp_path / "Program Files", tmp_path / "AppData" / "Local"
    for name, folder in (("ProgramFiles(x86)", x86), ("ProgramFiles", files), ("LOCALAPPDATA", local)):
        monkeypatch.setenv(name, str(folder))
    places = [x86 / EDGE, files / EDGE, files / CHROME, x86 / CHROME, local / CHROME]  # in the order looked for
    assert window.find_browser() is None  # (none installed)
    for i, place in enumerate(places):
        _only(places, places[i:])  # (this one and every later one)
        assert window.find_browser() == str(place), place
        _only(places, [place])  # (this one alone)
        assert window.find_browser() == str(place), place
    _only(places, [])
    (x86 / EDGE).mkdir(parents=True)  # (a folder of that name is no program)
    assert window.find_browser() is None


def test_on_windows_a_folder_the_environment_doesnt_name_is_never_looked_for_where_the_desk_started(monkeypatch,
                                                                                                    tmp_path):
    """Without ProgramFiles(x86) (or with one that's empty, or not a full path), no browser is taken from under the
    folder the desk happened to start in: a program put there isn't the person's browser."""
    monkeypatch.setattr(window, "_system", lambda: "windows")
    monkeypatch.chdir(tmp_path)
    for planted in (EDGE, CHROME, Path("somewhere") / CHROME):
        _program(tmp_path / planted)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.setenv("ProgramFiles", "")
    monkeypatch.setenv("LOCALAPPDATA", "somewhere")
    assert Path(EDGE).is_file() and (Path("somewhere") / CHROME).is_file()  # (there, seen from the starting folder)
    assert window.find_browser() is None
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "somewhere"))  # (named in full: found)
    assert window.find_browser() == str(tmp_path / "somewhere" / CHROME)


def test_on_macos_chrome_comes_before_edge_in_applications(monkeypatch, tmp_path):
    monkeypatch.setattr(window, "_system", lambda: "macos")
    assert [place.as_posix() for place in window._mac_browsers()] == [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    apps = tmp_path / "Applications"  # (this test's own Applications folder: the same programs under it)
    monkeypatch.setattr(window, "MAC_APPLICATIONS", apps)
    chrome = apps / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
    edge = apps / "Microsoft Edge.app" / "Contents" / "MacOS" / "Microsoft Edge"
    assert window.find_browser() is None
    _only([chrome, edge], [chrome, edge])
    assert window.find_browser() == str(chrome)
    _only([chrome, edge], [edge])
    assert window.find_browser() == str(edge)
    _only([chrome, edge], [chrome])
    assert window.find_browser() == str(chrome)
    _only([chrome, edge], [])
    assert window.find_browser() is None


def test_on_linux_the_first_browser_on_the_path_is_taken(monkeypatch):
    names = ["google-chrome", "google-chrome-stable", "microsoft-edge", "microsoft-edge-stable", "chromium",
             "chromium-browser"]  # in the order looked for
    there: set[str] = set()
    asked: list[str] = []

    def which(name, *a, **k):
        asked.append(name)
        return f"/usr/bin/{name}" if name in there else None

    monkeypatch.setattr(window, "_system", lambda: "linux")
    monkeypatch.setattr(window.shutil, "which", which)
    assert window.find_browser() is None and asked == names  # (none installed: each looked for, in order)
    for i, name in enumerate(names):
        there = set(names[i:])
        assert window.find_browser() == f"/usr/bin/{name}", name
        there = {name}
        assert window.find_browser() == f"/usr/bin/{name}", name
    there = {"firefox", "brave-browser"}  # (no window of its own from these: none)
    assert window.find_browser() is None


@pytest.fixture
def started(monkeypatch) -> SimpleNamespace:
    """What open_window starts and opens, with nothing started for real: each Popen's arguments and options
    (`programs`), and each address handed to the person's usual browser (`tabs`)."""
    seen = SimpleNamespace(programs=[], tabs=[])

    class Started:  # (what Popen gives back: never waited for, never read)
        def __getattr__(self, name):
            raise AssertionError(f"the browser started for the window was used: .{name}")

    def popen(args, **options):
        seen.programs.append((args, options))
        return Started()

    monkeypatch.setattr(window.subprocess, "Popen", popen)
    monkeypatch.setattr(window.webbrowser, "open", lambda url, *a, **k: seen.tabs.append((url, *a, *k)) or True)
    return seen


@pytest.mark.parametrize("system, detached", [
    ("windows", {"creationflags": 0x00000008 | 0x00000200}),  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    ("macos", {"start_new_session": True}),
    ("linux", {"start_new_session": True}),
])
def test_the_window_is_the_browser_started_with_the_address_and_a_size_and_nothing_else(monkeypatch, started, system,
                                                                                      detached):
    """The browser found is started with exactly two arguments, the page's address as an app window's (ending &w=app)
    and the window's size: never a profile folder, a debugging port or a switch that turns a protection off. On its
    own (detached, nothing of the desk's passed on to it), and not waited for."""
    monkeypatch.setattr(window, "_system", lambda: system)
    monkeypatch.setattr(window, "find_browser", lambda: "/made/up/browser")
    assert window.open_window(URL, "app") == "app"
    assert started.programs == [(
        ["/made/up/browser", "--app=http://127.0.0.1:8765/?t=a-made-up-key&w=app", "--window-size=1280,860"],
        {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, **detached})]
    assert started.tabs == []  # (and no tab beside the window)
    if sys.platform == "win32":  # (where Python has Windows' flags by name, these are their numbers)
        assert (subprocess.DETACHED_PROCESS, subprocess.CREATE_NEW_PROCESS_GROUP) == (0x00000008, 0x00000200)


def test_a_tab_opens_when_the_profile_says_tab_when_theres_no_browser_and_when_it_wont_start(monkeypatch, started):
    looked: list[bool] = []
    found: list[str | None] = ["/made/up/browser"]

    def find_browser():
        looked.append(True)
        return found[0]

    monkeypatch.setattr(window, "find_browser", find_browser)
    # the profile says tab: no browser is even looked for
    assert window.open_window(URL, "tab") == "tab"
    assert (started.programs, started.tabs, looked) == ([], [(URL,)], [])
    # no browser that can show a window of its own
    found[0] = None
    assert window.open_window(URL, "app") == "tab"
    assert (started.programs, started.tabs, looked) == ([], [(URL,), (URL,)], [True])
    # one found that won't start (gone since, or not allowed to run)
    found[0] = "/made/up/browser"
    for error in (FileNotFoundError("gone"), PermissionError("not allowed"), OSError("won't start")):
        def refuse(args, error=error, **options):
            raise error

        monkeypatch.setattr(window.subprocess, "Popen", refuse)
        before = len(started.tabs)
        assert window.open_window(URL, "app") == "tab", error
        assert started.tabs[before:] == [(URL,)], error  # (the address as the desk gave it: no &w=app in a tab)


def test_desk_window_is_app_unless_the_profile_says_tab(job_apply_home):
    """settings.desk_window: "app" where the profile doesn't say, "tab" as written; anything else is said, and the
    desk opens in its window. The template has it, under the comment the design gives."""
    def read(raw):
        s = config.Settings.from_dict({} if raw is None else {"desk_window": raw})
        return s.desk_window, s.warnings

    assert config.Settings().desk_window == "app" and config.Settings.from_dict(None).desk_window == "app"
    for raw in (None, "", "  ", "app", "App", " APP "):
        assert read(raw) == ("app", []), raw
    for raw in ("tab", "Tab", " TAB "):
        assert read(raw) == ("tab", []), raw
    for raw in ("window", "browser", "tabs", True, False, 0, 2, ["tab"]):
        assert read(raw) == ("app", [f"Unknown settings.desk_window {raw!r}; using app. Use app or tab."]), raw
    assert config.Settings.from_dict("tab").desk_window == "app"  # (a settings: line that isn't a section)
    assert "desk_window" in config.KNOWN_SETTINGS
    text = config.TEMPLATE_PROFILE.read_text(encoding="utf-8")
    assert ("\n  # app: the Job Desk opens in a window of its own. tab: in a tab of your usual browser.\n"
            "  desk_window: app\n") in text
    template = yaml.safe_load(text)
    assert template["settings"]["desk_window"] == "app" and config.Profile(template).settings.warnings == []
    # (and the desk can write it: a later Settings page's switch)
    config.set_setting("desk_window", "tab")
    assert config.Profile.load().settings.desk_window == "tab"


def test_the_desk_opens_its_page_as_the_profile_says_and_nothing_when_told_not_to(srv, monkeypatch, job_apply_home):
    """Desk.start (Claude's open_job_desk) and a second start of job-apply-desk open the page through open_window, with
    the profile's settings.desk_window as it is then; open_browser=False opens nothing at all. Each time with a new
    one-time code in the address, never the page's key (/api/window for the second start)."""
    opened: list[tuple[str, str]] = []

    def nothing_else(*a, **k):
        raise AssertionError("the page was opened another way than open_window")

    monkeypatch.setattr(window, "open_window", lambda url, mode: opened.append((url, mode)) or mode)
    monkeypatch.setattr(window.subprocess, "Popen", nothing_else)
    monkeypatch.setattr(window.webbrowser, "open", nothing_else)
    desk = Desk(srv)
    desk.applier.start = lambda: None  # (the queue isn't worked here)
    desk.search.update(status="done", at=time.time())

    def second_desk(srv_module):
        raise AssertionError("a second desk was started")

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            quiet = list(opened)
            await desk.start(open_browser=True)  # (asked for again, as open_job_desk does: the same desk, its page)
            in_window = list(opened)
            _set_profile_settings(job_apply_home, desk_window="tab")
            await desk.start(open_browser=True)
            in_tab = list(opened)
            # job-apply-desk started while this desk runs on its port: the running desk's page, as the profile says
            monkeypatch.setattr(desk_module, "get_desk", second_desk)
            assert await asyncio.to_thread(desk_module.main, ["--port", str(desk.port)]) == 0
            (job_apply_home / "profile.yaml").write_text("settings: [not, a, section\n", encoding="utf-8")
            assert await asyncio.to_thread(desk_module.main, ["--port", str(desk.port)]) == 0
            return quiet, in_window, in_tab
        finally:
            await desk.stop()

    quiet, in_window, in_tab = run(go())
    assert quiet == []
    assert in_window == opened[:1] and in_tab == opened[:2]
    # (a profile that can't be read just now: the setting's default, never a desk that won't open)
    assert [mode for _, mode in opened] == ["app", "tab", "tab", "app"]
    coded = rf"http://127\.0\.0\.1:{desk.port}/\?c=[A-Za-z0-9_-]{{20,}}"
    assert all(re.fullmatch(coded, url) for url, _ in opened), opened
    assert len({url for url, _ in opened}) == 4  # (a new code each time)


def test_the_window_is_never_handed_the_pages_key_and_no_log_line_holds_it_or_a_code(srv, monkeypatch, started,
                                                                                    job_apply_home, caplog):
    """The page's key on a browser's command line could be read by the computer's other accounts as long as the
    browser runs. Both places that open the desk's window (Desk.start, and a second start finding this desk running)
    start the browser with a one-time code in its place, which the desk trades for its own address with the key (and
    &w=app) once. Nothing the desk logs meanwhile holds the key or a code."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(window, "find_browser", lambda: "/made/up/browser")
    desk = Desk(srv)
    desk.applier.start = lambda: None  # (the queue isn't worked here)
    desk.search.update(status="done", at=time.time())

    def second_desk(srv_module):
        raise AssertionError("a second desk was started")

    monkeypatch.setattr(desk_module, "get_desk", second_desk)

    async def go():
        await desk.start(port=0, open_browser=True)
        try:
            assert await asyncio.to_thread(desk_module.main, ["--port", str(desk.port)]) == 0
            logged = list(caplog.records)  # (what the desk logged: the test's own fetches below log what they fetch)
            async with _client(desk) as c:
                gone_to = []
                for args, _ in started.programs:
                    r = await c.get(args[1].removeprefix("--app="))
                    gone_to.append((r.status_code, r.headers.get("location")))
                return gone_to, logged
        finally:
            await desk.stop()

    gone_to, records = run(go())
    assert len(started.programs) == 2 and started.tabs == [], started
    codes = []
    for args, _ in started.programs:
        assert len(args) == 3 and args[2] == "--window-size=1280,860", args
        assert desk.token not in " ".join(args), args
        address = args[1].removeprefix("--app=")
        assert re.fullmatch(rf"http://127\.0\.0\.1:{desk.port}/\?c=[A-Za-z0-9_-]{{20,}}&w=app", address), address
        codes.append(parse_qs(urlsplit(address).query)["c"][0])
    assert codes[0] != codes[1]
    assert gone_to == [(303, desk.url + "&w=app")] * 2, gone_to
    logged = "\n".join(f"{r.name} {r.getMessage()}" for r in records)
    assert all(secret not in logged for secret in (desk.token, *codes)), logged


def test_the_desk_reads_the_profile_for_its_window_off_its_event_loop(srv, monkeypatch, job_apply_home):
    """Desk.start reads settings.desk_window in the same call off the event loop as the window's opening: a profile
    read never holds up the desk's other work."""
    read_on: list[int] = []
    monkeypatch.setattr(desk_module, "_desk_window", lambda: read_on.append(threading.get_ident()) or "app")
    monkeypatch.setattr(window, "open_window", lambda url, mode: mode)
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())

    async def go():
        await desk.start(port=0, open_browser=True)
        try:
            return threading.get_ident()
        finally:
            await desk.stop()

    loop_thread = run(go())
    assert len(read_on) == 1 and read_on[0] != loop_thread, (read_on, loop_thread)


def test_no_browser_says_what_it_leaves_closed(capsys):
    with pytest.raises(SystemExit):
        desk_module.main(["--help"])
    assert "don't open the Job Desk's window" in " ".join(capsys.readouterr().out.split())


@pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")
def test_the_page_keeps_the_window_it_opened_in_and_carries_its_own_icon(srv):
    """The page reads w from its address as it reads its key, and keeps it for this tab or window (sessionStorage's
    desk-window), so a reload without it still knows. Its tab icon is the logo, held in the page as a data address:
    nothing is fetched for it. And its title is still Job Desk."""
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())  # (recent: opening the page searches nothing)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()

                async def at(address: str) -> list:
                    await page.goto(address)
                    await page.wait_for_selector("#plugin-version:not(:empty)")  # the page has its state
                    return await page.evaluate("[deskWindow, sessionStorage.getItem('desk-window')]")

                in_tab = await at(desk.url)
                in_window = await at(desk.url + "&w=app")
                reloaded = await at(f"http://127.0.0.1:{desk.port}/")  # (neither the key nor w in the address)
                icons = await page.eval_on_selector_all("link[rel=icon]", "ls => ls.map((l) => l.getAttribute('href'))")
                title = await page.title()
                await browser.close()
                return in_tab, in_window, reloaded, icons, title
        finally:
            await desk.stop()

    in_tab, in_window, reloaded, icons, title = run(go())
    assert in_tab == ["", None]
    assert in_window == ["app", "app"] and reloaded == ["app", "app"]
    assert len(icons) == 1 and icons[0].startswith("data:image/svg+xml,"), icons
    drawn = icons[0].removeprefix("data:image/svg+xml,")
    assert re.fullmatch(r"[A-Za-z0-9%._~-]+", drawn), drawn  # (percent-encoded through: nothing a browser reads wrong)
    assert unquote(drawn) == LOGO
    assert title == "Job Desk" and "<title>Job Desk</title>" in desk_module.PAGE.read_text(encoding="utf-8")
