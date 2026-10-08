"""What changed since last night's live check, employer by employer. No network.

    uv run python scripts/live_compare.py --issues issues.json \
        --log pipeline=live-pipeline/run.log --log search=live-search/run.log [--partial search] \
        --run-url URL --body-out body.md --comment-out comment.md [--github-output "$GITHUB_OUTPUT"]

Reads what scripts/live_smoke.py printed (one LIVE_PIPELINE or LIVE_RESULT line of JSON per
employer), turns each employer's record into one outcome that stays put from night to night,
and compares the outcomes with the ones the last run saved in the "Nightly live check" issue's
body (a JSON block; --issues is the output of `gh issue list --json number,title,state,body`).
It writes the issue's new body, and a comment listing the changes when there are any. The
nightly workflow (.github/workflows/live-nightly.yml) reads and writes the issue.

The outcomes:
  pipeline  (live_smoke.py --pipeline): where the Job Desk's pipeline ended, the report's
            "Ended" and "Waiting on": "ready", "needs_you sign_in", "needs_you stuck",
            "failed", "no postings" (the search found none) or "crash".
  search    (live_smoke.py without --pipeline): whether the employer's search ran without an
            error: "works" or "error"; "no search" for an employer without a search block (its
            careers page is only opened). How many openings it found changes daily, so it's in
            the detail, never compared.

An employer missing from a check that finished has left the list ("removed"). One missing
from a check that didn't finish (--partial) keeps its last outcome, and a check that reported
no employers at all keeps all of its last outcomes: a broken run isn't an employer's change.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple

ISSUE_TITLE = "Nightly live check"
STATE_MARK = "<!-- live-check state -->"  # the saved outcomes' JSON block comes right after this
STATE_VERSION = 1
DETAIL_CHARS = 140


class Result(NamedTuple):
    outcome: str  # compared from run to run
    detail: str = ""  # shown, never compared: posting titles, counts and error texts change


@dataclass(frozen=True)
class Change:
    check: str
    employer: str
    before: str | None  # None: not in the last run (added)
    after: str | None  # None: not in this run (removed)
    detail: str = ""


def _short(text: Any, limit: int = DETAIL_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _join(*parts: Any) -> str:
    return " · ".join(str(p) for p in parts if p)


def pipeline_outcome(rec: dict[str, Any]) -> Result:
    """One LIVE_PIPELINE record: where the pipeline ended on the employer's posting."""
    title = _short((rec.get("posting") or {}).get("title"), 45)  # room for the reason, which says more
    if rec.get("crash"):
        return Result("crash", _join(title, rec["crash"]))
    rounds = rec.get("rounds") or []
    if not rounds:  # the search found no posting to run it on
        return Result("no postings", rec.get("note") or "")
    last = rounds[-1]
    outcome = " ".join(p for p in (last.get("status") or "unknown", last.get("need") or "") if p)
    return Result(outcome, _join(title, last.get("reason")))


def search_outcome(rec: dict[str, Any]) -> Result:
    """One LIVE_RESULT record: whether the employer's search works."""
    if "search_config" not in rec:  # no search block: the run only opened its careers page
        problem = (rec.get("crash") or (rec.get("browser") or {}).get("error")
                   or (rec.get("page") or {}).get("navigation_error"))
        return Result("no search", f"careers page: {problem}" if problem else "careers page opened")
    searches = [s for s in (rec.get("search_az"), rec.get("search_any")) if isinstance(s, dict)]
    errors = [s["error"] for s in searches if s.get("error")]
    if errors:
        return Result("error", errors[0])
    if not searches:  # crashed or timed out before the search answered
        return Result("error", rec.get("crash") or "no search result")
    in_az = rec["search_az"].get("count", 0) if rec.get("search_az") else 0
    found = f"{in_az} in AZ" if in_az else (
        f"none in AZ, {rec['search_any'].get('count', 0)} anywhere" if rec.get("search_any") else "none in AZ")
    # a crash after the search (opening or filling the form) doesn't make the search fail
    return Result("works", _join(found, rec.get("crash") and f"then {rec['crash']}"))


@dataclass(frozen=True)
class Check:
    title: str
    prefix: str  # the lines of live_smoke.py's output that hold this check's records
    outcome: Callable[[dict[str, Any]], Result]


CHECKS = {
    "pipeline": Check("Apply pipeline: where it ended on one posting per employer", "LIVE_PIPELINE ",
                      pipeline_outcome),
    "search": Check("Search: does each employer's search work", "LIVE_RESULT ", search_outcome),
}


def read_log(text: str, prefix: str) -> dict[str, dict[str, Any]]:
    """The records in live_smoke.py's output, by company (its last record, if it has two)."""
    records: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        try:
            rec = json.loads(line[len(prefix):])
        except ValueError:  # a line cut short by a run that was stopped
            continue
        if isinstance(rec, dict) and rec.get("company"):
            records[str(rec["company"])] = rec
    return records


def results(check: str, text: str) -> dict[str, Result]:
    spec = CHECKS[check]
    out = {}
    for name, rec in read_log(text, spec.prefix).items():
        outcome, detail = spec.outcome(rec)
        out[name] = Result(outcome, _short(detail))
    return out


def compare(previous: dict[str, dict[str, str]] | None, current: dict[str, dict[str, Result]],
            partial: set[str] | frozenset[str] = frozenset()) -> tuple[dict[str, dict[str, str]], list[Change]]:
    """previous: {check: {employer: outcome}} the last run saved, or None on the first run.
    current: {check: {employer: Result}} from this run. partial: checks that didn't finish.
    Returns the outcomes to save and what changed. A check the last run didn't have is new:
    its employers aren't each reported as added."""
    state: dict[str, dict[str, str]] = {}
    changes: list[Change] = []
    for check in dict.fromkeys([*CHECKS, *(previous or {}), *current]):
        before = (previous or {}).get(check)
        now = current.get(check) or {}
        if not now:  # not run, or it reported no one: keep what the last run found
            if before is not None:
                state[check] = dict(before)
            continue
        saved = {name: r.outcome for name, r in now.items()}
        if before is not None:
            for name in sorted(now, key=str.lower):
                if before.get(name) != now[name].outcome:
                    changes.append(Change(check, name, before.get(name), now[name].outcome, now[name].detail))
            for name in sorted(set(before) - set(now), key=str.lower):
                if check in partial:  # not reached this time: not a change
                    saved[name] = before[name]
                else:
                    changes.append(Change(check, name, before[name], None))
        state[check] = dict(sorted(saved.items(), key=lambda kv: kv[0].lower()))
    return state, changes


_STATE = re.compile(re.escape(STATE_MARK) + r"\s*```json[ \t]*\n(.*?)\n[ \t]*```", re.S)


def read_state(body: str | None) -> dict[str, dict[str, str]] | None:
    """The outcomes a body written by render_body saved; None when it has none readable."""
    m = _STATE.search(body or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    outcomes = data.get("outcomes") if isinstance(data, dict) else None
    if not isinstance(outcomes, dict):
        return None
    return {str(check): {str(name): str(o) for name, o in found.items()}
            for check, found in outcomes.items() if isinstance(found, dict)}


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|")


def _code(text: Any) -> str:
    """Inline code: a job title or error text can't become a mention, a link or markup."""
    text = str(text).replace("`", "'").replace("|", "/")
    return f"`{text}`" if text else ""


def _title(check: str) -> str:
    return CHECKS[check].title if check in CHECKS else check


def render_body(state: dict[str, dict[str, str]], current: dict[str, dict[str, Result]], *, checked_at: str,
                run_url: str = "", partial: set[str] | frozenset[str] = frozenset()) -> str:
    lines = [
        "<!-- Rewritten by .github/workflows/live-nightly.yml after every run: edits here are lost. -->",
        "A read-only check of employers' career sites every night (scripts/live_smoke.py: a fake profile, "
        "submitting turned off). A comment is added here only when an employer's result changes; "
        "the tables are the latest run's.",
        "",
        f"Last checked: {checked_at}" + (f" · [run]({run_url})" if run_url else ""),
    ]
    for check, outcomes in state.items():
        now = current.get(check) or {}
        lines += ["", f"### {_title(check)}", ""]
        if not now:
            lines += ["Not checked in this run: these are the last run's results.", ""]
        elif check in partial:
            lines += ["This check didn't finish: employers it didn't reach keep their last result.", ""]
        lines += ["| Employer | Result | Detail |", "|---|---|---|"]
        for name, outcome in outcomes.items():
            r = now.get(name)
            detail = _code(r.detail) if r else "not checked in this run"
            lines.append(f"| {_cell(name)} | {_code(outcome)} | {detail} |")
    saved = json.dumps({"version": STATE_VERSION, "outcomes": state}, indent=1, sort_keys=True, ensure_ascii=False)
    lines += ["", "<details><summary>Saved outcomes (the next run compares with these)</summary>", "",
              STATE_MARK, "```json", saved, "```", "", "</details>", ""]
    return "\n".join(lines)


def render_comment(changes: list[Change], run_url: str = "") -> str:
    n = len(changes)
    lines = [f"{n} change{'' if n == 1 else 's'} since the last nightly live check"
             + (f" ([run]({run_url}))" if run_url else "") + ":"]
    for check in dict.fromkeys(c.check for c in changes):
        lines += ["", f"**{_title(check)}**", ""]
        for c in (c for c in changes if c.check == check):
            before = _code(c.before) if c.before is not None else "*new*"
            after = _code(c.after) if c.after is not None else "*no longer checked*"
            lines.append(f"- {c.employer}: {before} → {after}" + (f" ({_code(c.detail)})" if c.detail else ""))
    return "\n".join(lines) + "\n"


def find_issue(issues: Any, title: str = ISSUE_TITLE) -> dict[str, Any] | None:
    """The issue that holds the state: the oldest with exactly this title."""
    mine = [i for i in issues or [] if isinstance(i, dict) and i.get("title") == title]
    return min(mine, key=lambda i: i.get("number") or 0) if mine else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log", action="append", default=[], metavar="CHECK=PATH",
                    help=f"live_smoke.py's output for a check ({', '.join(CHECKS)}); repeatable")
    ap.add_argument("--partial", action="append", default=[], metavar="CHECK",
                    help="a check whose run didn't finish: employers it didn't report keep their last outcome")
    ap.add_argument("--issues", type=Path, help="`gh issue list --json number,title,state,body` output")
    ap.add_argument("--title", default=ISSUE_TITLE, help="the title of the issue that holds the state")
    ap.add_argument("--run-url", default="")
    ap.add_argument("--body-out", type=Path, required=True, help="the issue's new body")
    ap.add_argument("--comment-out", type=Path, required=True, help="the changes, written only when there are some")
    ap.add_argument("--github-output", type=Path, help="append issue=, issue_state= and changes= here")
    args = ap.parse_args(argv)

    current: dict[str, dict[str, Result]] = {}
    for spec in args.log:
        check, sep, path = spec.partition("=")
        if not sep or check not in CHECKS:
            ap.error(f"--log {spec!r}: expected CHECK=PATH, CHECK one of {', '.join(CHECKS)}")
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            text = ""
            print(f"::warning::{check}: can't read {path} ({e})")
        current[check] = results(check, text)
        print(f"{check}: {len(current[check])} employers reported")
        if not current[check]:
            print(f"::warning::{check}: no employers reported; keeping the last run's results")
    partial = {p.strip() for spec in args.partial for p in spec.split(",") if p.strip()}

    issues = json.loads(args.issues.read_text(encoding="utf-8")) if args.issues and args.issues.exists() else []
    issue = find_issue(issues, args.title)
    previous = read_state(issue.get("body")) if issue else None
    if issue and previous is None:
        print(f"::warning::issue #{issue.get('number')} has no saved outcomes to compare with; starting over")
    state, changes = compare(previous, current, partial)

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    args.body_out.write_text(render_body(state, current, checked_at=checked_at, run_url=args.run_url,
                                         partial=partial), encoding="utf-8")
    if changes:
        args.comment_out.write_text(render_comment(changes, args.run_url), encoding="utf-8")
    elif args.comment_out.exists():
        args.comment_out.unlink()
    for c in changes:
        print(f"changed: {c.check} / {c.employer}: {c.before or '(new)'} -> {c.after or '(no longer checked)'}")
    if not changes:
        print("no changes" + (" (nothing saved to compare with)" if previous is None else ""))
    if args.github_output:
        number, issue_state = (issue.get("number") or "", issue.get("state") or "") if issue else ("", "")
        with args.github_output.open("a", encoding="utf-8") as f:
            f.write(f"issue={number}\nissue_state={issue_state}\nchanges={len(changes)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
