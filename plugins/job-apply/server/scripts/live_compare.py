"""What changed since last night's live check, employer by employer. No network.

    uv run python scripts/live_compare.py --issues issues.json \
        --log pipeline=live-pipeline/run.log --log search=live-search/run.log --log hr=live-hr/run.log \
        [--log accounts=live-accounts/run.log] [--partial search] \
        --run-url URL --body-out body.md --comment-out comment.md [--github-output "$GITHUB_OUTPUT"]

Reads what scripts/live_smoke.py printed (one LIVE_PIPELINE or LIVE_RESULT line of JSON per
employer), turns each employer's record into one outcome that stays put from night to night,
and compares the outcomes with the ones the last run saved in the "Nightly live check" issue's
body (a JSON block; --issues is the output of `gh issue list --json number,title,state,body`).
It writes the issue's new body, and a comment listing the changes when there are any. The body
also names the employers an apply check took to the review page in an earlier run but not in the
last ("still back", with the date each last got there: a quiet night writes no comment), and
stays within GitHub's size for an issue body (the details shortened, then left out, as the lists
grow). The nightly workflow (.github/workflows/live-nightly.yml) reads and writes the issue.

The outcomes:
  pipeline  (live_smoke.py --pipeline): where the Job Desk's pipeline ended, the report's
            "Ended" and "Waiting on": "ready", "needs_you sign_in", "needs_you stuck",
            "failed", "no postings" (the search found none) or "crash".
  hr        (live_smoke.py --pipeline --lists phoenix-metro --role hr): the same, for HR jobs on
            the Phoenix list, as an applicant with an HR background.
  finance   (... --role finance): the same, for finance jobs, as a finance applicant.
  accounts  (live_smoke.py --pipeline --test-identity --everywhere): the same, as the test identity, a clearly
            fake applicant with an inbox of its own whose accounts are made and signed in to, at
            every employer on both lists (--everywhere: the owner's call, Oct 11). Only once
            the repository has the test identity's secrets (scripts/TEST_IDENTITY.md): until then
            the nightly leaves it out, and it isn't compared.
  search    (live_smoke.py without --pipeline): whether the employer's search ran without an
            error: "works" or "error"; "no search" for an employer without a search block (its
            careers page is only opened). How many openings it found changes daily, so it's in
            the detail, never compared.

An employer missing from a check that finished has left the list ("removed"). One missing
from a check that didn't finish (--partial) keeps its last outcome, and a check that reported
no employers at all keeps all of its last outcomes: a broken run isn't an employer's change.
Nor is a site down for its maintenance (Workday's at weekends): "site down" keeps the
employer's last outcome too.
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
STATE_VERSION = 2  # 1: {check: {employer: outcome}}; 2: {check: {outcome: [employers]}}, and when each was last reached
DETAIL_CHARS = 140
BODY_LIMIT = 65536  # GitHub's issue body, in characters: an edit past it fails (the body held 55k on Oct 16, 2026)


class BodyTooLong(ValueError):
    """The issue's body wouldn't fit GitHub's limit even with its details left out."""


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


# What the search or the Job Desk says of a site down for its maintenance (job_apply.search,
# pipeline: "... is down for maintenance"): no outcome of the employer's own
SITE_DOWN = "site down"
_DOWN = re.compile(r"\bdown for maintenance\b", re.I)


def pipeline_outcome(rec: dict[str, Any]) -> Result:
    """One LIVE_PIPELINE record: where the pipeline ended on the employer's posting."""
    title = _short((rec.get("posting") or {}).get("title"), 45)  # room for the reason, which says more
    if rec.get("crash"):
        return Result("crash", _join(title, rec["crash"]))
    rounds = rec.get("rounds") or []
    if not rounds:  # the search found no posting to run it on
        note = rec.get("note") or ""
        return Result(SITE_DOWN if _DOWN.search(note) else "no postings", note)
    last = rounds[-1]
    if last.get("need") == "stuck" and _DOWN.search(last.get("reason") or ""):
        return Result(SITE_DOWN, _join(title, last.get("reason")))
    outcome = " ".join(p for p in (last.get("status") or "unknown", last.get("need") or "") if p)
    if outcome == "ready":  # the review page: its one sentence says what the outcome says, on two hundred rows
        return Result(outcome, title)
    return Result(outcome, _join(title, last.get("reason")))


_PARTLY = re.compile(r"\(\d+ of \d+ searches failed\)")  # server.search_company_jobs: others answered


def search_outcome(rec: dict[str, Any]) -> Result:
    """One LIVE_RESULT record: whether the employer's search works."""
    if "search_config" not in rec:  # no search block: the run only opened its careers page
        problem = (rec.get("crash") or (rec.get("browser") or {}).get("error")
                   or (rec.get("page") or {}).get("navigation_error"))
        return Result("no search", f"careers page: {problem}" if problem else "careers page opened")
    searches = [s for s in (rec.get("search_az"), rec.get("search_any")) if isinstance(s, dict)]
    errors = [s["error"] for s in searches if s.get("error")]
    # one wording (or one later page) failing while the others answer is a bad night for the
    # site, not a broken search: it would flip the result back and forth from night to night
    if errors and not (any(s.get("count") for s in searches) or all(_PARTLY.search(e) for e in errors)):
        return Result(SITE_DOWN if all(_DOWN.search(e) for e in errors) else "error", errors[0])
    if not searches:  # crashed or timed out before the search answered
        return Result("error", rec.get("crash") or "no search result")
    in_az = rec["search_az"].get("count", 0) if rec.get("search_az") else 0
    found = f"{in_az} in AZ" if in_az else (
        f"none in AZ, {rec['search_any'].get('count', 0)} anywhere" if rec.get("search_any") else "none in AZ")
    # a crash after the search (opening or filling the form) doesn't make the search fail
    return Result("works", _join(found, errors and _short(errors[0], 80), rec.get("crash") and f"then {rec['crash']}"))


@dataclass(frozen=True)
class Check:
    title: str
    prefix: str  # the lines of live_smoke.py's output that hold this check's records
    outcome: Callable[[dict[str, Any]], Result]


CHECKS = {
    "pipeline": Check("Apply pipeline, semiconductor list (technician jobs): where it ended on one posting per "
                      "employer", "LIVE_PIPELINE ", pipeline_outcome),
    "hr": Check("Apply pipeline, Phoenix list (HR jobs): where it ended on one posting per employer",
                "LIVE_PIPELINE ", pipeline_outcome),
    "finance": Check("Apply pipeline, Phoenix list (finance jobs): where it ended on one posting per employer",
                     "LIVE_PIPELINE ", pipeline_outcome),
    "search": Check("Search: does each employer's search work", "LIVE_RESULT ", search_outcome),
    "accounts": Check("Apply pipeline with the test identity (accounts made and signed in): where it ended at one "
                      "or two employers per job system", "LIVE_PIPELINE ", pipeline_outcome),
}


def read_log(text: str, prefix: str) -> dict[str, dict[str, Any]]:
    """The records in live_smoke.py's output, by company (its last record, if it has two)."""
    records: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        at = line.find(prefix)  # (a record run on from a line cut short before it: a long screenshot's)
        if at < 0:
            continue
        try:
            rec = json.loads(line[at + len(prefix):])
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
                if now[name].outcome == SITE_DOWN and name in before:  # down for the night: not its change
                    saved[name] = before[name]
                elif before.get(name) != now[name].outcome:
                    changes.append(Change(check, name, before.get(name), now[name].outcome, now[name].detail))
            for name in sorted(set(before) - set(now), key=str.lower):
                if check in partial:  # not reached this time: not a change
                    saved[name] = before[name]
                else:
                    changes.append(Change(check, name, before[name], None))
        state[check] = dict(sorted(saved.items(), key=lambda kv: kv[0].lower()))
    return state, changes


_STATE = re.compile(re.escape(STATE_MARK) + r"\s*```json[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)  # \r\n once edited on GitHub


def _saved(body: str | None) -> dict[str, Any] | None:
    m = _STATE.search(body or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def read_state(body: str | None) -> dict[str, dict[str, str]] | None:
    """The outcomes a body written by render_body saved, {check: {employer: outcome}}; None when it has none
    readable. Version 1 saved them so; version 2 groups each check's employers by outcome."""
    data = _saved(body)
    outcomes = data.get("outcomes") if data else None
    if not isinstance(outcomes, dict):
        return None
    state: dict[str, dict[str, str]] = {}
    for check, found in outcomes.items():
        if not isinstance(found, dict):
            continue
        if (data or {}).get("version", 1) == 1:
            read = {str(name): str(o) for name, o in found.items()}
        else:
            read = {str(name): str(o) for o, names in found.items() if isinstance(names, list) for name in names}
        state[str(check)] = dict(sorted(read.items(), key=lambda kv: kv[0].lower()))
    return state


def read_reached(body: str | None) -> dict[str, dict[str, str]]:
    """When an apply check last took each employer as far as it goes ({check: {employer: date}}), as saved."""
    data = _saved(body) or {}
    reached = data.get("reached")
    if not isinstance(reached, dict):
        return {}
    return {str(check): {str(name): str(date) for name, date in dates.items()}
            for check, dates in reached.items() if isinstance(dates, dict)}


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|")


def _code(text: Any) -> str:
    """Inline code: a job title or error text can't become a mention, a link or markup."""
    text = str(text).replace("`", "'").replace("|", "/")
    return f"`{text}`" if text else ""


def _title(check: str) -> str:
    return CHECKS[check].title if check in CHECKS else check


# as far as an apply check's run goes (the person's own Submit, or Apply, is next)
REACHED = frozenset({"ready", "needs_you your_submit"})
_APPLY_CHECKS = frozenset({"pipeline", "hr", "finance", "accounts"})


def reached_dates(previous: dict[str, dict[str, str]] | None, state: dict[str, dict[str, str]],
                  current: dict[str, dict[str, Result]], today: str) -> dict[str, dict[str, str]]:
    """When each apply check last took each employer as far as it goes: today for the employers this run did,
    the saved date for the others still listed (one not reached this run, or not run: its last date stands)."""
    out: dict[str, dict[str, str]] = {}
    for check, outcomes in state.items():
        if check not in _APPLY_CHECKS:
            continue
        before = (previous or {}).get(check) or {}
        now = current.get(check) or {}
        dates = {}
        for name in outcomes:
            r = now.get(name)
            if r is not None and r.outcome in REACHED:
                dates[name] = today
            elif name in before:
                dates[name] = before[name]
        if dates:
            out[check] = dates
    return out


def still_back(state: dict[str, dict[str, str]], reached: dict[str, dict[str, str]]) -> list[tuple[str, str, str]]:
    """Employers an apply check took as far as it goes in an earlier run but not in the last, with the date it
    last got there: the regression alarm's quiet nights (a run with no change writes no comment)."""
    return [(check, name, reached[check][name]) for check, outcomes in state.items() if check in reached
            for name, outcome in outcomes.items() if outcome not in REACHED and name in reached[check]]


def _grouped(outcomes: dict[str, str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for name, outcome in sorted(outcomes.items(), key=lambda kv: kv[0].lower()):
        grouped.setdefault(outcome, []).append(name)
    return dict(sorted(grouped.items()))


def _body(state: dict[str, dict[str, str]], current: dict[str, dict[str, Result]], *, checked_at: str,
          run_url: str, partial: set[str] | frozenset[str], reached: dict[str, dict[str, str]],
          detail_chars: int) -> str:
    lines = [
        "<!-- Rewritten by .github/workflows/live-nightly.yml after every run: edits here are lost. -->",
        "A check of employers' career sites every night (scripts/live_smoke.py: a fake profile, submitting "
        "turned off; only the test identity's check makes accounts). A comment is added here only when an "
        "employer's result changes; the tables are the latest run's.",
        "",
        f"Last checked: {checked_at}" + (f" · [run]({run_url})" if run_url else ""),
    ]
    back = still_back(state, reached)
    if back:
        n = len(back)
        lines += ["", f"**Still back:** {n} employer{'' if n == 1 else 's'} reached the review page in an earlier run "
                  "but not in the last: " + ", ".join(f"{name} ({check}; last there {date})" for check, name, date in back)
                  + "."]
    if detail_chars < DETAIL_CHARS:
        lines += ["", "(The details are " + ("shortened" if detail_chars else "left out")
                  + " to keep this page within GitHub's size for an issue.)"]
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
            detail = (_code(_short(r.detail, detail_chars)) if detail_chars else "") if r else "not checked in this run"
            lines.append(f"| {_cell(name)} | {_code(outcome)} | {detail} |")
    saved = json.dumps({"version": STATE_VERSION, "outcomes": {check: _grouped(found) for check, found in state.items()},
                        "reached": {check: dict(sorted(dates.items(), key=lambda kv: kv[0].lower()))
                                    for check, dates in reached.items() if dates}},
                       separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    lines += ["", "<details><summary>Saved outcomes (the next run compares with these)</summary>", "",
              STATE_MARK, "```json", saved, "```", "", "</details>", ""]
    return "\n".join(lines)


def render_body(state: dict[str, dict[str, str]], current: dict[str, dict[str, Result]], *, checked_at: str,
                run_url: str = "", partial: set[str] | frozenset[str] = frozenset(),
                reached: dict[str, dict[str, str]] | None = None, limit: int = BODY_LIMIT) -> str:
    """The issue's body: the tables, the still-back line and the saved state, within GitHub's size (the details
    shortened, then left out, when the tables outgrow it; BodyTooLong when even that doesn't fit)."""
    for detail_chars in (DETAIL_CHARS, DETAIL_CHARS // 2, 0):
        body = _body(state, current, checked_at=checked_at, run_url=run_url, partial=partial, reached=reached or {},
                     detail_chars=detail_chars)
        if len(body) <= limit:
            return body
    raise BodyTooLong(f"the issue's body would be {len(body):,} characters even with its details left out, over "
                      f"GitHub's {limit:,}: fewer employers, or the saved outcomes in a comment of their own")


def went_back(changes: list[Change]) -> list[Change]:
    """The regression alarm: employers an apply check's last run took as far as it goes (their review page)
    that this run stopped short of it. Not one that left the list (nor a site down: its last outcome is kept)."""
    return [c for c in changes if c.check in _APPLY_CHECKS and c.before in REACHED
            and c.after is not None and c.after not in REACHED]


def render_comment(changes: list[Change], run_url: str = "") -> str:
    n = len(changes)
    lines = [f"{n} change{'' if n == 1 else 's'} since the last nightly live check"
             + (f" ([run]({run_url}))" if run_url else "") + ":"]
    back = went_back(changes)
    if back:  # (one line, not a list: nightly_summary.py --changes reads the list's lines once each)
        lines += ["", f"**Went backwards:** {len(back)} employer{'' if len(back) == 1 else 's'} no longer "
                  f"reach{'es' if len(back) == 1 else ''} the review page: "
                  + ", ".join(f"{c.employer} ({c.check}: {_code(c.after)})" for c in back) + "."]
    for check in dict.fromkeys(c.check for c in changes):
        lines += ["", f"**{_title(check)}**", ""]
        for c in (c for c in changes if c.check == check):
            before = _code(c.before) if c.before is not None else "*new*"
            after = _code(c.after) if c.after is not None else "*no longer checked*"
            lines.append(f"- {c.employer}: {before} → {after}" + (f" ({_code(c.detail)})" if c.detail else ""))
    return "\n".join(lines) + "\n"


def _now() -> datetime:
    return datetime.now(timezone.utc)


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

    now = _now()
    reached = reached_dates(read_reached(issue.get("body")) if issue else None, state, current, now.strftime("%Y-%m-%d"))
    try:
        body = render_body(state, current, checked_at=now.strftime("%Y-%m-%d %H:%M UTC"), run_url=args.run_url,
                           partial=partial, reached=reached)
    except BodyTooLong as e:  # before the comment: the next run compares with the same saved outcomes, and reports again
        print(f"::error::{e}")
        return 1
    args.body_out.write_text(body, encoding="utf-8")
    print(f"issue body: {len(body):,} of {BODY_LIMIT:,} characters")
    for check, name, date in still_back(state, reached):
        print(f"still back: {name} ({check}; last there {date})")
    if changes:
        args.comment_out.write_text(render_comment(changes, args.run_url), encoding="utf-8")
    elif args.comment_out.exists():
        args.comment_out.unlink()
    for c in changes:
        print(f"changed: {c.check} / {c.employer}: {c.before or '(new)'} -> {c.after or '(no longer checked)'}")
    back = went_back(changes)
    for c in back:  # (an annotation on the run, besides the issue's comment)
        print(f"::warning::{c.employer} ({c.check}) no longer reaches the review page: now {c.after}")
    if not changes:
        print("no changes" + (" (nothing saved to compare with)" if previous is None else ""))
    if args.github_output:
        number, issue_state = (issue.get("number") or "", issue.get("state") or "") if issue else ("", "")
        with args.github_output.open("a", encoding="utf-8") as f:
            f.write(f"issue={number}\nissue_state={issue_state}\nchanges={len(changes)}\nwent_back={len(back)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
