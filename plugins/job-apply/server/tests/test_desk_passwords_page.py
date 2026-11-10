"""Passwords made for you, the page half (the design's Version E): Settings' Accounts and passwords panel (E4), a row
for each job system with its words and buttons, Show and Copy, the two questions asked first, the panel inside Claude
and with the password vault not answering (E7's tests 9, 10 and 11, and the brief's three changes), and the driver's
six sentences that now say where to see the password (E5, E7's test 13). Every password here is made in the test or
plainly made up, and every vault is conftest's, in memory."""

import ast
import time

import pytest
from conftest import browser_available, open_desk, person_browser, press_menu, run
from test_passwords import on_day, put_out_of_use
from test_pause_messages import _driver_source

from job_apply import config, pipeline
from job_apply.desk import Desk
from job_apply.pipeline import Run

needs_browser = pytest.mark.skipif(not browser_available(), reason="no browser")

E1_FIRST = ("One strong password for each job system. The desk makes them on this computer and keeps them in your "
            "computer's password vault, or in a file only you can read where it has none.")
E1_SEE = " You can see any of them here."
E1_ON = ("If you already have an account at an employer with a different password, the desk resets that account's "
         "password to this one through the site's own Forgot password email, the first time it signs in there.")
E1_OFF = ("If you already have an account at an employer with a different password, the desk can't sign in there "
          "until you set this password at the site yourself.")
INSIDE = "Passwords are shown only when the Job Desk is opened on its own, not inside Claude."
ASK_NEW = ("Make a new Workday password? At each Workday employer where you have an account, the desk resets the "
           "password to the new one the next time it signs in.")
ASK_FORGET = ("Forget the Workday password? The desk stops signing in at Workday employers until one is saved again. "
              "Your accounts at the sites are not touched.")
TYPED = "made-up-typed-password-1"  # (plainly not a real one)
WORKDAY_JOB = "https://example.wd5.myworkdayjobs.com/en-US/careers/job/Tech_R1"

# Each row of the table: its job system, its name, what it says of the password, its buttons (an icon button by its
# label) and its More menu's
_ROWS = """() => [...document.querySelectorAll('#pw-rows [data-pw]')].map((r) => ({
  site: r.dataset.pw, name: r.querySelector('.t').innerText, said: r.querySelector('.pill').innerText,
  buttons: [...r.querySelectorAll('.c-acts > button, .c-acts > details > summary, .pw-shown > button')]
    .map((b) => b.getAttribute('aria-label') || b.innerText.trim()),
  menu: [...r.querySelectorAll('.menu-panel button')].map((b) => b.textContent.trim()),
  error: (r.querySelector('.set-err') || {}).textContent || ''}))"""
# Where a password could be left on the page: any attribute (a data-html string among them), storage, the state, the
# address or title, the page's words and the toast, and how many boxes hold it
_LEFT = """(pw) => ({
  attrs: [...document.querySelectorAll('*')].some((el) => [...el.attributes].some((a) => a.value.includes(pw))),
  storage: [localStorage, sessionStorage].some((s) => Object.keys(s).some((k) => k.includes(pw)
    || String(s.getItem(k)).includes(pw))),
  state: JSON.stringify(state).includes(pw),
  address: location.href.includes(pw) || document.title.includes(pw),
  words: document.body.innerText.includes(pw) || document.getElementById('toast').textContent.includes(pw),
  boxes: [...document.querySelectorAll('input, textarea')].filter((b) => b.value.includes(pw)).length})"""
NOWHERE = {"attrs": False, "storage": False, "state": False, "address": False, "words": False, "boxes": 0}
# (only in the shown box's value)
IN_THE_BOX = {**NOWHERE, "boxes": 1}


def _desk(srv, standalone: bool = True) -> Desk:
    """A desk that drives nothing, run on its own (as Show needs) unless not `standalone` (inside Claude)."""
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())  # (recent: opening the page searches nothing)
    desk.standalone = standalone
    return desk


async def _make(desk: Desk, name: str) -> str:
    """A password the desk makes for `name`, as Make one makes it (its day kept), and what it is."""
    assert config.make_site_password(name)
    desk.made_passwords[name] = "2026-11-10"
    return str(config.get_secret(name))


async def _rows(page) -> list[dict]:
    return await page.evaluate(_ROWS)


async def _row(page, site: str) -> dict:
    return next(r for r in await _rows(page) if r["site"] == site)


# ------------------------------------------------------------------ E7's test 9: the rows, Show and its letting go
@needs_browser
def test_each_row_says_its_password_in_its_three_states_with_its_buttons(srv, job_apply_home, monkeypatch):
    """E4: E1's first sentence (with "You can see any of them here." on a desk on its own) and the one that fits Make
    my accounts at the top, Make my passwords while any job system has none, then a row for each job system in the
    desk's order: "Made by the desk on Nov 10" or "Saved by you" with Show, Copy and More (Type my own, Make a new one,
    Forget), "None yet" with Make one and Type my own. Make one makes that one; Make my passwords the rest, and goes.
    Under the table, the API key's row."""
    on_day(monkeypatch, "2026-11-10")
    desk = _desk(srv)
    config.save_site_password("successfactors_password", TYPED)

    async def go():
        await desk.start(port=0, open_browser=False)
        seen: dict = {}
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/passwords")
                await page.click("[data-pw=workday] [data-pw-act=make]")  # Make one
                await page.wait_for_function("() => document.querySelector('[data-pw=workday] .pill').innerText"
                                             ".startsWith('Made')")
                seen["rows"] = await _rows(page)
                seen["said"] = " ".join((await page.inner_text("#pw-said")).split())
                seen["accounts"] = await page.inner_text("#pw-accounts")
                seen["inside"] = await page.is_visible("#pw-inside")
                seen["make_all"] = await page.is_visible("#pw-make-all")
                seen["key"] = " ".join((await page.inner_text("#pw-key")).split())
                seen["heads"] = await page.eval_on_selector_all(".pw-head [role=columnheader]",
                                                                "hs => hs.map((h) => h.innerText)")
                await page.click("#pw-make-all")
                await page.wait_for_selector("#pw-make-row", state="hidden")
                seen["all"] = await _rows(page)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    accounts = desk.state()["settings"]["manage_accounts"]
    assert seen["said"] == E1_FIRST + E1_SEE, seen["said"]
    assert seen["accounts"] == (E1_ON if accounts else E1_OFF), seen["accounts"]
    assert seen["inside"] is False and seen["make_all"] is True, seen
    assert seen["heads"] == ["Job system", "Password", ""], seen["heads"]
    rows = seen["rows"]
    assert [r["site"] for r in rows] == [n.removesuffix("_password") for n in config.DESK_PASSWORDS], rows
    assert [r["name"] for r in rows] == list(config.DESK_PASSWORDS.values()), rows
    saved = {"buttons": ["Show", "Copy", "More"], "menu": ["Type my own", "Make a new one", "Forget"], "error": ""}
    assert rows[0] == {"site": "workday", "name": "Workday", "said": "Made by the desk on Nov 10", **saved}, rows[0]
    assert rows[1] == {"site": "successfactors", "name": "SuccessFactors", "said": "Saved by you", **saved}, rows[1]
    for r in rows[2:]:
        assert {k: r[k] for k in ("said", "buttons", "menu", "error")} == {
            "said": "None yet", "buttons": ["Make one", "Type my own"], "menu": [], "error": ""}, r
    assert seen["key"].startswith("Anthropic API key Optional. For cover letters and for reading your resume, "
                                  "without Claude. Not saved Save a key"), seen["key"]
    assert "Show" not in seen["key"] and "Copy" not in seen["key"], seen["key"]
    assert [r["said"] for r in seen["all"]] == ["Made by the desk on Nov 10", "Saved by you"] + [
        "Made by the desk on Nov 10"] * (len(rows) - 2), seen["all"]
    assert config.get_secret("successfactors_password") == TYPED  # (a saved one is left alone)


@needs_browser
def test_a_shown_password_lives_only_in_its_box_and_is_let_go_every_way(srv, job_apply_home, monkeypatch):
    """E4's paragraph on a shown password, and E7's test 9: Show draws it in a read-only box (autocomplete off, no
    spellcheck) and nowhere else; it's let go, and is then in no attribute, no storage, not the state and not the
    page, on Hide, after 30 seconds (the clock faked: still there at 29), on leaving the panel, when the page is
    hidden, and when its row is drawn again."""
    desk = _desk(srv)

    async def go():
        made = await _make(desk, "workday_password")
        await desk.start(port=0, open_browser=False)
        seen: dict = {}

        async def show(page) -> dict:
            await page.click("[data-pw=workday] [data-pw-act=show]")
            await page.wait_for_function("(pw) => { const b = document.querySelector('[data-pw-box]');"
                                         " return !!b && b.value === pw; }", arg=made)
            return await page.evaluate(_LEFT, made)

        async def gone(page) -> dict:
            # (at once, or nearly: never as late as the 30 seconds, whose own timer would clear it whatever the page
            # did; the review of the panel)
            await page.wait_for_function("() => !document.querySelector('[data-pw-box]')", timeout=3000)
            return await page.evaluate(_LEFT, made)

        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await page.clock.install()
                await open_desk(page, desk, "settings/passwords")
                seen["shown"] = await show(page)
                seen["box"] = await page.eval_on_selector("[data-pw-box]", """(b) => ({readonly: b.readOnly,
                    autocomplete: b.getAttribute('autocomplete'), spellcheck: b.getAttribute('spellcheck'),
                    label: b.getAttribute('aria-label'), focused: document.activeElement === b})""")
                seen["while"] = await _row(page, "workday")
                await page.wait_for_timeout(2000)  # (refreshes meanwhile leave it shown)
                seen["refreshed"] = await page.evaluate(_LEFT, made)
                await page.click("[data-pw=workday] [data-pw-act=hide]")
                seen["hide"] = await gone(page)
                seen["after_hide"] = await _row(page, "workday")

                await show(page)
                await page.clock.fast_forward(29000)
                seen["at_29"] = await page.evaluate(_LEFT, made)
                await page.clock.fast_forward(1500)
                seen["at_30"] = await gone(page)
                # (the keyboard on the row's Show, never dropped to the page)
                seen["focus_30"] = await page.evaluate("(document.activeElement.dataset || {}).pwAct || "
                                                       "document.activeElement.tagName")

                await show(page)
                await press_menu(page, "settings/email")
                seen["left_panel"] = await gone(page)
                await press_menu(page, "settings/passwords")
                await show(page)
                await press_menu(page, "home")
                seen["left_settings"] = await gone(page)
                await press_menu(page, "settings/passwords")

                await show(page)
                await page.evaluate("""() => { Object.defineProperty(document, 'visibilityState',
                    {value: 'hidden', configurable: true}); document.dispatchEvent(new Event('visibilitychange')); }""")
                seen["page_hidden"] = await gone(page)
                await page.evaluate("() => { delete document.visibilityState; }")

                await show(page)
                config.save_site_password("workday_password", TYPED)  # (typed elsewhere: its row says so)
                desk.made_passwords.pop("workday_password", None)
                await page.wait_for_function("() => document.querySelector('[data-pw=workday] .pill').innerText"
                                             " === 'Saved by you'")
                seen["drawn_again"] = await gone(page)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["shown"] == IN_THE_BOX and seen["refreshed"] == IN_THE_BOX, seen
    assert seen["box"] == {"readonly": True, "autocomplete": "off", "spellcheck": "false",
                           "label": "Workday password", "focused": True}, seen["box"]
    assert seen["while"]["buttons"] == ["Copy", "More", "Hide"], seen["while"]
    assert seen["after_hide"]["buttons"] == ["Show", "Copy", "More"], seen["after_hide"]
    assert seen["at_29"] == IN_THE_BOX, seen["at_29"]
    assert seen["focus_30"] == "show", seen["focus_30"]
    for way in ("hide", "at_30", "left_panel", "left_settings", "page_hidden", "drawn_again"):
        assert seen[way] == NOWHERE, (way, seen[way])


# ------------------------------------------------------------------ E7's test 10: Copy
@needs_browser
def test_copy_puts_the_password_on_the_clipboard_and_never_draws_it(srv, job_apply_home):
    """Copy asks the desk for the password and puts it on the clipboard, says "Copied.", and draws it nowhere: no box,
    no attribute, no storage, not the state. Where the browser has no clipboard, it says so in a sentence under the
    row and draws nothing."""
    desk = _desk(srv)

    async def go():
        made = await _make(desk, "workday_password")
        await desk.start(port=0, open_browser=False)
        seen: dict = {}
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900},
                                              permissions=["clipboard-read", "clipboard-write"])
                await open_desk(page, desk, "settings/passwords")
                await page.click("[data-pw=workday] [data-pw-act=copy]")
                await page.wait_for_function("() => document.getElementById('toast').textContent === 'Copied.'")
                seen["clipboard"] = await page.evaluate("() => navigator.clipboard.readText()") == made
                seen["left"] = await page.evaluate(_LEFT, made)
                seen["box"] = await page.locator("[data-pw-box]").count()

                bare = await browser.new_page(viewport={"width": 1280, "height": 900})
                await bare.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: undefined})")
                await open_desk(bare, desk, "settings/passwords")
                await bare.click("[data-pw=workday] [data-pw-act=copy]")
                await bare.wait_for_selector("[data-pw=workday] .set-err")
                seen["bare"] = await _row(bare, "workday")
                seen["bare_left"] = await bare.evaluate(_LEFT, made)
                seen["bare_toast"] = await bare.inner_text("#toast")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["clipboard"] is True, seen
    assert seen["left"] == NOWHERE and seen["box"] == 0, seen
    assert seen["bare"]["error"] == "This browser won't let the desk use the clipboard, so nothing was copied.", seen
    assert seen["bare"]["said"] == "Made by the desk on Nov 10", seen
    assert seen["bare_left"] == NOWHERE and seen["bare_toast"] == "", seen


# ------------------------------------------------------------------ E7's test 11: the two questions
@needs_browser
def test_make_a_new_one_and_forget_ask_first_in_e4s_words_and_a_no_changes_nothing(srv, job_apply_home):
    """Make a new one and Forget, in a row's More menu, ask first in E4's words. A "no" changes nothing: the
    password, its day and its row stay. A "yes" makes a new one (another password) and forgets it."""
    desk = _desk(srv)

    async def go():
        made = await _make(desk, "workday_password")
        await desk.start(port=0, open_browser=False)
        seen: dict = {"asked": []}
        answer = {"yes": False}

        async def said(dialog):
            seen["asked"].append(dialog.message)
            await (dialog.accept() if answer["yes"] else dialog.dismiss())

        async def choose(page, what: str) -> None:
            await page.click("[data-pw=workday] summary")
            await page.click(f"[data-pw=workday] [data-pw-act={what}]")

        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("dialog", said)
                await open_desk(page, desk, "settings/passwords")
                before = await _row(page, "workday")
                for what in ("new", "forget"):
                    await choose(page, what)
                    await page.wait_for_timeout(2000)  # (a refresh or two: nothing came of it)
                seen["no"] = [await _row(page, "workday"), before, config.get_secret("workday_password") == made,
                              dict(desk.made_passwords)]
                seen["menu_closed"] = await page.eval_on_selector("[data-pw=workday] details.menu", "(d) => d.open")
                answer["yes"] = True
                await choose(page, "new")
                await page.wait_for_function("() => !document.querySelector('[data-pw=workday] .set-err')"
                                             " && !document.querySelector('[data-pw=workday] button[disabled]')")
                await page.wait_for_timeout(500)
                renewed = config.get_secret("workday_password")
                seen["new"] = renewed is not None and renewed != made
                await choose(page, "forget")
                await page.wait_for_function("() => document.querySelector('[data-pw=workday] .pill').innerText"
                                             " === 'None yet'")
                seen["forgot"] = config.get_secret("workday_password")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["asked"] == [ASK_NEW, ASK_FORGET, ASK_NEW, ASK_FORGET], seen["asked"]
    now, before, same, days = seen["no"]
    assert now == before and same and days == {"workday_password": "2026-11-10"}, seen["no"]
    assert seen["menu_closed"] is False, seen
    assert seen["new"] is True and seen["forgot"] is None, seen


@needs_browser
def test_an_open_more_menu_and_an_open_form_outlast_the_refresh_and_escape_closes_the_menu(srv, job_apply_home):
    """The page's rules for the panel: a More menu opened and a Type my own form typed in stay as they are across the
    page's refresh (every 1.5 seconds), and Escape closes the menu with the keyboard back on its button."""
    desk = _desk(srv)

    async def go():
        await _make(desk, "workday_password")
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/passwords")
                await page.click("[data-pw=icims] [data-pw-act=type]")
                await page.keyboard.type("made-up-half-typed")
                await page.click("[data-pw=workday] summary")
                await page.wait_for_timeout(3500)
                seen = {"open": await page.eval_on_selector("[data-pw=workday] details.menu", "(d) => d.open"),
                        "typed": await page.input_value("#pw"),
                        "form_in": await page.evaluate("document.getElementById('pw-form').closest('[data-pw]')"
                                                       ".dataset.pw")}
                await page.keyboard.press("Escape")
                seen["closed"] = not await page.eval_on_selector("[data-pw=workday] details.menu", "(d) => d.open")
                seen["focus"] = await page.evaluate("document.activeElement.getAttribute('aria-label')"
                                                    " + ' ' + document.activeElement.closest('[data-pw]').dataset.pw")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen == {"open": True, "typed": "made-up-half-typed", "form_in": "icims", "closed": True,
                    "focus": "More workday"}, seen


# ------------------------------------------------------------------ the brief's three changes, through the page
@needs_browser
def test_inside_claude_no_row_has_show_or_copy_and_a_line_says_why(srv, job_apply_home):
    """Inside Claude the desk never shows a password (Show answers 409 there): no row has Show or Copy, E1's first
    sentence ends at "where it has none.", and a line under E1's sentences says why."""
    desk = _desk(srv, standalone=False)

    async def go():
        await _make(desk, "workday_password")
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/passwords")
                await page.wait_for_selector("#pw-rows [data-pw=workday]")
                seen = {"rows": await _rows(page), "said": " ".join((await page.inner_text("#pw-said")).split()),
                        "inside": await page.inner_text("#pw-inside") if await page.is_visible("#pw-inside") else None,
                        "show_copy": await page.locator("[data-pw-act=show], [data-pw-act=copy]").count()}
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["said"] == E1_FIRST and seen["inside"] == INSIDE, seen
    assert seen["show_copy"] == 0, seen
    assert seen["rows"][0]["buttons"] == ["More"] and seen["rows"][0]["said"] == "Made by the desk on Nov 10", seen
    assert all(r["buttons"] == ["Make one", "Type my own"] for r in seen["rows"][1:]), seen["rows"]


@needs_browser
def test_with_the_vault_not_answering_a_row_with_none_cant_tell_and_offers_no_make(srv, job_apply_home, memory_vault,
                                                                                   monkeypatch):
    """While the password vault isn't answering (state.vault_problem), a row whose password wasn't found reads "Can't
    tell right now" with Type my own and no Make one, and Make my passwords isn't offered (the page's notice about the
    vault says why)."""
    desk = _desk(srv)
    put_out_of_use(memory_vault, monkeypatch)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/passwords")
                await page.wait_for_selector("#pw-rows [data-pw=workday]")
                seen = {"rows": await _rows(page), "make_all": await page.is_visible("#pw-make-row"),
                        "make": await page.locator("[data-pw-act=make]").count(),
                        "problem": desk.state()["vault_problem"]}
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["problem"], seen
    assert seen["make_all"] is False and seen["make"] == 0, seen
    for r in seen["rows"]:
        assert (r["said"], r["buttons"]) == ("Can't tell right now", ["Type my own"]), r


@needs_browser
def test_a_refusal_from_make_a_new_one_shows_the_routes_sentence_and_leaves_the_row(srv, job_apply_home):
    """Make a new one while a job at a Workday employer is being filled: the desk answers 409, and the page shows its
    sentence under the row, nothing added; the row and the password stay as they were."""
    desk = _desk(srv)
    job = srv.add_job(url=WORKDAY_JOB, title="Equipment Technician", company="Example Fab")["job"]

    async def go():
        made = await _make(desk, "workday_password")
        await desk.start(port=0, open_browser=False)
        desk.applier.runs[job["id"]] = Run(job["id"], "Equipment Technician", "Example Fab", status="running",
                                           url=WORKDAY_JOB)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("dialog", lambda d: d.accept())
                await open_desk(page, desk, "settings/passwords")
                await page.wait_for_selector("#pw-rows [data-pw=workday]")
                before = await _row(page, "workday")
                await page.click("[data-pw=workday] summary")
                await page.click("[data-pw=workday] [data-pw-act=new]")
                await page.wait_for_selector("[data-pw=workday] .set-err")
                await page.wait_for_timeout(2000)  # (and after a refresh or two)
                return before, await _row(page, "workday"), config.get_secret("workday_password") == made
        finally:
            await desk.stop()

    before, after, same = run(go())
    assert after["error"] == "A Workday job is being filled now. Try again when it is done.", after
    assert {**after, "error": ""} == before and same, (before, after)


# ------------------------------------------------------------------ E5 (E7's test 13): the driver's sentences
def _six(standalone: bool) -> list[str]:
    """The driver's sentences that ask the person to type the password they saved on the desk, each written out as a
    desk on its own (or inside Claude) says it: shown_where's part as it gives it, any other computed part as "{}"."""

    def words(node: ast.AST) -> str:
        if isinstance(node, ast.Constant):
            return str(node.value)
        if isinstance(node, ast.JoinedStr):
            return "".join(words(v) for v in node.values)
        if isinstance(node, ast.FormattedValue):
            call = node.value
            if isinstance(call, ast.Call) and ast.unparse(call.func) in ("shown_where", "pipeline.shown_where") \
                    and ast.unparse(call.args[0]) == "self.standalone":
                return pipeline.shown_where(standalone)
            return "{}"
        if isinstance(node, ast.BinOp):
            return words(node.left) + words(node.right)
        return "{}"

    tree = ast.parse(_driver_source())
    said: list[ast.AST] = []
    for node in ast.walk(tree):
        # a stop's reason (_pause's third), and the reset email's words, built apart from its stop
        # (_await_reset_email's `watching`, its two sides)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_pause" \
                and len(node.args) > 2:
            said.append(node.args[2])
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "watching" for t in node.targets):
            said.extend((node.value.body, node.value.orelse) if isinstance(node.value, ast.IfExp) else (node.value,))
    return [text for text in (" ".join(words(n).split()) for n in said) if "you saved on the desk" in text]


def test_the_six_sentences_say_where_to_see_the_password_only_on_a_desk_on_its_own():
    """E5: the six stops that ask the person to set or type the password they saved on the desk (and a seventh that
    asks the same, after a reset the site wouldn't take) add, straight after those words, "(Settings, Accounts and
    passwords shows it)" on a desk run on its own, and inside Claude (where the page never shows one) stay as they
    were."""
    alone, inside = _six(True), _six(False)
    assert len(alone) == len(inside) == 7, alone
    brackets = "(Settings, Accounts and passwords shows it)"
    for said in alone:
        assert f"you saved on the desk {brackets}" in said, said
    for said in inside:
        assert brackets not in said and "you saved on the desk {}" not in said, said
    assert pipeline.shown_where(True) == f" {brackets}" and pipeline.shown_where(False) == ""


def test_the_tip_and_the_line_before_applying_say_make_one_in_settings(srv, job_apply_home, monkeypatch):
    """E5: the tip at a sign-in done by hand, and the line before applying, say to make the password in Settings,
    under Accounts and passwords, on either desk."""
    monkeypatch.setattr(pipeline, "_secret", lambda name: None)
    assert pipeline._password_tip("https://intel.wd1.myworkdayjobs.com/External/login") == (
        " Make a Workday password in Settings, under Accounts and passwords, and the desk fills these in for you next "
        "time.")
    assert pipeline._password_tip("https://acme.taleo.net/careersection/iam/login.jsf").startswith(
        " Make a Taleo password in Settings")
    for standalone in (True, False):
        desk = _desk(srv, standalone)
        lines = desk.preflight(["https://acme.wd1.myworkdayjobs.com/External/job/Phoenix-AZ/Technician_R1",
                                "https://acme.wd1.myworkdayjobs.com/External/job/Phoenix-AZ/Technician_R2"])
        assert lines[0] == ("Workday: no password saved, so 2 of these jobs stop at the sign-in for you. Make one in "
                            "Settings, under Accounts and passwords, and the desk signs in itself."), lines


# ------------------------------------------------------------------ the review of the panel
def _held(page, holds: dict[str, float]):
    """Show's answers held back: the n-th asked (from 1), or the one for a name, by `holds` (seconds)."""
    import asyncio
    import json as _json

    asked = {"n": 0}

    async def answer(route):
        asked["n"] += 1
        name = _json.loads(route.request.post_data or "{}").get("name", "")
        wait = holds.get(str(asked["n"]), holds.get(name, 0))
        if wait:
            await asyncio.sleep(wait)
        await route.continue_()

    return page.route("**/api/password/show", answer)


@needs_browser
def test_a_second_show_while_the_first_is_asked_never_brings_the_password_back_after_hide(srv, job_apply_home):
    """Show pressed twice before its answer, the second's answer held back: after the first answer the person presses
    Hide, and 3 seconds on the password is nowhere (the second answer never fills the box again)."""
    desk = _desk(srv)

    async def go():
        made = await _make(desk, "workday_password")
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await _held(page, {"2": 1.5})
                await open_desk(page, desk, "settings/passwords")
                await page.evaluate("() => { const b = document.querySelector('[data-pw=workday] [data-pw-act=show]');"
                                    " b.click(); b.click(); }")
                await page.wait_for_selector("[data-pw-box]")
                await page.click("[data-pw=workday] [data-pw-act=hide]")
                await page.wait_for_timeout(3000)
                left = await page.evaluate(_LEFT, made)
                await browser.close()
                return left
        finally:
            await desk.stop()

    assert run(go()) == NOWHERE


@needs_browser
def test_each_show_lasts_its_own_30_seconds(srv, job_apply_home):
    """A Show answered after another one asked before it (Workday's, then SuccessFactors') is the one shown, for its
    own 30 seconds: the earlier Show's timer never hides it early. And a Show after a Hide lasts its own 30 seconds."""
    desk = _desk(srv)

    async def go():
        await _make(desk, "workday_password")
        sf = await _make(desk, "successfactors_password")
        await desk.start(port=0, open_browser=False)
        seen: dict = {}
        shown = "(pw) => { const b = document.querySelector('[data-pw-box]'); return !!b && b.value === pw; }"
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await page.clock.install()
                await _held(page, {"workday_password": 1.0, "successfactors_password": 2.5})
                await open_desk(page, desk, "settings/passwords")
                await page.evaluate("() => { for (const s of ['workday', 'successfactors']) document.querySelector("
                                    "`[data-pw=${s}] [data-pw-act=show]`).click(); }")
                await page.wait_for_function(shown, arg=sf)
                await page.clock.fast_forward(29000)
                seen["at_29"] = await page.evaluate(shown, sf)
                await page.clock.fast_forward(1500)
                seen["at_30"] = await page.evaluate("() => !!document.querySelector('[data-pw-box]')")
                await page.unroute("**/api/password/show")
                await page.click("[data-pw=successfactors] [data-pw-act=show]")
                await page.wait_for_function(shown, arg=sf)
                await page.clock.fast_forward(20000)
                await page.click("[data-pw=successfactors] [data-pw-act=hide]")
                await page.click("[data-pw=successfactors] [data-pw-act=show]")
                await page.wait_for_function(shown, arg=sf)
                await page.clock.fast_forward(15000)
                seen["again_15"] = await page.evaluate(shown, sf)
                await page.clock.fast_forward(16000)
                seen["again_31"] = await page.evaluate("() => !!document.querySelector('[data-pw-box]')")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    assert run(go()) == {"at_29": True, "at_30": False, "again_15": True, "again_31": False}


@needs_browser
def test_copy_answered_after_the_panel_is_left_puts_nothing_on_the_clipboard(srv, job_apply_home):
    """Copy's answer held back while the person leaves the panel: nothing is put on the clipboard."""
    desk = _desk(srv)

    async def go():
        made = await _make(desk, "workday_password")
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900},
                                              permissions=["clipboard-read", "clipboard-write"])
                await _held(page, {"1": 1.5})
                await open_desk(page, desk, "settings/passwords")
                await page.evaluate("() => navigator.clipboard.writeText('made-up-before')")
                await page.click("[data-pw=workday] [data-pw-act=copy]")
                await press_menu(page, "settings/email")
                await page.wait_for_timeout(3000)
                seen = {"clipboard": await page.evaluate("() => navigator.clipboard.readText()"),
                        "toast": await page.inner_text("#toast"), "left": await page.evaluate(_LEFT, made)}
                await browser.close()
                return seen
        finally:
            await desk.stop()

    assert run(go()) == {"clipboard": "made-up-before", "toast": "", "left": NOWHERE}


@needs_browser
def test_the_desk_stopping_lets_a_shown_password_go(srv, job_apply_home):
    """Quit the Job Desk gone through (stopPage): a password shown is let go with the rest of the page."""
    desk = _desk(srv)

    async def go():
        made = await _make(desk, "workday_password")
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/passwords")
                await page.click("[data-pw=workday] [data-pw-act=show]")
                await page.wait_for_selector("[data-pw-box]")
                await page.evaluate("() => stopPage()")
                left = await page.evaluate(_LEFT, made)
                await browser.close()
                return left
        finally:
            await desk.stop()

    assert run(go()) == NOWHERE


@needs_browser
def test_leaving_the_panel_closes_the_form_and_lets_what_was_typed_go(srv, job_apply_home):
    """A password typed in a row's form, not saved: leaving the panel (or Settings) empties the box and closes the form,
    and coming back finds it closed."""
    desk = _desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        seen: dict = {}
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/passwords")
                for leave in ("settings/email", "home"):
                    await page.click("[data-pw=icims] [data-pw-act=type]")
                    await page.keyboard.type("made-up-half-typed")
                    await press_menu(page, leave)
                    seen[leave] = await page.evaluate("""() => ({typed: document.getElementById('pw').value,
                        inRow: !!document.getElementById('pw-form').closest('[data-pw]')})""")
                    await press_menu(page, "settings/passwords")
                    seen[leave]["open"] = await page.is_visible("#pw")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    closed = {"typed": "", "inRow": False, "open": False}
    assert run(go()) == {"settings/email": closed, "home": closed}


@needs_browser
def test_the_keyboard_stays_in_the_row_after_make_a_new_one_forget_and_make_one(srv, job_apply_home):
    """A row drawn again after Make a new one keeps the keyboard on its More button; after Forget (no More any more) on
    the row's first button; after Make one on its Show. Never the page itself. Each More names its job system."""
    desk = _desk(srv)
    focused = "() => [document.activeElement.closest('[data-pw]')?.dataset.pw, " \
              "document.activeElement.dataset.pwAct || document.activeElement.getAttribute('aria-label')]"

    async def go():
        await _make(desk, "workday_password")
        await desk.start(port=0, open_browser=False)
        seen: dict = {}
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("dialog", lambda d: d.accept())
                await open_desk(page, desk, "settings/passwords")
                seen["named"] = await page.eval_on_selector(
                    "[data-pw=workday] summary",
                    "(s) => document.getElementById(s.getAttribute('aria-describedby')).textContent")
                await page.click("[data-pw=workday] summary")
                await page.click("[data-pw=workday] [data-pw-act=new]")
                await page.wait_for_function("() => !document.querySelector('[data-pw=workday] button[disabled]')")
                await page.wait_for_timeout(2000)
                seen["new"] = await page.evaluate(focused)
                await page.click("[data-pw=workday] summary")
                await page.click("[data-pw=workday] [data-pw-act=forget]")
                await page.wait_for_function("() => document.querySelector('[data-pw=workday] .pill').innerText"
                                             " === 'None yet'")
                await page.wait_for_timeout(1000)
                seen["forget"] = await page.evaluate(focused)
                await page.click("[data-pw=workday] [data-pw-act=make]")
                await page.wait_for_function("() => document.querySelector('[data-pw=workday] .pill').innerText"
                                             ".startsWith('Made')")
                await page.wait_for_timeout(1000)
                seen["make"] = await page.evaluate(focused)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    assert run(go()) == {"named": "Workday", "new": ["workday", "More"], "forget": ["workday", "make"],
                         "make": ["workday", "show"]}


# ------------------------------------------------------------------ the confirming read of the panel
@needs_browser
def test_a_shows_answer_that_comes_after_another_rows_show_is_never_put_in_a_box(srv, job_apply_home):
    """Workday's Show held back, SuccessFactors' Show pressed meanwhile and answered: Workday's answer, come later, is
    let go. It is never in a box, not even for a moment, nor anywhere else on the page."""
    desk = _desk(srv)

    async def go():
        wd = await _make(desk, "workday_password")
        sf = await _make(desk, "successfactors_password")
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await _held(page, {"workday_password": 2.0})
                await open_desk(page, desk, "settings/passwords")
                # (every box looked at as the page changes: a value put in one for a moment is seen)
                await page.evaluate("""(pw) => { window.__seen = false;
                    const look = () => { if ([...document.querySelectorAll('input')].some((b) => b.value === pw))
                      window.__seen = true; };
                    new MutationObserver(look).observe(document.body, {subtree: true, childList: true});
                    setInterval(look, 20); }""", wd)
                await page.click("[data-pw=workday] [data-pw-act=show]")
                await page.click("[data-pw=successfactors] [data-pw-act=show]")
                await page.wait_for_function("(pw) => [...document.querySelectorAll('[data-pw-box]')]"
                                             ".some((b) => b.value === pw)", arg=sf)
                await page.wait_for_timeout(3500)  # (Workday's answer come and gone)
                seen = {"seen": await page.evaluate("window.__seen"), "left": await page.evaluate(_LEFT, wd),
                        "sf_shown": await page.evaluate("(pw) => document.querySelector('[data-pw-box]')?.value === pw",
                                                        sf)}
                await browser.close()
                return seen
        finally:
            await desk.stop()

    assert run(go()) == {"seen": False, "left": NOWHERE, "sf_shown": True}


@needs_browser
def test_copy_asked_before_hide_still_copies(srv, job_apply_home):
    """Show, then Copy with its answer held back, then Hide: the Copy the person asked for still goes on the clipboard
    and says "Copied." (only a newer Copy and the ways out of the panel drop it)."""
    desk = _desk(srv)

    async def go():
        made = await _make(desk, "workday_password")
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900},
                                              permissions=["clipboard-read", "clipboard-write"])
                await _held(page, {"2": 1.5})
                await open_desk(page, desk, "settings/passwords")
                await page.evaluate("() => navigator.clipboard.writeText('made-up-before')")
                await page.click("[data-pw=workday] [data-pw-act=show]")
                await page.wait_for_selector("[data-pw-box]")
                await page.click("[data-pw=workday] [data-pw-act=copy]")
                await page.click("[data-pw=workday] [data-pw-act=hide]")
                await page.wait_for_function("() => document.getElementById('toast').textContent === 'Copied.'",
                                             timeout=5000)
                seen = {"clipboard": await page.evaluate("() => navigator.clipboard.readText()") == made,
                        "left": await page.evaluate(_LEFT, made)}
                await browser.close()
                return seen
        finally:
            await desk.stop()

    assert run(go()) == {"clipboard": True, "left": NOWHERE}


@needs_browser
def test_a_show_never_answered_is_given_up_after_15_seconds_and_the_row_hears_again(srv, job_apply_home):
    """A Show whose request never answers: 15 seconds on (the clock faked) its row says "It couldn't be shown.", and a
    press of Show asks again."""
    import asyncio
    import contextlib

    desk = _desk(srv)

    async def go():
        await _make(desk, "workday_password")
        await desk.start(port=0, open_browser=False)
        asked = {"n": 0}

        async def never(route):
            asked["n"] += 1
            if asked["n"] == 1:
                await asyncio.sleep(25)
                with contextlib.suppress(Exception):
                    await route.abort()
                return
            await route.continue_()

        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await page.clock.install()
                await page.route("**/api/password/show", never)
                await open_desk(page, desk, "settings/passwords")
                await page.click("[data-pw=workday] [data-pw-act=show]")
                await page.wait_for_timeout(500)
                await page.clock.fast_forward(14000)
                seen = {"at_14": (await _row(page, "workday"))["error"]}
                await page.clock.fast_forward(1500)
                await page.wait_for_selector("[data-pw=workday] .set-err", timeout=3000)
                seen["at_15"] = (await _row(page, "workday"))["error"]
                await page.click("[data-pw=workday] [data-pw-act=show]")
                await page.wait_for_selector("[data-pw-box]", timeout=5000)
                seen["asked"] = asked["n"]
                await browser.close()
                return seen
        finally:
            await desk.stop()

    assert run(go()) == {"at_14": "", "at_15": "It couldn't be shown.", "asked": 2}


@needs_browser
def test_the_desk_come_back_with_a_key_of_its_own_lets_the_panel_go(srv, job_apply_home):
    """The page's other way of stopping (load(): after an update the desk answers with a key of its own, and the page
    says it opened again in a window of its own): a shown password, and what was typed in the form, let go."""
    desk = _desk(srv)

    async def go():
        made = await _make(desk, "workday_password")
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/passwords")
                await page.click("[data-pw=icims] [data-pw-act=type]")
                await page.keyboard.type("made-up-half-typed")
                await page.click("[data-pw=workday] [data-pw-act=show]")
                await page.wait_for_selector("[data-pw-box]")
                await page.route("**/api/state", lambda r: r.fulfill(status=403, body="{}",
                                                                     content_type="application/json"))
                await page.evaluate("() => { updateDown = true; }")
                await page.wait_for_selector("text=opened again in a window of its own", timeout=5000)
                seen = {"left": await page.evaluate(_LEFT, made), "typed": await page.input_value("#pw")}
                await browser.close()
                return seen
        finally:
            await desk.stop()

    assert run(go()) == {"left": NOWHERE, "typed": ""}
