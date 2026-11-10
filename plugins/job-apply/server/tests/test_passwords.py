"""Passwords made for you (the design's Version E, its server half): the generator (passwords.py), the desk's three
routes that make, show and forget a saved password (/api/password/make, /show and /forget), and the day each was made
(desk.json's made_passwords, the state's passwords_made). Every test here has conftest's vault in memory (memory_vault)
or none (no_vault): none reaches the computer's own. Every password in it is made up, or made in the test."""

import ast
import asyncio
import contextlib
import json
import logging
import os
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import keyring.errors
import pytest
from conftest import MemoryVault, run

from job_apply import config, pipeline, vault
from job_apply import desk as desk_module
from job_apply.desk import Desk
from job_apply.pipeline import Applier, Run

# The design's table for a made password (E2), spelled out here, not read from the module it checks
LOWER, UPPER = "abcdefghijkmnpqrstuvwxyz", "ABCDEFGHJKLMNPQRSTUVWXYZ"  # (no l, no o; no I, no O)
DIGITS, SYMBOLS = "23456789", "!@#$%*"  # (no 0, no 1)
GROUPS = (LOWER, UPPER, DIGITS, SYMBOLS)
ALLOWED = "".join(GROUPS)

SITE = list(config.DESK_PASSWORDS)  # the job systems' passwords, by name, in the page's order
KEY = "sk-ant-api03-not-a-real-key-000000000000"
TOPIC = "jobdesk-notarealtopic0000000001"
NEVER_SHOWN = "That one is never shown."
FROM_ENVIRONMENT = "This one comes from your computer's own settings, not from the desk."
MAKE, SHOW, FORGET = "/api/password/make", "/api/password/show", "/api/password/forget"


def keeps_the_rules(password: str) -> bool:
    """A password as the design's table makes one: 16 long, a letter first, two or more from each group, only the
    allowed characters, and never the same character twice in a row."""
    return (len(password) == 16 and password[0] in LOWER + UPPER and set(password) <= set(ALLOWED)
            and all(sum(c in group for c in password) >= 2 for group in GROUPS)
            and all(a != b for a, b in zip(password, password[1:])))


def in_vault(fake: MemoryVault, name: str) -> str | None:
    return fake.store.get((vault.service(), name))


def idle_desk(srv) -> Desk:
    """A desk that drives nothing: only its page's routes are asked."""
    desk = Desk(srv)
    desk.applier.start = lambda: None
    return desk


@contextlib.asynccontextmanager
async def serving(desk: Desk):
    """The desk served: a client for its routes, and the header that carries its page's key."""
    await desk.start(port=0, open_browser=False)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=30, trust_env=False) as c:
            yield c, {"x-desk-token": desk.token}
    finally:
        await desk.stop()


async def ask(c: httpx.AsyncClient, h: dict, path: str, body: object, status: int = 200) -> httpx.Response:
    """One of the three routes asked, as the page asks it: its answer, which has the status expected and is never kept
    by a browser or a proxy (Cache-Control: no-store), whatever it says. (A failure here says the route and the status
    alone: never the answer's words, which for Show hold a password.)"""
    r = await c.post(path, headers=h, json=body)
    assert r.status_code == status, (path, r.status_code)
    assert r.headers.get("cache-control") == "no-store", (path, r.status_code)
    return r


def made_days(home: Path) -> dict | None:
    """desk.json's made_passwords, as written (None with none written)."""
    path = home / "desk.json"
    return json.loads(path.read_text(encoding="utf-8")).get("made_passwords") if path.exists() else None


def on_day(monkeypatch, day: str) -> dict:
    """The day the desk takes today for: `day`, until the test sets another in what this gives (its "is")."""
    today = {"is": day}
    monkeypatch.setattr(desk_module, "_today", lambda: today["is"], raising=False)
    return today


# ------------------------------------------------------------------ the generator (E7's tests 1 and 2)
def test_two_thousand_made_passwords_each_keep_every_rule_and_no_two_are_the_same():
    from job_apply import passwords

    made = [passwords.make() for _ in range(2000)]
    for password in made:
        assert len(password) == 16, password
        assert password[0] in LOWER + UPPER, password  # a letter first
        assert set(password) <= set(ALLOWED), password  # (and so no l, o, I, O, 0 or 1)
        for group in GROUPS:
            assert sum(c in group for c in password) >= 2, (password, group)
        assert all(a != b for a, b in zip(password, password[1:])), password  # never the same character twice in a row
    assert len(set(made)) == 2000
    # (and every character of the table is in use: none of its four groups is cut short)
    assert set("".join(made)) == set(ALLOWED)


# A draw that keeps every rule, one that only just does (two digits, two symbols), and one that breaks each rule in turn
# (all made up for the test)
GOOD, JUST = "aB3!cD4@eF5#gH6$", "aBcDeFgHjKmN2#3%"
BREAKS = {"a digit first": "9aB3!cD4@eF5#gH6", "a symbol first": "!aB3cD4@eF5#gH6$",
          "one lower-case letter": "aB3!CD4@EF5#GH6$", "one capital": "aB3!cd4@ef5#gh6$",
          "one digit": "aB3!cD@eFg#hJ$kL", "one symbol": "aB3!cD4eF5gH6jK7",
          "the same character twice in a row": "aB3!cD4@eF5#gH66", "the same symbol twice in a row": "aB3!!cD4@eF5gH6$"}


def test_a_made_password_is_drawn_with_secrets_choice_and_the_module_never_imports_random(monkeypatch):
    """E7's test 2: the module imports `secrets` and nothing else that could draw, every draw is secrets.choice over
    the table's 62 characters, and what make() gives is those draws, in the order drawn. A draw that breaks a rule is
    thrown away whole and all 16 are drawn again."""
    from job_apply import passwords

    tree = ast.parse(Path(passwords.__file__).read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {(node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported == {"__future__", "secrets"}, imported  # never random (nor os, time, uuid or hashlib)
    used = {node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "secrets"}
    assert used == {"choice"}, used
    assert not hasattr(passwords, "random")
    # (the test's own draws: each 16 long, and kept or thrown away as it says)
    assert keeps_the_rules(GOOD) and keeps_the_rules(JUST)
    assert all(len(bad) == 16 and set(bad) <= set(ALLOWED) and not keeps_the_rules(bad) for bad in BREAKS.values())

    for why, drawn, made in [(why, bad + GOOD, GOOD) for why, bad in BREAKS.items()] + [("just enough", JUST, JUST)]:
        script, asked = iter(drawn), []

        def choice(characters, script=script, asked=asked):
            asked.append(characters)
            return next(script)

        monkeypatch.setattr(passwords.secrets, "choice", choice)
        assert passwords.make() == made, why
        assert len(asked) == len(drawn), why  # (16 thrown away and 16 kept; or the 16 kept alone)
        # each draw is from all 62 characters, each listed once: none is likelier than another
        assert all(len(characters) == 62 and set(characters) == set(ALLOWED) for characters in asked), why


# ------------------------------------------------------------------ Make my passwords (E7's test 3)
def test_make_my_passwords_makes_one_for_each_job_system_that_has_none_and_answers_with_names_only(
        srv, job_apply_home, memory_vault, monkeypatch):
    """E7's test 3: one saved on the desk before, one written into secrets.yaml by hand and one set in the environment
    are left as they were; each other job system gets one of the desk's making, kept as a saved one is (here in the
    vault); the answer has their names and no more; and desk.json and the state have the day each was made."""
    kept = {"workday_password": "Typed-on-the-desk-1", "icims_password": "typed in the file",
            "taleo_password": "set-in-the-environment"}
    config.save_site_password("workday_password", kept["workday_password"])
    config.secrets_path().write_text(f'# mine\nicims_password: "{kept["icims_password"]}"\n', encoding="utf-8")
    monkeypatch.setenv("JOB_APPLY_SECRET_TALEO_PASSWORD", kept["taleo_password"])
    none = [name for name in SITE if name not in kept]
    on_day(monkeypatch, "2026-11-10")
    desk = idle_desk(srv)

    async def go():
        async with serving(desk) as (c, h):
            await ask(c, {}, MAKE, {}, 403)  # (the page's key)
            assert all(config.get_secret(name) is None for name in none)
            first = await ask(c, h, MAKE, {})
            state = (await c.get("/api/state", headers=h)).json()
            again = await ask(c, h, MAKE, {})
            return first, state, again

    first, state, again = run(go())
    assert first.json() == {"made": none}  # names only, in the page's order
    made = {name: config.get_secret(name) for name in none}
    assert all(keeps_the_rules(password) for password in made.values()) and len(set(made.values())) == len(none)
    assert {name: in_vault(memory_vault, name) for name in none} == made  # (kept as a saved one is: in the vault)
    assert config.secrets_path().read_text(encoding="utf-8") == "# mine\n"
    assert {name: config.get_secret(name) for name in kept} == kept  # the saved ones as they were
    assert in_vault(memory_vault, "taleo_password") is None  # (none made under the environment's)
    days = {name: "2026-11-10" for name in none}
    assert state["passwords"] == {**{name.removesuffix("_password"): True for name in SITE}, "email": False,
                                  "anthropic": False}
    assert state["passwords_made"] == days and made_days(job_apply_home) == days
    # asked again, it makes none: every job system has one now
    assert again.json() == {"made": []} and {name: config.get_secret(name) for name in none} == made
    assert Desk(srv).made_passwords == days  # (read back from desk.json at the desk's next start)


def test_without_a_vault_a_made_password_is_kept_in_the_file_the_person_alone_can_read(srv, job_apply_home, no_vault,
                                                                                       monkeypatch):
    """E1's bargain: "in your computer's password vault, or in a file only you can read where it has none"."""
    path = config.secrets_path()
    path.write_text("# mine\nworkday_password: typed-by-hand\n", encoding="utf-8")
    on_day(monkeypatch, "2026-11-10")
    desk = idle_desk(srv)

    async def go():
        async with serving(desk) as (c, h):
            return await ask(c, h, MAKE, {})

    assert run(go()).json() == {"made": SITE[1:]}
    saved = config.read_secrets(path)
    assert list(saved) == SITE and saved["workday_password"] == "typed-by-hand"
    assert all(keeps_the_rules(saved[name]) for name in SITE[1:]) and len(set(saved.values())) == len(SITE)
    assert path.read_text(encoding="utf-8").startswith("# mine\nworkday_password: typed-by-hand\n")
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert {name: config.get_secret(name) for name in SITE} == saved
    # and nowhere else in the desk's folder
    for file in job_apply_home.rglob("*"):
        if file.is_file() and file != path:
            assert not any(saved[name].encode() in file.read_bytes() for name in SITE[1:]), file.name


def test_make_makes_a_password_for_a_job_system_and_for_no_other_name(srv, job_apply_home, memory_vault):
    """The email app password, the Anthropic API key and the phone alerts' topic are never made here, nor a password
    under any other name, however it's spelled: each is a 400, and nothing is saved."""
    others = ["email_password", config.API_KEY_NAME, config.NTFY_TOPIC_NAME, "honeywell_password", "linkedin_password",
              "workday", "Workday_Password", "WORKDAY_PASSWORD", "workday_password ", "workday-password", "", 7, True,
              ["workday_password"], {"workday_password": 1}]
    desk = idle_desk(srv)

    async def go():
        async with serving(desk) as (c, h):
            for name in others:
                for body in ({"name": name}, {"name": name, "replace": True}):
                    assert "error" in (await ask(c, h, MAKE, body, 400)).json(), body
            # a new one for every job system at once is never made: Make a new one names its job system
            assert "error" in (await ask(c, h, MAKE, {"replace": True}, 400)).json()
            return (await c.get("/api/state", headers=h)).json()

    state = run(go())
    assert memory_vault.store == {} and not config.secrets_path().exists()
    assert not any(state["passwords"].values()) and state["passwords_made"] == {}
    assert not made_days(job_apply_home)


# ------------------------------------------------------------------ Make one, Make a new one (E7's test 4)
def test_make_a_new_one_gives_one_name_a_new_and_different_password_and_changes_no_other(
        srv, job_apply_home, memory_vault, monkeypatch):
    """E7's test 4, and Make one ({name} without replace), which leaves a saved one alone as Make my passwords does."""
    today = on_day(monkeypatch, "2026-11-10")
    desk = idle_desk(srv)
    seen: dict = {}

    def saved() -> dict:
        return {name: config.get_secret(name) for name in SITE}

    async def go():
        async with serving(desk) as (c, h):
            # Make one: for the one job system named, and only while it has none
            assert (await ask(c, h, MAKE, {"name": "ukg_password"})).json() == {"made": ["ukg_password"]}
            seen["one"] = saved()
            assert (await ask(c, h, MAKE, {"name": "ukg_password"})).json() == {"made": []}
            assert saved() == seen["one"]
            assert (await ask(c, h, MAKE, {})).json() == {"made": [name for name in SITE if name != "ukg_password"]}
            seen["before"] = saved()
            # Make a new one
            today["is"] = "2026-11-12"
            assert (await ask(c, h, MAKE, {"name": "workday_password", "replace": True})).json() == {
                "made": ["workday_password"]}
            seen["after"] = saved()
            seen["state"] = (await c.get("/api/state", headers=h)).json()
            # only `true` makes a new one over a saved one
            for said in ("true", 1, "yes", None, False):
                assert (await ask(c, h, MAKE, {"name": "infor_password", "replace": said})).json() == {"made": []}
            assert saved() == seen["after"]
            # ...over one the person typed too ("saved or not")
            typed = await c.post("/api/password", headers=h, json={"name": "icims_password", "value": "Typed-by-me-2"})
            assert typed.json() == {"saved": True}
            assert (await ask(c, h, MAKE, {"name": "icims_password"})).json() == {"made": []}
            assert config.get_secret("icims_password") == "Typed-by-me-2"
            today["is"] = "2026-11-13"
            assert (await ask(c, h, MAKE, {"name": "icims_password", "replace": True})).json() == {
                "made": ["icims_password"]}
            seen["last"] = saved()
            seen["last state"] = (await c.get("/api/state", headers=h)).json()

    run(go())
    one, before, after, last = seen["one"], seen["before"], seen["after"], seen["last"]
    assert keeps_the_rules(one["ukg_password"]) and [n for n in SITE if one[n]] == ["ukg_password"]
    assert before["ukg_password"] == one["ukg_password"] and all(keeps_the_rules(p) for p in before.values())
    assert after["workday_password"] != before["workday_password"] and keeps_the_rules(after["workday_password"])
    assert {n: p for n, p in after.items() if n != "workday_password"} == {
        n: p for n, p in before.items() if n != "workday_password"}  # no other name changed
    assert in_vault(memory_vault, "workday_password") == after["workday_password"]
    days = {**{name: "2026-11-10" for name in SITE}, "workday_password": "2026-11-12"}
    assert seen["state"]["passwords_made"] == days
    assert last["icims_password"] not in ("Typed-by-me-2", before["icims_password"])
    assert keeps_the_rules(last["icims_password"])
    assert {n: p for n, p in last.items() if n != "icims_password"} == {
        n: p for n, p in after.items() if n != "icims_password"}
    assert seen["last state"]["passwords_made"] == {**days, "icims_password": "2026-11-13"}
    assert made_days(job_apply_home) == {**days, "icims_password": "2026-11-13"}
    # the one replaced is taken out of "What went in" and problem reports for as long as the desk runs, as a typed one
    # replaced is
    assert before["workday_password"] in pipeline._secret_values()


def test_a_made_password_that_cant_be_saved_leaves_the_one_saved_before(srv, job_apply_home, memory_vault,
                                                                        monkeypatch):
    """A failed save leaves what was saved before, as save_site_password promises: the vault refuses the new one and
    the file can't be written either, so the older one stays, with its day; the answer says why with no password in
    it, and nothing is said to be made."""
    today = on_day(monkeypatch, "2026-11-10")
    desk = idle_desk(srv)

    def refuse(*args):
        raise keyring.errors.KeyringLocked("locked")

    def full(path, text):
        raise OSError(28, "No space left on device")

    async def go():
        async with serving(desk) as (c, h):
            await ask(c, h, MAKE, {"name": "workday_password"})
            await c.post("/api/password", headers=h, json={"name": "icims_password", "value": "Typed-by-me-2"})
            before = {name: config.get_secret(name) for name in SITE}
            today["is"] = "2026-11-12"
            with monkeypatch.context() as m:
                m.setattr(memory_vault, "set_password", refuse)
                m.setattr(config, "write_text_atomic", full)
                answers = [await ask(c, h, MAKE, body, 400) for body in (
                    {"name": "workday_password", "replace": True}, {"name": "icims_password", "replace": True}, {})]
            vault.reset()  # (as a new start reads them: the vault answers again)
            return before, answers, (await c.get("/api/state", headers=h)).json()

    before, answers, state = run(go())
    assert {name: config.get_secret(name) for name in SITE} == before
    assert before["icims_password"] == "Typed-by-me-2" and keeps_the_rules(before["workday_password"])
    for answer in answers:
        assert "No space left on device" in answer.json()["error"] and "made" not in answer.json()
        assert before["workday_password"] not in answer.text
    assert state["passwords_made"] == {"workday_password": "2026-11-10"}
    assert not config.secrets_path().exists()


# ------------------------------------------------------------------ nowhere but the vault (E7's test 5)
def test_a_made_password_is_in_no_answer_state_log_file_or_problem_report(srv, job_apply_home, memory_vault, caplog):
    """E7's test 5: no made password (nor one replaced since) is in the route's answer, the page's state, the desk's
    log, desk.json or any other file of the desk's folder, or a problem report built afterwards, should a page quote it
    back: a made one is taken out of reports and "What went in" exactly as a typed one is."""
    caplog.set_level(logging.DEBUG)
    desk = idle_desk(srv)
    job = srv.add_job(url="https://example.com/jobs/fse", title="Field Service Engineer", company="Example Fab")["job"]
    texts: dict = {}
    made: set = set()

    async def go():
        async with serving(desk) as (c, h):
            first = await ask(c, h, MAKE, {})
            made.update(config.get_secret(name) for name in SITE)
            new = await ask(c, h, MAKE, {"name": "workday_password", "replace": True})
            made.update(config.get_secret(name) for name in SITE)
            forgot = await ask(c, h, FORGET, {"name": "ukg_password"})
            refused = [await ask(c, h, SHOW, {"name": name}, 400) for name in ("email_password", "ukg")]
            none = await ask(c, h, SHOW, {"name": "ukg_password"}, 404)
            texts.update({f"answer {i}": r.text for i, r in enumerate([first, new, forgot, none, *refused])})
            # (Show's own answer is the one place a password goes: to the page that asked, and into no log)
            assert (await ask(c, h, SHOW, {"name": "icims_password"})).json()["value"] in made
            for path in ("/api/state", "/api/safety", "/api/history", "/"):
                texts[path] = (await c.get(path, headers=h)).text
            desk.saw()  # (desk.json written once more, as at each look at the page)
            # then a page's error quotes the password back, and the person asks for a problem report
            now = config.get_secret("workday_password")
            said = f"{now} isn't a valid answer"
            stopped = Run(job["id"], "Field Service Engineer", "Example Fab", status="needs_you", need="stuck",
                          reason=f"review page shows errors: {said}", log=["the review page shows errors", said],
                          page_info={"url": "https://example.com/apply/review", "title": "Review", "errors": [said]})
            assert now in Applier._report_hidden(stopped)
            desk.applier.runs[job["id"]] = stopped
            report = (await c.post(f"/api/job/{job['id']}/report", headers=h, json={})).json()
            texts["report"] = report["preview"]
            texts["report's issue"] = parse_qs(urlsplit(report["issue_url"]).query)["body"][0]
            desk.applier._take_note(stopped)

    run(go())
    assert len(made) == len(SITE) + 1 and all(keeps_the_rules(password) for password in made)
    for where, text in texts.items():
        assert not any(password in text for password in made), where
    assert "review page shows errors" in texts["report"] and "review page shows errors" in texts["report's issue"]
    assert not any(password in caplog.text for password in made)  # the desk's log, down to its debug lines
    assert not any(password in str(record.args) for record in caplog.records for password in made)
    files = [path for path in job_apply_home.rglob("*") if path.is_file()]
    assert {"desk.json", vault.INDEX_FILE} <= {path.name for path in files}
    assert any(path.parent.name == "notes" and path.suffix == ".md" for path in files)  # (the note on the stop)
    for path in files:  # desk.json, the list of the vault's names, the note, the tracker, the history...
        held = path.read_bytes()
        assert not any(password.encode() in held for password in made), path.name
    assert set(made_days(job_apply_home)) == set(SITE) - {"ukg_password"}
    # "What went in" keeps it off its panel, wherever it was typed, and so does the next start's reading of the vault
    vault.reset()
    now = config.get_secret("workday_password")
    rest = [name for name in SITE if name not in ("workday_password", "ukg_password")]
    assert made - {now} >= {config.get_secret(name) for name in rest}
    assert now in pipeline._secret_values()
    assert pipeline._kept_out({"kind": "text", "label": "Anything else?"}, f"it is {now}", pipeline._secret_values())


# ------------------------------------------------------------------ Show and Copy (E7's test 6)
def test_show_gives_a_site_password_and_refuses_every_other_name(srv, job_apply_home, memory_vault, monkeypatch):
    """E7's test 6: a job system's saved password, typed or made, as the desk would type it. Never the email app
    password, the Anthropic API key or the phone alerts' topic, however the name is spelled (config.get_secret reads
    the environment by the name in capitals, so "email-password" and "EMAIL_PASSWORD" would find it), nor a password
    under a name of the person's own. 404 with none saved, 403 without the page's key."""
    typed = 'Typed "Workday" pässword #1'
    secrets = {"email_password": "abcd efgh ijkl mnop", config.API_KEY_NAME: KEY, config.NTFY_TOPIC_NAME: TOPIC,
               "honeywell_password": "Hand-Named-Pw-9"}
    config.save_site_password("workday_password", typed)
    for name, value in secrets.items():
        config.save_site_password(name, value)
    monkeypatch.setenv("JOB_APPLY_SECRET_SUCCESSFACTORS_PASSWORD", "set-in-the-environment")
    monkeypatch.setenv("JOB_APPLY_SECRET_LINKEDIN_PASSWORD", "another-sites-own")
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "the-inbox-key-in-the-environment")
    spellings = [*secrets, "linkedin_password", "email-password", "EMAIL_PASSWORD", "Email_Password", "email password",
                 "emaıl_password", "ANTHROPIC_API_KEY", "anthropic-api-key", "NTFY_TOPIC", "ntfy-topic",
                 "no_such_password", "workday", "Workday_Password", "WORKDAY_PASSWORD", " workday_password",
                 "workday_password\n", vault.NAMES_ENTRY, "", None, 7, True, ["workday_password"],
                 {"workday_password": 1}]
    desk = idle_desk(srv)

    async def go():
        async with serving(desk) as (c, h):
            await ask(c, {}, SHOW, {"name": "workday_password"}, 403)
            await ask(c, {"x-desk-token": "guess"}, SHOW, {"name": "workday_password"}, 403)
            await ask(c, {**h, "host": "evil.example"}, SHOW, {"name": "workday_password"}, 403)
            shown = await ask(c, h, SHOW, {"name": "workday_password"})
            environment = await ask(c, h, SHOW, {"name": "successfactors_password"})
            none = await ask(c, h, SHOW, {"name": "taleo_password"}, 404)
            await ask(c, h, MAKE, {"name": "icims_password"})
            made = await ask(c, h, SHOW, {"name": "icims_password"})
            refused = [await ask(c, h, SHOW, body, 400)
                       for body in ({}, *({"name": name} for name in spellings),
                                    {"name": "email_password", "replace": True})]
            return shown, environment, none, made, refused

    shown, environment, none, made, refused = run(go())
    assert shown.json() == {"value": typed}  # (as typed, to the letter)
    assert environment.json() == {"value": "set-in-the-environment"}  # what the desk would type: the environment's
    assert none.json() == {"error": "No password is saved for Taleo."}
    assert made.json() == {"value": config.get_secret("icims_password")} and keeps_the_rules(made.json()["value"])
    hidden = [typed, *secrets.values(), "set-in-the-environment", "another-sites-own",
              "the-inbox-key-in-the-environment", made.json()["value"]]
    for answer in (none, *refused):
        assert not any(value in answer.text for value in hidden)
    assert all(answer.json() == {"error": NEVER_SHOWN} for answer in refused)


# ------------------------------------------------------------------ Forget (E7's test 7)
def test_forget_takes_a_password_out_of_the_vault_and_the_file_and_the_state_says_so(srv, job_apply_home,
                                                                                    memory_vault, monkeypatch):
    """E7's test 7: a made password in the vault, and a line written into secrets.yaml by hand since (the newer, which
    would come back at the next start), both go; the rest of the file, the other passwords and their days stay. The
    email app password and the Anthropic API key are forgotten here too."""
    on_day(monkeypatch, "2026-11-10")
    desk = idle_desk(srv)
    path = config.secrets_path()
    index = job_apply_home / vault.INDEX_FILE

    async def go():
        async with serving(desk) as (c, h):
            await ask(c, h, MAKE, {})
            for name, value in (("email_password", "abcd efgh ijkl mnop"), (config.API_KEY_NAME, KEY)):
                assert (await c.post("/api/password", headers=h, json={"name": name, "value": value})).json() == {
                    "saved": True}
            desk.applier.mail_problem, desk.applier._mail_refused = "refused", "abcd efgh ijkl mnop"
            before = {name: config.get_secret(name) for name in SITE}
            path.write_text('# mine\nworkday_password: "by hand, the newer"\nhoneywell_password: keep-me\n',
                            encoding="utf-8")
            assert config.get_secret("workday_password") == "by hand, the newer"
            assert "workday_password" in index.read_text(encoding="utf-8")
            await ask(c, {}, FORGET, {"name": "workday_password"}, 403)
            assert in_vault(memory_vault, "workday_password") == before["workday_password"]
            forgot = await ask(c, h, FORGET, {"name": "workday_password"})
            state = (await c.get("/api/state", headers=h)).json()
            again = await ask(c, h, FORGET, {"name": "workday_password"})  # (none left: forgotten all the same)
            mail = await ask(c, h, FORGET, {"name": "email_password"})
            key = await ask(c, h, FORGET, {"name": config.API_KEY_NAME})
            return before, forgot, state, again, mail, key, (await c.get("/api/state", headers=h)).json()

    before, forgot, state, again, mail, key, last = run(go())
    assert forgot.json() == again.json() == mail.json() == key.json() == {"forgotten": True}
    assert in_vault(memory_vault, "workday_password") is None and config.get_secret("workday_password") is None
    assert path.read_text(encoding="utf-8") == "# mine\nhoneywell_password: keep-me\n"
    assert "workday_password" not in index.read_text(encoding="utf-8")
    assert state["passwords"]["workday"] is False and "workday_password" not in state["passwords_made"]
    others = [name for name in SITE if name != "workday_password"]
    assert {name: config.get_secret(name) for name in others} == {name: before[name] for name in others}
    assert state["passwords_made"] == made_days(job_apply_home) == {name: "2026-11-10" for name in others}
    assert {name: state["passwords"][name.removesuffix("_password")] for name in others} == dict.fromkeys(others, True)
    assert state["passwords"]["email"] is True and state["passwords"]["anthropic"] is True
    assert config.get_secret("email_password") is None and config.get_secret(config.API_KEY_NAME) is None
    assert in_vault(memory_vault, "email_password") is None and in_vault(memory_vault, config.API_KEY_NAME) is None
    assert last["passwords"]["email"] is False and last["passwords"]["anthropic"] is False
    # (and the page's note about the app password turned down goes with it, as it does when a new one is saved)
    assert last["mail_problem"] is None and desk.applier._mail_refused is None
    assert Desk(srv).made_passwords == {name: "2026-11-10" for name in others}


def test_forget_refuses_one_from_the_environment_the_phone_topic_and_every_other_name(srv, job_apply_home,
                                                                                    memory_vault, monkeypatch):
    """409 for one that comes from the environment (the one saved under it stays too); the phone alerts' topic keeps its
    own Off button, and is refused here, as is a password under any other name, however it's spelled."""
    saved = {"workday_password": "Typed-Workday-1", "taleo_password": "under-the-environments",
             "email_password": "abcd efgh ijkl mnop", config.NTFY_TOPIC_NAME: TOPIC,
             "honeywell_password": "Hand-Named-Pw-9"}
    for name, value in saved.items():
        config.save_site_password(name, value)
    for name in ("taleo_password", "email_password"):
        monkeypatch.setenv(f"JOB_APPLY_SECRET_{name.upper()}", "set-in-the-environment")
    others = [config.NTFY_TOPIC_NAME, "NTFY_TOPIC", "ntfy-topic", "honeywell_password", "linkedin_password",
              "Workday_Password", "WORKDAY_PASSWORD", "workday", "workday_password ", "Email_Password",
              "email-password", vault.NAMES_ENTRY, "", None, 7, True, ["workday_password"], {"workday_password": 1}]
    desk = idle_desk(srv)
    before = dict(memory_vault.store)

    async def go():
        async with serving(desk) as (c, h):
            environment = [await ask(c, h, FORGET, {"name": name}, 409)
                           for name in ("taleo_password", "email_password")]
            refused = [await ask(c, h, FORGET, body, 400) for body in ({}, *({"name": name} for name in others))]
            return environment, refused

    environment, refused = run(go())
    assert all(answer.json() == {"error": FROM_ENVIRONMENT} for answer in environment)
    assert all("error" in answer.json() for answer in refused)
    assert memory_vault.store == before  # nothing was taken out, the one under the environment's neither
    assert {name: in_vault(memory_vault, name) for name in saved} == saved
    assert config.get_secret(config.NTFY_TOPIC_NAME) == TOPIC


def test_a_vault_that_wont_let_a_password_go_keeps_it_and_its_day(srv, job_apply_home, memory_vault, monkeypatch):
    """All or nothing, as the phone alerts' topic is forgotten: the vault refuses, so the password stays, its day stays,
    and the answer says the vault wouldn't let it go. (A vault that refused is out of use until the desk restarts: the
    page says so, and says no day for a password it can't read meanwhile.)"""
    on_day(monkeypatch, "2026-11-10")
    desk = idle_desk(srv)

    def refuse(*args):
        raise keyring.errors.KeyringLocked("locked")

    async def go():
        async with serving(desk) as (c, h):
            await ask(c, h, MAKE, {"name": "workday_password"})
            made = in_vault(memory_vault, "workday_password")
            monkeypatch.setattr(memory_vault, "delete_password", refuse)
            answer = await ask(c, h, FORGET, {"name": "workday_password"}, 400)
            meanwhile = (await c.get("/api/state", headers=h)).json()
            vault.reset()  # (as the desk's next start finds the vault: answering again)
            return made, answer, meanwhile, (await c.get("/api/state", headers=h)).json()

    made, answer, meanwhile, state = run(go())
    assert "wouldn't let it go" in answer.json()["error"] and made not in answer.text
    assert in_vault(memory_vault, "workday_password") == made and keeps_the_rules(made)
    assert meanwhile["vault_problem"] and meanwhile["passwords"]["workday"] is False
    assert meanwhile["passwords_made"] == {}
    assert state["passwords"]["workday"] is True and config.get_secret("workday_password") == made
    assert state["passwords_made"] == made_days(job_apply_home) == {"workday_password": "2026-11-10"}


def test_without_a_vault_forget_takes_its_line_out_of_the_file_and_leaves_the_rest(srv, job_apply_home, no_vault,
                                                                                    monkeypatch):
    on_day(monkeypatch, "2026-11-10")
    path = config.secrets_path()
    path.write_text("# mine\nhoneywell_password: keep-me\n", encoding="utf-8")
    desk = idle_desk(srv)

    async def go():
        async with serving(desk) as (c, h):
            await ask(c, h, MAKE, {})
            kept = config.get_secret("icims_password")
            assert (await ask(c, h, FORGET, {"name": "workday_password"})).json() == {"forgotten": True}
            return kept, (await c.get("/api/state", headers=h)).json()

    kept, state = run(go())
    saved = config.read_secrets(path)
    assert list(saved) == ["honeywell_password", *SITE[1:]] and saved["icims_password"] == kept
    assert path.read_text(encoding="utf-8").startswith("# mine\nhoneywell_password: keep-me\n")
    assert state["passwords"]["workday"] is False and state["passwords"]["icims"] is True
    assert state["passwords_made"] == {name: "2026-11-10" for name in SITE[1:]}


# ------------------------------------------------------------------ Type my own (E7's test 8)
def test_typing_a_password_over_a_made_one_takes_its_day_out_of_made_passwords(srv, job_apply_home, memory_vault,
                                                                              monkeypatch):
    """E7's test 8: saved through the existing /api/password, it's no longer one the desk made. One that isn't saved (a
    line break in it) leaves the made one and its day."""
    on_day(monkeypatch, "2026-11-10")
    desk = idle_desk(srv)

    async def go():
        async with serving(desk) as (c, h):
            await ask(c, h, MAKE, {})
            made = config.get_secret("icims_password")
            before = (await c.get("/api/state", headers=h)).json()["passwords_made"]
            typed = await c.post("/api/password", headers=h,
                                 json={"name": "workday_password", "value": "Typed-by-me-2"})
            bad = await c.post("/api/password", headers=h, json={"name": "icims_password", "value": "a\nb"})
            return made, before, typed, bad, (await c.get("/api/state", headers=h)).json()

    made, before, typed, bad, state = run(go())
    assert before == {name: "2026-11-10" for name in SITE}
    assert typed.json() == {"saved": True} and bad.status_code == 400
    assert config.get_secret("workday_password") == "Typed-by-me-2" and config.get_secret("icims_password") == made
    others = {name: "2026-11-10" for name in SITE if name != "workday_password"}
    assert state["passwords_made"] == others and made_days(job_apply_home) == others
    assert state["passwords"]["workday"] is True
    assert Desk(srv).made_passwords == others


def test_a_password_typed_while_the_desk_makes_its_passwords_is_never_written_over(srv, job_apply_home, memory_vault,
                                                                                 monkeypatch):
    """Make my passwords and Type my own pressed together: whichever is saved first, the typed one is the one kept,
    and it has no day (saved ones are left alone, and a typed one is never one the desk made)."""
    on_day(monkeypatch, "2026-11-10")
    desk = idle_desk(srv)
    last = SITE[-1]
    real = memory_vault.set_password

    def slow(*args):  # (a vault that takes a moment over each save, so the two meet)
        threading.Event().wait(0.02)
        real(*args)

    monkeypatch.setattr(memory_vault, "set_password", slow)

    async def go():
        async with serving(desk) as (c, h):
            async def typed():
                await asyncio.sleep(0.1)
                return await c.post("/api/password", headers=h, json={"name": last, "value": "Typed-by-me-2"})

            made, saved = await asyncio.gather(ask(c, h, MAKE, {}), typed())
            return made, saved, (await c.get("/api/state", headers=h)).json()

    made, saved, state = run(go())
    assert saved.json() == {"saved": True} and made.json()["made"] in (SITE, SITE[:-1])
    assert config.get_secret(last) == "Typed-by-me-2"
    days = {name: "2026-11-10" for name in SITE[:-1]}
    assert state["passwords_made"] == days and made_days(job_apply_home) == days
    assert state["passwords"][last.removesuffix("_password")] is True


# ------------------------------------------------------------------ what the review asks of all three
def test_every_answer_of_the_three_routes_says_no_store_a_refusal_and_a_failure_too(srv, job_apply_home, memory_vault,
                                                                                   monkeypatch, caplog):
    """Cache-Control: no-store on every answer the three routes give: 200, 400, 403, 404, 409, and a failure nobody
    foresaw, which is answered and logged by its kind alone, never in its own words (they could hold anything)."""
    config.save_site_password("workday_password", "Typed-Workday-1")
    monkeypatch.setenv("JOB_APPLY_SECRET_TALEO_PASSWORD", "set-in-the-environment")
    caplog.set_level(logging.DEBUG)
    desk = idle_desk(srv)
    seen: list = []
    real = config.get_secret

    def boom(*args, **kwargs):
        raise RuntimeError("the vault said Typed-Workday-1")

    def unread(name):  # (the one asked for here alone: the desk's own looks at the others go on)
        return boom() if name == "workday_password" else real(name)

    async def go():
        async with serving(desk) as (c, h):
            typed = {**h, "content-type": "application/json"}
            for path in (MAKE, SHOW, FORGET):
                for headers in ({}, {"x-desk-token": "guéss".encode("latin-1")}, {**h, "host": "evil.example"}):
                    seen.append(await ask(c, headers, path, {"name": "workday_password"}, 403))
                for body in (b"not json", b"[1]", b"null", b'"workday_password"'):
                    r = await c.post(path, headers=typed, content=body)
                    assert r.status_code == 400, (path, body, r.status_code)
                    seen.append(r)
                seen.append(await ask(c, h, path, {"name": config.NTFY_TOPIC_NAME}, 400))
            seen.append(await ask(c, h, SHOW, {"name": "icims_password"}, 404))
            seen.append(await ask(c, h, SHOW, {"name": "workday_password"}, 200))
            # one from the environment is left alone by Make my passwords; Make a new one and Forget say why
            environment = [await ask(c, h, MAKE, {"name": "taleo_password", "replace": True}, 409),
                           await ask(c, h, FORGET, {"name": "taleo_password"}, 409)]
            assert (await ask(c, h, MAKE, {"name": "taleo_password"})).json() == {"made": []}
            seen.append(await ask(c, h, MAKE, {"name": "icims_password"}, 200))
            seen.append(await ask(c, h, FORGET, {"name": "icims_password"}, 200))
            with monkeypatch.context() as m:
                m.setattr(config, "make_site_password", boom, raising=False)
                m.setattr(config, "forget_password", boom, raising=False)
                failed = [await ask(c, h, path, {"name": "workday_password", "replace": True}, 500)
                          for path in (MAKE, FORGET)]
                m.setattr(config, "get_secret", unread)
                none = await ask(c, h, SHOW, {"name": "workday_password"}, 404)  # (none the desk could type just now)
            return environment, failed, none

    environment, failed, none = run(go())
    for answer in (*seen, *environment, *failed, none):
        assert answer.headers["cache-control"] == "no-store", answer.status_code
    assert all(answer.json() == {"error": FROM_ENVIRONMENT} for answer in environment)
    assert in_vault(memory_vault, "taleo_password") is None
    for answer in (*failed, none):
        assert "Typed-Workday-1" not in answer.text and "the vault said" not in answer.text
    assert all("RuntimeError" in answer.json()["error"] for answer in failed)
    assert "Typed-Workday-1" not in caplog.text and "the vault said" not in caplog.text
    assert config.get_secret("workday_password") == "Typed-Workday-1"


def test_the_three_routes_ask_for_a_saved_password_off_the_desks_event_loop(srv, job_apply_home, memory_vault,
                                                                           monkeypatch):
    """The vault's call can wait vault.VAULT_WAIT seconds (a keyring locked since): each route makes it on a thread, as
    the page's save does, never on the loop the page's every poll is answered on."""
    desk = idle_desk(srv)
    asked: list = []
    loop_thread = threading.current_thread()

    def noting(what: str):
        real = getattr(config, what, None)

        def noted(name, *args, **kwargs):
            if name == "icims_password":
                asked.append((what, threading.current_thread() is loop_thread))
            return real(name, *args, **kwargs)
        return noted

    async def go():
        async with serving(desk) as (c, h):
            for what in ("make_site_password", "get_secret", "forget_password"):
                monkeypatch.setattr(config, what, noting(what), raising=False)
            await ask(c, h, MAKE, {"name": "icims_password"})
            await ask(c, h, SHOW, {"name": "icims_password"})
            await ask(c, h, FORGET, {"name": "icims_password"})

    run(go())
    assert {what for what, _ in asked} == {"make_site_password", "get_secret", "forget_password"}
    assert not any(on_loop for _, on_loop in asked), asked


def test_made_passwords_in_a_desk_json_changed_by_hand_is_read_as_names_and_days_alone(srv, job_apply_home,
                                                                                      memory_vault):
    """desk.json's made_passwords goes on to the page's state: only a job system's name with a day, never anything else
    a file changed by hand may hold. And a day is listed only while that password is saved."""
    for name in ("workday_password", "icims_password", "email_password"):
        config.save_site_password(name, "Typed-or-made-1")
    wrong = {"workday_password": "2026-11-10", "icims_password": "<b>yesterday</b>", "ukg_password": "2026-11-09",
             "taleo_password": 20261110, "infor_password": "2026-13-45", "email_password": "2026-11-10",
             "honeywell_password": "2026-11-10", "neogov_password": None}
    for said, days in ((wrong, {"workday_password": "2026-11-10", "ukg_password": "2026-11-09"}),
                       (["workday_password"], {}), ("2026-11-10", {}), (None, {})):
        (job_apply_home / "desk.json").write_text(json.dumps({"auto_submit": True, "made_passwords": said}),
                                                  encoding="utf-8")
        desk = Desk(srv)
        assert desk.made_passwords == days and desk.applier.auto_submit is True, said
        # (none is saved for UKG Pro, taken out of the vault another way since: its day isn't said)
        assert desk.state()["passwords_made"] == {n: d for n, d in days.items() if n == "workday_password"}, said


def test_config_makes_and_forgets_only_the_names_on_its_own_lists(job_apply_home, memory_vault, monkeypatch):
    """Beneath the routes: config.make_site_password makes a job system's password and no other, and leaves a saved
    one alone unless told to replace it; config.forget_password forgets a job system's, the email app password or the
    API key, and no other; and config.forget_secret still forgets the phone alerts' topic alone."""
    for name in ("email_password", config.API_KEY_NAME, config.NTFY_TOPIC_NAME, "honeywell_password",
                 "Workday_Password", "", None, ["workday_password"]):
        for replace in (False, True):
            with pytest.raises(ValueError):
                config.make_site_password(name, replace)
    assert memory_vault.store == {}
    assert config.make_site_password("workday_password") is True
    made = config.get_secret("workday_password")
    assert keeps_the_rules(made) and config.make_site_password("workday_password") is False
    assert config.get_secret("workday_password") == made
    # another program's save since the vault was last read is found, and left alone
    assert config.get_secret("icims_password") is None
    memory_vault.set_password(vault.service(), "icims_password", "saved-by-another-program")
    assert config.get_secret("icims_password") is None  # (as the vault was last read, for vault.READ_FOR seconds)
    assert config.make_site_password("icims_password") is False
    assert config.get_secret("icims_password") == "saved-by-another-program"
    assert config.make_site_password("workday_password", replace=True) is True
    assert keeps_the_rules(config.get_secret("workday_password")) and config.get_secret("workday_password") != made
    monkeypatch.setenv("JOB_APPLY_SECRET_UKG_PASSWORD", "set-in-the-environment")
    assert config.make_site_password("ukg_password") is False and in_vault(memory_vault, "ukg_password") is None

    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)
    config.save_site_password("honeywell_password", "Hand-Named-Pw-9")
    assert config.FORGETTABLE == (*SITE, "email_password", config.API_KEY_NAME)
    for name in (config.NTFY_TOPIC_NAME, "honeywell_password", "Workday_Password", vault.NAMES_ENTRY, "", None,
                 ["workday_password"]):
        with pytest.raises(ValueError):
            config.forget_password(name)
    assert config.get_secret(config.NTFY_TOPIC_NAME) == TOPIC
    assert config.get_secret("honeywell_password") == "Hand-Named-Pw-9"
    assert config.forget_password("workday_password") is True and config.get_secret("workday_password") is None
    assert config.forget_password("workday_password") is False  # (none left)
    with pytest.raises(ValueError):
        config.forget_secret("icims_password")  # (the topic's own: never a password)
    assert config.get_secret("icims_password") == "saved-by-another-program"
    assert config.forget_secret(config.NTFY_TOPIC_NAME) is True
