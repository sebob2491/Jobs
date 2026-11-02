"""What's working (the owner's item 2, learn what works from responses): the person's own applications' outcomes by
group (outcomes.py, from Tracker.outcomes), the desk's What's working card, and the ranking's capped "worked before"
part (recommend.worked_before), which settings.learn_from_responses turns off. Made-up employers only: the repo is
public."""

from datetime import datetime, timedelta, timezone

import pytest
import yaml
from conftest import browser_available

from job_apply import config, outcomes
from job_apply.desk import Desk
from job_apply.outcomes import MIN_GROUP, Learned, Tally, keys_of, learn, summary, takeaways
from job_apply.recommend import WORKED_BEFORE_CAP, worked_before, worked_before_points
from job_apply.tracker import Tracker

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=60)).isoformat()  # sent long enough ago to count, answered or not
NEW = (NOW - timedelta(days=3)).isoformat()  # sent too lately to count without an answer


def _row(ats="workday", answered=False, further=False, sent=OLD, first="", company="Example Fab",
         title="Equipment Technician", status="applied", source="job desk"):
    return {"ats": ats, "company": company, "title": title, "source": source, "status": status, "sent_at": sent,
            "answered": answered, "further": further, "first_answer": first}


def _after(days: int) -> str:
    return (datetime.fromisoformat(OLD) + timedelta(days=days)).isoformat()


def _workday_beats_icims() -> list[dict]:
    """12 applications through Workday, 6 of them to an interview (a week each); 20 through iCIMS, 2 rejections (two
    weeks each) and no interview; one more through iCIMS sent three days ago."""
    return ([_row("workday", True, True, first=_after(7))] * 6 + [_row("workday")] * 6
            + [_row("icims", True, False, first=_after(14))] * 2 + [_row("icims")] * 18 + [_row("icims", sent=NEW)])


# ------------------------------------------------------------------ the tracker's outcomes


def _job(t: Tracker, n: int, company: str, status: str = "applied", sent: str = OLD) -> int:
    job, _ = t.upsert({"url": f"https://example.wd1.myworkdayjobs.com/Careers/job/Tempe/Tech_{n}",
                       "title": "Equipment Technician", "company": company, "ats": "workday"})
    t.update(job["id"], status=status)
    t.conn.execute("UPDATE jobs SET applied_at = ? WHERE id = ?", (sent, job["id"]))
    t.conn.commit()
    return job["id"]


def test_the_tracker_says_which_applications_were_answered_and_went_further(job_apply_home):
    """An answer is an assessment, an interview, an offer or a rejection, by email or by the status the person set;
    a "we received it" or an email of another kind isn't one. Going further is an assessment, an interview or an
    offer, now or before (rejected after an interview still went further). The first answer's time is the earliest of
    them, and a job never sent in isn't counted."""
    t = Tracker()
    quiet = _job(t, 1, "Quiet Fab")
    t.log_email(quiet, "q-1", "confirmation", "We received it", _after(1))
    t.log_email(quiet, "q-2", "other", "A job alert", _after(2))
    talk = _job(t, 2, "Talk Fab")
    t.log_email(talk, "t-1", "interview", "Phone screen", _after(5))
    t.log_email(talk, "t-2", "rejection", "Not moving forward", _after(20))
    no = _job(t, 3, "No Fab")
    t.log_email(no, "n-1", "rejection", "Not moving forward", _after(9))
    hand = _job(t, 4, "Hand Fab")
    t.update(hand, status="interviewing")  # (marked by the person, no email)
    _job(t, 5, "Saved Fab", status="saved")
    rows = {r["company"]: r for r in t.outcomes()}
    assert set(rows) == {"Quiet Fab", "Talk Fab", "No Fab", "Hand Fab"}
    said = {c: (r["answered"], r["further"]) for c, r in rows.items()}
    assert said == {"Quiet Fab": (False, False), "Talk Fab": (True, True), "No Fab": (True, False),
                    "Hand Fab": (True, True)}, said
    assert rows["Talk Fab"]["first_answer"] == _after(5) and rows["No Fab"]["first_answer"] == _after(9)
    assert rows["Quiet Fab"]["first_answer"] == "" and rows["Hand Fab"]["first_answer"]
    assert rows["Talk Fab"]["sent_at"] == OLD and rows["Talk Fab"]["ats"] == "workday"


# ------------------------------------------------------------------ the stats per group


def test_each_group_counts_its_applications_answers_and_interviews():
    learned = learn(_workday_beats_icims(), ["Equipment Technician"], now=NOW)
    o = learned.overall
    assert (o.n, o.answered, o.further, learned.waiting) == (32, 8, 6, 1)
    workday, icims = learned.tally("system", "workday"), learned.tally("system", "icims")
    assert (workday.n, workday.answered, workday.further, workday.label) == (12, 6, 6, "Workday")
    assert (icims.n, icims.answered, icims.further, icims.label) == (20, 2, 0, "iCIMS")
    assert workday.median_days() == 7 and icims.median_days() is None  # (2 answers: too few to say)
    assert o.median_days() == 7
    assert learned.tally("title", "equipment technician").n == 32
    assert learned.tally("employer", "example fab").n == 32


def test_one_not_answered_waits_three_weeks_and_one_withdrawn_unanswered_isnt_counted():
    rows = [_row(sent=NEW), _row(sent=NEW, answered=True, first=NEW), _row(status="withdrawn"),
            _row(status="withdrawn", answered=True, first=_after(3)), _row(sent="")]
    learned = learn(rows, [], now=NOW)
    assert (learned.overall.n, learned.waiting) == (2, 2)


def test_the_groups_an_opening_is_in():
    job = {"ats": "icims", "company": "Example Fab", "title": "Senior Process Engineer", "source": "indeed",
           "pay_min": 25, "pay_max": 30, "pay_period": "hour", "pay_source": "structured", "workplace": ""}
    keys = keys_of(job, ["Process Engineer", "Technician"], miles=12.0)
    assert {d: k[0] for d, k in keys.items()} == {"system": "icims", "employer": "example fab", "source": "indeed",
                                                  "title": "process engineer", "pay": "$60k_to_$80k",
                                                  "distance": "10_to_25_miles"}
    assert keys["source"][2] == "found on Indeed" and keys["pay"][1] == "$60k to $80k"
    bare = keys_of({"title": "Welder", "workplace": "remote"}, ["Technician"])
    assert {d: k[0] for d, k in bare.items()} == {"title": "other", "pay": "none", "distance": "remote"}


# ------------------------------------------------------------------ the minimum size, and the card's state


def test_a_group_under_five_isnt_shown_and_too_few_say_nothing():
    rows = _workday_beats_icims() + [_row("lever", True, True, first=_after(3))] * (MIN_GROUP - 1)
    shown = summary(learn(rows, [], now=NOW), True, WORKED_BEFORE_CAP)
    systems = next(g for g in shown["groups"] if g["dimension"] == "system")
    assert [r["label"] for r in systems["rows"]] == ["iCIMS", "Workday"]  # (Lever's 4: not shown)
    assert systems["rows"][1] == {"label": "Workday", "n": 12, "answered": 6, "further": 6, "answered_rate": "50%",
                                  "further_rate": "50%", "median_days": 7}
    # every application in one group (one employer, one title): nothing to compare, not shown
    assert {g["dimension"] for g in shown["groups"]} == {"system"}
    assert (shown["min_group"], shown["enough"], shown["nudging"], shown["cap"]) == (5, True, True, 10)
    # four with an outcome: not enough for anything but the count
    few = summary(learn([_row("workday", True, True, first=_after(2))] * 4 + [_row(sent=NEW)], [], now=NOW),
                  False, WORKED_BEFORE_CAP)
    assert (few["n"], few["waiting"], few["enough"], few["groups"], few["takeaways"]) == (4, 1, False, [], [])
    assert few["median_days"] is None
    # none sent: no card
    assert summary(learn([], [], now=NOW), True, WORKED_BEFORE_CAP) is None


# ------------------------------------------------------------------ the takeaways


def test_a_takeaway_only_where_the_numbers_hold_it_up():
    said = takeaways(learn(_workday_beats_icims(), [], now=NOW))
    assert said == ["Applications through Workday led to an interview or a test 6 times in 12; those through iCIMS, "
                    "not once in 20."]
    # a ratio, where both had some
    rows = ([_row("workday", True, first=_after(5))] * 9 + [_row("workday")] * 3
            + [_row("icims", True, first=_after(5))] * 5 + [_row("icims")] * 15)
    assert takeaways(learn(rows, [], now=NOW)) == [
        "Applications through Workday got an answer 3 times as often as those through iCIMS (12 vs 20 applications)."]
    # too small to say: 2 of 5 against none of 5, and 3 of 10 against 2 of 10
    for small in ([_row("workday", True, True, first=_after(5))] * 2 + [_row("workday")] * 3 + [_row("icims")] * 5,
                  [_row("workday", True, True, first=_after(5))] * 3 + [_row("workday")] * 7
                  + [_row("icims", True, True, first=_after(5))] * 2 + [_row("icims")] * 8):
        assert takeaways(learn(small, [], now=NOW)) == []
    # a group under five never makes one, however it went
    lopsided = [_row("lever", True, True, first=_after(1))] * 4 + [_row("icims")] * 30
    assert takeaways(learn(lopsided, [], now=NOW)) == []


# ------------------------------------------------------------------ the nudge: its cap and its smoothing


def test_the_nudge_never_passes_its_cap_and_is_none_without_outcomes():
    assert WORKED_BEFORE_CAP == 10  # (a tenth of the score's 0 to 100)
    for total, hits in ((40, 4), (40, 20), (100, 1), (12, 11)):
        overall = Tally(n=total, further=hits)
        for n in range(0, 61):
            for k in range(0, n + 1):
                points = worked_before_points(Tally(n=n, further=k), overall)
                assert -WORKED_BEFORE_CAP <= points <= WORKED_BEFORE_CAP, (total, hits, n, k, points)
                if n < MIN_GROUP:
                    assert points == 0, (n, k)
    # nothing to tell apart: no outcomes, none went further, or all did
    assert worked_before_points(Tally(n=10, further=5), Tally()) == 0
    assert worked_before_points(Tally(n=10, further=0), Tally(n=30, further=0)) == 0
    assert worked_before_points(Tally(n=10, further=10), Tally(n=30, further=30)) == 0
    assert worked_before_points(None, Tally(n=30, further=5)) == 0


def test_a_small_group_moves_a_job_less_than_a_big_one_at_the_same_rate():
    overall = Tally(n=100, further=10)
    small, big = (worked_before_points(Tally(n=n, further=n // 2), overall) for n in (6, 40))
    assert 0 < small < big <= WORKED_BEFORE_CAP, (small, big)
    small, big = (worked_before_points(Tally(n=n, further=0), overall) for n in (6, 40))
    assert -WORKED_BEFORE_CAP <= big < small <= 0, (small, big)
    # at the person's own rate: nothing
    assert worked_before_points(Tally(n=20, further=2), overall) == 0


def _fit(score=62, recommended=True):
    return {"score": score, "recommended": recommended, "reasons": [], "concerns": [],
            "parts": [{"why": "start", "points": 50}, {"why": "title matches", "points": score - 50}]}


def test_worked_before_moves_the_score_and_says_why_but_never_the_recommendation():
    learned = learn(_workday_beats_icims(), [], now=NOW)
    up = worked_before(_fit(), learned, keys_of({"ats": "workday", "company": "Other Fab"}, []))
    down = worked_before(_fit(), learned, keys_of({"ats": "icims", "company": "Other Fab"}, []))
    for fit in (up, down):
        assert sum(p["points"] for p in fit["parts"]) == fit["score"], fit
        assert fit["recommended"] is True  # (under RECOMMEND_AT now, and still among the recommended: never hidden)
    assert up["score"] > 62 > down["score"] >= 62 - WORKED_BEFORE_CAP
    assert up["parts"][-1]["why"] == ("worked before: applications through Workday led to an interview or a test "
                                      "6 times in 12; yours overall, 6 in 32")
    assert down["parts"][-1]["why"].startswith("worked before: applications through iCIMS led to an interview or a "
                                               "test not once in 20")
    # kept within 0 to 100, said as what it moved
    top = worked_before(_fit(98), learned, keys_of({"ats": "workday"}, []))
    assert top["score"] == 100 and top["parts"][-1]["points"] == 2
    # nothing known of its groups, no outcomes, or a fit from before scores had parts: as it was
    assert worked_before(_fit(), learned, keys_of({"ats": "lever"}, [])) == _fit()
    assert worked_before(_fit(), Learned(), keys_of({"ats": "workday"}, [])) == _fit()
    old = {"score": 70, "recommended": True}
    assert worked_before(old, learned, keys_of({"ats": "workday"}, [])) == old


# ------------------------------------------------------------------ the setting


def test_learn_from_responses_is_on_unless_the_profile_turns_it_off():
    def on(settings):
        return config.Settings.from_dict(settings).learn_from_responses

    assert on({}) is True and on({"learn_from_responses": None}) is True and on({"learn_from_responses": "yes"})
    for off in (False, "no", "off", "false"):
        assert on({"learn_from_responses": off}) is False, off
    assert "learn_from_responses" in config.KNOWN_SETTINGS
    assert config.Settings.from_dict({"learn_from_responses": False}).warnings == []
    template = yaml.safe_load(config.TEMPLATE_PROFILE.read_text(encoding="utf-8"))
    assert template["settings"]["learn_from_responses"] is True


# ------------------------------------------------------------------ on the desk


def _desk_with_outcomes(srv) -> Desk:
    """A tracker holding 12 Workday applications (6 to an interview) and 20 iCIMS ones (none), all made up, and two
    openings: one on each system."""
    t = srv.tracker()
    sent = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    for n in range(32):
        system = "workday" if n < 12 else "icims"
        url = (f"https://example.wd1.myworkdayjobs.com/Careers/job/Tempe/Tech_{n}" if system == "workday"
               else f"https://careers-example.icims.com/jobs/{1000 + n}/technician/job")
        job, _ = t.upsert({"url": url, "title": "Equipment Technician", "company": f"Example Fab {n % 3}",
                           "ats": system})
        t.update(job["id"], status="applied")
        t.conn.execute("UPDATE jobs SET applied_at = ? WHERE id = ?", (sent, job["id"]))
        t.conn.commit()
        if n < 6:
            t.log_email(job["id"], f"thread-{n}", "interview", "Phone screen", "")
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.listings = [{"url": f"https://example.wd1.myworkdayjobs.com/Careers/job/Tempe/Tech_{s}", "ats": s,
                      "title": "Equipment Technician", "company": "Example Litho", "location": "Tempe, AZ",
                      "fit": _fit()} for s in ("workday",)] + [
        {"url": "https://careers-example.icims.com/jobs/9999/technician/job", "ats": "icims",
         "title": "Equipment Technician", "company": "Example Litho", "location": "Tempe, AZ", "fit": _fit()}]
    return desk


def test_the_desk_shows_what_worked_and_leans_on_it_unless_turned_off(srv, job_apply_home):
    desk = _desk_with_outcomes(srv)
    state = desk.state()
    fits = {r["system"]["key"]: r["fit"] for r in state["listings"]}
    assert fits["workday"]["score"] > 62 > fits["icims"]["score"], fits
    assert fits["workday"]["parts"][-1]["why"].startswith("worked before: applications through Workday")
    assert fits["icims"]["recommended"] is True
    working = state["outcomes"]
    assert (working["n"], working["further"], working["nudging"]) == (32, 6, True)
    assert [r["label"] for r in next(g for g in working["groups"] if g["dimension"] == "system")["rows"]] == \
        ["iCIMS", "Workday"]
    assert working["takeaways"] and "Workday" in working["takeaways"][0]
    assert state["settings"]["learn_from_responses"] is True
    # turned off: the card still counts, the scores don't lean on it
    profile = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    profile["settings"]["learn_from_responses"] = False
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    state = desk.state()
    assert all(r["fit"] == _fit() for r in state["listings"]), [r["fit"] for r in state["listings"]]
    assert state["outcomes"]["nudging"] is False and state["outcomes"]["n"] == 32
    assert state["settings"]["learn_from_responses"] is False


def test_the_desk_without_applications_has_no_card_and_moves_nothing(srv):
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.listings = [{"url": "https://example.com/job/1", "title": "Technician", "company": "Example Fab",
                      "fit": _fit()}]
    state = desk.state()
    assert state["outcomes"] is None and state["listings"][0]["fit"] == _fit()


@pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")
def test_the_card_says_too_few_yet_and_when_the_ranking_doesnt_use_it(srv, job_apply_home, monkeypatch):
    from test_desk_cards import _by_card, _cards, _state

    state = _state("state-openings.json")

    def card(outcomes_said):
        return "".join(_by_card(_cards(srv, monkeypatch, {**state, "outcomes": outcomes_said}))["working-h"])

    few = {**state["outcomes"], "n": 3, "waiting": 2, "enough": False, "groups": [], "takeaways": []}
    said = card(few)
    assert "Once 5 of your applications have had an answer, or 3 weeks without one" in said, said
    assert "So far: 3 of 5 (and 2 sent in the last 3 weeks)." in said and "By job system" not in said, said
    said = card({**state["outcomes"], "nudging": False})
    assert "Off in your profile (settings.learn_from_responses: false)" in said and "worked before" not in said, said
    assert "Only groups with 5 applications or more are shown." in said
    assert card(None) == "(hidden)\n\n"


def test_nothing_of_the_outcomes_is_logged(srv, caplog):
    """Counted, never written down: working them out logs nothing (problem reports carry the desk's logs)."""
    desk = _desk_with_outcomes(srv)
    with caplog.at_level("DEBUG", logger="job_apply"):
        outcomes.summary(outcomes.learn(srv.tracker().outcomes(), []), True, WORKED_BEFORE_CAP)
        desk.state()
    assert not [r for r in caplog.records if "Example Fab" in r.getMessage() or "worked" in r.getMessage()]
