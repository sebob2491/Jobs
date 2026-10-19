"""The weekly flake rate (scripts/flake_rate.py): the tests that failed in the week's CI runs, a flake told from a
real failure by whether the commit's tests passed in another run or attempt. No network: GitHub's answers are made
here."""

import datetime
import importlib.util
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_spec = importlib.util.spec_from_file_location("flake_rate", SCRIPTS / "flake_rate.py")
fr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fr)

LOG = """2026-10-16T21:40:01Z ============ short test summary info ============
2026-10-16T21:40:01Z FAILED tests/test_pipeline.py::test_an_emailed_link_or_code_is_waited_for[] - AssertionError: assert 'verify'
2026-10-16T21:40:01Z FAILED tests/test_desk.py::test_links_past_the_first_twenty_are_handed_back
2026-10-16T21:40:01Z = 2 failed, 640 passed in 600.00s =
"""  # noqa: E501


def test_a_jobs_log_names_its_failed_tests():
    assert fr.failed_tests(LOG) == {"tests/test_pipeline.py::test_an_emailed_link_or_code_is_waited_for[]",
                                    "tests/test_desk.py::test_links_past_the_first_twenty_are_handed_back"}
    assert fr.failed_tests("12 passed in 3.0s\nFAILED to start the browser") == set()


def test_a_test_that_failed_on_a_commit_whose_tests_passed_later_is_a_flake():
    """Run 1 (commit a) failed in its first attempt and passed when re-run; run 2 (commit b) failed and its
    commit never passed: the first is a flake, the second a failure."""
    runs = {"workflow_runs": [
        {"id": 1, "head_sha": "a", "run_attempt": 2, "conclusion": "success"},
        {"id": 2, "head_sha": "b", "run_attempt": 1, "conclusion": "failure"}]}
    answers = {
        "repos/o/r/actions/workflows/tests.yml/runs?created=%3E%3D2026-10-09&per_page=100&page=1": runs,
        "repos/o/r/actions/runs/1/attempts/1/jobs?per_page=100": {"jobs": [
            {"id": 11, "name": "tests (Python 3.13, part 2/2)", "conclusion": "failure"},
            {"id": 12, "name": "tests (Python 3.10, part 2/2)", "conclusion": "success"}]},
        "repos/o/r/actions/runs/1/attempts/2/jobs?per_page=100": {"jobs": [
            {"id": 13, "name": "tests (Python 3.13, part 2/2)", "conclusion": "success"}]},
        "repos/o/r/actions/runs/2/attempts/1/jobs?per_page=100": {"jobs": [
            {"id": 21, "name": "tests (Python 3.10, part 1/2)", "conclusion": "failure"}]},
        "repos/o/r/actions/jobs/11/logs": LOG.split("\n", 2)[0] + "\n" + LOG.splitlines()[1] + "\n",
        "repos/o/r/actions/jobs/21/logs": "FAILED tests/test_search.py::test_a_board_page - KeyError: 'x'\n",
    }
    since = datetime.date(2026, 10, 9)
    found = fr.tally("o/r", since, api=answers.__getitem__)
    assert found["runs"] == 2 and found["jobs_failed"] == 2
    out = fr.report(found, since)
    flaky, failed = out.split("**Failed**")
    assert "test_an_emailed_link_or_code_is_waited_for[]`: 1 failure on 1 commit (tests (Python 3.13, part 2/2))" \
        in flaky, out
    assert "test_search.py::test_a_board_page`: 1 failure on 1 commit" in failed, out


def test_a_week_with_no_failure_says_so():
    since = datetime.date(2026, 10, 9)
    out = fr.report({"runs": 12, "jobs_failed": 0, "failures": {}}, since)
    assert out.endswith("No test failed.") and "12 runs, 0 failed jobs." in out


def test_a_log_github_wont_give_is_counted_not_the_end_of_the_report():
    """The workflow's first run (Oct 16): one failed job's log came back as an error from GitHub's API (gh api exited
    1), and the whole report fell over with it. That job is said among the logs that couldn't be read; the others'
    tests are still counted."""
    runs = {"workflow_runs": [{"id": 1, "head_sha": "a", "run_attempt": 1, "conclusion": "failure"}]}

    def api(path):
        if path.endswith("/jobs/12/logs"):
            raise fr.ApiError("gh api repos/o/r/actions/jobs/12/logs: HTTP 410: Gone")
        return {"repos/o/r/actions/workflows/tests.yml/runs?created=%3E%3D2026-10-09&per_page=100&page=1": runs,
                "repos/o/r/actions/runs/1/attempts/1/jobs?per_page=100": {"jobs": [
                    {"id": 11, "name": "tests (Python 3.13, part 1/2)", "conclusion": "failure"},
                    {"id": 12, "name": "windows (Python 3.13, part 2/2)", "conclusion": "failure"}]},
                "repos/o/r/actions/jobs/11/logs": "FAILED tests/test_search.py::"
                                                  "test_a_board_page - KeyError: 'x'\n"}[path]

    since = datetime.date(2026, 10, 9)
    found = fr.tally("o/r", since, api=api)
    assert list(found["failures"]) == ["tests/test_search.py::test_a_board_page"]
    out = fr.report(found, since)
    assert "1 failed job's log couldn't be read: windows (Python 3.13, part 2/2) (GitHub said: gh api " in out, out
    assert "HTTP 410: Gone" in out, out


def test_each_job_family_is_tabled_and_the_windows_only_rate_said():
    """A4 (the review of Oct 18): each job family's runs, failed jobs and failed tests, and the runs that failed on
    Windows alone (a Windows part failed while every Linux part of that attempt passed) as a rate. Run 1 failed on
    Windows only; run 2 on Linux and Windows both (not Windows-only); run 3 passed everywhere. The aggregate checks
    ("server (Python ...)", "windows") repeat their parts and aren't counted."""
    runs = {"workflow_runs": [{"id": n, "head_sha": sha, "run_attempt": 1, "conclusion": c}
                              for n, sha, c in ((1, "a", "failure"), (2, "b", "failure"), (3, "c", "success"))]}

    def jobs(linux, windows):
        return {"jobs": [{"id": 0, "name": "tests (Python 3.13, part 1/6)", "conclusion": linux},
                         {"id": 0, "name": "server (Python 3.13)", "conclusion": linux},
                         {"id": 0, "name": "windows (Python 3.13, part 2/4)", "conclusion": windows},
                         {"id": 0, "name": "windows", "conclusion": windows},
                         {"id": 0, "name": "plugin manifests", "conclusion": "success"}]}

    answers = {"repos/o/r/actions/workflows/tests.yml/runs?created=%3E%3D2026-10-09&per_page=100&page=1": runs,
               "repos/o/r/actions/runs/1/attempts/1/jobs?per_page=100": jobs("success", "failure"),
               "repos/o/r/actions/runs/2/attempts/1/jobs?per_page=100": jobs("failure", "failure"),
               "repos/o/r/actions/runs/3/attempts/1/jobs?per_page=100": jobs("success", "success")}
    logs = iter(["FAILED tests/test_desk.py::test_a_windows_path - UnicodeDecodeError\n",
                 "FAILED tests/test_search.py::test_a_board_page - KeyError: 'x'\n",
                 "FAILED tests/test_desk.py::test_a_windows_path - UnicodeDecodeError\n"])

    def api(path):
        return next(logs) if path.endswith("/logs") else answers[path]

    since = datetime.date(2026, 10, 9)
    found = fr.tally("o/r", since, api=api)
    assert found["families"] == {
        "tests": {"runs": 3, "failed_jobs": 1, "tests": ["tests/test_search.py::test_a_board_page"]},
        "windows": {"runs": 3, "failed_jobs": 2, "tests": ["tests/test_desk.py::test_a_windows_path"]},
        "plugin manifests": {"runs": 3, "failed_jobs": 0, "tests": []}}
    assert found["windows_only"] == 1 and found["jobs_failed"] == 3
    assert round(fr.windows_rate(found), 1) == 33.3
    out = fr.report(found, since)
    assert "| tests | 3 | 1 | — | `test_a_board_page` |" in out, out
    assert "| windows | 3 | 2 | 1 | `test_a_windows_path` |" in out, out
    assert "| plugin manifests | 3 | 0 | — | — |" in out, out
    assert "Windows-only rate: 1 of 3 runs (33.3%). `windows` can be made a required check once this stays under " \
           "about 2% for four weeks." in out, out
    # (a week without Windows runs says no rate; one with no failure still has its table)
    assert fr.windows_rate({"families": {"tests": {"runs": 2}}}) is None
    quiet = fr.report({"runs": 1, "jobs_failed": 0, "failures": {}, "windows_only": 0,
                       "families": {"windows": {"runs": 1, "failed_jobs": 0, "tests": []}}}, since)
    assert "Windows-only rate: 0 of 1 run (0.0%)" in quiet and quiet.endswith("No test failed."), quiet
