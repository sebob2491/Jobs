"""The tests that failed in the last week's CI runs (the tests workflow), and which of them are flaky: a test that
failed on a commit whose tests then passed (a re-run, or another run of the same commit) failed by chance, not by
the change. Read from GitHub's API with the gh CLI (GH_TOKEN); the weekly workflow puts it in its summary.

And each job family's week (A4, the review of Oct 18: "tests" on Linux, "windows", "plugin manifests"): its runs,
its failed jobs, the tests that failed in it, and for Windows the runs that failed there only (a Windows part
failed while every Linux part of that attempt passed): the Windows-only rate. The aggregate checks ("server
(Python ...)", "windows") only repeat their parts, so they aren't counted. Once the Windows-only rate stays under
about 2% for four weeks, the `windows` check can be made one main's ruleset requires (the owner's call).

    uv run python scripts/flake_rate.py --repo sebob2491/Jobs --days 7
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
from typing import Any, Callable

# pytest's summary line for a failed test: "FAILED tests/test_x.py::test_y[case] - AssertionError: ..."
_FAILED = re.compile(r"\bFAILED (tests/\S+?\.py::\S+?)(?: - |\s*$)", re.M)

Api = Callable[[str], Any]

# The aggregate jobs (tests.yml's "server (Python 3.10)", "windows"): green when every part is, so never counted
_AGGREGATE = re.compile(r"^(?:server \(Python [\d.]+\)|windows)$")
WINDOWS, LINUX = "windows", "tests"  # the job families of the suite's parts (a job's name before " (")
DASH = "\u2014"  # (a table's empty cell)
WINDOWS_TARGET = 2.0  # percent: the Windows-only rate under which `windows` can be required (four weeks running)


def family(job_name: str) -> str:
    """A job's family: its name before its matrix's " (...)" ("windows (Python 3.13, part 2/4)": "windows")."""
    return job_name.split(" (", 1)[0].strip() or job_name


class ApiError(Exception):
    """GitHub's API turned a call down (gh api exited with an error), in its own words."""


def gh_api(path: str) -> Any:
    """One GitHub API call through the gh CLI: JSON where the answer is JSON, else its text (a job's log)."""
    done = subprocess.run(["gh", "api", path], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode:
        raise ApiError(f"gh api {path}: {(done.stderr or done.stdout).strip()[:300]}")
    out = done.stdout
    try:
        return json.loads(out)
    except ValueError:
        return out


def failed_tests(log: str) -> set[str]:
    """The tests a job's log says failed (each once, however many times its lines repeat it)."""
    return set(_FAILED.findall(log or ""))


def tally(repo: str, since: datetime.date, api: Api = gh_api, workflow: str = "tests.yml") -> dict[str, Any]:
    """Each failed test's failures in the runs since `since`: on which commits, and whether those commits' tests
    passed in the end (a flake) or didn't (a real failure, or one still being fixed)."""
    runs: list[dict[str, Any]] = []
    page = 1
    while True:
        got = api(f"repos/{repo}/actions/workflows/{workflow}/runs?created=%3E%3D{since.isoformat()}&per_page=100&page={page}")
        batch = (got or {}).get("workflow_runs") or []
        runs += batch
        if len(batch) < 100:
            break
        page += 1
    passed = {r["head_sha"] for r in runs if r.get("conclusion") == "success"}
    failures: dict[str, list[dict[str, Any]]] = {}
    unread: list[str] = []  # failed jobs whose logs GitHub wouldn't give (expired, or never kept)
    why = ""  # (GitHub's words for the first)
    jobs_failed = 0
    families: dict[str, dict[str, Any]] = {}  # each job family's runs, failed jobs and failed tests
    windows_only: set[Any] = set()  # the runs an attempt of which failed on Windows alone
    for run in runs:
        for attempt in range(1, int(run.get("run_attempt") or 1) + 1):
            jobs = (api(f"repos/{repo}/actions/runs/{run['id']}/attempts/{attempt}/jobs?per_page=100") or {}).get("jobs") or []
            failed_in: set[str] = set()  # the families with a failed job in this attempt
            for job in jobs:
                name = str(job.get("name") or "")
                if _AGGREGATE.match(name):
                    continue
                kin = families.setdefault(family(name), {"runs": set(), "failed_jobs": 0, "tests": set()})
                kin["runs"].add(run["id"])
                if job.get("conclusion") != "failure":
                    continue
                jobs_failed += 1
                kin["failed_jobs"] += 1
                failed_in.add(family(name))
                try:
                    log = api(f"repos/{repo}/actions/jobs/{job['id']}/logs")
                except ApiError as e:
                    unread.append(str(job.get("name") or job["id"]))
                    why = why or str(e)
                    continue
                for test in failed_tests(log if isinstance(log, str) else ""):
                    kin["tests"].add(test)
                    failures.setdefault(test, []).append(
                        {"sha": run["head_sha"], "job": job.get("name") or "", "run": run["id"], "attempt": attempt,
                         "commit_passed": run["head_sha"] in passed})
            if WINDOWS in failed_in and LINUX not in failed_in:
                windows_only.add(run["id"])
    return {"runs": len(runs), "jobs_failed": jobs_failed, "failures": failures, "unread": unread, "unread_why": why,
            "families": {name: {"runs": len(kin["runs"]), "failed_jobs": kin["failed_jobs"],
                                "tests": sorted(kin["tests"])} for name, kin in families.items()},
            "windows_only": len(windows_only)}


def windows_rate(found: dict[str, Any]) -> float | None:
    """The Windows-only rate, in percent: the runs that failed on Windows alone, of the runs Windows was tested in
    (None when it wasn't)."""
    runs = ((found.get("families") or {}).get(WINDOWS) or {}).get("runs") or 0
    return 100.0 * (found.get("windows_only") or 0) / runs if runs else None


def family_table(found: dict[str, Any], shown: int = 5) -> list[str]:
    """Each job family's week, as a Markdown table, and the Windows-only rate under it."""
    families = found.get("families") or {}
    if not families:
        return []
    lines = ["", "**By job family**", "",
             "| Family | Runs | Failed jobs | Windows-only failed runs | Tests that failed |",
             "|---|---|---|---|---|"]
    for name in sorted(families, key=lambda n: (n != LINUX, n != WINDOWS, n)):
        kin = families[name]
        tests = [f"`{t.split('::', 1)[-1]}`" for t in kin["tests"][:shown]]
        if len(kin["tests"]) > shown:
            tests.append(f"and {len(kin['tests']) - shown} more")
        only = str(found.get("windows_only") or 0) if name == WINDOWS else DASH
        said = ", ".join(tests) or DASH
        lines.append(f"| {name} | {kin['runs']} | {kin['failed_jobs']} | {only} | {said} |")
    rate = windows_rate(found)
    if rate is not None:
        runs = families[WINDOWS]["runs"]
        lines += ["", f"Windows-only rate: {found.get('windows_only') or 0} of {runs} run{'' if runs == 1 else 's'} "
                  f"({rate:.1f}%). `windows` can be made a required check once this stays under about "
                  f"{WINDOWS_TARGET:g}% for four weeks."]
    return lines


def report(found: dict[str, Any], since: datetime.date) -> str:
    """The tally as Markdown: the flaky tests first (most often first), then the others that failed."""
    failures = found["failures"]
    lines = [f"### Tests that failed in CI since {since.isoformat()}",
             f"{found['runs']} runs, {found['jobs_failed']} failed jobs."]
    unread = found.get("unread") or []
    if unread:
        whose = "job's log" if len(unread) == 1 else "jobs' logs"
        lines.append(f"{len(unread)} failed {whose} couldn't be read: " + ", ".join(sorted(set(unread)))
                     + (f" (GitHub said: {found['unread_why']})" if found.get("unread_why") else ""))
    if not failures:
        return "\n".join(lines + family_table(found) + ["", "No test failed."])

    def row(test: str) -> str:
        seen = failures[test]
        shas = {f["sha"] for f in seen}
        jobs = sorted({f["job"] for f in seen})
        return f"- `{test}`: {len(seen)} failure{'' if len(seen) == 1 else 's'} on {len(shas)} commit" \
               f"{'' if len(shas) == 1 else 's'} ({', '.join(jobs)})"

    flaky = sorted((t for t in failures if any(f["commit_passed"] for f in failures[t])),
                   key=lambda t: (-len(failures[t]), t))
    other = sorted((t for t in failures if t not in flaky), key=lambda t: (-len(failures[t]), t))
    if flaky:
        lines += ["", "**Flaky** (failed on a commit whose tests passed later):"] + [row(t) for t in flaky]
    if other:
        lines += ["", "**Failed** (the commit's tests never passed: a real failure, or one fixed by a later commit):"] \
            + [row(t) for t in other]
    return "\n".join(lines + family_table(found))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    since = datetime.date.today() - datetime.timedelta(days=args.days)
    print(report(tally(args.repo, since), since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
