"""What and where to search (the Job Desk's "What and where to search" window and Claude's search_area tool): the job
titles and places in the profile's preferences.titles and preferences.locations and the plugin's employer lists in the
person's companies.yaml, changed in place, every other line of those files (comments too) kept as the person wrote
it."""

import pytest
import yaml

from job_apply import config, search
from job_apply.recommend import target_location

PROFILE = """# My profile
personal:
  first_name: Pat   # what they go by
preferences:
  titles: [Field Service Engineer]
  locations: []               # e.g. ["Phoenix, AZ", "Chandler, AZ", "Tempe, AZ"]
  willing_to_relocate: false
answers: []
"""


def test_a_list_is_set_in_place_and_every_other_line_kept():
    out = config.set_yaml_value(PROFILE, ("preferences", "locations"), ["Phoenix, AZ", "Las Vegas, NV"])
    assert yaml.safe_load(out)["preferences"]["locations"] == ["Phoenix, AZ", "Las Vegas, NV"]
    assert out.replace('["Phoenix, AZ", "Las Vegas, NV"]', "[]") == PROFILE
    assert '# e.g. ["Phoenix, AZ"' in out and "# what they go by" in out


@pytest.mark.parametrize("written", [
    "  locations:\n    - Phoenix, AZ\n    - Tempe, AZ\n",      # a block list, indented
    "  locations:\n  - Phoenix, AZ\n",                        # a block list at the key's own indent
    "  locations: Phoenix, AZ\n",                             # one place, written bare
    "  locations: ~\n",
    "",                                                        # no such key: added under preferences
])
def test_a_list_written_any_way_is_replaced_whole(written):
    text = PROFILE.replace("  locations: []               # e.g. [\"Phoenix, AZ\", \"Chandler, AZ\", \"Tempe, AZ\"]\n",
                           written)
    out = config.set_yaml_value(text, ("preferences", "locations"), ["Las Vegas, NV"])
    data = yaml.safe_load(out)
    assert data["preferences"]["locations"] == ["Las Vegas, NV"]
    assert data["preferences"]["willing_to_relocate"] is False and data["answers"] == [] \
        and data["personal"]["first_name"] == "Pat"


def test_a_missing_section_is_added_and_a_top_level_key_set():
    out = config.set_yaml_value("personal:\n  first_name: Pat\n", ("preferences", "locations"), ["Reno, NV"])
    assert yaml.safe_load(out) == {"personal": {"first_name": "Pat"}, "preferences": {"locations": ["Reno, NV"]}}
    text = "# mine\nlists: [phoenix-metro]   # the plugin's lists\ncompanies: []\n"
    out = config.set_yaml_value(text, ("lists",), ["phoenix-metro", "las-vegas"])
    assert yaml.safe_load(out) == {"lists": ["phoenix-metro", "las-vegas"], "companies": []}
    assert "# the plugin's lists" in out and out.startswith("# mine\n")


def test_a_file_laid_out_another_way_is_never_half_changed():
    with pytest.raises(ValueError):
        config.set_yaml_value("preferences: {locations: [Phoenix, AZ]}\n", ("preferences", "locations"), ["x"])
    with pytest.raises(ValueError):
        config.set_yaml_value("preferences:\n  locations: [\"a\",\n    \"b\"]\n", ("preferences", "locations"), ["x"])
    with pytest.raises(ValueError):
        config.set_yaml_value("preferences: [\n", ("preferences", "locations"), ["x"])


def test_places_and_lists_are_saved_for_find_jobs(job_apply_home):
    assert search.search_area()["lists"] == [search.BUILTIN_LIST]
    search.set_search_area(places=["Phoenix, AZ", "Las Vegas, NV", "  "], lists=["phoenix-metro", search.BUILTIN_LIST])
    area = search.search_area()
    assert area["places"] == ["Phoenix, AZ", "Las Vegas, NV"]
    assert area["lists"] == ["phoenix-metro", search.BUILTIN_LIST]
    assert target_location(config.Profile.load()) == "AZ|NV"
    assert {c["name"] for c in search.load_companies()} >= {"Banner Health", "ASML"}
    # the person's own file kept as it was, but its lists
    own = search.own_companies_path()
    own.write_text("# mine\nlists: [phoenix-metro]\ncompanies:\n  - name: Example Fab\n    careers_url: "
                   "https://example.com\ninclude_builtin: true\n", encoding="utf-8")
    search.set_search_area(lists=["phoenix-metro"])
    data = yaml.safe_load(own.read_text(encoding="utf-8"))
    assert data["lists"] == ["phoenix-metro"] and data["companies"][0]["name"] == "Example Fab"
    assert search.search_area()["lists"] == ["phoenix-metro"]  # (include_builtin turned off with its list)
    assert "ASML" not in {c["name"] for c in search.load_companies()}
    with pytest.raises(ValueError):
        search.set_search_area(lists=["no-such-list"])
    with pytest.raises(ValueError):
        own.unlink()
        search.set_search_area(lists=[])  # nothing to search at all


def test_nevadas_towns_are_known_to_the_search():
    terms = search.location_terms("AZ|NV")
    assert {"las vegas", "henderson", "north las vegas", "reno", "phoenix"} <= set(terms)
    assert search.location_matches("Henderson, NV", search.location_terms("NV")) is True


def test_claude_reads_and_changes_where_find_jobs_searches(srv):
    from conftest import run

    now = run(srv.search_area())
    assert now["lists"] == [search.BUILTIN_LIST] and {c["name"] for c in now["choices"]} >= {"phoenix-metro"}
    out = run(srv.search_area(places=["Las Vegas, NV"], lists=["phoenix-metro"]))
    assert out["places"] == ["Las Vegas, NV"] and out["lists"] == ["phoenix-metro"] and out["states"] == ["NV"]
    assert run(srv.search_area())["places"] == ["Las Vegas, NV"]


def test_job_titles_are_saved_for_find_jobs_one_each(job_apply_home):
    from job_apply.recommend import target_query

    config.profile_path().write_text(PROFILE, encoding="utf-8")
    area = search.search_area()
    assert area["titles"] == ["Field Service Engineer"] and area["searched_titles"] == search.MAX_ALTERNATIVES
    assert area["default_titles"] == search.DEFAULT_TITLES and "IT" in area["title_ideas"]
    search.set_search_area(titles=["Help Desk Technician", " IT  Support Specialist ", "help desk technician", ""])
    assert search.search_area()["titles"] == ["Help Desk Technician", "IT Support Specialist"]
    assert target_query(config.Profile.load()) == "Help Desk Technician | IT Support Specialist"
    assert "first_name: Pat   # what they go by" in config.profile_path().read_text(encoding="utf-8")
    for two in (["Help Desk | Recruiter"], ["Analyst OR Engineer"]):  # (the search would read two titles)
        with pytest.raises(ValueError, match="One job title each"):
            search.set_search_area(titles=two)
    with pytest.raises(ValueError):
        search.set_search_area(titles=[f"Title {n}" for n in range(13)])
    # titles and places together: one write of the profile, neither when either is refused
    with pytest.raises(ValueError):
        search.set_search_area(titles=["Recruiter"], places=["Las Vegas"])
    assert search.search_area()["titles"] == ["Help Desk Technician", "IT Support Specialist"]
    search.set_search_area(titles=["Recruiter"], places=["Las Vegas, NV"])
    area = search.search_area()
    assert area["titles"] == ["Recruiter"] and area["places"] == ["Las Vegas, NV"]
    search.set_search_area(titles=[])  # none: the plugin's own
    assert search.search_area()["titles"] == [] and target_query(config.Profile.load()).startswith("field service")


def test_claude_changes_the_job_titles_too(srv):
    from conftest import run

    out = run(srv.search_area(titles=["Systems Administrator"]))
    assert out["titles"] == ["Systems Administrator"] and out["places"] == []


def test_the_desks_what_and_where_window_saves_and_finds_jobs_again(srv):
    """The header's What and where button says what Find jobs searches; its window changes the job titles, the
    places and the lists, saves them to the profile and companies.yaml, clears the openings found for others and
    starts Find jobs again. A place it can't search is said in the window, and nothing is saved."""
    import asyncio
    import time

    from conftest import browser_available, launch_options, run
    if not browser_available():
        pytest.skip("needs Chromium")
    from playwright.async_api import async_playwright

    from job_apply.desk import Desk

    search.set_search_area(places=["Phoenix, AZ"], titles=["Field Service Engineer"])
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())  # (recent: opening the page doesn't search the real sites)
    desk.listings = [{"url": "https://example.com/job/1", "title": "Technician", "company": "Example Fab"}]
    searched: list[float] = []

    async def find_jobs():
        searched.append(time.time())

    desk.find_jobs = find_jobs

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_function("document.getElementById('area-btn').textContent.includes('Phoenix, AZ')")
                assert "Field Service Engineer" in await page.text_content("#area-btn")
                await page.click("#area-btn")
                assert await page.is_visible("#area")
                assert await page.is_checked(f"[data-area-list='{search.BUILTIN_LIST}']")
                # a title from the ideas, one typed (Enter adds it), the old one taken out
                await page.click("[data-area-add='titles'][data-value='Help Desk Technician']")
                await page.fill("#area-title-new", "Network Administrator")
                await page.press("#area-title-new", "Enter")
                await page.click("[data-area-drop='titles'][data-i='0']")
                # a place from the ideas, one typed with no state (refused, said in the window)
                await page.click("[data-area-add='places'][data-value='Las Vegas, NV']")
                await page.fill("#area-place-new", "Reno")
                await page.check("[data-area-list='phoenix-metro']")
                await asyncio.sleep(2)  # (the page refreshes every 1.5 s: what's being changed stays put)
                titles = await page.locator("#area-titles .chip").all_text_contents()
                assert [t.replace("×", "").strip() for t in titles] == ["Help Desk Technician", "Network Administrator"]
                await page.click("#area-form button[type=submit]")
                await page.wait_for_selector("#area-error:not([hidden])")
                assert "state" in (await page.text_content("#area-error") or "")
                assert await page.is_visible("#area") and not searched
                assert search.search_area()["places"] == ["Phoenix, AZ"]  # (nothing saved)
                await page.click("[data-area-drop='places'][data-i='2']")  # Reno out
                await page.click("#area-form button[type=submit]")
                for _ in range(50):
                    if searched:
                        break
                    await asyncio.sleep(0.1)
                assert not await page.is_visible("#area")
                await page.wait_for_function("document.getElementById('area-btn').textContent.includes('Help Desk')")
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert searched, "Find jobs wasn't started again"
    area = search.search_area()
    assert area["titles"] == ["Help Desk Technician", "Network Administrator"]
    assert area["places"] == ["Phoenix, AZ", "Las Vegas, NV"]
    assert set(area["lists"]) == {search.BUILTIN_LIST, "phoenix-metro"}
    assert desk.listings == []


# ---------------------------------------------------------------- the review of 0.3.195's findings

def test_a_town_of_any_named_metro_is_near():
    """With places in two metros (Phoenix and Las Vegas, the card's own example) a Henderson, NV posting was blocked
    as "away from the places you named": the metros knew no Las Vegas valley, and only the first that matched
    counted. Every named place's metro counts."""
    from job_apply.recommend import _far_place
    assert _far_place("Henderson, NV", ["phoenix", "las vegas"]) is None
    assert _far_place("Summerlin, NV", ["phoenix", "las vegas"]) is None
    assert _far_place("Oro Valley, AZ", ["phoenix", "tucson"]) is None
    assert _far_place("Bagdad, AZ", ["phoenix", "las vegas"]) == "Bagdad, AZ"
    assert _far_place("Reno, NV", ["las vegas"]) == "Reno, NV"


def test_a_save_while_find_jobs_runs_searches_the_new_area(srv, monkeypatch):
    """Saving Where to search while Find jobs was still searching the old area: the old search finished and its
    openings were kept as the new area's, and the new search never started (one was still running)."""
    import asyncio

    from conftest import run

    from job_apply import desk as desk_module
    from job_apply.desk import Desk

    released = asyncio.Event()
    calls: list[str] = []

    async def fake_recommend(prof, run_search, **kw):
        where = ",".join(prof.get("preferences.locations") or [])
        calls.append(where)
        if len(calls) == 1:
            await released.wait()  # (the first search, of the old area, still going)
        return {"results": [{"url": f"https://example.com/{where}", "title": "Tech", "fit": {}}], "query": "q",
                "location": where, "errors": {}, "browser_only": []}

    monkeypatch.setattr(desk_module, "recommend", fake_recommend)
    search.set_search_area(places=["Phoenix, AZ"])
    desk = Desk(srv)

    async def go():
        first = asyncio.ensure_future(desk.find_jobs())
        desk._search_task = first
        await asyncio.sleep(0.05)
        search.set_search_area(places=["Las Vegas, NV"])
        desk._forget_openings()
        second = asyncio.ensure_future(desk.find_jobs())
        await second
        released.set()
        await first

    run(go())
    assert [r["url"] for r in desk.listings] == ["https://example.com/Las Vegas, NV"], desk.listings
    assert desk.search["status"] == "done" and desk.search["location"] == "Las Vegas, NV"


def test_a_save_either_file_refuses_writes_neither(job_apply_home):
    """Places went into profile.yaml before companies.yaml was found unchangeable (a list over several lines): the
    save failed half done. Both are worked out first; nothing is written unless both can be."""
    profile = config.profile_path().read_text(encoding="utf-8")
    search.own_companies_path().write_text("lists: [phoenix-metro,\n  semiconductor-az]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        search.set_search_area(places=["Reno, NV"], lists=["phoenix-metro"])
    assert config.profile_path().read_text(encoding="utf-8") == profile


@pytest.mark.parametrize("text", ['"lists": [a]\n', "lists : [a]\n", "'lists': [a]\n"])
def test_a_key_spelled_another_way_is_refused_not_written_twice(text):
    """A key the line match didn't see ("lists" quoted, a space before its colon) got a second one appended, which
    YAML reads in place of the person's first."""
    with pytest.raises(ValueError):
        config.set_yaml_value(text, ("lists",), ["b"])


def test_a_byte_order_mark_stays_and_the_key_after_it_is_found():
    """PowerShell 5 writes a byte order mark before the first key; the key after it is the one changed."""
    out = config.set_yaml_value("﻿lists: [a]\ncompanies: []\n", ("lists",), ["b"])
    assert out == '﻿lists: ["b"]\ncompanies: []\n'
    out = config.set_yaml_value("﻿preferences:\n  locations: []\n", ("preferences", "locations"), ["Reno, NV"])
    assert yaml.safe_load(out)["preferences"]["locations"] == ["Reno, NV"] and out.startswith("﻿")


@pytest.mark.parametrize("line, comment", [
    ('  locations: "Phoenix, AZ"  # where I\'d go', "  # where I'd go"),
    ("  locations: ~  # don't know yet", "  # don't know yet"),
    ('  locations: ["a]b"]  # c', "  # c"),
    ("  locations: [Phoenix]   # e.g. [x]", "   # e.g. [x]"),
])
def test_a_values_comment_is_kept_whatever_the_value_holds(line, comment):
    out = config.set_yaml_value(f"preferences:\n{line}\n", ("preferences", "locations"), ["Reno, NV"])
    assert out == f'preferences:\n  locations: ["Reno, NV"]{comment}\n'


def test_a_town_name_shared_abroad_or_with_arizona_isnt_nevada():
    nv = search.location_terms("NV")
    for place in ("Winchester, Hampshire, United Kingdom", "Saint-Jean-sur-Richelieu, Quebec, Canada",
                  "Minden, Germany", "Paradise, Newfoundland, Canada", "Paradise Valley"):
        assert search.location_matches(place, nv) is not True, place


def test_a_place_without_its_state_is_turned_away_and_the_states_searched_are_said(job_apply_home):
    """"Las Vegas" (no state) was left out of the search without a word, and "Phoenix, AZ, Las Vegas, NV" typed with
    commas searched Arizona alone."""
    with pytest.raises(ValueError, match="state"):
        search.set_search_area(places=["Las Vegas"])
    with pytest.raises(ValueError, match="semicolon"):
        search.set_search_area(places=["Phoenix, AZ, Las Vegas, NV"])
    search.set_search_area(places=["Phoenix, Arizona", "Las Vegas, NV", "Remote"])
    assert search.search_area()["states"] == ["AZ", "NV"]


def test_the_tool_clears_an_open_desks_openings(srv):
    """Claude's search_area changed where Find jobs looks while the desk kept showing the old area's openings."""
    from conftest import run

    from job_apply import desk as desk_module
    from job_apply.desk import Desk

    desk = Desk(srv)
    desk.listings = [{"url": "https://example.com/1", "title": "Tech"}]
    desk_module._desk = desk
    try:
        run(srv.search_area(places=["Las Vegas, NV"]))
        assert desk.listings == [] and desk.search["status"] == "idle"
        desk.listings = [{"url": "https://example.com/1", "title": "Tech"}]
        run(srv.search_area())  # (only reading: the openings stay)
        assert desk.listings
    finally:
        desk_module._desk = None


@pytest.mark.skipif(__import__("os").name == "nt", reason="symbolic links need privileges on Windows")
def test_a_linked_profile_stays_a_link(job_apply_home, tmp_path):
    real = tmp_path / "elsewhere" / "profile.yaml"
    real.parent.mkdir()
    real.write_text(config.profile_path().read_text(encoding="utf-8"), encoding="utf-8")
    config.profile_path().unlink()
    config.profile_path().symlink_to(real)
    search.set_search_area(places=["Reno, NV"])
    assert config.profile_path().is_symlink() and "Reno, NV" in real.read_text(encoding="utf-8")


def test_eightfolds_page_search_isnt_narrowed_to_one_of_several_states():
    """Eightfold's browser search sent only the first of "AZ|NV": Nevada's openings there were never seen. With
    several states its page is asked for every place, and the places are kept here."""
    from urllib.parse import parse_qs, urlparse
    cfg = {"host": "careers.example.com", "domain": "example.com"}
    assert parse_qs(urlparse(search.eightfold_page_url(cfg, "technician", "AZ")).query)["location"] == ["Arizona"]
    assert "location" not in parse_qs(urlparse(search.eightfold_page_url(cfg, "technician", "AZ|NV")).query)
