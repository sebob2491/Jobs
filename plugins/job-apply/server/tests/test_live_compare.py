"""The nightly live check's comparison (scripts/live_compare.py): one stable outcome per
employer, compared with the last run's, and the issue body that carries them between runs."""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "live_compare.py"
_spec = importlib.util.spec_from_file_location("live_compare", _PATH)
lc = importlib.util.module_from_spec(_spec)
sys.modules["live_compare"] = lc  # dataclasses look their module up here
_spec.loader.exec_module(lc)

Result, Change = lc.Result, lc.Change


def pipeline_rec(company, status="ready", need="", reason="", title="Field Service Engineer", **extra):
    """A LIVE_PIPELINE record shaped like live_smoke.check_pipeline's."""
    return {"company": company, "posting": {"title": title, "location": "Chandler, AZ", "url": "https://example.com/1"},
            "rounds": [{"status": status, "need": need, "reason": reason, "log": ["opened", "filled"],
                        "questions": []}], "seconds": 42.0, **extra}


def search_rec(company, az=3, any_=None, error=None, **extra):
    """A LIVE_RESULT record shaped like live_smoke.check_company's."""
    rec = {"company": company, "search_config": {"workday": "https://example.wd1.myworkdayjobs.com/Jobs"},
           "search_az": {"count": az, "error": error, "sample": []}, "seconds": 30.0, **extra}
    if any_ is not None:
        rec["search_any"] = {"count": any_, "error": None, "sample": []}
    return rec


def log(prefix, records, noise=True):
    lines = ["Resolved 40 packages in 1ms"] if noise else []
    for rec in records:
        lines.append(prefix + json.dumps(rec))
        if noise:
            lines.append('LIVE_SHOT "stuck" /9j/4AAQSkZJRgABAQ')
    return "\n".join(lines) + "\n"


# --- comparing ---------------------------------------------------------------

def test_unchanged_outcomes_are_no_change():
    previous = {"pipeline": {"ASM": "ready", "KLA": "needs_you sign_in"}}
    current = {"pipeline": {"ASM": Result("ready", "Field Service Engineer"),
                            "KLA": Result("needs_you sign_in", "Another posting tonight")}}
    state, changes = lc.compare(previous, current)
    assert changes == []
    assert state == previous


def test_changed_added_and_removed_employers():
    previous = {"pipeline": {"ASM": "ready", "KLA": "needs_you sign_in", "Old Co": "needs_you stuck"}}
    current = {"pipeline": {"ASM": Result("ready"), "KLA": Result("ready", "Field Service Engineer"),
                            "New Co": Result("needs_you stuck", "Technician")}}
    state, changes = lc.compare(previous, current)
    assert changes == [
        Change("pipeline", "KLA", "needs_you sign_in", "ready", "Field Service Engineer"),
        Change("pipeline", "New Co", None, "needs_you stuck", "Technician"),
        Change("pipeline", "Old Co", "needs_you stuck", None),
    ]
    assert state == {"pipeline": {"ASM": "ready", "KLA": "ready", "New Co": "needs_you stuck"}}


def test_first_run_saves_everything_and_reports_nothing():
    current = {"pipeline": {"ASM": Result("ready")}, "search": {"Banner Health": Result("works")}}
    state, changes = lc.compare(None, current)
    assert changes == []
    assert state == {"pipeline": {"ASM": "ready"}, "search": {"Banner Health": "works"}}


def test_a_check_new_since_the_last_run_reports_no_additions():
    state, changes = lc.compare({"pipeline": {"ASM": "ready"}},
                                {"pipeline": {"ASM": Result("ready")}, "search": {"USAA": Result("works")}})
    assert changes == []
    assert state == {"pipeline": {"ASM": "ready"}, "search": {"USAA": "works"}}


def test_a_check_that_didnt_finish_keeps_the_employers_it_didnt_reach():
    previous = {"search": {"Banner Health": "works", "USAA": "works", "Vanguard": "error"}}
    current = {"search": {"Banner Health": Result("error", "HTTP 503"), "Vanguard": Result("error")}}
    state, changes = lc.compare(previous, current, partial={"search"})
    assert changes == [Change("search", "Banner Health", "works", "error", "HTTP 503")]
    assert state == {"search": {"Banner Health": "error", "USAA": "works", "Vanguard": "error"}}


def test_a_check_that_reported_no_one_keeps_the_last_results():
    previous = {"pipeline": {"ASM": "ready"}, "search": {"USAA": "works"}}
    for current in ({"pipeline": {}, "search": {"USAA": Result("works")}}, {"search": {"USAA": Result("works")}}):
        state, changes = lc.compare(previous, current)
        assert changes == []
        assert state == previous


# --- outcomes -----------------------------------------------------------------

def test_pipeline_outcome_is_where_it_ended():
    assert lc.pipeline_outcome(pipeline_rec("ASM")) == Result("ready", "Field Service Engineer")
    assert lc.pipeline_outcome(pipeline_rec("KLA", "needs_you", "sign_in", "a sign-in page")) == \
        Result("needs_you sign_in", "Field Service Engineer · a sign-in page")
    # the last round counts: earlier rounds were questions the run answered
    rec = pipeline_rec("Intel", "needs_you", "stuck")
    rec["rounds"].insert(0, {"status": "needs_you", "need": "questions", "questions": [{"label": "Q"}]})
    assert lc.pipeline_outcome(rec).outcome == "needs_you stuck"
    crashed = pipeline_rec("TI", crash="TimeoutError: still running after 180s")
    assert (lc.pipeline_outcome(crashed)
            == Result("crash", "Field Service Engineer · TimeoutError: still running after 180s"))
    assert lc.pipeline_outcome({"company": "X", "note": "no postings found", "seconds": 3}) == \
        Result("no postings", "no postings found")


def test_a_rounds_stop_code_goes_before_its_reason():
    """T1 (the review of Oct 18): a round's stop code (pipeline.STOP_CODES) leads its reason in the detail, where
    nightly_summary.py sorts by it; never compared, and a round from before the codes reads as it did."""
    rec = pipeline_rec("KLA", "needs_you", "stuck", "The page didn't move on.")
    rec["rounds"][-1]["code"] = "step_not_saved"
    assert lc.pipeline_outcome(rec) == Result("needs_you stuck",
                                              "Field Service Engineer · [step_not_saved] The page didn't move on.")
    rec["rounds"][-1]["code"] = "site_down"  # (whatever the words: the employer's last outcome is kept)
    assert lc.pipeline_outcome(rec).outcome == lc.SITE_DOWN
    rec["rounds"][-1]["code"] = "Not a code!"  # (nothing but a code's letters goes into the detail)
    assert lc.pipeline_outcome(rec).detail == "Field Service Engineer · The page didn't move on."


def test_search_outcome_ignores_how_many_openings_there_are():
    assert lc.search_outcome(search_rec("USAA", az=5)) == Result("works", "5 in AZ")
    assert lc.search_outcome(search_rec("USAA", az=0, any_=3)) == Result("works", "none in AZ, 3 anywhere")
    assert lc.search_outcome(search_rec("USAA", az=0)) == Result("works", "none in AZ")  # (none looked for anywhere)
    # a crash after the search answered (opening the form) isn't the search's
    assert lc.search_outcome(search_rec("USAA", az=2, crash="TimeoutError: ")) == \
        Result("works", "2 in AZ · then TimeoutError: ")


def test_a_search_that_answered_and_found_nothing_anywhere_is_empty():
    """A parser that no longer reads its site's answer, or a site whose openings went, found nothing in Arizona nor
    with the broader words anywhere: it read as "works" (the review of Oct 15). One that failed somewhere isn't
    empty: what failed might have found some."""
    assert lc.search_outcome(search_rec("USAA", az=0, any_=0)) == Result("empty", "none in AZ, none anywhere")
    assert lc.search_outcome(search_rec("USAA", az=0, any_=0, crash="TimeoutError: ")) == \
        Result("empty", "none in AZ, none anywhere · then TimeoutError: ")
    partly = "browser search: TimeoutError: page didn't load (1 of 4 searches failed)"
    assert lc.search_outcome(search_rec("Aerotek", az=0, any_=0, error=partly)).outcome == "works"
    state, changes = lc.compare({"search": {"USAA": "works"}}, {"search": {"USAA": Result("empty", "none")}})
    assert [(c.employer, c.before, c.after) for c in changes] == [("USAA", "works", "empty")]


def test_search_outcome_errors():
    assert lc.search_outcome(search_rec("Wells Fargo", az=0, error="HTTP 503 from Workday")) == \
        Result("error", "HTTP 503 from Workday")
    fallback_failed = search_rec("Wells Fargo", az=0, any_=0)
    fallback_failed["search_any"]["error"] = "ReadTimeout"
    assert lc.search_outcome(fallback_failed) == Result("error", "ReadTimeout")
    # crashed before the search answered
    rec = {"company": "Wells Fargo", "search_config": {"workday": "x"}, "crash": "TimeoutError: ", "seconds": 150}
    assert lc.search_outcome(rec) == Result("error", "TimeoutError: ")


def test_a_search_that_failed_for_one_wording_still_works():
    """One of four wordings (or a later page) timing out while the others answer flipped the
    employer to "error" and back from night to night, a comment each time."""
    partly = "browser search: TimeoutError: page didn't load (1 of 4 searches failed)"
    assert lc.search_outcome(search_rec("Aerotek", az=2, error=partly)).outcome == "works"
    assert lc.search_outcome(search_rec("Aerotek", az=0, any_=0, error=partly)).outcome == "works"
    assert lc.search_outcome(search_rec("Aerotek", az=2, error="HTTP 503")).outcome == "works"  # it found some
    assert lc.search_outcome(search_rec("Aerotek", az=0, error="HTTP 503")).outcome == "error"


def test_search_outcome_without_a_search_block():
    opened = {"company": "City of Phoenix", "careers_url": "https://example.gov/careers", "ats": "custom",
              "page": {"title": "Careers", "navigation_error": None}}
    assert lc.search_outcome(opened) == Result("no search", "careers page opened")
    failed = {**opened, "page": {"navigation_error": "net::ERR_TIMED_OUT"}}
    assert lc.search_outcome(failed) == Result("no search", "careers page: net::ERR_TIMED_OUT")
    # its list entry's why (`no_search:`, which live_smoke.py notes) said first: a skip is never left unexplained
    why = {**opened, "no_search": "PeopleSoft Talent Acquisition, not a system the plugin searches"}
    assert lc.search_outcome(why) == Result(
        "no search", "PeopleSoft Talent Acquisition, not a system the plugin searches · careers page opened")


def test_results_read_the_run_log():
    text = log("LIVE_PIPELINE ", [pipeline_rec("ASM"), pipeline_rec("KLA", "needs_you", "sign_in")])
    text += 'LIVE_PIPELINE {"company": "Cut short", "rou\n'  # a line a stopped run left half-written
    text += "LIVE_RESULT " + json.dumps(search_rec("USAA")) + "\n"  # another check's record
    found = lc.results("pipeline", text)
    assert {name: r.outcome for name, r in found.items()} == {"ASM": "ready", "KLA": "needs_you sign_in"}
    assert lc.results("search", text) == {"USAA": Result("works", "3 in AZ")}
    long = lc.results("pipeline", log("LIVE_PIPELINE ", [pipeline_rec("X", "failed", reason="x" * 500)]))
    assert len(long["X"].detail) == lc.DETAIL_CHARS


# --- the issue that keeps the state ---------------------------------------------

def test_the_body_saves_the_outcomes_for_the_next_run():
    state = {"pipeline": {"ASM": "ready", "KLA": "needs_you sign_in"}, "search": {"USAA": "works"}}
    current = {"pipeline": {"ASM": Result("ready", "Title | with `ticks` @someone"),
                            "KLA": Result("needs_you sign_in")}}
    body = lc.render_body(state, current, checked_at="2026-10-08 10:17 UTC", run_url="https://example.com/run/1")
    assert lc.read_state(body) == state
    assert "| ASM | `ready` | `Title / with 'ticks' @someone` |" in body
    assert "Not checked in this run" in body  # the search check: last run's results
    assert "[run](https://example.com/run/1)" in body


def test_a_body_saved_from_githubs_editor_still_reads():
    """Saving the issue body in GitHub's web editor stores it with \\r\\n line ends: the
    saved results read as none, so that night's changes were never reported."""
    state = {"pipeline": {"ASM": "ready"}, "search": {}}
    body = lc.render_body(state, {}, checked_at="2026-10-08 10:17 UTC", run_url="https://example.com/run/1")
    assert lc.read_state(body.replace("\n", "\r\n")) == state


def test_a_body_without_saved_outcomes_reads_as_none():
    assert lc.read_state(None) is None
    assert lc.read_state("Someone rewrote this issue.") is None
    assert lc.read_state(f"{lc.STATE_MARK}\n```json\n{{not json\n```\n") is None


def test_the_comment_lists_each_change():
    comment = lc.render_comment([
        Change("pipeline", "KLA", "needs_you sign_in", "ready", "Field Service Engineer"),
        Change("search", "New Co", None, "works"),
        Change("search", "Old Co", "works", None),
    ], "https://example.com/run/2")
    assert comment.startswith("3 changes since the last nightly live check ([run](https://example.com/run/2)):")
    assert "- KLA: `needs_you sign_in` → `ready` (`Field Service Engineer`)" in comment
    assert "- New Co: *new* → `works`" in comment
    assert "- Old Co: `works` → *no longer checked*" in comment


def test_an_employer_that_no_longer_reaches_its_review_page_is_said_first():
    """The regression alarm: an employer the last run took to its review page that this one stopped short of
    opens the comment, apart from the list (so nightly_summary.py --changes reads each change once)."""
    changes = [Change("pipeline", "KLA", "ready", "needs_you stuck", "Technician"),
               Change("hr", "Axon", "needs_you your_submit", "needs_you questions"),
               Change("hr", "Carvana", "needs_you sign_in", "ready"),
               Change("accounts", "Gone Co", "ready", None),
               Change("search", "Intel", "works", "error")]
    assert [c.employer for c in lc.went_back(changes)] == ["KLA", "Axon"]
    comment = lc.render_comment(changes)
    head = comment.split("\n\n")[1]
    assert head.startswith("**Went backwards:** 2 employers no longer reach the review page: "
                           "KLA (pipeline: `needs_you stuck`), Axon (hr: `needs_you questions`)."), comment
    assert comment.count("KLA") == 2 and "**Went backwards" not in lc.render_comment(changes[2:])


def test_find_issue_takes_the_oldest_with_the_title():
    issues = [{"number": 9, "title": "Nightly live check"}, {"number": 4, "title": "Nightly live check"},
              {"number": 2, "title": "Nightly live check (old notes)"}]
    assert lc.find_issue(issues)["number"] == 4
    assert lc.find_issue([]) is None


def _run(tmp_path, issues, pipeline_records, search_records=(), partial=(), hr_records=None, finance_records=None,
         accounts_records=None, expect=0, extra=()):
    (tmp_path / "pipeline.log").write_text(log("LIVE_PIPELINE ", pipeline_records), encoding="utf-8")
    (tmp_path / "search.log").write_text(log("LIVE_RESULT ", search_records), encoding="utf-8")
    (tmp_path / "issues.json").write_text(json.dumps(issues), encoding="utf-8")
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    hr = []
    for name, records in (("hr", hr_records), ("finance", finance_records), ("accounts", accounts_records)):
        if records is not None:
            (tmp_path / f"{name}.log").write_text(log("LIVE_PIPELINE ", records), encoding="utf-8")
            hr += ["--log", f"{name}={tmp_path / f'{name}.log'}"]
    argv = ["--issues", str(tmp_path / "issues.json"), "--run-url", "https://example.com/run/3",
            "--log", f"pipeline={tmp_path / 'pipeline.log'}", "--log", f"search={tmp_path / 'search.log'}", *hr,
            "--body-out", str(tmp_path / "body.md"), "--comment-out", str(tmp_path / "comment.md"),
            "--github-output", str(out), *[a for p in partial for a in ("--partial", p)], *extra]
    assert lc.main(argv) == expect
    outputs = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines())
    body, comment = tmp_path / "body.md", tmp_path / "comment.md"
    return (body.read_text(encoding="utf-8") if body.exists() else None,
            comment.read_text(encoding="utf-8") if comment.exists() else None, outputs)


def test_main_first_night_then_a_change_then_a_quiet_night(tmp_path):
    night1 = [pipeline_rec("ASM"), pipeline_rec("KLA", "needs_you", "sign_in")]
    body, comment, outputs = _run(tmp_path, [], night1, [search_rec("USAA")])
    assert comment is None
    assert outputs == {"issue": "", "issue_state": "", "changes": "0", "went_back": "0"}
    issue = {"number": 7, "title": "Nightly live check", "state": "CLOSED", "body": body}

    night2 = [pipeline_rec("ASM"), pipeline_rec("KLA", "needs_you", "stuck")]
    body, comment, outputs = _run(tmp_path, [issue], night2, [search_rec("USAA", az=0, any_=2)])
    assert outputs == {"issue": "7", "issue_state": "CLOSED", "changes": "1", "went_back": "0"}
    assert "- KLA: `needs_you sign_in` → `needs_you stuck`" in comment
    assert "USAA" not in comment  # fewer openings isn't a change
    issue["body"] = body

    body, comment, outputs = _run(tmp_path, [issue], night2, [search_rec("USAA", az=4)])
    assert comment is None
    assert outputs["changes"] == "0"
    assert lc.read_state(body) == {"pipeline": {"ASM": "ready", "KLA": "needs_you stuck"}, "search": {"USAA": "works"}}


def test_main_a_check_that_stopped_part_way(tmp_path):
    body, _, _ = _run(tmp_path, [], [pipeline_rec("ASM"), pipeline_rec("KLA")], [search_rec("USAA")])
    issue = {"number": 7, "title": "Nightly live check", "state": "OPEN", "body": body}
    body, comment, outputs = _run(tmp_path, [issue], [pipeline_rec("ASM")], [], partial=["pipeline"])
    assert comment is None and outputs["changes"] == "0"
    assert lc.read_state(body) == {"pipeline": {"ASM": "ready", "KLA": "ready"}, "search": {"USAA": "works"}}
    assert "This check didn't finish" in body


def test_the_hr_pipeline_check_starts_quietly_then_reports_changes(tmp_path):
    """The nightly check's HR run (the pipeline on the Phoenix list, as an HR applicant) is a
    check of its own: its first night saves its employers without reporting each as added, and
    after that a change in one is reported under its own heading."""
    body = _run(tmp_path, [], [pipeline_rec("ASM")], [search_rec("USAA")])[0]
    issue = {"number": 7, "title": "Nightly live check", "state": "OPEN", "body": body}
    hr1 = [pipeline_rec("Axon"), pipeline_rec("Banner Health", "needs_you", "sign_in")]
    body, comment, outputs = _run(tmp_path, [issue], [pipeline_rec("ASM")], [search_rec("USAA")], hr_records=hr1)
    assert comment is None and outputs["changes"] == "0"  # a check new since the last run: nothing added
    assert lc.read_state(body)["hr"] == {"Axon": "ready", "Banner Health": "needs_you sign_in"}
    assert "### Apply pipeline, Phoenix list (HR jobs)" in body
    issue["body"] = body
    hr2 = [pipeline_rec("Axon", "needs_you", "stuck"), pipeline_rec("Banner Health", "needs_you", "sign_in")]
    body, comment, outputs = _run(tmp_path, [issue], [pipeline_rec("ASM")], [search_rec("USAA")], hr_records=hr2)
    assert outputs["changes"] == "1"
    hr_part = comment.split("Apply pipeline, Phoenix list (HR jobs)", 1)[1]
    assert "Axon: `ready` → `needs_you stuck`" in hr_part and "ASM" not in comment


def test_a_record_run_on_from_a_cut_screenshot_line_is_read():
    """A screenshot printed before an employer's record was cut at 64 KiB, and the record ran on
    into it: ASM, Ebara and onsemi went missing from the nightly issue, and 'stuck' outcomes (the
    ones with a screenshot) were never saved."""
    text = ('LIVE_SHOT "ASM" /9j/' + "A" * 65530 + "LIVE_PIPELINE "
            + json.dumps(pipeline_rec("ASM", "needs_you", "stuck")))
    text += "\n" + log("LIVE_PIPELINE ", [pipeline_rec("KLA")])
    assert {name: r.outcome for name, r in lc.results("pipeline", text).items()} == {
        "ASM": "needs_you stuck", "KLA": "ready"}


def test_the_finance_pipeline_check_is_a_check_of_its_own(tmp_path):
    """The nightly check's finance run (the pipeline on the Phoenix list, as a finance applicant)
    keeps its own results beside the HR run's: the same employer can end differently in each."""
    body = _run(tmp_path, [], [pipeline_rec("ASM")], [search_rec("USAA")],
                hr_records=[pipeline_rec("Axon")])[0]
    issue = {"number": 7, "title": "Nightly live check", "state": "OPEN", "body": body}
    fin1 = [pipeline_rec("Axon", "needs_you", "stuck"), pipeline_rec("EY", "needs_you", "your_submit")]
    body, comment, outputs = _run(tmp_path, [issue], [pipeline_rec("ASM")], [search_rec("USAA")],
                                  hr_records=[pipeline_rec("Axon")], finance_records=fin1)
    assert comment is None and outputs["changes"] == "0"  # new since the last run: nothing added
    state = lc.read_state(body)
    assert state["finance"] == {"Axon": "needs_you stuck", "EY": "needs_you your_submit"}
    assert state["hr"] == {"Axon": "ready"}
    assert "### Apply pipeline, Phoenix list (finance jobs)" in body
    issue["body"] = body
    fin2 = [pipeline_rec("Axon", "needs_you", "stuck"), pipeline_rec("EY", "needs_you", "sign_in")]
    body, comment, outputs = _run(tmp_path, [issue], [pipeline_rec("ASM")], [search_rec("USAA")],
                                  hr_records=[pipeline_rec("Axon")], finance_records=fin2)
    assert outputs["changes"] == "1"
    assert "EY: `needs_you your_submit` → `needs_you sign_in`" in comment.split("finance jobs", 1)[1]


def test_a_site_down_for_maintenance_keeps_its_employers_last_outcome():
    """Workday's weekend maintenance (live, Oct 2026) sent every Workday employer's search to its
    maintenance page: a night's run would have reported each one as changed to "no postings",
    and changed back the next night. Down for maintenance is no outcome of the employer's own."""
    down = {"company": "KLA", "seconds": 3,
            "note": "no postings found (the search failed: SearchError: kla.wd1.myworkdayjobs.com is down for "
                    "maintenance (it sends searches to community.workday.com/"
                    "maintenance-page); try again in a few hours)"}
    assert lc.pipeline_outcome(down).outcome == lc.SITE_DOWN
    stopped = pipeline_rec("ASML", "needs_you", "stuck", "ASML's site is down for maintenance (it sent the job to "
                           "https://www.asml.com/en/maintenance). Try again in a few "
                           "hours: press Resume once it's back.")
    assert lc.pipeline_outcome(stopped).outcome == lc.SITE_DOWN
    assert lc.search_outcome(search_rec("KLA", az=0, error="SearchError: kla.wd1.myworkdayjobs.com is down for "
                                         "maintenance (...); try again in a few hours")).outcome == lc.SITE_DOWN
    previous = {"pipeline": {"KLA": "needs_you sign_in", "ASML": "needs_you sign_in", "Intel": "needs_you sign_in"}}
    current = {"pipeline": {"KLA": lc.pipeline_outcome(down), "ASML": lc.pipeline_outcome(stopped),
                            "Intel": Result("ready", "")}}
    state, changes = lc.compare(previous, current)
    assert [(c.employer, c.after) for c in changes] == [("Intel", "ready")]
    assert state["pipeline"] == {"ASML": "needs_you sign_in", "Intel": "ready", "KLA": "needs_you sign_in"}


ACCOUNTS = "Apply pipeline with the test identity (accounts made and signed in)"


def test_the_accounts_check_is_left_out_until_set_up_then_reports_its_own_changes(tmp_path):
    """The test identity's check (live_smoke.py --pipeline --test-identity) is a check of its own.
    Until the repository has its secrets the nightly leaves it out (no --log for it): nothing of it
    is saved or shown. Its first night saves its employers without reporting each as added; after
    that a change in one is reported under its own heading, apart from the same employer's result
    in another check. With its secrets gone again, its last results stay."""
    sign_in = [pipeline_rec("KLA", "needs_you", "sign_in")]
    body = _run(tmp_path, [], sign_in, [search_rec("USAA")])[0]
    assert ACCOUNTS not in body and "accounts" not in lc.read_state(body)
    issue = {"number": 7, "title": "Nightly live check", "state": "OPEN", "body": body}

    night1 = [pipeline_rec("KLA", "needs_you", "email_code"), pipeline_rec("onsemi")]
    body, comment, outputs = _run(tmp_path, [issue], sign_in, [search_rec("USAA")], accounts_records=night1)
    assert comment is None and outputs["changes"] == "0"  # new since the last run: nothing added
    assert f"### {ACCOUNTS}" in body
    state = lc.read_state(body)
    assert state["accounts"] == {"KLA": "needs_you email_code", "onsemi": "ready"}
    assert state["pipeline"] == {"KLA": "needs_you sign_in"}
    issue["body"] = body

    night2 = [pipeline_rec("KLA"), pipeline_rec("onsemi")]
    body, comment, outputs = _run(tmp_path, [issue], sign_in, [search_rec("USAA")], accounts_records=night2)
    assert outputs["changes"] == "1"
    assert "KLA: `needs_you email_code` → `ready`" in comment.split(f"**{ACCOUNTS}", 1)[1]
    issue["body"] = body

    body, comment, outputs = _run(tmp_path, [issue], sign_in, [search_rec("USAA")])  # its secrets removed
    assert comment is None and outputs["changes"] == "0"
    assert lc.read_state(body)["accounts"] == {"KLA": "ready", "onsemi": "ready"}
    assert "Not checked in this run" in body.split(ACCOUNTS, 1)[1]


def test_an_accounts_check_that_stopped_part_way_keeps_the_employers_it_didnt_reach(tmp_path):
    """Waiting on emailed codes, the test identity's check can run into its time limit: the employers
    it didn't reach keep their last results, as in the other checks. One that finished and no longer
    reports an employer has dropped it from its list."""
    night1 = [pipeline_rec("KLA"), pipeline_rec("Kforce", "needs_you", "stuck")]
    body = _run(tmp_path, [], [pipeline_rec("ASM")], [search_rec("USAA")], accounts_records=night1)[0]
    issue = {"number": 7, "title": "Nightly live check", "state": "OPEN", "body": body}
    body, comment, outputs = _run(tmp_path, [issue], [pipeline_rec("ASM")], [search_rec("USAA")],
                                  partial=["accounts"], accounts_records=[pipeline_rec("KLA")])
    assert comment is None and outputs["changes"] == "0"
    assert lc.read_state(body)["accounts"] == {"KLA": "ready", "Kforce": "needs_you stuck"}
    assert "This check didn't finish" in body.split(ACCOUNTS, 1)[1]
    issue["body"] = body
    body, comment, outputs = _run(tmp_path, [issue], [pipeline_rec("ASM")], [search_rec("USAA")],
                                  accounts_records=[pipeline_rec("KLA")])
    assert outputs["changes"] == "1" and "Kforce: `needs_you stuck` → *no longer checked*" in comment


# --- the issue's size, and the employers still back ---------------------------------

def _saved(body):
    return json.loads(body.split(lc.STATE_MARK, 1)[1].split("```json\n", 1)[1].split("\n```", 1)[0])


def test_a_ready_rows_detail_is_its_title_and_the_saved_outcomes_are_grouped():
    """The issue's body (#54) held 55k of GitHub's 65,536 characters on Oct 16, 2026: half of it the detail cells,
    two hundred of them the review page's one sentence, and a fifth the saved outcomes, one line an employer. A
    ready row's detail is its posting's title (the outcome says the rest), and the saved outcomes group each
    check's employers by outcome, on one line."""
    ready = pipeline_rec("ASM",
                         reason="Filled and waiting on the review page. Check it in the browser, then press Submit.")
    assert lc.pipeline_outcome(ready) == Result("ready", "Field Service Engineer")
    stuck = pipeline_rec("KLA", "needs_you", "stuck", "Workday didn't move on.")
    assert lc.pipeline_outcome(stuck).detail == "Field Service Engineer · Workday didn't move on."
    # the real run's shape: 102 employers in three apply checks, 63 in one not run (finance), 68 searches
    state = {c: {f"Employer {i}": "ready" if i % 3 else "needs_you stuck" for i in range(102)}
             for c in ("pipeline", "hr", "accounts")}
    state["finance"] = {f"Employer {i}": "needs_you sign_in" for i in range(63)}
    state["search"] = {f"Employer {i}": "works" for i in range(68)}
    current = {c: {n: Result(o, "Some Posting Title" if o == "ready" else "Some Posting Title · " + "said " * 30)
                   for n, o in found.items()} for c, found in state.items() if c != "finance"}
    body = lc.render_body(state, current, checked_at="2026-10-16 10:47 UTC", run_url="https://example.com/run/102")
    assert lc.read_state(body) == state
    assert len(body) < 52_000, len(body)  # (run 102's real body: 55,444 characters in the old shape, 42,207 in this)
    saved = _saved(body)
    assert saved["version"] == 3 and saved["forms"] == {}  # (no review page's form noted: an older live_smoke's)
    assert len(body.split(lc.STATE_MARK, 1)[1]) < 8_000  # the saved outcomes (12k for run 102 in the old shape)
    assert saved["outcomes"]["search"] == {"works": sorted((f"Employer {i}" for i in range(68)), key=str.lower)}
    assert set(saved["outcomes"]["pipeline"]) == {"ready", "needs_you stuck"} and saved["reached"] == {}
    assert body.split(lc.STATE_MARK, 1)[1].count("\n") <= 6  # the saved outcomes on one line
    assert "| Employer 1 | `ready` | `Some Posting Title` |" in body and "Still back" not in body


def test_a_version_1_state_block_still_reads():
    """The body as the runs before Oct 16, 2026 wrote it: the outcomes one line an employer, no dates."""
    v1 = json.dumps({"version": 1, "outcomes": {"pipeline": {"KLA": "needs_you sign_in", "ASM": "ready"},
                                                "search": {"USAA": "works"}}}, indent=1)
    body = f"Last checked: 2026-10-15 10:17 UTC\n\n{lc.STATE_MARK}\n```json\n{v1}\n```\n"
    assert (lc.read_state(body)
            == {"pipeline": {"ASM": "ready", "KLA": "needs_you sign_in"}, "search": {"USAA": "works"}})
    assert list(lc.read_state(body)["pipeline"]) == ["ASM", "KLA"]  # by name, as the tables list them
    assert lc.read_reached(body) == {}


def test_a_body_too_long_for_github_is_shortened_then_refused(tmp_path, capsys):
    """Past GitHub's 65,536 characters the body's edit fails after the comment is posted, the outcomes aren't
    saved, and the next run reports every change again against stale ones: the details are shortened, then left
    out, to fit; when even that doesn't fit, the run fails before the comment."""
    many = {"accounts": {f"Employer {i}": "needs_you stuck" for i in range(400)}}
    current = {"accounts": {n: Result("needs_you stuck", "Title · " + "x" * 132) for n in many["accounts"]}}
    body = lc.render_body(many, current, checked_at="2026-10-16 10:47 UTC")
    assert len(body) <= lc.BODY_LIMIT
    assert "(The details are shortened to keep this page within GitHub's size for an issue.)" in body
    assert "| Employer 1 | `needs_you stuck` | `Title · xxxx" in body and lc.read_state(body) == many
    more = {"accounts": {f"Employer {i}": "needs_you stuck" for i in range(800)}}
    current = {"accounts": {n: Result("needs_you stuck", "Title · " + "x" * 132) for n in more["accounts"]}}
    body = lc.render_body(more, current, checked_at="2026-10-16 10:47 UTC")
    assert len(body) <= lc.BODY_LIMIT and "(The details are left out to keep" in body
    assert "| Employer 1 | `needs_you stuck` |  |" in body
    with pytest.raises(lc.BodyTooLong, match="even with its details left out"):
        lc.render_body({"accounts": {f"Employer {i}": "needs_you stuck" for i in range(2000)}}, {},
                       checked_at="2026-10-16 10:47 UTC")
    records = [pipeline_rec(f"Employer {i}", "needs_you", "stuck", "x" * 100) for i in range(2000)]
    body, comment, _ = _run(tmp_path, [], records, [search_rec("USAA")], expect=1)
    assert body is None and comment is None  # nothing written: nothing posted
    assert "::error::the issue's body would be" in capsys.readouterr().out


def test_an_employer_still_back_from_an_earlier_run_is_said_in_the_body(tmp_path, monkeypatch, capsys):
    """The regression alarm fires in the comment the night an employer stops reaching its review page, and a
    quiet night after that writes no comment: the body names the employers still back, with the date each last
    got there, until one gets there again (a check that didn't reach the employer keeps its date)."""
    clock = {"day": 14}
    monkeypatch.setattr(lc, "_now", lambda: datetime(2026, 10, clock["day"], 10, 17, tzinfo=timezone.utc))
    body, comment, _ = _run(tmp_path, [], [pipeline_rec("ASM"), pipeline_rec("KLA")], [search_rec("USAA")])
    assert "Still back" not in body
    assert lc.read_reached(body) == {"pipeline": {"ASM": "2026-10-14", "KLA": "2026-10-14"}}
    issue = {"number": 7, "title": "Nightly live check", "state": "OPEN", "body": body}

    clock["day"] = 15
    back = [pipeline_rec("ASM"), pipeline_rec("KLA", "needs_you", "stuck", "Workday didn't move on.")]
    body, comment, outputs = _run(tmp_path, [issue], back, [search_rec("USAA")])
    assert outputs["went_back"] == "1" and "**Went backwards:**" in comment
    assert ("**Still back:** 1 employer reached the review page in an earlier run but not in the last: "
            "KLA (pipeline; last there 2026-10-14).") in body
    issue["body"] = body

    clock["day"] = 16
    body, comment, outputs = _run(tmp_path, [issue], back, [search_rec("USAA")])
    assert comment is None and outputs["changes"] == "0"  # a quiet night: no comment, so the body carries the alarm
    assert "KLA (pipeline; last there 2026-10-14)" in body
    assert "still back: KLA (pipeline; last there 2026-10-14)" in capsys.readouterr().out
    assert lc.read_reached(body) == {"pipeline": {"ASM": "2026-10-16", "KLA": "2026-10-14"}}
    issue["body"] = body

    clock["day"] = 17  # the check didn't reach KLA: its date stands
    body, comment, _ = _run(tmp_path, [issue], [pipeline_rec("ASM")], [search_rec("USAA")], partial=["pipeline"])
    assert comment is None and "KLA (pipeline; last there 2026-10-14)" in body
    issue["body"] = body

    clock["day"] = 18
    body, comment, _ = _run(tmp_path, [issue], [pipeline_rec("ASM"), pipeline_rec("KLA")], [search_rec("USAA")])
    assert "KLA: `needs_you stuck` → `ready`" in comment
    assert "Still back" not in body and lc.read_reached(body)["pipeline"]["KLA"] == "2026-10-18"


# --- the trend issue: the last runs' counts, and the employers that flip -------------------------------

TREND = "Nightly live trends"


def _trend_run(tmp_path, trend_issues, pipeline_records, search_records=(), issues=(), **kw):
    """_run with the trend issue's arguments: (the trend issue's new body or None, the body, the comment, outputs)."""
    (tmp_path / "trend-issues.json").write_text(json.dumps(trend_issues), encoding="utf-8")
    out = tmp_path / "trend.md"
    extra = ["--trend-issues", str(tmp_path / "trend-issues.json"), "--trend-body-out", str(out)]
    body, comment, outputs = _run(tmp_path, list(issues), pipeline_records, search_records, extra=extra, **kw)
    return (out.read_text(encoding="utf-8") if out.exists() else None), body, comment, outputs


@pytest.fixture
def clock(monkeypatch):
    when = {"day": 17}
    monkeypatch.setattr(lc, "_now", lambda: datetime(2026, 10, when["day"], 10, 17, tzinfo=timezone.utc))
    return when


def test_the_first_trend_body_starts_the_history(tmp_path, clock):
    """The issue holds one run: a second issue keeps the last runs' counts per check (at the review page out of those
    checked; the search check's: its searches that work), each outcome's count and the median seconds an employer
    took, and each employer's run as a letter. Neither issue's saved block reads as the other's."""
    pipeline = [pipeline_rec("ASM"), pipeline_rec("KLA", "needs_you", "sign_in", seconds=100.0)]
    search = [search_rec("USAA"), search_rec("Intel", az=0, error="HTTP 503")]
    trend, body, comment, outputs = _trend_run(tmp_path, [], pipeline, search)
    assert outputs["trend_issue"] == "" and comment is None
    saved = lc.read_trend(trend)
    assert saved.runs == [{"at": "2026-10-17 10:17 UTC", "run_url": "https://example.com/run/3", "checks": {
        "pipeline": {"reached": 1, "total": 2, "outcomes": {"needs_you sign_in": 1, "ready": 1}, "median_seconds": 71},
        "search": {"reached": 1, "total": 2, "outcomes": {"error": 1, "works": 1}, "median_seconds": 30}}}]
    assert saved.letters == {"pipeline": {"ASM": "R", "KLA": "x"}, "search": {"Intel": "x", "USAA": "R"}}
    assert "| Run | pipeline | search |" in trend
    assert "| [2026-10-17 10:17 UTC](https://example.com/run/3) | 1/2 · 71 s | 1/2 · 30 s |" in trend
    assert "### Flipping\n" in trend and "\nNone.\n" in trend
    assert lc.read_state(trend) is None and lc.read_trend(body) is None  # (never taken for each other)
    assert lc.STATE_MARK not in trend and lc.TREND_MARK not in body


def test_each_run_is_added_to_the_trend_issues_history(tmp_path, clock):
    """The next run reads the history from the trend issue (by its own title, whatever else its label's list holds),
    adds itself, and names the issue to edit; the table shows the newest run first."""
    trend = _trend_run(tmp_path, [], [pipeline_rec("ASM"), pipeline_rec("KLA", "needs_you", "sign_in")])[0]
    main_body = (tmp_path / "body.md").read_text(encoding="utf-8")
    listed = [{"number": 54, "title": "Nightly live check", "state": "OPEN", "body": main_body},
              {"number": 70, "title": TREND, "state": "OPEN", "body": trend}]
    clock["day"] = 18
    trend, _, _, outputs = _trend_run(tmp_path, listed, [pipeline_rec("ASM"), pipeline_rec("KLA")])
    assert outputs["trend_issue"] == "70"
    saved = lc.read_trend(trend)
    assert [run["at"] for run in saved.runs] == ["2026-10-17 10:17 UTC", "2026-10-18 10:17 UTC"]
    assert [run["checks"]["pipeline"]["reached"] for run in saved.runs] == [1, 2]
    assert saved.letters["pipeline"] == {"ASM": "RR", "KLA": "xR"}
    rows = [line for line in trend.splitlines() if line.startswith("| [")]
    assert rows[0].startswith("| [2026-10-18 10:17 UTC]") and rows[1].startswith("| [2026-10-17 10:17 UTC]")
    assert lc.find_issue(listed, TREND)["number"] == 70 and lc.find_issue(listed)["number"] == 54


def test_the_trend_keeps_the_last_sixty_runs(tmp_path, clock):
    trend = None
    for i in range(60):
        trend = lc.add_run(trend, {"pipeline": {"ASM": Result("ready")}}, at=f"run {i}")
    assert len(trend.runs) == 60
    issue = {"number": 70, "title": TREND, "state": "OPEN", "body": lc.render_trend(trend)}
    saved = lc.read_trend(_trend_run(tmp_path, [issue], [pipeline_rec("ASM")])[0])
    assert len(saved.runs) == lc.TREND_RUNS == 60
    assert saved.runs[0]["at"] == "run 1" and saved.runs[-1]["at"] == "2026-10-17 10:17 UTC"
    assert saved.letters == {"pipeline": {"ASM": "R" * 20}}


def test_each_employer_keeps_its_last_twenty_runs_letters():
    """R at the review page, x not, - not in the check that run; the oldest first, twenty kept. One gone from its
    check for twenty runs is let go."""
    trend = None
    for i in range(25):
        pipeline = {"ASM": Result("ready" if i % 2 else "needs_you stuck")}
        if i < 3:
            pipeline["KLA"] = Result("ready")
        trend = lc.add_run(trend, {"pipeline": pipeline}, at=f"run {i}")
        if i == 21:
            assert trend.letters["pipeline"]["KLA"] == "R" + "-" * 19
    assert trend.letters == {"pipeline": {"ASM": "Rx" * 10}}  # (runs 5 to 24: the oldest, run 5, reached)
    assert all(len(said) <= lc.TREND_LETTERS for found in trend.letters.values() for said in found.values())


def test_a_check_that_didnt_finish_or_didnt_run_adds_no_counts_and_its_employers_get_dashes(tmp_path, clock):
    """A check the run left partial, or didn't run (the accounts check without its secrets), says nothing of its
    employers that night: no counts, and "-" for each. A site down for its maintenance is "-" too."""
    trend = _trend_run(tmp_path, [], [pipeline_rec("ASM"), pipeline_rec("KLA")], [search_rec("USAA")],
                       accounts_records=[pipeline_rec("KLA")])[0]
    issue = {"number": 70, "title": TREND, "state": "OPEN", "body": trend}
    clock["day"] = 18
    down = search_rec("USAA", az=0, error="SearchError: usaa.wd1.myworkdayjobs.com is down for maintenance")
    trend = _trend_run(tmp_path, [issue], [pipeline_rec("ASM")], [down], partial=["pipeline"])[0]
    saved = lc.read_trend(trend)
    assert set(saved.runs[0]["checks"]) == {"pipeline", "search", "accounts"}
    assert saved.runs[1]["checks"] == {"search": {"reached": 0, "total": 1, "outcomes": {"site down": 1},
                                                  "median_seconds": 30}}
    assert saved.letters == {"pipeline": {"ASM": "R-", "KLA": "R-"}, "accounts": {"KLA": "R-"},
                             "search": {"USAA": "R-"}}
    row = next(line for line in trend.splitlines() if line.startswith("| [2026-10-18"))
    assert row == "| [2026-10-18 10:17 UTC](https://example.com/run/3) | — | — | 0/1 · 30 s |"
    assert lc.letter("pipeline", lc.SITE_DOWN) == "-" and lc.letter("search", "no search") == "x"


def test_a_malformed_trend_body_starts_a_fresh_history_without_failing_the_compare(tmp_path, clock, capsys):
    good = lc.render_trend(lc.add_run(None, {"pipeline": {"ASM": Result("ready")}}, at="run 0"))
    saved = good.split(lc.TREND_MARK, 1)[1].split("```json\n", 1)[1].split("\n```", 1)[0]
    data = json.loads(saved)
    columns = {**data["counts"]["pipeline"], "reached": [1, 1]}  # (a column longer than the runs)
    broken = [f"{lc.TREND_MARK}\n```json\n{{not json\n```\n", "Someone rewrote this issue.",
              good.replace(saved, json.dumps({**data, "version": 99})),
              good.replace(saved, json.dumps({**data, "runs": "run 0"})),
              good.replace(saved, json.dumps({**data, "counts": {"pipeline": columns}}))]
    assert lc.read_trend(good) is not None
    for body in broken:
        assert lc.read_trend(body) is None, body
        issue = {"number": 70, "title": TREND, "state": "OPEN", "body": body}
        trend, main_body, _, outputs = _trend_run(tmp_path, [issue], [pipeline_rec("KLA")])
        assert main_body and outputs["trend_issue"] == "70"
        assert [run["at"] for run in lc.read_trend(trend).runs] == ["2026-10-17 10:17 UTC"]
        said = capsys.readouterr().out
        assert "::warning::the trend issue #70 has no saved history to add to; starting afresh" in said
    # the issues' list itself unreadable: no trend body (the issue stays as it was), and the compare goes on
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    extra = ["--trend-issues", str(tmp_path / "bad.json"), "--trend-body-out", str(tmp_path / "t.md")]
    body, _, outputs = _run(tmp_path, [], [pipeline_rec("KLA")], extra=extra)
    assert body and not (tmp_path / "t.md").exists() and outputs["trend_issue"] == ""
    assert "::warning::the trend issue's body wasn't written (JSONDecodeError" in capsys.readouterr().out


def test_the_trend_body_fits_githubs_size_with_sixty_runs_of_four_checks():
    """The biggest history it keeps: sixty runs of four checks, 110 employers in each with long names, eight outcomes a
    check, every employer flipping (so the flipping section is full). Well within GitHub's 65,536 characters, all
    sixty runs kept; past a smaller size, the oldest runs are let go."""
    names = [f"Employer With A Long Name {i:03d} Inc" for i in range(110)]
    apply = ["ready", "needs_you sign_in", "needs_you stuck", "needs_you bot_check", "needs_you your_submit",
             "needs_you questions", "no postings", "crash"]
    search = ["works", "no search", "error", "empty"]
    trend = None
    for run in range(lc.TREND_RUNS):
        current = {c: {n: Result(apply[(run + i + k) % 8]) for i, n in enumerate(names)}
                   for k, c in enumerate(("pipeline", "hr", "accounts"))}
        current["search"] = {n: Result(search[(run + i) % 4]) for i, n in enumerate(names)}
        seconds = {c: {n: 100.0 + 7 * i + run for i, n in enumerate(names)} for c in current}
        trend = lc.add_run(trend, current, at=f"2026-{8 + run // 28:02d}-{run % 28 + 1:02d} 10:17 UTC", seconds=seconds,
                           run_url=f"https://github.com/sebob2491/Jobs/actions/runs/{39068637558 + run}")
    assert len(trend.letters) == 4 and all(len(found) == 110 for found in trend.letters.values())
    # and the apply checks' employers' last ten runs' seconds (for the comment's Slower: 7k of it here)
    assert set(trend.seconds) == {"pipeline", "hr", "accounts"} and all(
        len(found) == 110 and all(len(took) == lc.TREND_SECONDS for took in found.values())
        for found in trend.seconds.values())
    body = lc.render_trend(trend)
    assert len(body) < 56_000, len(body)
    assert lc.read_trend(body) == trend  # (all of it: sixty runs, every count and letter)
    assert "and 90 more (`scripts/nightly_summary.py --trend` names them all)" in body
    smaller = lc.render_trend(trend, limit=40_000)
    assert len(smaller) <= 40_000 and 0 < len(lc.read_trend(smaller).runs) < 60
    with pytest.raises(lc.BodyTooLong):
        lc.render_trend(trend, limit=20_000)  # (the letters alone are more)


@pytest.mark.parametrize("sizes", [
    # (both lists and Las Vegas, finance back)
    {"pipeline": 39, "hr": 68, "finance": 68, "accounts": 144, "search": 144},
    {c: 150 for c in ("pipeline", "hr", "finance", "accounts", "search")}])
def test_the_trend_body_with_five_checks_and_more_employers_sheds_runs_to_fit(sizes):
    """The review of c4edd30: finance may run again, and the Las Vegas list would bring 144 employers to the accounts
    and search checks. Every employer flipping, with long names and its seconds: the body stays within GitHub's
    65,536 characters, letting its oldest runs go where it must, and what it keeps reads back whole."""
    apply = ["ready", "needs_you sign_in", "needs_you stuck", "needs_you bot_check", "needs_you your_submit",
             "needs_you questions", "no postings", "crash"]
    trend = None
    for run in range(lc.TREND_RUNS):
        current = {c: {f"Employer With A Long Name {i:03d} Inc": Result(
            ["works", "no search", "error", "empty"][(run + i) % 4] if c == "search" else apply[(run + i + k) % 8])
            for i in range(n)} for k, (c, n) in enumerate(sizes.items())}
        seconds = {c: {name: 100.0 + 7 * i + run for i, name in enumerate(found)} for c, found in current.items()}
        trend = lc.add_run(trend, current, at=f"2026-{8 + run // 28:02d}-{run % 28 + 1:02d} 10:17 UTC", seconds=seconds,
                           run_url=f"https://github.com/sebob2491/Jobs/actions/runs/{39068637558 + run}")
    body = lc.render_trend(trend)
    kept = lc.read_trend(body)
    assert len(body) <= lc.BODY_LIMIT and len(kept.runs) >= 1, (len(body), len(kept.runs))
    assert kept.runs == trend.runs[-len(kept.runs):] and kept.letters == trend.letters and kept.seconds == trend.seconds


def test_the_issue_body_with_every_employers_review_page_form_fits_githubs_size():
    """The state's version 3 saves each review page's form ("<boxes>:<sha>:<post>"): with both lists and Las Vegas,
    finance back, every apply check's employers at their review page with long names and long details, the body
    still fits (its details shortened or left out) and reads back whole."""
    sizes = {"pipeline": 39, "hr": 68, "finance": 68, "accounts": 144, "search": 144}
    state = {c: {f"Employer With A Long Name {i:03d} Inc": "works" if c == "search" else "ready" for i in range(n)}
             for c, n in sizes.items()}
    current = {c: {name: Result(o, "A Long Posting Title For A Field Service Engineer · " + "said " * 20)
                   for name, o in found.items()} for c, found in state.items()}
    forms = {c: {name: {"n": 120, "sha": "01234567", "post": "89abcd"} for name in found}
             for c, found in state.items() if c != "search"}
    reached = {c: {name: "2026-10-18" for name in found} for c, found in forms.items()}
    body = lc.render_body(state, current, checked_at="2026-10-18 10:47 UTC", run_url="https://example.com/run/120",
                          forms=forms, reached=reached)
    assert len(body) <= lc.BODY_LIMIT and lc.read_state(body) == state and lc.read_forms(body) == forms
    assert lc.read_reached(body) == reached
    # past that, the forms are left out (a night's form alarm lost), never the outcomes the next run compares with
    smaller = lc.render_body(state, current, checked_at="2026-10-18 10:47 UTC", forms=forms, reached=reached,
                             limit=len(body) - 100)
    assert lc.read_state(smaller) == state and lc.read_forms(smaller) == {}


def test_the_issue_comes_out_the_same_with_the_trend_issue(tmp_path, clock):
    """The trend issue is written beside the issue, never into it: its body and comment are the same, byte for byte."""
    night1 = [pipeline_rec("ASM"), pipeline_rec("KLA", "needs_you", "sign_in")]
    night2 = [pipeline_rec("ASM", "needs_you", "stuck", "Workday didn't move on."), pipeline_rec("KLA")]
    without, with_ = tmp_path / "without", tmp_path / "with"
    without.mkdir()
    with_.mkdir()
    body, _, _ = _run(without, [], night1, [search_rec("USAA")], partial=["search"])
    trend, body2, _, _ = _trend_run(with_, [], night1, [search_rec("USAA")], partial=["search"])
    assert body2 == body and trend
    issue = {"number": 7, "title": "Nightly live check", "state": "OPEN", "body": body}
    clock["day"] = 18
    body, comment, outputs = _run(without, [issue], night2, [search_rec("USAA")])
    trend_issue = {"number": 70, "title": TREND, "state": "OPEN", "body": trend}
    _, body2, comment2, outputs2 = _trend_run(with_, [trend_issue], night2, [search_rec("USAA")], issues=[issue])
    assert (body2, comment2) == (body, comment) and "**Went backwards:**" in comment
    assert outputs2 == {**outputs, "trend_issue": "70"}


WORKFLOW = Path(__file__).resolve().parents[4] / ".github" / "workflows" / "live-nightly.yml"


def test_the_workflow_keeps_the_trend_issue_apart_from_the_issue():
    """The "fresh" job reads the issue as the first open one with the live-check label: the trend issue has a label of
    its own (made if missing), and every step that finds either issue looks it up by its own label and title."""
    yaml = pytest.importorskip("yaml")
    flow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    compare = flow["jobs"]["compare"]
    assert compare["env"]["ISSUE_LABEL"] == "live-check" and compare["env"]["ISSUE_TITLE"] == lc.ISSUE_TITLE
    assert compare["env"]["TREND_LABEL"] == "live-trends" and compare["env"]["TREND_TITLE"] == lc.TREND_TITLE == TREND
    steps = {step.get("name", ""): step for step in compare["steps"]}
    run = steps["Compare with the last run"]["run"]
    assert 'gh issue list --label "$TREND_LABEL" --state all --limit 5' in run
    assert '--trend-issues live-compare/trend-issues.json --trend-title "$TREND_TITLE"' in run
    update = steps["Update the trend issue"]
    assert update["if"] == steps["Update the issue"]["if"]  # (full runs of the default branch only, as the issue)
    assert 'gh label create "$TREND_LABEL" --force' in update["run"]
    assert 'gh issue create --title "$TREND_TITLE" --label "$TREND_LABEL"' in update["run"]
    assert "live-check" not in update["run"] and "ISSUE" not in update["run"].replace("TREND_ISSUE", "")
    fresh = flow["jobs"]["fresh"]["steps"][0]["run"]
    assert "--label live-check" in fresh and f'select(.title == "{lc.ISSUE_TITLE}")' in fresh
    assert "live-trends" not in fresh


# --- the compare's three alarms (the audit of Oct 18): a review page's form, an employer's time, the bot checks ---

REVIEW = [{"id": "a", "kind": "text", "label": "Legal Name", "value": "Testy McTestface"},
          {"id": "b", "kind": "text", "label": "Phone Number", "value": "480-555-0199"},
          {"id": "c", "kind": "select", "label": "Degree", "value": "Bachelor's"}]
POSTING = {"title": "Field Service Engineer", "location": "Chandler, AZ", "url": "https://example.com/1"}


def test_a_review_page_whose_form_changed_is_said_and_never_counted_as_went_backwards(tmp_path, clock):
    """An employer still at its review page whose form there asks something new (form_shape: its boxes' labels and
    kinds, never their values), on the same posting, is a "Form changed" line in the issue's body; in a comment only
    beside a change of result (a quiet night writes none), and never as a step back. Another posting's form is
    another form. The forms are saved in the state's version 3; a version 2 body still reads (no forms yet)."""
    asm = lc.form_shape(REVIEW)
    assert asm == lc.form_shape([{**f, "value": "other"} for f in reversed(REVIEW)])  # (its values and order: the same)
    more = lc.form_shape(REVIEW + [{"id": "d", "kind": "radio_group", "label": "Are you 18 or older?"},
                                   {"id": "e", "kind": "text", "label": "Desired Pay"}])
    night1 = [pipeline_rec("ASM", form=asm), pipeline_rec("KLA", "needs_you", "stuck", form=asm)]
    body, comment, _ = _run(tmp_path, [], night1, [search_rec("USAA")])
    post = lc.run_forms("pipeline", log("LIVE_PIPELINE ", night1))["ASM"]["post"]
    assert comment is None and lc.read_forms(body) == {"pipeline": {"ASM": {**asm, "post": post}}}  # (ready alone)
    assert lc._saved(body)["version"] == 3
    issue = {"number": 7, "title": "Nightly live check", "state": "OPEN", "body": body}
    night2 = [pipeline_rec("ASM", form=more), pipeline_rec("KLA", "needs_you", "stuck", form=more)]
    body, comment, outputs = _run(tmp_path, [issue], night2, [search_rec("USAA")])
    assert comment is None and outputs["changes"] == "0" and outputs["went_back"] == "0"  # (a quiet night's)
    assert "**Form changed (still at the review page, the same posting: never a step back):** ASM (pipeline, still " \
           "`ready`): 3 → 5 boxes, +2." in body
    assert lc.read_forms(body) == {"pipeline": {"ASM": {**more, "post": post}}}
    # beside a change of result, in the comment too
    night2b = [pipeline_rec("ASM", form=more), pipeline_rec("KLA", form=more)]
    _, comment, outputs = _run(tmp_path, [issue], night2b, [search_rec("USAA")])
    assert outputs["changes"] == "1" and "- Form changed at ASM (pipeline, still `ready`): 3 → 5 boxes, +2" in comment
    issue["body"] = body
    body, _, _ = _run(tmp_path, [issue], night2, [search_rec("USAA")])  # (the same form: nothing to say)
    assert "Form changed" not in body
    issue["body"] = body
    other = [pipeline_rec("ASM", form=asm, posting={**POSTING, "url": "https://example.com/2"}),
             pipeline_rec("KLA", "needs_you", "stuck")]
    body, _, _ = _run(tmp_path, [issue], other, [search_rec("USAA")])  # (another posting's form: another form)
    assert "Form changed" not in body
    v2 = json.dumps({"version": 2, "outcomes": {"pipeline": {"ready": ["ASM"]}}})
    assert lc.read_forms(f"{lc.STATE_MARK}\n```json\n{v2}\n```\n") == {}


def test_an_employer_twice_as_slow_as_usual_this_run_and_the_last_is_said(tmp_path, clock):
    """The trend keeps each apply check's employers' last ten runs' seconds; one that took twice its usual (the median
    of its runs before) and 60 s or more, this run and the last, is a "Slower" line in the body (in a comment only
    beside a change of result). Not one slow night alone, nor a fast employer twice as slow (under 60 s), nor one
    whose result changed or that waited on an emailed code."""
    trend = None
    for day in range(4):
        trend = lc.add_run(trend, {"pipeline": {n: Result("ready") for n in ("ASM", "KLA", "Mail Co", "Once Co")}},
                           at=f"run {day}", seconds={"pipeline": {"ASM": 95.0 if day == 3 else 40.0 + day, "KLA": 20.0,
                                                                  "Mail Co": 95.0 if day == 3 else 40.0,
                                                                  "Once Co": 40.0}})
    assert trend.seconds["pipeline"]["ASM"] == [40, 41, 42, 95] and trend.seconds["pipeline"]["KLA"] == [20] * 4
    assert lc.read_trend(lc.render_trend(trend)).seconds == trend.seconds  # (saved and read back)
    trend_issue = {"number": 70, "title": TREND, "state": "OPEN", "body": lc.render_trend(trend)}
    first = [pipeline_rec(n) for n in ("ASM", "KLA", "Mail Co", "Once Co", "Other Co")]
    body, _, _ = _run(tmp_path, [], first)
    issue = {"number": 7, "title": "Nightly live check", "state": "OPEN", "body": body}
    mail = pipeline_rec("Mail Co", seconds=95.0)
    mail["rounds"].insert(0, {"status": "needs_you", "need": "email_code", "reason": "", "log": [], "questions": []})
    night = [pipeline_rec("ASM", seconds=95.0), pipeline_rec("KLA", seconds=50.0), mail,
             pipeline_rec("Once Co", seconds=95.0), pipeline_rec("Other Co", "needs_you", "stuck", seconds=500.0)]
    new_trend, body, comment, outputs = _trend_run(tmp_path, [trend_issue], night, issues=[issue])
    assert outputs["changes"] == "1"  # (Other Co's)
    assert "- Slower at ASM (pipeline): 95 s, its usual 41 s" in comment
    assert ("**Slower (2× an employer's usual time or more, and 60 s or more, this run and the last):** ASM "
            "(pipeline): 95 s, its usual 41 s.") in body
    assert not any(n in comment.split("**Slower")[1] for n in ("KLA", "Mail Co", "Once Co", "Other Co")), comment
    assert lc.read_trend(new_trend).seconds["pipeline"]["ASM"] == [40, 41, 42, 95, 95]


def test_the_seconds_saved_read_back_to_their_own_employers_whatever_the_names_order():
    """The review of c4edd30: the seconds were saved in the letters' order (by name without case) and read back in
    the saved block's (as Python sorts: capitals first), so after one round trip most employers had another's."""
    names = ["ASML", "Amkor", "eBay", "ASM (ASM America)", "Applied Materials", "intel", "KLA", "onsemi"]
    trend = None
    for run in range(3):
        trend = lc.add_run(trend, {"accounts": {n: Result("ready") for n in names}}, at=f"run {run}",
                           seconds={"accounts": {n: float(10 + i) for i, n in enumerate(names)}})
    assert lc.read_trend(lc.render_trend(trend)).seconds == trend.seconds == {
        "accounts": {n: [10 + i] * 3 for i, n in enumerate(names)}}
    assert lc.Trend([], {}).seconds is None and lc._seconds(lc.Trend([], {})) == {}


def test_bot_checks_up_at_many_employers_open_the_comment(tmp_path, clock):
    """A CAPTCHA come up at a job system's employers reads as many "expected" lines: the comment opens with one line
    that says so, when the employers stopped by a bot check or a CAPTCHA are 5 or more and twice the last run's."""
    names = [f"Employer {i}" for i in range(8)]
    night1 = [pipeline_rec(n) for n in names[:6]] + [pipeline_rec(n, "needs_you", "bot_check") for n in names[6:]]
    body, _, _ = _run(tmp_path, [], night1)
    issue = {"number": 7, "title": "Nightly live check", "state": "OPEN", "body": body}
    night2 = [pipeline_rec(n, "needs_you", "captcha") for n in names[:3]] + \
        [pipeline_rec(n, "needs_you", "bot_check") for n in names[3:]]
    _, comment, _ = _run(tmp_path, [issue], night2)
    assert comment.startswith("**Bot checks up**: 8 employers (last run 2) stopped at a bot check or a CAPTCHA.")
    night3 = [pipeline_rec(n) for n in names[:4]] + [pipeline_rec(n, "needs_you", "captcha") for n in names[4:]]
    _, comment, _ = _run(tmp_path, [issue], night3)  # (4 of them: fewer than 5)
    assert "Bot checks up" not in comment


def test_a_job_tried_again_after_a_site_error_says_so_in_its_detail():
    """The desk tries a job the site's own passing error stopped again by itself (pipeline.RETRY_AFTER: a minute in the
    live check), and live_smoke's round keeps the stop it made first ("retried"): the detail says "tried again", where
    it went to (the review page, or another stop), never compared."""
    rec = pipeline_rec("Circle K")
    rec["rounds"][-1]["retried"] = {"code": "site_went_wrong", "reason": "Something went wrong again."}
    assert lc.pipeline_outcome(rec) == Result("ready", "Field Service Engineer · tried again after a site error")
    rec = pipeline_rec("BDO", "needs_you", "stuck", "It went wrong again.")
    rec["rounds"][-1].update(code="site_error", retried={"code": "try_later", "reason": "Try again later."})
    assert lc.pipeline_outcome(rec) == Result("needs_you stuck", "Field Service Engineer · tried again after a site "
                                                                 "error · [site_error] It went wrong again.")
    assert lc.pipeline_outcome(pipeline_rec("ASM")).detail == "Field Service Engineer"  # (none: as before)
    # a try called off (its tab changed as it came due): never said to be tried again, and why
    rec = pipeline_rec("Mayo", "needs_you", "stuck", "Something went wrong.")
    rec["rounds"][-1].update(code="site_went_wrong", retried={"code": "site_went_wrong", "reason": "x", "opened": False,
                                                             "called_off": "its tab was closed"})
    assert lc.pipeline_outcome(rec).detail == ("Field Service Engineer · retry called off: its tab was closed · "
                                               "[site_went_wrong] Something went wrong."), lc.pipeline_outcome(rec)
