"""How far a job is from home (the design of Oct 18, F8): the towns of data/places.yaml, the straight-line distance,
the nearest of a job's places, and what "Within __ miles" keeps. Made-up profiles only (the repo is public)."""

import re

import pytest
import yaml

from job_apply import config, places
from job_apply.recommend import _METROS
from job_apply.search import STATE_CITIES, employer_lists

# each state's rough box (latitude, longitude), a little wider than its borders: a typo puts a town outside it
BOXES = {"AZ": ((31.3, 37.05), (-114.85, -109.0)), "NV": ((34.95, 42.05), (-120.05, -114.0))}


def town(name: str, state: str) -> places.Place:
    return next(t for t in places.known()[name.lower()] if t.state == state)


def test_every_town_is_inside_its_state():
    """So a typo can't put Tempe in the ocean: each entry is inside its state's box, and the file's states are the
    boxes'."""
    data = yaml.safe_load(places.PLACES_FILE.read_text(encoding="utf-8"))
    assert set(data) == set(BOXES)
    for state, entry in data.items():
        (south, north), (west, east) = BOXES[state]
        for name, (lat, lon) in entry["towns"].items():
            assert south <= lat <= north and west <= lon <= east, (name, state, lat, lon)
        assert set(entry["needs_state"]) <= set(entry["towns"]), state


def test_every_town_the_lists_and_the_search_name_is_known():
    """The towns of search.STATE_CITIES (Arizona's and Nevada's), recommend._METROS, and the lists' az_sites and
    nv_sites each have an entry."""
    known = set(places.known())
    named = {*STATE_CITIES["AZ"], *STATE_CITIES["NV"], *(t for metro in _METROS for t in metro)}
    for path in employer_lists().values():
        for company in yaml.safe_load(path.read_text(encoding="utf-8"))["companies"]:
            for key in ("az_sites", "nv_sites"):
                # ("Mesa (Polytechnic)", "Las Vegas (corporate office, The STRAT)": the town before its aside)
                for part in re.sub(r"\([^)]*\)", "", str(company.get(key) or "")).split(","):
                    words = part.strip()
                    if words and words[0].isupper() and not re.search(r"(?i)area|wide|none|across|field", words):
                        named.add(words.lower())
    assert sorted(named - known) == []


def test_the_distance_is_a_straight_line_in_miles():
    """Two known pairs, to within two miles: Phoenix to Tucson about 106 miles in a straight line (the drive is
    about 115), Las Vegas to Henderson about 13."""
    assert 104 <= places.miles(town("Phoenix", "AZ"), town("Tucson", "AZ")) <= 110
    assert 11 <= places.miles(town("Las Vegas", "NV"), town("Henderson", "NV")) <= 15
    assert 250 <= places.miles(town("Phoenix", "AZ"), town("Las Vegas", "NV")) <= 262
    assert places.miles(town("Mesa", "AZ"), town("Mesa", "AZ")) == 0


def test_a_job_in_several_places_is_as_near_as_its_nearest():
    home = town("Chandler", "AZ")
    said = places.away(home, "Tucson, AZ; Tempe, AZ | Flagstaff, AZ")
    assert said is not None and said["place"] == "Tempe, AZ" and said["sure"] and 8 <= said["miles"] <= 12
    # (its list's place and its posting's: the nearest of either, sure when either names all of its places)
    said = places.away(home, "2 Locations", "Phoenix, AZ or Mesa, AZ")
    assert said is not None and said["place"] == "Mesa, AZ" and said["sure"]
    assert places.away(home, "Chandler, AZ")["home"] is True


@pytest.mark.parametrize("location, towns, sure", [
    ("Chandler, AZ", ["Chandler, AZ"], True),
    ("US-AZ-Chandler", ["Chandler, AZ"], True),
    ("CHANDLER, Arizona, United States of America", ["Chandler, AZ"], True),
    ("North Las Vegas, NV", ["North Las Vegas, NV"], True),  # (not Las Vegas: the longest name)
    ("Sun City West, AZ", ["Sun City West, AZ"], True),
    ("Paradise Valley, AZ", ["Paradise Valley, AZ"], True),
    ("Chandler; Tempe, AZ", ["Chandler, AZ", "Tempe, AZ"], True),
    ("Peoria", ["Peoria, AZ"], True),  # (a town only one state here has: the search's area found it)
    # a town's name as a road's, a county's or a school's: not the town
    ("4500 W Chandler Blvd, Phoenix, AZ", ["Phoenix, AZ"], True),
    ("Maricopa County, AZ", [], False),
    ("University of Phoenix", [], False),
    # a name that is also a word or another place's, with no state: unknown; with it, known
    ("Paradise", [], False),
    ("Enterprise", [], False),
    ("Paradise, NV", ["Paradise, NV"], True),
    ("Tonopah", [], False),  # (Arizona's and Nevada's)
    # places it can't place, or more than it names: never sure
    ("Tempe, AZ (+3 more)", ["Tempe, AZ"], False),
    ("Phoenix, AZ; Statewide", ["Phoenix, AZ"], False),
    ("Phoenix, AZ; Arizona", ["Phoenix, AZ"], False),
    ("Phoenix, AZ; Remote", ["Phoenix, AZ"], False),
    ("2 Locations", [], False),
    ("Arizona", [], False),
    ("Banner Heart Hosp (6750 E Baywood Ave)", [], False),
    ("Peoria, IL", [], False),  # (another state's Peoria)
    ("Austin, TX", [], False),
    ("Remote - US", [], False),
    ("", [], False),
    # (the review) longer names that are near places of their own, never a far town's: Del Webb's Sun City
    # communities in the Las Vegas valley, and the old Williams air base's names in Gilbert and Mesa
    ("Sun City Anthem", ["Sun City Anthem, NV"], True),
    ("Sun City Anthem, Henderson, NV", ["Sun City Anthem, NV", "Henderson, NV"], True),
    ("Sun City Aliante", ["Sun City Aliante, NV"], True),
    ("Sun City Summerlin, NV", ["Sun City Summerlin, NV"], True),
    ("Williams Field, AZ", ["Williams Field, AZ"], True),
    ("Williams Gateway", ["Williams Gateway, AZ"], True),
    ("Sun City, AZ", ["Sun City, AZ"], True),
    # (the review) a comma-set piece naming a place it doesn't know, beside one it does: not all its places
    ("Tucson, AZ, Fort Huachuca, AZ", ["Tucson, AZ"], False),
    ("Tucson, AZ, Higley, AZ", ["Tucson, AZ"], False),
    ("Tucson, Higley", ["Tucson, AZ"], False),
    ("Chandler, AZ, US, 85226", ["Chandler, AZ"], True),
    ("Chandler, AZ 85226, USA", ["Chandler, AZ"], True),
    ("Tempe, AZ, United States (Hybrid)", ["Tempe, AZ"], True),
    ("Ahwatukee, Phoenix, AZ", ["Ahwatukee, AZ", "Phoenix, AZ"], True),
    ("Las Vegas, NV (Summerlin)", ["Las Vegas, NV", "Summerlin, NV"], True),
    # (the review) a state as newspapers write it, or a code in lower case: another state's town isn't ours
    ("Las Vegas, N.M.", [], False),
    ("Peoria, Ill.", [], False),
    ("Peoria, il", [], False),
    ("Glendale, Calif.", [], False),
    ("Chandler, Ariz.", ["Chandler, AZ"], True),
    ("Henderson, Nev. 89052", ["Henderson, NV"], True),
    ("Chandler, az", ["Chandler, AZ"], True),
])
def test_a_location_names_the_towns_it_names(location, towns, sure):
    named, whole = places.read(location)
    assert ([t.label for t in named], whole) == (towns, sure)


def test_only_a_job_surely_farther_than_asked_and_not_remote_is_left_out():
    home = town("Chandler", "AZ")
    tucson = places.away(home, "Tucson, AZ")
    assert places.keeps(tucson, "", 100) and not places.keeps(tucson, "", 50)
    assert not places.keeps(tucson, "onsite", 50)
    assert not places.keeps(tucson, "hybrid", 50)  # (a hybrid job is judged by its place)
    assert places.keeps(tucson, "remote", 10)  # (a remote one always stays)
    assert places.keeps(places.away(home, "Tucson, AZ (+2 more)"), "", 10)  # (its other places may be nearer)
    assert places.away(home, "Austin, TX") is None and places.keeps(None, "", 10)  # (an unknown place stays)
    assert places.keeps(tucson, "", None)  # (any distance)


@pytest.mark.parametrize("locations", [
    ("Tucson, AZ", "Tucson, AZ (+2 more)"),
    ("Tucson, AZ", "Multiple Locations"),
    ("Tucson, AZ", "Tucson, AZ; Statewide"),
    ("Tucson, AZ", "Tucson, AZ; Remote"),
    ("Tucson, AZ", "Tucson, AZ, Fort Huachuca, AZ"),
    ("Tucson, AZ", None, "Phoenix, AZ (+1 more)"),
])
def test_one_location_saying_more_places_makes_the_distance_unsure(locations):
    """(The review, M1) a job's list may say one town while its posting says more ("+2 more", "Multiple Locations",
    "Statewide", "Remote", a place it can't place): its distance isn't sure, so Within never hides it."""
    said = places.away(town("Chandler", "AZ"), *locations)
    assert said is not None and said["sure"] is False
    assert places.keeps(said, "", 25)


def test_home_is_the_profiles_town_or_why_not():
    def home(**address):
        return places.home(config.Profile({"personal": {"address": address}}))

    assert home(city="Chandler", state="AZ")[0] == town("Chandler", "AZ")
    assert home(city="chandler", state="arizona")[0] == town("Chandler", "AZ")
    assert home(city="Henderson")[0] == town("Henderson", "NV")  # (a name one state here has)
    assert home(city="Mesa", state="Arizona ")[1] == ""
    none, why = home()
    assert none is None and why == places.NO_HOME and "personal.address.city" in why
    assert places.home(config.Profile({}))[0] is None
    none, why = home(city="Springfield", state="IL")
    assert none is None and "Springfield, IL" in why and "isn't one of them" in why
    assert home(city="Tonopah")[0] is None  # (Arizona's or Nevada's: it needs the state)
    assert home(city="Tonopah", state="NV")[0] == town("Tonopah", "NV")
    assert home(city="Mesa", state="CO")[0] is None
    assert home(city="Chandler", state="Ariz.")[0] == town("Chandler", "AZ")
    # (the review, L2) a town whose name is also a word or another place's, with no state: it asks for the state
    for city in ("Tonopah", "Paradise", "Florence", "Miami", "Page", "Douglas", "Williams"):
        none, why = home(city=city)
        assert none is None and f"which {city} you mean" in why and "personal.address.state" in why, city
    assert home(city="Paradise", state="NV")[0] == town("Paradise", "NV")
