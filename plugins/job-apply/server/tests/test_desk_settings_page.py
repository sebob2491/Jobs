"""The Settings page (the design's Version D, D1): a menu of eight panels at the left, the one chosen at its right, each
panel with an address of its own (#settings/<panel>), so a message elsewhere can link straight to it. Each test opens
the real page in a browser. Made-up values only: the repo is public."""

import pytest
from conftest import browser_available, open_desk, person_browser, press_menu, run
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
    control, then the next, as they read (the Applying panel: Jobs at once's 1 to 5, Don't wait for me, the Safety
    window's button, For fixing the desk). Enter on a link opens its panel."""
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
                for _ in range(len(PANELS) + 8):
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
    assert order == [*(f"#settings/{panel}" for panel in PANELS), "at-once-1", "at-once-2", "at-once-3", "at-once-4",
                     "at-once-5", "dont-wait", "safety-btn", "For fixing the desk"], order
    assert pressed == "#settings/passwords", pressed
    assert ring == "solid", ring  # (the focus ring shows on a menu link reached by the keyboard)
    assert then == ["#settings/email", "#settings/phone", "#settings/documents", "#settings/about", "pw-site"], then
