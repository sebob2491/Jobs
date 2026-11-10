"""The Settings page (the design's Version D, D1): a menu of eight panels at the left, the one chosen at its right, each
panel with an address of its own (#settings/<panel>), so a message elsewhere can link straight to it. Each test opens
the real page in a browser. Made-up values only: the repo is public."""

import asyncio

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
# heading for now
HOLDS = {"profile": "setup-btn", "search": "auto-search", "applying": "dont-wait", "passwords": "pw-form",
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
    assert then == ["#settings/email", "#settings/phone", "#settings/documents", "#settings/about", "pw-site"], then


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
