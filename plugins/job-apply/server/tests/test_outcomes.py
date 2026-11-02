"""What's working (the owner's item 2, learn what works from responses): the person's own applications' outcomes by
group (outcomes.py, from Tracker.outcomes), the desk's What's working card, and where what worked before places an
opening in the desk's list (recommend.worked_before: its order only, never its score), which
settings.learn_from_responses turns off. Made-up employers only: the repo is public."""

import random
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from conftest import browser_available

from job_apply import config, outcomes
from job_apply.desk import Desk
from job_apply.outcomes import MIN_GROUP, TEST_MIN, Learned, Tally, fisher, keys_of, learn, summary, takeaways
from job_apply.recommend import WORKED_BEFORE_CAP, worked_before, worked_before_points
from job_apply.tracker import Tracker

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=60)).isoformat()  # sent long enough ago to count, answered or not
NEW = (NOW - timedelta(days=3)).isoformat()  # sent too lately to count without an answer


def _row(ats="workday", answered=False, further=False, sent=OLD, first="", company="Example Fab",
         title="Equipment Technician", status="applied", source="job desk", applied=None):
    return {"ats": ats, "company": company, "title": title, "source": source, "status": status, "sent_at": sent,
            "applied_at": sent if applied is None else applied, "answered": answered, "further": further,
            "first_answer": first}


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
    t.conn.execute("UPDATE jobs SET applied_at = ? WHERE id = ?", (sent if status == "applied" else "", job["id"]))
    t.conn.commit()
    return job["id"]


def test_the_tracker_says_which_applications_were_answered_and_went_further(job_apply_home):
    """An answer is an assessment, an interview, an offer or a rejection, by email or by the status the person set;
    a "we received it" or an email of another kind isn't one. Going further is an assessment, an interview or an
    offer, now or before (rejected after an interview still went further). A job never sent in isn't counted."""
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
    assert rows["Talk Fab"]["sent_at"] == OLD and rows["Talk Fab"]["ats"] == "workday"


def test_a_status_set_by_mistake_and_put_back_says_nothing(job_apply_home):
    """The review of da222be: interviewing set by mistake, then applied again with no email, counted as an interview.
    A status the job was moved back from says nothing; its emails count either way, and a rejection after an
    interview still went further."""
    t = Tracker()
    oops = _job(t, 1, "Oops Fab")
    t.update(oops, status="interviewing")
    t.update(oops, status="applied")
    told = _job(t, 2, "Told Fab")
    t.log_email(told, "t-1", "interview", "Phone screen", _after(4))
    t.update(told, status="applied")  # (put back by hand, but the interview email came)
    no = _job(t, 3, "No Fab")
    t.update(no, status="rejected")
    t.update(no, status="applied")
    later = _job(t, 4, "Later Fab")
    t.update(later, status="interviewing")
    t.update(later, status="rejected")
    rows = {r["company"]: (r["answered"], r["further"]) for r in t.outcomes()}
    assert rows == {"Oops Fab": (False, False), "Told Fab": (True, True), "No Fab": (False, False),
                    "Later Fab": (True, True)}, rows


def test_the_days_to_an_answer_are_from_real_answer_times_only(job_apply_home):
    """The review of da222be: the days to a first answer are from the day it was applied to the answer email's time:
    a status set by hand (its time is when the person set it) counts as an answer but not in the days; a thread whose
    row a later message replaced keeps its first answer's time (what My applications shows stays the latest
    message's); and a job marked interviewing without ever being applied has no days."""
    t = Tracker()
    talk = _job(t, 1, "Talk Fab")
    t.log_email(talk, "t-1", "interview", "Phone screen", _after(5))
    t.log_email(talk, "t-1", "rejection", "Not moving forward", _after(20))  # (the same thread, later)
    hand = _job(t, 2, "Hand Fab")
    t.update(hand, status="rejected")
    straight = _job(t, 3, "Straight Fab", status="interviewing")
    t.log_email(straight, "s-1", "interview", "Phone screen", _after(6))
    rows = {r["company"]: r for r in t.outcomes()}
    assert rows["Talk Fab"]["first_answer"] == _after(5) and rows["Talk Fab"]["applied_at"] == OLD
    heard = t.last_heard()[talk]
    assert (heard["category"], heard["received_at"], heard["emails"]) == ("rejection", _after(20), 1)
    assert rows["Hand Fab"]["answered"] and rows["Hand Fab"]["first_answer"] == ""
    assert rows["Straight Fab"]["applied_at"] == "" and rows["Straight Fab"]["first_answer"] == _after(6)
    learned = learn(list(rows.values()), [], now=NOW)
    assert (learned.overall.n, learned.overall.answered, learned.overall.days) == (3, 3, [5.0])
    # a row from before the first answer's time was kept: its own time, as before
    t.conn.execute("UPDATE emails SET answered_at = '' WHERE thread_id = 's-1'")
    t.conn.commit()
    assert {r["company"]: r["first_answer"] for r in t.outcomes()}["Straight Fab"] == _after(6)


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


def test_fishers_exact_test():
    assert fisher(6, 12, 0, 20) == pytest.approx(924 / 906192, rel=1e-6)  # (only the table seen is as unlikely)
    assert fisher(3, 10, 2, 10) == pytest.approx(1.0)
    assert fisher(0, 10, 0, 10) == pytest.approx(1.0)
    assert fisher(1, 10, 9, 10) == pytest.approx(0.0011, abs=1e-4)


def test_a_takeaway_only_where_the_numbers_hold_it_up():
    said = takeaways(learn(_workday_beats_icims(), [], now=NOW))
    assert said == ["Applications through Workday led to an interview or a test more often than those through iCIMS: "
                    "6 of 12 (50%), against 0 of 20 (0%)."]
    rows = ([_row("workday", True, first=_after(5))] * 9 + [_row("workday")] * 3
            + [_row("icims", True, first=_after(5))] * 3 + [_row("icims")] * 17)
    assert takeaways(learn(rows, [], now=NOW)) == [
        "Applications through Workday got an answer more often than those through iCIMS: 9 of 12 (75%), against 3 of "
        "20 (15%)."]
    # one group against the rest of its kind: more often, or less
    rows = ([_row("lever", True, True, first=_after(5))] * 8 + [_row("lever")] * 4
            + [_row("workday")] * 15 + [_row("icims", True, True, first=_after(5))] + [_row("icims")] * 14)
    assert takeaways(learn(rows, [], now=NOW)) == [
        "Applications through Lever led to an interview or a test more often than your others: 8 of 12 (67%), "
        "against 1 of 30 (3%)."]
    # too small to say: 3 of 5 against none of 5; 3 of 10 against 2 of 10; a group under TEST_MIN, however it went
    for small in ([_row("workday", True, True, first=_after(5))] * 3 + [_row("workday")] * 2 + [_row("icims")] * 5,
                  [_row("workday", True, True, first=_after(5))] * 3 + [_row("workday")] * 7
                  + [_row("icims", True, True, first=_after(5))] * 2 + [_row("icims")] * 8,
                  [_row("lever", True, True, first=_after(1))] * (TEST_MIN - 1) + [_row("icims")] * 30):
        assert takeaways(learn(small, [], now=NOW)) == []
    # employers aren't compared (too many, each too small): only shown in the card's table
    rows = ([_row("workday", True, True, first=_after(5), company="Good Fab")] * 12
            + [_row("workday", company=f"Quiet Fab {i % 2}") for i in range(20)])
    assert takeaways(learn(rows, [], now=NOW)) == []


SYSTEMS = ["workday", "icims", "greenhouse", "lever", "taleo"]


def _random_rows(n: int, r: random.Random, better: str = "", rate: float = 0.25) -> list[dict]:
    """`n` applications at made-up employers, every group alike (a quarter answered, a third of those to an
    interview), or one job system (`better`) with interviews at `rate`."""
    out = []
    for _ in range(n):
        system = r.choice(SYSTEMS)
        further = r.random() < (rate if system == better else 0.09)
        answered = further or r.random() < 0.18
        out.append({"ats": system, "company": f"Fab {r.randrange(max(1, n // 10))}", "title": "Technician",
                    "source": r.choice(["job desk", "indeed"]), "status": "applied", "sent_at": OLD,
                    "applied_at": OLD, "answered": answered, "further": further,
                    "pay_min": r.choice([18, 25, 35, 50]), "pay_period": "hour", "pay_source": "structured"})
    return out


def test_on_numbers_with_no_real_difference_a_takeaway_is_rare():
    """The review of da222be: every pair of groups compared, uncorrected, said a takeaway in 74 to 100% of runs on
    numbers where every group is alike. Now each group is compared with the rest of its kind by Fisher's exact test,
    Holm-corrected over every comparison made: 5% of runs at most (seeded, 200 runs each)."""
    for n in (60, 150, 300):
        r = random.Random(n)
        said = sum(bool(takeaways(learn(_random_rows(n, r), [], now=NOW))) for _ in range(200))
        assert said <= 10, (n, said)
    # a strong real difference is still said: Workday's interviews at half, the others' at 9%
    r = random.Random(7)
    said = [takeaways(learn(_random_rows(150, r, better="workday", rate=0.5), [], now=NOW)) for _ in range(20)]
    assert sum(any("through Workday led to an interview or a test more often" in s for s in t) for t in said) >= 15


# ------------------------------------------------------------------ the nudge: its cap and its smoothing


def test_the_nudge_never_passes_its_cap_and_is_none_without_outcomes():
    assert WORKED_BEFORE_CAP == 10  # (a tenth of the score's 0 to 100)
    for total, hits in ((40, 4), (40, 20), (100, 1), (12, 11)):
        overall = Tally(n=total, further=hits)
        for n in range(0, 61):
            for k in range(0, n + 1):
                points = worked_before_points(Tally(n=n, further=k), overall)
                assert -WORKED_BEFORE_CAP <= points <= WORKED_BEFORE_CAP, (total, hits, n, k, points)
                if n < TEST_MIN or (points > 0 and k < 3):
                    assert points == 0, (n, k)
    # nothing to tell apart: no outcomes, none went further, or all did
    assert worked_before_points(Tally(n=10, further=5), Tally()) == 0
    assert worked_before_points(Tally(n=10, further=0), Tally(n=30, further=0)) == 0
    assert worked_before_points(Tally(n=10, further=10), Tally(n=30, further=30)) == 0
    assert worked_before_points(None, Tally(n=30, further=5)) == 0
    # one interview in a few: nothing, however few the person's are (the review of da222be: it gave the whole +10)
    assert worked_before_points(Tally(n=12, further=1), Tally(n=100, further=1)) == 0


def test_a_small_group_moves_a_job_less_than_a_big_one_at_the_same_rate():
    overall = Tally(n=100, further=10)
    small, big = worked_before_points(Tally(n=12, further=3), overall), worked_before_points(Tally(n=40, further=10),
                                                                                            overall)
    assert 0 < small < big <= WORKED_BEFORE_CAP, (small, big)
    small, big = (worked_before_points(Tally(n=n, further=0), overall) for n in (12, 40))
    assert -WORKED_BEFORE_CAP <= big < small < 0, (small, big)
    assert worked_before_points(Tally(n=20, further=2), overall) == 0  # (at the person's own rate: nothing)


def _fit(score=62, recommended=True):
    return {"score": score, "recommended": recommended, "reasons": [], "concerns": [],
            "parts": [{"why": "start", "points": 50}, {"why": "title matches", "points": score - 50}]}


def test_worked_before_places_an_opening_and_never_changes_its_score():
    """The review of da222be: the shown score crossed the recommended line while recommended stayed as it was. Now
    the score, its parts and recommended are recommend()'s; the nudge is `rank` (the order) and `placed` (why)."""
    learned = learn(_workday_beats_icims(), [], now=NOW)
    up = worked_before(_fit(), learned, keys_of({"ats": "workday", "company": "Other Fab"}, []))
    down = worked_before(_fit(), learned, keys_of({"ats": "icims", "company": "Other Fab"}, []))
    for fit in (up, down):
        assert {k: fit[k] for k in _fit()} == _fit(), fit
    assert up["rank"] > 62 > down["rank"] >= 62 - WORKED_BEFORE_CAP
    assert up["placed"] == ("Placed higher: applications through Workday led to an interview or a test 6 times in 12; "
                            "yours overall, 6 in 32")
    assert down["placed"].startswith("Placed lower: applications through iCIMS led to an interview or a test not "
                                     "once in 20")
    # nothing known of its groups, no outcomes, no difference that holds up: as it was
    assert worked_before(_fit(), learned, keys_of({"ats": "lever"}, [])) == _fit()
    assert worked_before(_fit(), Learned(), keys_of({"ats": "workday"}, [])) == _fit()
    even = learn([_row("workday", True, True, first=_after(5))] * 3 + [_row("workday")] * 9
                 + [_row("icims", True, True, first=_after(5))] * 5 + [_row("icims")] * 15, [], now=NOW)
    assert worked_before(_fit(), even, keys_of({"ats": "workday"}, [])) == _fit()
    # two groups that hold up: the bigger move, never the two added up
    rows = [{**r, "source": "indeed" if r["ats"] == "workday" else "job desk"} for r in _workday_beats_icims()]
    both = worked_before(_fit(), learn(rows, [], now=NOW), keys_of({"ats": "workday", "source": "indeed"}, []))
    assert both["rank"] - 62 == up["rank"] - 62 <= WORKED_BEFORE_CAP


def test_on_numbers_with_no_real_difference_almost_no_opening_moves():
    """The review of da222be: it moved 99.8% of openings on numbers where every group is alike. Now only a difference
    that holds up moves one: 5% of openings at most (seeded: 100 runs of 150 applications, 20 openings each)."""
    r = random.Random(11)
    moved = total = 0
    for _ in range(100):
        learned = learn(_random_rows(150, r), [], now=NOW)
        for job in _random_rows(20, r):
            total += 1
            moved += worked_before(_fit(), learned, keys_of(job, [])) != _fit()
    assert moved <= total * 0.05, (moved, total)


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
    desk.listings = [{"url": "https://example.wd1.myworkdayjobs.com/Careers/job/Tempe/Tech_99", "ats": "workday",
                      "title": "Equipment Technician", "company": "Example Litho", "location": "Tempe, AZ",
                      "fit": _fit()},
                     {"url": "https://careers-example.icims.com/jobs/9999/technician/job", "ats": "icims",
                      "title": "Equipment Technician", "company": "Example Litho", "location": "Tempe, AZ",
                      "fit": _fit()}]
    return desk


def test_the_desk_shows_what_worked_and_places_by_it_unless_turned_off(srv, job_apply_home):
    desk = _desk_with_outcomes(srv)
    state = desk.state()
    fits = {r["system"]["key"]: r["fit"] for r in state["listings"]}
    assert fits["workday"]["score"] == fits["icims"]["score"] == 62 and fits["icims"]["recommended"] is True
    assert fits["workday"]["rank"] > 62 > fits["icims"]["rank"], fits
    assert fits["workday"]["placed"].startswith("Placed higher: applications through Workday")
    working = state["outcomes"]
    assert (working["n"], working["further"], working["nudging"]) == (32, 6, True)
    assert [r["label"] for r in next(g for g in working["groups"] if g["dimension"] == "system")["rows"]] == \
        ["iCIMS", "Workday"]
    assert working["takeaways"] and "Workday" in working["takeaways"][0]
    assert state["settings"]["learn_from_responses"] is True
    # turned off: the card still counts; the order doesn't lean on it
    profile = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    profile["settings"]["learn_from_responses"] = False
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    state = desk.state()
    assert all(r["fit"] == _fit() for r in state["listings"]), [r["fit"] for r in state["listings"]]
    assert state["outcomes"]["nudging"] is False and state["outcomes"]["n"] == 32
    assert state["settings"]["learn_from_responses"] is False


def test_the_nudge_reorders_the_desks_list(srv, job_apply_home):
    """The review of da222be: the nudge changed a score, but the rows stayed in their search's order (the page's Fit
    sort keeps the desk's). The iCIMS opening, a little better on its own (64 against 63), comes after the Workday one
    once what worked before places them; turned off, the search's order again."""
    desk = _desk_with_outcomes(srv)
    workday, icims = desk.listings
    workday["fit"], icims["fit"] = _fit(63), _fit(64)
    desk.listings = [icims, workday]
    order = [(r["system"]["key"], r["fit"]["score"]) for r in desk.state()["listings"]]
    assert order == [("workday", 63), ("icims", 64)], order
    profile = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    profile["settings"]["learn_from_responses"] = False
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    assert [r["system"]["key"] for r in desk.state()["listings"]] == ["icims", "workday"]


def test_the_desk_without_applications_has_no_card_and_moves_nothing(srv):
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.listings = [{"url": "https://example.com/job/1", "title": "Technician", "company": "Example Fab",
                      "fit": _fit()}]
    state = desk.state()
    assert state["outcomes"] is None and state["listings"][0]["fit"] == _fit()


@pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")
def test_the_card_says_too_few_yet_and_when_the_order_doesnt_use_it(srv, job_apply_home, monkeypatch):
    from test_desk_cards import _by_card, _cards, _state

    state = _state("state-openings.json")

    def card(outcomes_said):
        return "".join(_by_card(_cards(srv, monkeypatch, {**state, "outcomes": outcomes_said}))["working-h"])

    few = {**state["outcomes"], "n": 3, "waiting": 2, "enough": False, "groups": [], "takeaways": []}
    said = card(few)
    assert "Once 5 of your applications have had an answer, or 3 weeks without one" in said, said
    assert "So far: 3 of 5 (and 2 sent in the last 3 weeks)." in said and "By job system" not in said, said
    said = card({**state["outcomes"], "nudging": False})
    assert "Off in your profile (settings.learn_from_responses: false)" in said and "Placed higher" not in said, said
    assert "Only groups with 5 applications or more are shown." in said
    said = card(state["outcomes"])
    assert "Scores, and which openings are recommended, don't change." in said, said
    assert card(None) == "(hidden)\n\n"


def test_nothing_of_the_outcomes_is_logged(srv, caplog):
    """Counted, never written down: working them out logs nothing (problem reports carry the desk's logs)."""
    desk = _desk_with_outcomes(srv)
    with caplog.at_level("DEBUG", logger="job_apply"):
        outcomes.summary(outcomes.learn(srv.tracker().outcomes(), []), True, WORKED_BEFORE_CAP)
        desk.state()
    assert not [r for r in caplog.records if "Example Fab" in r.getMessage() or "Placed" in r.getMessage()]
