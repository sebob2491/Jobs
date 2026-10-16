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
    assert lc.pipeline_outcome(crashed) == Result("crash", "Field Service Engineer · TimeoutError: still running after 180s")
    assert lc.pipeline_outcome({"company": "X", "note": "no postings found", "seconds": 3}) == \
        Result("no postings", "no postings found")


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
         accounts_records=None, expect=0):
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
            "--github-output", str(out), *[a for p in partial for a in ("--partial", p)]]
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
    text = 'LIVE_SHOT "ASM" /9j/' + "A" * 65530 + "LIVE_PIPELINE " + json.dumps(pipeline_rec("ASM", "needs_you", "stuck"))
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
                    "maintenance (it sends searches to community.workday.com/maintenance-page); try again in a few hours)"}
    assert lc.pipeline_outcome(down).outcome == lc.SITE_DOWN
    stopped = pipeline_rec("ASML", "needs_you", "stuck", "ASML's site is down for maintenance (it sent the job to "
                           "https://www.asml.com/en/maintenance). Try again in a few hours: press Resume once it's back.")
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
    ready = pipeline_rec("ASM", reason="Filled and waiting on the review page. Check it in the browser, then press Submit.")
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
    assert saved["version"] == 2
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
    assert lc.read_state(body) == {"pipeline": {"ASM": "ready", "KLA": "needs_you sign_in"}, "search": {"USAA": "works"}}
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
