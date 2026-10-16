"""The tests that failed in the last week's CI runs (the tests workflow), and which of them are flaky: a test that
failed on a commit whose tests then passed (a re-run, or another run of the same commit) failed by chance, not by
the change. Read from GitHub's API with the gh CLI (GH_TOKEN); the weekly workflow puts it in its summary.

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
    for run in runs:
        for attempt in range(1, int(run.get("run_attempt") or 1) + 1):
            jobs = (api(f"repos/{repo}/actions/runs/{run['id']}/attempts/{attempt}/jobs?per_page=100") or {}).get("jobs") or []
            for job in jobs:
                if job.get("conclusion") != "failure":
                    continue
                jobs_failed += 1
                try:
                    log = api(f"repos/{repo}/actions/jobs/{job['id']}/logs")
                except ApiError as e:
                    unread.append(str(job.get("name") or job["id"]))
                    why = why or str(e)
                    continue
                for test in failed_tests(log if isinstance(log, str) else ""):
                    failures.setdefault(test, []).append(
                        {"sha": run["head_sha"], "job": job.get("name") or "", "run": run["id"], "attempt": attempt,
                         "commit_passed": run["head_sha"] in passed})
    return {"runs": len(runs), "jobs_failed": jobs_failed, "failures": failures, "unread": unread, "unread_why": why}


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
        return "\n".join(lines + ["", "No test failed."])

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
    return "\n".join(lines)


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
