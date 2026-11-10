"""The Settings page (the design's Version D, D1): a menu of eight panels at the left, the one chosen at its right, each
panel with an address of its own (#settings/<panel>), so a message elsewhere can link straight to it. Each test opens
the real page in a browser. Made-up values only: the repo is public."""

import asyncio
import io
import json
import zipfile

import pytest
import yaml
from conftest import PROFILE, browser_available, open_desk, person_browser, press_menu, run
from test_desk_buttons import _idle_desk

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

# D1's panels, in the menu's order: each with its address's name and its heading
PANELS = {"profile": "Your profile", "search": "Job search", "applying": "Applying",
          "passwords": "Accounts and passwords", "email": "Email codes", "phone": "Phone alerts",
          "documents": "Resume and cover letters", "about": "Updates and about"}
# The sentence under a panel's heading, where D3 gives one
SAID = {"applying": "What the desk may do in your name. It never solves a CAPTCHA, and never signs you up for a "
                    "newsletter or job alerts.",
        "email": "Many job sites email a code or a link when you sign in or make an account. The desk can read it and "
                 "put it in for you."}
# A part each panel holds that no other does (D3), by its id; Email codes and Resume and cover letters hold only their
# heading for now. (Accounts and passwords: its table of job systems; its form shows only in the row whose Type my own
# was pressed, the design's E4)
HOLDS = {"profile": "setup-btn", "search": "auto-search", "applying": "dont-wait", "passwords": "pw-rows",
         "email": "panel-email-h", "phone": "phone-h", "documents": "panel-documents-h", "about": "program-h"}

# What the Settings page shows: the page, the panels in sight (by their data-panel), the headings in sight, the side
# menu's link marked as the current page and Settings' own menu's, and the parts of HOLDS in sight
_SHOWN = """(holds) => {
  const inSight = (el) => !!el && el.getClientRects().length > 0;
  return {
    page: document.body.dataset.page, title: document.getElementById('page-title').textContent, hash: location.hash,
    panels: [...document.querySelectorAll('.spanel')].filter(inSight).map((p) => p.dataset.panel),
    heading: [...document.querySelectorAll('.spanel h2[id^=panel-]')].filter(inSight).map((h) => h.textContent),
    said: [...document.querySelectorAll('.spanel-h p')].filter(inSight).map((p) => p.textContent),
    menu: [...document.querySelectorAll('.nav a[aria-current=page]')].map((a) => a.getAttribute('href')),
    current: [...document.querySelectorAll('.snav a[aria-current=page]')].map((a) => a.getAttribute('href')),
    holds: Object.entries(holds).filter(([, id]) => inSight(document.getElementById(id))).map(([name]) => name),
  };
}"""


def _shown(panel: str, address: str) -> dict:
    return {"page": "settings", "title": "Settings", "hash": address, "panels": [panel], "heading": [PANELS[panel]],
            "said": [SAID[panel]] if panel in SAID else [], "menu": ["#settings"], "current": [f"#settings/{panel}"],
            "holds": [panel]}


def test_each_settings_address_opens_its_panel_and_any_other_opens_your_profile(srv):
    """The design's D7, test 12. Each address of D1 opens its panel, and only it: its heading (and D3's sentence under
    it, where it gives one), what it holds, its link
    in Settings' menu marked as the current one (aria-current), and the side menu's Settings still lit. #settings alone
    opens Your profile, and so does a panel that isn't one. The menu's links in the order D1 lists them."""
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                seen = {}
                for panel in PANELS:
                    await open_desk(page, desk, f"settings/{panel}")
                    await page.reload()  # (opened by its address, as a link from elsewhere opens it)
                    await page.wait_for_function("(p) => document.body.dataset.panel === p", arg=panel)
                    seen[panel] = await page.evaluate(_SHOWN, HOLDS)
                for address in ("settings", "settings/", "settings/nowhere", "settings/constructor"):
                    await page.goto(f"{desk.url}#home")
                    await page.goto(f"{desk.url}#{address}")
                    await page.reload()
                    await page.wait_for_function("() => document.body.dataset.page === 'settings'")
                    seen[address] = await page.evaluate(_SHOWN, HOLDS)
                seen["menu"] = await page.evaluate("""() => [...document.querySelectorAll('.snav a')]
                    .map((a) => [a.textContent.trim(), a.getAttribute('href')])""")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    for panel in PANELS:
        assert seen[panel] == _shown(panel, f"#settings/{panel}"), (panel, seen[panel])
    for address in ("settings", "settings/", "settings/nowhere", "settings/constructor"):
        assert seen[address] == _shown("profile", f"#{address}"), (address, seen[address])
    assert seen["menu"] == [[name, f"#settings/{panel}"] for panel, name in PANELS.items()], seen["menu"]


def test_the_settings_menu_marks_the_panel_pressed_and_back_returns_to_the_one_before(srv):
    """Pressing a panel's link in Settings' menu shows that panel and marks it (aria-current), as a person would; Back
    in the browser returns to the panel before, and again to the one before that; leaving Settings marks no panel."""
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "home")
                seen = {}
                await press_menu(page, "settings")
                seen["settings"] = await page.evaluate(_SHOWN, HOLDS)
                await press_menu(page, "settings/applying")
                seen["applying"] = await page.evaluate(_SHOWN, HOLDS)
                await press_menu(page, "settings/phone")
                seen["phone"] = await page.evaluate(_SHOWN, HOLDS)
                await page.go_back()
                await page.wait_for_function("() => document.body.dataset.panel === 'applying'")
                seen["back"] = await page.evaluate(_SHOWN, HOLDS)
                await page.go_back()
                await page.wait_for_function("() => document.body.dataset.panel === 'profile'")
                seen["back again"] = await page.evaluate(_SHOWN, HOLDS)
                await press_menu(page, "home")
                seen["home"] = await page.evaluate("""() => [document.body.dataset.panel || null,
                    [...document.querySelectorAll('.snav a[aria-current]')].length]""")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["settings"] == _shown("profile", "#settings"), seen["settings"]
    assert seen["applying"] == _shown("applying", "#settings/applying"), seen["applying"]
    assert seen["phone"] == _shown("phone", "#settings/phone"), seen["phone"]
    assert seen["back"] == _shown("applying", "#settings/applying"), seen["back"]
    assert seen["back again"] == _shown("profile", "#settings"), seen["back again"]
    assert seen["home"] == [None, 0], seen["home"]


def test_the_keyboard_goes_through_settings_menu_and_into_the_panel_in_reading_order(srv):
    """Tab goes down Settings' menu, its eight links in order, and from the last into the panel shown: its first
    control, then the next, as they read (the Applying panel: Submit for me, Practice mode, Make my accounts, Agree to
    required notices, Cookie banners' two choices, Jobs at once's 1 to 5, Don't wait for me, the Safety window's
    button, For fixing the desk). Enter on a link opens its panel."""
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/applying")
                focused = """() => { const el = document.activeElement;
                    return el.getAttribute('href') || el.id || (el.dataset.atOnce && 'at-once-' + el.dataset.atOnce)
                      || el.textContent.trim(); }"""
                await page.focus(".snav a[href='#settings/profile']")
                order = []
                for _ in range(len(PANELS) + 14):
                    order.append(await page.evaluate(focused))
                    await page.keyboard.press("Tab")
                # (Shift+Tab back up to Accounts and passwords, the menu's fourth link; Enter: its panel shows, and
                # Tab goes on down the menu and into it)
                for _ in range(len(order) - 3):
                    await page.keyboard.press("Shift+Tab")
                pressed = await page.evaluate(focused)
                await page.keyboard.press("Enter")
                await page.wait_for_function("() => document.body.dataset.panel === 'passwords'")
                ring = await page.evaluate("""() => { const a = document.activeElement;
                    return a.matches(':focus-visible') && getComputedStyle(a).outlineStyle; }""")
                then = []
                for _ in range(5):
                    await page.keyboard.press("Tab")
                    then.append(await page.evaluate(focused))
                await browser.close()
                return order, pressed, ring, then
        finally:
            await desk.stop()

    order, pressed, ring, then = run(go())
    assert order == [*(f"#settings/{panel}" for panel in PANELS), "set-auto", "set-practice", "set-accounts",
                     "set-notices", "Decline", "Accept if there is no other way", "at-once-1", "at-once-2", "at-once-3",
                     "at-once-4", "at-once-5", "dont-wait", "safety-btn", "For fixing the desk"], order
    assert pressed == "#settings/passwords", pressed
    assert ring == "solid", ring  # (the focus ring shows on a menu link reached by the keyboard)
    # (the panel's first control: Make my passwords, where the job systems' list was: the design's E4)
    assert then == ["#settings/email", "#settings/phone", "#settings/documents", "#settings/about", "pw-make-all"], then


# ---------------------------------------------------------------- the rows (D3: the design's package 14)

def _with_settings(home, **settings) -> None:
    """The profile's settings: the test profile's, with these over them."""
    path = home / "profile.yaml"
    prof = yaml.safe_load(path.read_text(encoding="utf-8"))
    prof["settings"] = {**prof["settings"], **settings}
    path.write_text(yaml.safe_dump(prof), encoding="utf-8")


def _written(home) -> dict:
    """The profile's settings as written in it now."""
    return yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8"))["settings"]


# What each row's control shows (a switch on or off; a choice row, the words of the choice pressed; a box, its value),
# the sentence in red under a row (a save the desk refused) by its control's id where one shows, and the apply bar's
# Submit for me
_ROWS = """() => {
  const $ = (id) => document.getElementById(id);
  const pressed = (el) => [...el.querySelectorAll('button[aria-pressed=true]')].map((b) => b.textContent);
  const err = (el) => { const e = el.closest('.setrow').querySelector('.set-err');
    return e && !e.hidden ? e.textContent : null; };
  const ids = ['set-auto', 'set-practice', 'set-accounts', 'set-notices', 'set-cookies', 'set-learn', 'set-follow-up',
               'set-email-tracking', 'set-alert-server', 'set-window', 'set-browser'];
  return {
    shown: Object.fromEntries(ids.map((id) => { const el = $(id);
      return [id, el.matches('.seg') ? pressed(el) : el.type === 'checkbox' ? el.checked : el.value]; })),
    errors: Object.fromEntries(ids.map((id) => [id, err($(id))]).filter(([, e]) => e)),
    auto: $('auto').checked,
  };
}"""
_SAVED = "() => !document.querySelector('[data-saving]')"


async def _on_panel(page, at: str) -> None:
    if await page.evaluate("() => document.body.dataset.panel") != at.split("/")[1]:
        await press_menu(page, at)


async def _press(page, at: str, selector: str) -> None:
    """A row's control on the panel `at` pressed (that panel opened first), and its save over."""
    await _on_panel(page, at)
    await page.click(selector)
    await page.wait_for_function(_SAVED)


async def _type(page, at: str, selector: str, words: str) -> None:
    """Words typed into a row's box on the panel `at`, then the box left (Tab), and its save over."""
    await _on_panel(page, at)
    await page.fill(selector, words)
    await page.keyboard.press("Tab")
    await page.wait_for_function(_SAVED)


def test_the_state_says_make_my_accounts_as_the_profile_writes_it(srv, job_apply_home):
    """The switch Make my accounts shows the setting as written (the state's manage_accounts_setting), where the
    state's manage_accounts is whether the desk may make one now: off in practice mode, whatever the profile says."""
    _with_settings(job_apply_home, submit_mode="dry_run", manage_accounts=True)
    settings = _idle_desk(srv).state()["settings"]
    assert (settings["manage_accounts"], settings["manage_accounts_setting"]) == (False, True), settings
    _with_settings(job_apply_home, submit_mode="review", manage_accounts=False)
    settings = _idle_desk(srv).state()["settings"]
    assert (settings["manage_accounts"], settings["manage_accounts_setting"]) == (False, False), settings


def test_each_settings_row_shows_the_state_saves_on_a_press_and_goes_back_after_a_refused_save(srv, job_apply_home,
                                                                                              monkeypatch):
    """The design's D7, test 3, for the rows of Job search, Applying, Phone alerts and Updates and about. Each control
    shows the profile's setting; a press (or a box left) saves it at once, the control disabled while the save is under
    way; a save the desk refuses puts the control back where it was, with the desk's sentence under its row in red
    (a real refusal, a follow-up of 0 days; the route's check made to refuse; Submit for me's route made to refuse),
    and nothing in the profile changes; a save that goes through takes the sentence away."""
    from job_apply import desk as desk_module

    check = desk_module.profile_setting_problem
    _with_settings(job_apply_home, submit_mode="review", manage_accounts=True, accept_notices=False,
                   accept_cookies=True, learn_from_responses=False, email_tracking=True, follow_up_days=12,
                   desk_window="tab", browser_channel="msedge", phone_alerts_server="https://ntfy.example.com")
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))  # (Submit for me asks first)
                await open_desk(page, desk, "settings/applying")
                seen = {"first": await page.evaluate(_ROWS)}
                # (the switch disabled while its save is under way: looked at as the desk is asked)
                held = []

                async def look(route):
                    held.append(await page.evaluate("() => document.getElementById('set-learn').disabled"))
                    await route.continue_()
                await page.route("**/api/profile/settings", look)
                await _press(page, "settings/search", "#set-learn")
                await page.unroute("**/api/profile/settings", look)
                seen["held"] = held
                await _press(page, "settings/search", "#set-email-tracking")
                await _type(page, "settings/search", "#set-follow-up", "30")
                await _press(page, "settings/applying", "#set-accounts")
                await _press(page, "settings/applying", "#set-notices")
                await _press(page, "settings/applying", "#set-cookies button[data-value=false]")
                await press_menu(page, "settings/phone")
                await page.click("#alert-server-h")  # (Your own alert server, opened)
                await _type(page, "settings/phone", "#set-alert-server", "")
                await _press(page, "settings/about", """#set-window button[data-value='"app"']""")
                await _press(page, "settings/about", """#set-browser button[data-value='"chromium"']""")
                await page.wait_for_timeout(1700)  # (a refresh: the same)
                seen["saved"] = await page.evaluate(_ROWS)
                seen["written"] = _written(job_apply_home)
                # refused: a follow-up of 0 days (the desk's own check), then any key (the check made to refuse)
                await _type(page, "settings/search", "#set-follow-up", "0")
                monkeypatch.setattr(desk_module, "profile_setting_problem", lambda key, value: "The desk said no.")
                await _press(page, "settings/search", "#set-learn")
                await _press(page, "settings/applying", "#set-accounts")
                await _press(page, "settings/applying", "#set-cookies button[data-value=true]")
                await _type(page, "settings/phone", "#set-alert-server", "https://alerts.example.com")
                await _press(page, "settings/about", """#set-window button[data-value='"tab"']""")
                # (Submit for me, the desk's own switch: its route made to refuse)
                await page.route("**/api/settings", lambda route: route.fulfill(
                    status=400, content_type="application/json", body='{"error": "Submit for me stays as it is."}'))
                await _press(page, "settings/applying", "#set-auto")
                await page.unroute("**/api/settings")
                await page.wait_for_timeout(1700)  # (a refresh: still as they were)
                seen["refused"] = await page.evaluate(_ROWS)
                seen["unchanged"] = _written(job_apply_home)
                # a save that goes through: its row's sentence gone
                monkeypatch.setattr(desk_module, "profile_setting_problem", check)
                await _press(page, "settings/applying", "#set-accounts")
                seen["again"] = await page.evaluate(_ROWS)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["first"] == {"shown": {
        "set-auto": False, "set-practice": False, "set-accounts": True, "set-notices": False,
        "set-cookies": ["Accept if there is no other way"], "set-learn": False, "set-follow-up": "12",
        "set-email-tracking": True, "set-alert-server": "https://ntfy.example.com", "set-window": ["A browser tab"],
        "set-browser": ["Edge"]}, "errors": {}, "auto": False}, seen["first"]
    assert seen["held"] == [True], seen["held"]
    saved = {"set-auto": False, "set-practice": False, "set-accounts": False, "set-notices": True,
             "set-cookies": ["Decline"], "set-learn": True, "set-follow-up": "30", "set-email-tracking": False,
             "set-alert-server": "", "set-window": ["Its own window"], "set-browser": ["Its own"]}
    assert seen["saved"] == {"shown": saved, "errors": {}, "auto": False}, seen["saved"]
    written = {**PROFILE["settings"], "submit_mode": "review", "manage_accounts": False, "accept_notices": True,
               "accept_cookies": False, "learn_from_responses": True, "email_tracking": False, "follow_up_days": 30,
               "desk_window": "app", "browser_channel": "chromium", "phone_alerts_server": ""}
    assert seen["written"] == written, seen["written"]
    no = "The desk said no."
    assert seen["refused"] == {"shown": saved, "errors": {
        "set-follow-up": "Follow-up days is a whole number from 1 to 90.", "set-learn": no, "set-accounts": no,
        "set-cookies": no, "set-alert-server": no, "set-window": no, "set-auto": "Submit for me stays as it is."},
        "auto": False}, seen["refused"]
    assert seen["unchanged"] == written, seen["unchanged"]
    assert seen["again"]["shown"]["set-accounts"] is True, seen["again"]
    assert "set-accounts" not in seen["again"]["errors"], seen["again"]


def test_practice_mode_writes_dry_run_and_the_top_bar_says_so(srv, job_apply_home):
    """The design's D7, test 4. Practice mode on writes submit_mode: dry_run, and the top bar says "Practice mode:
    nothing is sent"; off writes review, and the top bar says the desk stops at each review page."""
    _with_settings(job_apply_home, submit_mode="review")
    desk = _idle_desk(srv)
    mode = "() => document.getElementById('mode').innerText.trim()"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/applying")
                seen = {"before": [await page.is_checked("#set-practice"), await page.evaluate(mode)]}
                await _press(page, "settings/applying", "#set-practice")
                await page.wait_for_function(f"() => ({mode})() === 'Practice mode: nothing is sent'")
                seen["on"] = [await page.is_checked("#set-practice"), await page.evaluate(mode),
                              _written(job_apply_home)["submit_mode"]]
                await _press(page, "settings/applying", "#set-practice")
                await page.wait_for_function(f"() => ({mode})() !== 'Practice mode: nothing is sent'")
                seen["off"] = [await page.is_checked("#set-practice"), await page.evaluate(mode),
                               _written(job_apply_home)["submit_mode"]]
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["before"] == [False, "Stops at each review page"], seen
    assert seen["on"] == [True, "Practice mode: nothing is sent", "dry_run"], seen
    assert seen["off"] == [False, "Stops at each review page", "review"], seen


@pytest.mark.parametrize("standalone", [True, False])
def test_rows_only_with_claude_are_absent_on_a_desk_run_on_its_own(srv, standalone):
    """The design's D7, test 5, for Job search: "Let Claude look for replies in my email" shows only when the desk
    runs inside Claude (state.standalone false)."""
    desk = _idle_desk(srv)
    desk.standalone = standalone

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/search")
                shown = await page.evaluate("""() => [...document.querySelectorAll('#panel-search .setrow')]
                    .filter((r) => r.getClientRects().length).map((r) => r.querySelector('.t').textContent)""")
                await browser.close()
                return shown
        finally:
            await desk.stop()

    shown = run(go())
    rows = ["What and where", "Search by itself every 12 hours", "Learn from what worked", "Remind me to follow up"]
    assert shown == (rows if standalone else [*rows, "Let Claude look for replies in my email"]), shown


# The look as the page has it: <html>'s data-theme, what this browser keeps, the choice pressed, the window's
# background (light #f5f6f8, dark #0f1115), and the look <html> had as the page's <body> first appeared (_FIRST_PAINT)
_LOOK = """() => ({theme: document.documentElement.dataset.theme || null,
  kept: (() => { try { return localStorage.getItem('desk-theme'); } catch (e) { return 'unreadable'; } })(),
  pressed: [...document.querySelectorAll('[data-look][aria-pressed=true]')].map((b) => b.textContent),
  bg: getComputedStyle(document.body).backgroundColor, first: window.__themeAtBody})"""
# (an init script, run before any of the page's own: it notes the look on <html> as the page's <body> first appears,
# before anything in it can be drawn)
_FIRST_PAINT = """(() => { window.__themeAtBody = 'no body yet';
  new MutationObserver((changes, watch) => { if (document.body) {
    window.__themeAtBody = document.documentElement.dataset.theme || null; watch.disconnect(); } })
    .observe(document, {childList: true, subtree: true}); })()"""
LIGHT, DARK = "rgb(245, 246, 248)", "rgb(15, 17, 21)"


async def _reloaded(page) -> None:
    await page.reload()
    await page.wait_for_function("() => document.body.dataset.panel === 'about'")
    await page.wait_for_selector("#plugin-version:not(:empty)", state="attached")


def test_look_sets_or_removes_the_theme_keeps_it_through_a_reload_and_the_first_paint_has_it(srv):
    """The design's D7, test 11. Look's Dark and Light set data-theme on <html> (the colors with it, whatever the
    computer's own look) and keep it in this browser; Match my computer removes it. A reload keeps the choice, and the
    page's <body> appears with the look already set (the small script in <head>), so the first paint has it."""
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900}, color_scheme="light")
                await page.add_init_script(_FIRST_PAINT)
                await open_desk(page, desk, "settings/about")
                seen = {"first": await page.evaluate(_LOOK)}
                await page.click("[data-look=dark]")
                seen["dark"] = await page.evaluate(_LOOK)
                await _reloaded(page)
                seen["dark again"] = await page.evaluate(_LOOK)
                await page.emulate_media(color_scheme="dark")
                await page.click("[data-look=light]")
                seen["light"] = await page.evaluate(_LOOK)
                await _reloaded(page)
                seen["light again"] = await page.evaluate(_LOOK)
                await page.click("[data-look='']")
                seen["computer's"] = await page.evaluate(_LOOK)
                await _reloaded(page)
                seen["computer's again"] = await page.evaluate(_LOOK)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    computer = {"theme": None, "kept": None, "pressed": ["Match my computer"]}
    assert seen["first"] == {**computer, "bg": LIGHT, "first": None}, seen["first"]
    assert seen["dark"] == {"theme": "dark", "kept": "dark", "pressed": ["Dark"], "bg": DARK, "first": None}, seen
    assert seen["dark again"] == {"theme": "dark", "kept": "dark", "pressed": ["Dark"], "bg": DARK,
                                  "first": "dark"}, seen["dark again"]
    assert seen["light"] == {"theme": "light", "kept": "light", "pressed": ["Light"], "bg": LIGHT,
                             "first": "dark"}, seen["light"]
    assert seen["light again"] == {"theme": "light", "kept": "light", "pressed": ["Light"], "bg": LIGHT,
                                   "first": "light"}, seen["light again"]
    assert seen["computer's"] == {**computer, "bg": DARK, "first": "light"}, seen["computer's"]
    assert seen["computer's again"] == {**computer, "bg": DARK, "first": None}, seen["computer's again"]


def test_submit_for_me_in_settings_and_in_the_apply_bar_always_agree_and_settings_asks_first(srv):
    """Settings' Submit for me (#set-auto) is the twin of the apply bar's (#auto): both drawn from the same state, a
    press on either shows on the other at once, and turning it on from Settings asks the apply bar's question first
    (declined: nothing changes)."""
    from test_desk_openings import _opening

    desk = _idle_desk(srv)
    desk.listings = [_opening(1, "Example Fab", "Field Service Engineer")]
    asks = "Submit complete applications without asking you first? You can turn this off any time."
    both = "() => [document.getElementById('set-auto').checked, document.getElementById('auto').checked]"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                said, answer = [], {"yes": False}

                async def dialog(d):
                    said.append(d.message)
                    await (d.accept() if answer["yes"] else d.dismiss())
                page.on("dialog", dialog)
                await open_desk(page, desk, "settings/applying")
                seen = {"first": await page.evaluate(both)}
                await _press(page, "settings/applying", "#set-auto")  # (asked, and declined)
                await page.wait_for_timeout(1700)  # (a refresh: the same)
                seen["declined"] = [await page.evaluate(both), desk.applier.auto_submit, list(said)]
                answer["yes"] = True
                await _press(page, "settings/applying", "#set-auto")
                seen["on"] = [await page.evaluate(both), desk.applier.auto_submit, list(said)]
                # the apply bar's, turned off: Settings' shows it at once
                await press_menu(page, "openings")
                await page.check("[data-pick='https://careers.example.com/1']")
                await page.wait_for_selector(".applybar", state="visible")
                await page.click("#auto")
                seen["off now"] = await page.evaluate(both)
                await page.wait_for_timeout(1700)  # (a refresh: the same)
                seen["off"] = [await page.evaluate(both), desk.applier.auto_submit]
                # and on again there: asked, and Settings' shows it
                await page.click("#auto")
                seen["on now"] = await page.evaluate(both)
                await page.wait_for_timeout(1700)
                seen["on there"] = [await page.evaluate(both), desk.applier.auto_submit, len(said)]
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["first"] == [False, False], seen
    assert seen["declined"] == [[False, False], False, [asks]], seen
    assert seen["on"] == [[True, True], True, [asks, asks]], seen
    assert seen["off now"] == [False, False], seen
    assert seen["off"] == [[False, False], False], seen
    assert seen["on now"] == [True, True], seen
    assert seen["on there"] == [[True, True], True, 3], seen


# ---------------------------------------------------------------- Email codes, Resume and cover letters (package 15)

APP_PASSWORD = "wxyz abcd efgh ijkl"  # (a made-up app password, as Gmail shows one)
LETTER_PDF = b"%PDF-1.4\n% a made-up cover letter\n"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(zipfile.ZipInfo("word/document.xml", (2026, 1, 1, 0, 0, 0)), "<w:document/>")
    return out.getvalue()


async def _pick(page, button: str, name: str, data: bytes, mime: str) -> None:
    """A file picked with a row's button (the system's file window, answered with this file), and its save over."""
    async with page.expect_file_chooser() as chooser:
        await page.click(button)
    await (await chooser.value).set_files({"name": name, "mimeType": mime, "buffer": data})
    await page.wait_for_function(_SAVED)
    await page.wait_for_timeout(300)  # (and the state after it drawn)


_WRITERS = {
    "key": "Written through Anthropic's API with your key, about 10 to 20 cents a letter. The first one waits for your "
           "OK.",
    "claude": "Written through your own Claude sign-in. The first one waits for your OK.",
    "neither": "This needs an Anthropic API key. Save one under Accounts and passwords.",
}


@pytest.mark.parametrize("writer, standalone", [("key", False), ("key", True), ("claude", False), ("neither", True)])
def test_the_cover_letter_rows_sentence_says_what_would_write_the_letters(srv, monkeypatch, writer, standalone):
    """The design's D7, test 6. Write a cover letter for each job says what would write the letters: an Anthropic API
    key saved (inside Claude or not), the person's Claude sign-in (no key, inside Claude), or that it needs a key (no
    key, a desk run on its own), its switch disabled then. The key is never on the page."""
    from job_apply import config

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if writer == "key":
        config.save_site_password(config.API_KEY_NAME, "sk-ant-not-a-real-key")
    desk = _idle_desk(srv)
    desk.standalone = standalone

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/documents")
                seen = await page.evaluate("""() => [document.getElementById('set-letters-said').textContent,
                    document.getElementById('set-letters').disabled,
                    document.documentElement.outerHTML.includes('sk-ant')]""")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    assert run(go()) == [_WRITERS[writer], writer == "neither", False]


@pytest.mark.parametrize("standalone", [True, False])
def test_email_codes_and_documents_rows_only_with_claude_are_absent_on_a_desk_run_on_its_own(srv, standalone):
    """The design's D7, test 5, for Email codes and Resume and cover letters: "Let Claude read codes through Gmail" and
    "Tailor my resume for each job" show only when the desk runs inside Claude (state.standalone false)."""
    desk = _idle_desk(srv)
    desk.standalone = standalone
    rows = """(panel) => [...document.querySelectorAll(`#panel-${panel} .setrow`)]
        .filter((r) => r.getClientRects().length && r.querySelector('.t'))
        .map((r) => r.querySelector('.t').textContent)"""

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/email")
                shown = {"email": await page.evaluate(rows, "email")}
                await press_menu(page, "settings/documents")
                shown["documents"] = await page.evaluate(rows, "documents")
                await browser.close()
                return shown
        finally:
            await desk.stop()

    shown = run(go())
    email = ["Your email's app password", "Test it"]
    documents = ["Your resume", "Your usual cover letter", "Write a cover letter for each job"]
    if standalone:
        assert shown == {"email": email, "documents": documents}, shown
    else:
        assert shown == {"email": [*email, "Let Claude read codes through Gmail"],
                         "documents": [*documents[:2], "Tailor my resume for each job", documents[2]]}, shown


# What the rows of Email codes and Resume and cover letters show: each switch on or off, the letters' model's box, and
# the sentence in red under a row where one shows, by its control's id; and the apply bar's twins
_MY_ROWS = """() => {
  const $ = (id) => document.getElementById(id);
  const err = (el) => { const e = el.closest('.setrow').querySelector('.set-err');
    return e && !e.hidden ? e.textContent : null; };
  const ids = ['set-email-codes', 'set-tailor', 'set-letters', 'set-letter-model'];
  return {
    shown: Object.fromEntries(ids.map((id) => [id, $(id).type === 'checkbox' ? $(id).checked : $(id).value])),
    errors: Object.fromEntries(ids.map((id) => [id, err($(id))]).filter(([, e]) => e)),
    twins: [$('tailor').checked, $('letters').checked],
  };
}"""


def test_email_codes_and_documents_switches_show_the_state_save_on_a_press_and_go_back_after_a_refused_save(
        srv, job_apply_home, monkeypatch):
    """The design's D7, test 3, for Email codes and Resume and cover letters: Let Claude read codes through Gmail
    (email_codes), Tailor my resume and Write a cover letter (the desk's own, the twins of the apply bar's) and the
    letters' model (cover_letter_model, saved when its box is left) show the state, save at once (held while saving),
    and after a refused save are back where they were with the desk's sentence under the row; nothing changes then."""
    from job_apply import desk as desk_module

    _with_settings(job_apply_home, email_codes=True, cover_letter_model="")
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/email")
                seen = {"first": await page.evaluate(_MY_ROWS)}
                held = []

                async def look(route):
                    held.append(await page.evaluate("() => document.getElementById('set-letters').disabled"))
                    await route.continue_()
                await page.route("**/api/settings", look)
                await _press(page, "settings/documents", "#set-letters")
                await page.unroute("**/api/settings", look)
                seen["held"] = held
                await _press(page, "settings/email", "#set-email-codes")
                await _press(page, "settings/documents", "#set-tailor")
                await page.click("#letter-model-h")  # (Which model writes the letters, opened)
                await _type(page, "settings/documents", "#set-letter-model", "claude-opus-5-5")
                await page.wait_for_timeout(1700)  # (a refresh: the same)
                seen["saved"] = await page.evaluate(_MY_ROWS)
                seen["written"] = _written(job_apply_home)
                seen["desk"] = [desk.applier.tailoring_on(), desk.applier.letters]
                # refused: the profile's (the check made to refuse), and the desk's own (its route made to refuse)
                monkeypatch.setattr(desk_module, "profile_setting_problem", lambda key, value: "The desk said no.")
                await _type(page, "settings/documents", "#set-letter-model", "claude-sonnet-5")
                await _press(page, "settings/email", "#set-email-codes")
                await page.route("**/api/settings", lambda route: route.fulfill(
                    status=400, content_type="application/json", body='{"error": "That stays as it is."}'))
                await _press(page, "settings/documents", "#set-tailor")
                await _press(page, "settings/documents", "#set-letters")
                await page.unroute("**/api/settings")
                await page.wait_for_timeout(1700)  # (a refresh: still as they were)
                seen["refused"] = await page.evaluate(_MY_ROWS)
                seen["unchanged"] = _written(job_apply_home)
                seen["desk again"] = [desk.applier.tailoring_on(), desk.applier.letters]
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["first"] == {"shown": {"set-email-codes": True, "set-tailor": False, "set-letters": False,
                                       "set-letter-model": ""}, "errors": {}, "twins": [False, False]}, seen["first"]
    assert seen["held"] == [True], seen["held"]
    saved = {"set-email-codes": False, "set-tailor": True, "set-letters": True, "set-letter-model": "claude-opus-5-5"}
    assert seen["saved"] == {"shown": saved, "errors": {}, "twins": [True, True]}, seen["saved"]
    assert seen["written"] == {**PROFILE["settings"], "email_codes": False,
                               "cover_letter_model": "claude-opus-5-5"}, seen["written"]
    assert seen["desk"] == [True, True], seen["desk"]
    no = "The desk said no."
    assert seen["refused"] == {"shown": saved, "errors": {
        "set-letter-model": no, "set-email-codes": no, "set-tailor": "That stays as it is.",
        "set-letters": "That stays as it is."}, "twins": [True, True]}, seen["refused"]
    assert seen["unchanged"] == seen["written"], seen["unchanged"]
    assert seen["desk again"] == [True, True], seen["desk again"]


def test_the_email_app_password_saves_shows_saved_with_the_box_empty_and_is_nowhere_in_the_page(srv, monkeypatch):
    """Email codes' app password: Save with nothing typed says so under the row; typed and saved (the Site passwords
    card's own call, /api/password), the row says Saved and the box is empty, and the password is in no part of the
    page's state, its words, its boxes or its console afterwards. Test it now is disabled with none saved; pressed, it
    says each of its two results in the design's words (the sign-in made to answer through the test's desk)."""
    from job_apply import config, mailbox

    answers = iter([None, mailbox.SIGN_IN_SAID["refused"]])
    tried = []

    def sign_in(address, password, wait=mailbox.SIGN_IN_TEST_WAIT):
        tried.append(password == APP_PASSWORD)
        return next(answers)
    monkeypatch.setattr(mailbox, "try_sign_in", sign_in)
    desk = _idle_desk(srv)
    shown = """() => ({state: document.getElementById('mail-pw-state').textContent,
      box: document.getElementById('mail-pw').value, test: document.getElementById('mail-test').disabled,
      said: document.getElementById('mail-test-said').textContent,
      err: [...document.querySelectorAll('#panel-email .set-err')].filter((e) => !e.hidden)
        .map((e) => e.textContent)})"""

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                logged = []
                page.on("console", lambda m: logged.append(m.text))
                await open_desk(page, desk, "settings/email")
                seen = {"first": await page.evaluate(shown)}
                await page.click("#mail-pw-save")
                await page.wait_for_function(_SAVED)
                seen["empty"] = await page.evaluate(shown)
                await page.fill("#mail-pw", APP_PASSWORD)
                await page.keyboard.press("Enter")  # (Save, by the keyboard)
                await page.wait_for_function("() => document.getElementById('mail-pw-state').textContent === 'Saved'")
                seen["saved"] = await page.evaluate(shown)
                seen["kept"] = config.get_secret("email_password")
                seen["anywhere"] = await page.evaluate("""(pw) => [JSON.stringify(state).includes(pw),
                    document.documentElement.outerHTML.includes(pw), document.body.innerText.includes(pw),
                    [...document.querySelectorAll('input')].some((i) => i.value.includes(pw))]""", APP_PASSWORD)
                await page.click("#mail-test")
                await page.wait_for_function("() => document.getElementById('mail-test-said').textContent")
                seen["works"] = await page.evaluate(shown)
                await page.click("#mail-test")
                await page.wait_for_function("() => document.getElementById('mail-test-said').textContent"
                                             ".startsWith('It didn')")
                seen["refused"] = await page.evaluate(shown)
                seen["logged"] = any(APP_PASSWORD in line for line in logged)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["first"] == {"state": "Not saved", "box": "", "test": True, "said": "", "err": []}, seen["first"]
    assert seen["empty"] == {**seen["first"], "err": ["Type the app password first."]}, seen["empty"]
    assert seen["saved"] == {"state": "Saved", "box": "", "test": False, "said": "", "err": []}, seen["saved"]
    assert seen["kept"] == APP_PASSWORD
    assert seen["anywhere"] == [False, False, False, False], seen["anywhere"]
    assert seen["works"]["said"] == "It works. The desk signed in to your inbox and read nothing.", seen["works"]
    assert seen["refused"]["said"] == "It didn't work: your email provider refused the password.", seen["refused"]
    assert tried == [True, True] and not seen["logged"], (tried, seen["logged"])
    assert APP_PASSWORD not in json.dumps(desk.state())


def test_the_usual_cover_letter_and_the_resume_are_picked_named_and_removed_on_the_page(srv, job_apply_home):
    """Resume and cover letters: the usual cover letter picked (a PDF, then a Word file) shows its file's name and
    Remove, and Remove takes it away (the file stays); a file the desk refuses says the desk's sentence under the row,
    and nothing changes; Your resume names the resume, and Replace replaces it."""
    desk = _idle_desk(srv)
    shown = """() => { const $ = (id) => document.getElementById(id);
      return {resume: $('set-resume-said').textContent, button: $('set-resume').textContent,
        letter: $('set-letter-file').hidden ? null : $('set-letter-file').textContent,
        remove: !$('set-letter-remove').hidden,
        err: [...document.querySelectorAll('#panel-documents .set-err')].filter((e) => !e.hidden)
          .map((e) => e.textContent)}; }"""

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/documents")
                seen = {"first": await page.evaluate(shown)}
                await _pick(page, "#set-letter", "My letter.pdf", LETTER_PDF, "application/pdf")
                seen["pdf"] = await page.evaluate(shown)
                await _pick(page, "#set-letter", "My letter.docx", _docx(), DOCX_TYPE)
                seen["docx"] = await page.evaluate(shown)
                await _pick(page, "#set-letter", "letter.txt", b"Dear hiring manager", "text/plain")
                seen["refused"] = await page.evaluate(shown)
                seen["kept"] = _profile_documents(job_apply_home)
                await page.click("#set-letter-remove")
                await page.wait_for_function(_SAVED)
                await page.wait_for_function("() => document.getElementById('set-letter-remove').hidden")
                seen["removed"] = await page.evaluate(shown)
                await _pick(page, "#set-resume", "My resume.docx", _docx(), DOCX_TYPE)
                seen["resume"] = await page.evaluate(shown)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["first"] == {"resume": "resume.pdf", "button": "Replace", "letter": None, "remove": False,
                             "err": []}, seen["first"]
    assert seen["pdf"] == {**seen["first"], "letter": "cover_letter.pdf", "remove": True}, seen["pdf"]
    assert seen["docx"] == {**seen["first"], "letter": "cover_letter.docx", "remove": True}, seen["docx"]
    refused = "Pick your cover letter as a PDF or a Word (.docx) file"  # (the desk's sentence, as it says it)
    assert seen["refused"] == {**seen["docx"], "err": [refused]}, seen["refused"]
    assert seen["kept"]["cover_letter"].endswith("cover_letter.docx"), seen["kept"]
    assert seen["removed"] == seen["first"], seen["removed"]  # (a save of the row that goes through: its sentence gone)
    assert seen["resume"] == {**seen["first"], "resume": "resume.docx"}, seen["resume"]
    documents = _profile_documents(job_apply_home)
    assert documents["cover_letter"] == "" and documents["resume"].endswith("resume.docx"), documents
    assert (job_apply_home / "cover_letter.docx").read_bytes() == _docx()  # (removed from the profile, the file kept)


def _profile_documents(home) -> dict:
    return yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8"))["documents"]


def test_tailor_and_cover_letter_in_settings_and_in_the_apply_bar_always_agree(srv):
    """Settings' Tailor my resume (#set-tailor) and Write a cover letter (#set-letters) are the twins of the apply bar's
    #tailor and #letters: both drawn from the same state, and a press on either shows on the other at once."""
    from test_desk_openings import _opening

    desk = _idle_desk(srv)
    desk.listings = [_opening(1, "Example Fab", "Field Service Engineer")]
    both = "() => ['set-tailor', 'tailor', 'set-letters', 'letters'].map((id) => document.getElementById(id).checked)"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                await open_desk(page, desk, "settings/documents")
                seen = {"first": await page.evaluate(both)}
                await _press(page, "settings/documents", "#set-tailor")
                await _press(page, "settings/documents", "#set-letters")
                seen["on"] = [await page.evaluate(both), desk.applier.tailoring_on(), desk.applier.letters]
                await press_menu(page, "openings")
                await page.check("[data-pick='https://careers.example.com/1']")
                await page.wait_for_selector(".applybar", state="visible")
                await page.click("#letters")
                seen["off now"] = await page.evaluate(both)
                await page.wait_for_timeout(1700)  # (a refresh: the same)
                seen["off"] = [await page.evaluate(both), desk.applier.letters]
                await page.click("#tailor")
                await page.wait_for_timeout(1700)
                seen["tailor off"] = [await page.evaluate(both), desk.applier.tailoring_on()]
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["first"] == [False, False, False, False], seen
    assert seen["on"] == [[True, True, True, True], True, True], seen
    assert seen["off now"] == [True, True, False, False], seen
    assert seen["off"] == [[True, True, False, False], False], seen
    assert seen["tailor off"] == [[False, False, False, False], False], seen


# The Safety window's lines whose "where" names a panel (the design's D4), each with the panel its link opens
SAFETY_PANELS = {"practice": "applying", "auto_submit": "applying", "accept_notices": "applying",
                 "manage_accounts": "applying", "accept_cookies": "applying", "email_codes": "email",
                 "email_password": "email"}
# Each line of the Safety window as drawn: its key, its "where" line's words, and the link in it (its address, its
# words), or null
_WHERE = """() => [...document.querySelectorAll('#safety-body li')].map((li) => {
  const w = li.querySelector('.where'), a = w.querySelector('a');
  return {key: li.dataset.safety, said: w.textContent, link: a && [a.getAttribute('href'), a.textContent]};
})"""
_WHERE_NOW = """() => [document.getElementById('safety').open, document.body.dataset.page, document.body.dataset.panel,
  document.querySelector('.snav a[aria-current=page]').getAttribute('href'), location.hash]"""


def test_each_where_in_the_safety_window_links_to_its_panel_and_pressing_it_opens_the_panel(srv, job_apply_home):
    """The design's D7, test 13. In the Safety window, each "where" of D4 is a link to its panel (its words the
    "where" itself, said as before: "Where: Settings, Applying: Practice mode."); a "where" naming no panel ("Not a
    setting: always so") stays words. Pressing one closes the window and opens that panel; so does pressing it by
    the keyboard."""
    desk = _idle_desk(srv)
    lines = {line["key"]: line["where"] for line in desk.safety()["lines"]}
    by_name = {name: panel for panel, name in PANELS.items()}

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/applying")
                seen = {}
                await page.click("#safety-btn")
                await page.wait_for_selector("#safety[open] #safety-body li")
                seen["where"] = await page.evaluate(_WHERE)
                # pressed: the window closes, Email codes opens
                await page.click("#safety-body li[data-safety=email_codes] .where a")
                await page.wait_for_function("() => document.body.dataset.panel === 'email'")
                seen["email"] = await page.evaluate(_WHERE_NOW)
                # from Email codes (its own address), the window again, and Practice mode's link pressed by the
                # keyboard: Applying opens
                await page.evaluate("() => document.getElementById('safety-btn').click()")
                await page.wait_for_selector("#safety[open] #safety-body li")
                await page.focus("#safety-body li[data-safety=practice] .where a")
                await page.keyboard.press("Enter")
                await page.wait_for_function("() => document.body.dataset.panel === 'applying'")
                seen["practice"] = await page.evaluate(_WHERE_NOW)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    where = {w["key"]: w for w in seen["where"]}
    assert list(where) == list(lines), where
    for key, said in lines.items():
        assert where[key]["said"] == f"Where: {said}.", where[key]
        named = said.removeprefix("Settings, ").split(":")[0] if said.startswith("Settings, ") else None
        if named in by_name:
            assert where[key]["link"] == [f"#settings/{by_name[named]}", said], where[key]
        else:
            assert where[key]["link"] is None, where[key]
    for key, panel in SAFETY_PANELS.items():
        if lines[key].startswith("Settings, "):  # (practice mode set by the environment comes before Settings)
            assert where[key]["link"] and where[key]["link"][0] == f"#settings/{panel}", where[key]
    assert where["email_codes"]["link"] == ["#settings/email", "Settings, Email codes"]
    assert where["boards"]["link"] is None and where["bot_checks"]["link"] is None
    assert seen["email"] == [False, "settings", "email", "#settings/email", "#settings/email"], seen["email"]
    assert seen["practice"] == [False, "settings", "applying", "#settings/applying", "#settings/applying"], seen


def test_the_note_about_accounts_names_the_switch_and_keep_it_on_saves_it(srv, job_apply_home):
    """Plain wording, Version D: the blue note about accounts (shown while the profile doesn't say) tells the person
    to turn off Make my accounts in Settings, under Applying, and names no file or key; its Keep it on writes
    manage_accounts: true to the profile, and the note goes."""
    path = job_apply_home / "profile.yaml"
    prof = yaml.safe_load(path.read_text(encoding="utf-8"))
    prof["settings"].pop("manage_accounts")
    path.write_text(yaml.safe_dump(prof), encoding="utf-8")
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "home")
                note = page.locator("#notices p", has_text="The desk now handles your job-site accounts")
                said = await note.inner_text()
                await note.locator("[data-act=accounts-keep]").click()
                await note.wait_for(state="detached")
                await browser.close()
                return said
        finally:
            await desk.stop()

    said = " ".join(run(go()).split())
    assert ("To keep doing these yourself, turn off Make my accounts in Settings, under Applying. Keep it on"
            in said), said
    assert "manage_accounts" not in said and "profile.yaml" not in said and "saved below" not in said, said
    assert _written(job_apply_home)["manage_accounts"] is True


@pytest.mark.parametrize("standalone", [False, True])
def test_the_passwords_and_email_codes_words_name_claude_only_where_claude_is(srv, standalone):
    """Plain wording, Version D: the Accounts and passwords card's first paragraph (E1's first sentence since Version
    E) ends "You can see any of them here." without Claude, and at "where it has none." with Claude, where the line
    under it says passwords are shown only on a desk opened on its own; its long paragraph names Make my accounts and
    no file, and its paragraph about email is in the Email codes panel's How this works, whose sentence about Claude
    reading codes through Gmail shows only with Claude."""
    desk = _idle_desk(srv)
    desk.standalone = standalone

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/passwords")
                await page.evaluate("() => document.querySelectorAll('details').forEach((d) => { d.open = true; })")
                seen = {"first": await page.inner_text("#pw-said"),
                        "card": await page.inner_text("section[aria-labelledby=pw-h]"),
                        "inside": await page.is_visible("#pw-inside")}
                await press_menu(page, "settings/email")
                seen["email"] = await page.inner_text("#panel-email details.setmore")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    start = ("One strong password for each job system. The desk makes them on this computer and keeps them in your "
             "computer's password vault, or in a file only you can read where it has none.")
    assert " ".join(seen["first"].split()) == start + (" You can see any of them here." if standalone else ""), seen
    assert seen["inside"] is not standalone, seen
    card = " ".join(seen["card"].split())
    assert ("Passwords are shown only when the Job Desk is opened on its own, not inside Claude." in card) \
        is not standalone, card
    # (the review of the panel: signing in always, an account and a reset only with Make my accounts on)
    assert ("With it, the desk signs you in at each of that job system's employers. Unless Make my accounts is off in "
            "Settings, it also creates your account there (agreeing to the site's terms for you), and when a site "
            "turns the password down, it resets that account's password to this one through the site's "
            "\"Forgot password\" email.") in card, card
    # (How this works fits a password the desk makes and one typed: the review of the panel)
    assert ("desktop's keyring), or in a file only you can read where it has none. A password the desk makes is used "
            "nowhere else. If you type your own, use one you use nowhere else.") in card, card
    assert card.endswith("(about 10 to 20 cents a letter, written and checked)."), card
    assert "Your email: with an app password" not in card, card
    assert "manage_accounts" not in card and "secrets.yaml" not in card, card
    email = " ".join(seen["email"].split())
    assert email.startswith("How this works Your email: with an app password for the address in your profile"), email
    assert ("Claude reads it through Gmail" in email) is not standalone, email
    assert "email_codes" not in email, email
