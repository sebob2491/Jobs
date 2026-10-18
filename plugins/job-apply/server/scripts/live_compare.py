"""What changed since last night's live check, employer by employer. No network.

    uv run python scripts/live_compare.py --issues issues.json \
        --log pipeline=live-pipeline/run.log --log search=live-search/run.log --log hr=live-hr/run.log \
        [--log accounts=live-accounts/run.log] [--partial search] \
        --run-url URL --body-out body.md --comment-out comment.md [--github-output "$GITHUB_OUTPUT"] \
        [--trend-issues trend-issues.json --trend-body-out trend.md]

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
            "failed", "no postings" (the search found none) or "crash". A stop's detail starts its reason with
            its stop code ("[step_not_saved] The page didn't move on..."): nightly_summary.py sorts by it.
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

The comment also opens with "Bot checks up" when the employers stopped by a bot check or a CAPTCHA are
5 or more and twice the last run's (one cause behind many changes), and ends with the employers still
at their review page whose form there changed ("Form changed": a sha of its boxes' labels and kinds,
live_smoke.py's "form"; never counted as went backwards) and those an apply check took twice their
usual time or more, and 60 s or more ("Slower": the median of their last 10 runs, kept in the trend
issue). Either alone writes a comment.

An employer missing from a check that finished has left the list ("removed"). One missing
from a check that didn't finish (--partial) keeps its last outcome, and a check that reported
no employers at all keeps all of its last outcomes: a broken run isn't an employer's change.
Nor is a site down for its maintenance (Workday's at weekends): "site down" keeps the
employer's last outcome too.

The history goes in a second issue, "Nightly live trends" (label live-trends: never live-check, which the
workflow's other steps look the first one up by): with --trend-issues (`gh issue list --label live-trends
--json number,title,state,body`) and --trend-body-out, its new body is written too. It keeps the last 60
runs' counts per check (the employers at the review page, or whose search works, out of those checked; each
outcome's count; the median seconds an employer took) and each employer's last 20 runs as letters (R there,
x not, - not checked) and, for the apply checks, the seconds of its last 10 runs, and names the employers that flip (3 changes or more in their last 10 runs). A check
that didn't finish, or didn't run, adds no counts that night and gives its employers "-". A missing or
unreadable history starts afresh; trouble with it never fails the compare.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple

ISSUE_TITLE = "Nightly live check"
STATE_MARK = "<!-- live-check state -->"  # the saved outcomes' JSON block comes right after this
# 1: {check: {employer: outcome}}; 2: {check: {outcome: [employers]}}, and when each was last reached; 3: and the form
# each apply check's employers at their review page had there ("forms": {check: {employer: "<boxes>:<sha>"}})
STATE_VERSION = 3
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
# A search that answered without an error and found nothing, in Arizona or anywhere (its broader words too): what a
# parser that no longer reads the site's answer finds, or a site that took its openings away (the review of Oct 15).
# No search here has found none anywhere since the broader words came (run 114), so it doesn't flip from night to night
EMPTY = "empty"
_DOWN = re.compile(r"\bdown for maintenance\b", re.I)
_CODE = re.compile(r"[a-z_]{1,40}")  # a round's stop code (pipeline.STOP_CODES), put before its reason in the detail


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
    code = last.get("code") if isinstance(last.get("code"), str) and _CODE.fullmatch(last["code"]) else ""
    reason = f"[{code}] {last.get('reason') or ''}".strip() if code else last.get("reason")
    if code == "site_down" or last.get("need") == "stuck" and _DOWN.search(last.get("reason") or ""):
        return Result(SITE_DOWN, _join(title, reason))
    outcome = " ".join(p for p in (last.get("status") or "unknown", last.get("need") or "") if p)
    if outcome == "ready":  # the review page: its one sentence says what the outcome says, on two hundred rows
        return Result(outcome, title)
    return Result(outcome, _join(title, reason))


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
    if not in_az and not errors and isinstance(rec.get("search_any"), dict) and not rec["search_any"].get("count"):
        return Result(EMPTY, _join("none in AZ, none anywhere", rec.get("crash") and f"then {rec['crash']}"))
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


def run_seconds(check: str, text: str) -> dict[str, float]:
    """How long the check took at each employer (the seconds live_smoke.py keeps in each record), where it says."""
    return {name: float(rec["seconds"]) for name, rec in read_log(text, CHECKS[check].prefix).items()
            if isinstance(rec.get("seconds"), (int, float)) and not isinstance(rec.get("seconds"), bool)}


FORM_SHA = 12  # the hex digits of a form's sha kept: enough to tell two forms apart, on two hundred rows


def form_shape(fields: Any) -> dict[str, Any]:
    """A page's form as the compare keeps it (live_smoke.py notes it in each employer's record, "form", from the page it
    saved last): how many boxes, and a sha1 of their sorted (label, kind) pairs, so a review page that asks something
    new is seen though the employer is still at its review page. Never a box's value."""
    pairs = sorted((" ".join(str(f.get("label") or "").split()), str(f.get("kind") or ""))
                   for f in fields or [] if isinstance(f, dict))
    return {"n": len(pairs), "sha": hashlib.sha1(json.dumps(pairs).encode("utf-8")).hexdigest()[:FORM_SHA]}


def run_forms(check: str, text: str) -> dict[str, dict[str, Any]]:
    """Each employer's form (form_shape), where its record says."""
    out = {}
    for name, rec in read_log(text, CHECKS[check].prefix).items():
        form = rec.get("form")
        if isinstance(form, dict) and isinstance(form.get("n"), int) and isinstance(form.get("sha"), str):
            out[name] = {"n": form["n"], "sha": form["sha"][:FORM_SHA]}
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


def read_forms(body: str | None) -> dict[str, dict[str, dict[str, Any]]]:
    """The form each apply check's employers had at their review page, as saved ({check: {employer: {"n", "sha"}}});
    none before version 3."""
    forms = (_saved(body) or {}).get("forms")
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for check, found in (forms.items() if isinstance(forms, dict) else ()):
        for name, said in (found.items() if isinstance(found, dict) else ()):
            n, _, sha = str(said).partition(":")
            if n.isdigit() and sha:
                out.setdefault(str(check), {})[str(name)] = {"n": int(n), "sha": sha}
    return out


def forms_to_save(previous: dict[str, dict[str, dict[str, Any]]], state: dict[str, dict[str, str]],
                  current: dict[str, dict[str, Result]],
                  forms: dict[str, dict[str, dict[str, Any]]]) -> dict[str, dict[str, dict[str, Any]]]:
    """The forms to save: each apply check's employers whose saved outcome is their review page (REACHED), with this
    run's form where this run checked them, else the one saved (not checked this time, or the check didn't run)."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for check, outcomes in state.items():
        if check not in _APPLY_CHECKS:
            continue
        now, mine = current.get(check) or {}, {}
        for name, outcome in outcomes.items():
            form = (forms.get(check) or {}).get(name) if name in now else (previous.get(check) or {}).get(name)
            if outcome in REACHED and form:
                mine[name] = form
        if mine:
            out[check] = mine
    return out


class FormChange(NamedTuple):
    check: str
    employer: str
    outcome: str  # (as far as the check goes, both runs)
    before: int  # boxes
    after: int


def form_changes(previous: dict[str, dict[str, str]] | None, saved: dict[str, dict[str, dict[str, Any]]],
                 current: dict[str, dict[str, Result]],
                 forms: dict[str, dict[str, dict[str, Any]]]) -> list[FormChange]:
    """The employers an apply check took to their review page in the last run and this one whose form there isn't the
    same (form_shape's sha): something new asked, or something gone. Never a step back (went_back): the employer is
    still as far as it goes."""
    out = []
    for check in current:
        if check not in _APPLY_CHECKS:
            continue
        for name in sorted(current[check], key=str.lower):
            outcome = current[check][name].outcome
            was, now = (saved.get(check) or {}).get(name), (forms.get(check) or {}).get(name)
            if (outcome in REACHED and ((previous or {}).get(check) or {}).get(name) in REACHED and was and now
                    and was["sha"] != now["sha"]):
                out.append(FormChange(check, name, outcome, int(was["n"]), int(now["n"])))
    return out


# An employer slower than its usual: this many times the median of its last runs (Trend.seconds), and this many seconds
# or more, after this many runs of it at least (one slow night alone is no usual). nightly_summary.py's job systems
# compare by the same factor
SLOWER = 2.0
SLOWER_FLOOR = 60
SLOWER_RUNS = 3


class Slower(NamedTuple):
    check: str
    employer: str
    seconds: int
    usual: int  # the median of its last runs


def slower(trend: Trend | None, seconds: dict[str, dict[str, float]],
           partial: set[str] | frozenset[str] = frozenset()) -> list[Slower]:
    """The employers this run took SLOWER times their usual or more (the median of their last runs, the trend's), and
    SLOWER_FLOOR seconds or more: a site, or the desk, gone slow before any result changes. Apply checks that
    finished only."""
    out = []
    for check in _ordered(seconds):
        if check not in _APPLY_CHECKS or check in partial:
            continue
        for name, took in sorted(seconds[check].items(), key=lambda kv: kv[0].lower()):
            last = ((trend.seconds if trend else {}).get(check) or {}).get(name) or []
            if len(last) < SLOWER_RUNS:
                continue
            usual = statistics.median(last)
            if took >= SLOWER_FLOOR and took >= SLOWER * usual:
                out.append(Slower(check, name, round(took), round(usual)))
    return out


BOT_OUTCOMES = frozenset({"needs_you bot_check", "needs_you captcha"})
BOTS_UP_MIN = 5  # employers stopped by a bot check or a CAPTCHA, at least, and at least BOTS_UP times the last run's
BOTS_UP = 2


def bots_up(previous: dict[str, dict[str, str]] | None, current: dict[str, dict[str, Result]]) -> tuple[int, int] | None:
    """(this run's employers stopped by a bot check or a CAPTCHA, the last run's) across the apply checks this run
    has, when this run's are BOTS_UP_MIN or more and BOTS_UP times the last run's or more: a CAPTCHA come up at a
    job system's employers reads as many "expected" lines, never as the one change it is. None otherwise."""
    if previous is None:
        return None
    checks = [c for c in current if c in _APPLY_CHECKS and current[c]]
    now = sum(1 for c in checks for r in current[c].values() if r.outcome in BOT_OUTCOMES)
    was = sum(1 for c in checks for o in (previous.get(c) or {}).values() if o in BOT_OUTCOMES)
    return (now, was) if now >= BOTS_UP_MIN and now >= BOTS_UP * was else None


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
          forms: dict[str, dict[str, dict[str, Any]]], detail_chars: int) -> str:
    lines = [
        "<!-- Rewritten by .github/workflows/live-nightly.yml after every run: edits here are lost. -->",
        "A check of employers' career sites every night (scripts/live_smoke.py: a fake profile, submitting "
        "turned off; only the test identity's check makes accounts). A comment is added here only when an "
        "employer's result changes, its review page asks something else, or it takes twice as long as usual; the "
        "tables are the latest run's.",
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
                                    for check, dates in reached.items() if dates},
                        # (one short string an employer: the body's size is GitHub's, and two hundred rows share it)
                        "forms": {check: {name: f"{form['n']}:{form['sha']}"
                                          for name, form in sorted(found.items(), key=lambda kv: kv[0].lower())}
                                  for check, found in forms.items() if found}},
                       separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    lines += ["", "<details><summary>Saved outcomes (the next run compares with these)</summary>", "",
              STATE_MARK, "```json", saved, "```", "", "</details>", ""]
    return "\n".join(lines)


def render_body(state: dict[str, dict[str, str]], current: dict[str, dict[str, Result]], *, checked_at: str,
                run_url: str = "", partial: set[str] | frozenset[str] = frozenset(),
                reached: dict[str, dict[str, str]] | None = None,
                forms: dict[str, dict[str, dict[str, Any]]] | None = None, limit: int = BODY_LIMIT) -> str:
    """The issue's body: the tables, the still-back line and the saved state (the outcomes, the dates and the review
    pages' forms: forms_to_save), within GitHub's size (the details shortened, then left out, when the tables outgrow
    it; BodyTooLong when even that doesn't fit)."""
    for detail_chars in (DETAIL_CHARS, DETAIL_CHARS // 2, 0):
        body = _body(state, current, checked_at=checked_at, run_url=run_url, partial=partial, reached=reached or {},
                     forms=forms or {}, detail_chars=detail_chars)
        if len(body) <= limit:
            return body
    raise BodyTooLong(f"the issue's body would be {len(body):,} characters even with its details left out, over "
                      f"GitHub's {limit:,}: fewer employers, or the saved outcomes in a comment of their own")


def went_back(changes: list[Change]) -> list[Change]:
    """The regression alarm: employers an apply check's last run took as far as it goes (their review page)
    that this run stopped short of it. Not one that left the list (nor a site down: its last outcome is kept)."""
    return [c for c in changes if c.check in _APPLY_CHECKS and c.before in REACHED
            and c.after is not None and c.after not in REACHED]


def _plus_minus(before: int, after: int) -> str:
    return f"+{after - before}" if after > before else f"−{before - after}" if after < before else "±0, other boxes"


def render_comment(changes: list[Change], run_url: str = "", *, forms: list[FormChange] | None = None,
                   slow: list[Slower] | None = None, bots: tuple[int, int] | None = None) -> str:
    """The changes since the last run, a check at a time, after the alarms: the bot checks up (bots_up), the employers
    that went backwards (went_back); and at the end the review pages whose form changed (form_changes, never a step
    back) and the employers much slower than usual (slower). None of the alarms' lines reads as a change's line
    (nightly_summary.py's triage reads those)."""
    n = len(changes)
    lines = []
    if bots is not None:  # (first: one cause behind many changes' lines)
        lines += [f"**Bot checks up**: {bots[0]} employers (last run {bots[1]}) stopped at a bot check or a CAPTCHA.", ""]
    lines += [(f"{n} change{'' if n == 1 else 's'} since the last nightly live check" if n else
               "No result changed since the last nightly live check") + (f" ([run]({run_url}))" if run_url else "") + ":"]
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
    if forms:
        lines += ["", "**Form changed** (still at the review page: never a step back)", ""]
        lines += [f"- Form changed at {f.employer} ({f.check}, still {_code(f.outcome)}): {f.before} → {f.after} "
                  f"boxes, {_plus_minus(f.before, f.after)}" for f in forms]
    if slow:
        lines += ["", f"**Slower** ({SLOWER:g}× an employer's usual time or more, and {SLOWER_FLOOR} s or more)", ""]
        lines += [f"- Slower at {s.employer} ({s.check}): {s.seconds} s, its usual {s.usual} s" for s in slow]
    return "\n".join(lines) + "\n"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def find_issue(issues: Any, title: str = ISSUE_TITLE) -> dict[str, Any] | None:
    """The issue that holds the state: the oldest with exactly this title."""
    mine = [i for i in issues or [] if isinstance(i, dict) and i.get("title") == title]
    return min(mine, key=lambda i: i.get("number") or 0) if mine else None


# --- the trend issue: the last runs' counts, and the employers that flip ----------------------------------

TREND_TITLE = "Nightly live trends"  # (label live-trends; the issue above is found by its own title and label)
TREND_MARK = "<!-- live-trends state -->"  # the saved history's JSON block comes right after this
TREND_VERSION = 1
TREND_RUNS = 60  # the runs kept
TREND_LETTERS = 20  # each employer's last runs kept as letters, per check
TREND_SHOWN = 14  # the runs in the body's table
FLIP_WINDOW = 10  # an employer flips when its result changed FLIP_MIN times or more in its last FLIP_WINDOW runs
FLIP_MIN = 3
FLIP_SHOWN = 20  # the flipping employers the body names per check (nightly_summary.py --trend names them all)
TREND_SECONDS = 10  # each apply check's employers' last runs' seconds kept (slower)
# (saved as two base-36 digits a run, to keep the body within GitHub's size: a run longer than 21 minutes and a half is
# kept as that long)
SECONDS_MOST = 36 * 36 - 1
TREND_ORDER = ("pipeline", "hr", "accounts", "search", "finance")  # (the order the table shows them in)


class Trend(NamedTuple):
    """The trend issue's history. runs: oldest first, each {"at", "run_url", "checks": {check: {"reached", "total",
    "outcomes": {outcome: count}, "median_seconds"?}}}, where "reached" is the employers an apply check took as far
    as it goes (the search check's: its searches that work) and a check the run left partial or didn't run is left
    out. letters: {check: {employer: its last runs' letters, oldest first}}, R reached (works), x not, - not checked
    (the check partial or not run, the employer not in it, or its site down for maintenance). seconds: {check:
    {employer: the seconds its last TREND_SECONDS runs took, oldest first}}, the apply checks' (slower), for the
    employers with letters."""
    runs: list[dict[str, Any]]
    letters: dict[str, dict[str, str]]
    seconds: dict[str, dict[str, list[int]]] = {}


def _ordered(checks: Any) -> list[str]:
    return sorted(checks, key=lambda c: (TREND_ORDER.index(c) if c in TREND_ORDER else len(TREND_ORDER), c))


def letter(check: str, outcome: str) -> str:
    """One run's letter for an employer: R as far as the check goes (the search check: its search works), x not,
    - a site down for its maintenance (not the employer's own result)."""
    if outcome == SITE_DOWN:
        return "-"
    return "R" if (outcome == "works" if check == "search" else outcome in REACHED) else "x"


def add_run(trend: Trend | None, current: dict[str, dict[str, Result]], *, at: str, run_url: str = "",
            partial: set[str] | frozenset[str] = frozenset(),
            seconds: dict[str, dict[str, float]] | None = None) -> Trend:
    """The history with this run added (the oldest past TREND_RUNS let go): each check that finished counted, and a
    letter for each employer of each check (- where the check didn't finish or didn't run, or the employer isn't in
    it now). An employer with no R or x in its last TREND_LETTERS runs is let go."""
    counted = {check: found for check, found in current.items() if found and check not in partial}
    checks: dict[str, dict[str, Any]] = {}
    for check in _ordered(counted):
        found = counted[check]
        one: dict[str, Any] = {
            "reached": sum(1 for r in found.values() if letter(check, r.outcome) == "R"), "total": len(found),
            "outcomes": dict(sorted(Counter(r.outcome for r in found.values()).items()))}
        took = [s for name, s in ((seconds or {}).get(check) or {}).items() if name in found]
        if took:
            one["median_seconds"] = round(statistics.median(took))
        checks[check] = one
    runs = [*(trend.runs if trend else []), {"at": at, "run_url": run_url, "checks": checks}][-TREND_RUNS:]
    before = trend.letters if trend else {}
    letters: dict[str, dict[str, str]] = {}
    for check in _ordered({*before, *counted}):
        found, was = counted.get(check) or {}, before.get(check) or {}
        strings = {}
        for name in sorted({*was, *found}, key=str.lower):
            said = (was.get(name, "") + (letter(check, found[name].outcome) if name in found else "-"))[-TREND_LETTERS:]
            if said.strip("-"):
                strings[name] = said
        if strings:
            letters[check] = strings
    # each apply check's employers' seconds (slower): this run's added where the check finished; kept for the
    # employers still with letters
    kept_seconds: dict[str, dict[str, list[int]]] = {}
    for check, strings in letters.items():
        if check not in _APPLY_CHECKS:
            continue
        now = ((seconds or {}).get(check) or {}) if check in counted else {}
        was_took = (trend.seconds if trend else {}).get(check) or {}
        kept: dict[str, list[int]] = {}
        for name in strings:
            last = list(was_took.get(name) or []) + ([min(SECONDS_MOST, max(0, round(now[name])))] if name in now else [])
            if last:
                kept[name] = last[-TREND_SECONDS:]
        if kept:
            kept_seconds[check] = kept
    return Trend(runs, letters, kept_seconds)


def flips(letters: str, window: int = FLIP_WINDOW) -> int:
    """How many times an employer's result changed in its last `window` runs (the runs it wasn't checked in skipped)."""
    seen = [c for c in letters[-window:] if c != "-"]
    return sum(1 for a, b in zip(seen, seen[1:]) if a != b)


def flipping(trend: Trend, at_least: int = FLIP_MIN,
             window: int = FLIP_WINDOW) -> dict[str, list[tuple[str, str, int]]]:
    """{check: [(employer, its last `window` letters, how many times it changed)]}: the employers whose result changed
    `at_least` times or more in their last `window` runs, the most changes first."""
    out: dict[str, list[tuple[str, str, int]]] = {}
    for check in _ordered(trend.letters):
        found = [(name, said[-window:], flips(said, window)) for name, said in trend.letters[check].items()]
        found = sorted((f for f in found if f[2] >= at_least), key=lambda f: (-f[2], f[0].lower()))
        if found:
            out[check] = found
    return out


def _pack(trend: Trend) -> dict[str, Any]:
    """The history as saved: the runs' counts a column per check and figure (a list with one entry a run, null where the
    check added no counts that run), so each name is written once, not sixty times: the runs as rows ran past
    GitHub's size for the body with sixty runs of four checks."""
    runs = trend.runs
    counts: dict[str, Any] = {}
    for check in _ordered({c for run in runs for c in run["checks"]}):
        ones = [run["checks"].get(check) for run in runs]
        entry: dict[str, Any] = {key: [one[key] if one else None for one in ones] for key in ("reached", "total")}
        entry["outcomes"] = {o: [one["outcomes"].get(o, 0) if one else None for one in ones]
                             for o in sorted({o for one in ones if one for o in one["outcomes"]})}
        if any(one and one.get("median_seconds") is not None for one in ones):
            entry["median_seconds"] = [one.get("median_seconds") if one else None for one in ones]
        counts[check] = entry
    # each check's employers' seconds in its letters' order, without their names again: two base-36 digits a run, an
    # employer's runs run together, a comma between employers ("1o1q,,2b": none for the second)
    took = {check: ",".join("".join(_b36(s) for s in trend.seconds.get(check, {}).get(name) or [])
                            for name in trend.letters[check])
            for check in trend.letters if trend.seconds.get(check)}
    return {"version": TREND_VERSION, "runs": [{"at": run["at"], "run_url": run.get("run_url") or ""} for run in runs],
            "counts": counts, "letters": trend.letters, **({"seconds": took} if took else {})}


_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(n: int) -> str:
    n = min(SECONDS_MOST, max(0, int(n)))
    return _B36[n // 36] + _B36[n % 36]


def _unpack(data: dict[str, Any]) -> Trend:
    """The history _pack saved (ValueError, TypeError, KeyError or AttributeError where it doesn't read)."""
    runs: list[dict[str, Any]] = [{"at": str(run["at"]), "run_url": str(run.get("run_url") or ""), "checks": {}}
                                  for run in data["runs"]]
    n = len(runs)
    for check, entry in (data.get("counts") or {}).items():
        reached, total, outcomes = entry["reached"], entry["total"], entry.get("outcomes") or {}
        took = entry.get("median_seconds") or [None] * n
        if any(not isinstance(col, list) or len(col) != n for col in (reached, total, took, *outcomes.values())):
            raise ValueError(f"{check}: a column of another length than the runs'")
        for i, run in enumerate(runs):
            if total[i] is None:
                continue
            one: dict[str, Any] = {"reached": int(reached[i]), "total": int(total[i]),
                                   "outcomes": {str(o): int(col[i]) for o, col in outcomes.items() if col[i]}}
            if took[i] is not None:
                one["median_seconds"] = round(float(took[i]))
            run["checks"][str(check)] = one
    for run in runs:
        run["checks"] = {c: run["checks"][c] for c in _ordered(run["checks"])}
    letters: dict[str, dict[str, str]] = {}
    for check, found in (data.get("letters") or {}).items():
        for name, said in (found.items() if isinstance(found, dict) else ()):
            said = "".join(c for c in str(said) if c in "Rx-")[-TREND_LETTERS:]  # (anything else: an edit's)
            if said.strip("-"):
                letters.setdefault(str(check), {})[str(name)] = said
    kept_seconds: dict[str, dict[str, list[int]]] = {}
    for check, said in (data.get("seconds") or {}).items():
        names, each = list((data.get("letters") or {}).get(check) or {}), str(said).split(",")
        if len(names) != len(each) or str(check) not in letters:  # (an edit's: the seconds let go, the rest kept)
            continue
        for name, runs_took in zip(names, each):
            if str(name) in letters[str(check)] and runs_took and re.fullmatch(r"(?:[0-9a-z]{2})+", runs_took):
                kept_seconds.setdefault(str(check), {})[str(name)] = [
                    _B36.index(runs_took[i]) * 36 + _B36.index(runs_took[i + 1])
                    for i in range(0, len(runs_took), 2)][-TREND_SECONDS:]
    return Trend(runs[-TREND_RUNS:], letters, kept_seconds)


_TREND = re.compile(re.escape(TREND_MARK) + r"\s*```json[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)


def read_trend(body: str | None) -> Trend | None:
    """The history a body written by render_trend saved; None when it has none readable (a fresh one starts)."""
    m = _TREND.search(body or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        if not isinstance(data, dict) or data.get("version") != TREND_VERSION:
            return None
        return _unpack(data)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _trend_cell(one: dict[str, Any] | None) -> str:
    if not one:
        return "—"
    took = one.get("median_seconds")
    return f"{one['reached']}/{one['total']}" + (f" · {took} s" if took is not None else "")


def _trend_body(trend: Trend) -> str:
    shown = trend.runs[-TREND_SHOWN:][::-1]
    checks = _ordered({c for run in shown for c in run["checks"]})
    lines = [
        "<!-- Rewritten by .github/workflows/live-nightly.yml after every full run: edits here are lost. -->",
        "The nightly live check's history (the \"Nightly live check\" issue holds the last run's results, and its "
        "changes). Each run's checks, newest first: the employers taken to the review page out of those checked "
        "(search: the searches that work), and the median time an employer took. A check that didn't finish, or "
        "didn't run, adds nothing that night (—).",
        "",
    ]
    if checks:
        lines += ["| Run | " + " | ".join(checks) + " |", "|---|" + "---|" * len(checks)]
        for run in shown:
            when = f"[{run['at']}]({run['run_url']})" if run.get("run_url") else run["at"]
            lines.append(f"| {when} | " + " | ".join(_trend_cell(run["checks"].get(c)) for c in checks) + " |")
    else:
        lines.append("No run has finished a check yet.")
    flip = flipping(trend)
    lines += ["", "### Flipping", "",
              f"Employers whose result changed {FLIP_MIN} times or more in their last {FLIP_WINDOW} runs, their "
              "letters oldest first: `R` at the review page (search: it works), `x` not, `-` not checked. "
              "`scripts/nightly_summary.py --handled` leaves them out of data/desk_handled.yaml.", ""]
    if not flip:
        lines.append("None.")
    for check, found in flip.items():
        more = len(found) - FLIP_SHOWN
        lines.append(f"- **{check}** ({len(found)}): "
                     + ", ".join(f"{_cell(name)} `{said}`" for name, said, _ in found[:FLIP_SHOWN])
                     + (f", and {more} more (`scripts/nightly_summary.py --trend` names them all)" if more > 0 else ""))
    saved = json.dumps(_pack(trend), separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    lines += ["", "<details><summary>Saved history (the next run adds to it)</summary>", "",
              TREND_MARK, "```json", saved, "```", "", "</details>", ""]
    return "\n".join(lines)


def render_trend(trend: Trend, limit: int = BODY_LIMIT) -> str:
    """The trend issue's body, within GitHub's size: its oldest runs let go if it would outgrow it (BodyTooLong when
    even one run doesn't fit)."""
    while True:
        body = _trend_body(trend)
        if len(body) <= limit or len(trend.runs) <= 1:
            break
        trend = Trend(trend.runs[1:], trend.letters, trend.seconds)
    if len(body) > limit:
        raise BodyTooLong(f"the trend issue's body would be {len(body):,} characters, over GitHub's {limit:,}")
    return body


def _last_trend(issues_path: Path | None, title: str = TREND_TITLE) -> Trend | None:
    """The history the trend issue holds now (before this run's added), for slower; None when there's none to be read
    (write_trend says why)."""
    try:
        issues = json.loads(issues_path.read_text(encoding="utf-8")) if issues_path and issues_path.exists() else []
        issue = find_issue(issues, title)
        return read_trend(issue.get("body")) if issue else None
    except Exception:  # (a look back: never a reason to lose tonight's results)
        return None


def write_trend(issues_path: Path | None, out: Path, current: dict[str, dict[str, Result]], *, at: str,
                run_url: str, partial: set[str] | frozenset[str], seconds: dict[str, dict[str, float]],
                title: str = TREND_TITLE) -> dict[str, Any] | None:
    """The trend issue's new body, written to `out`; the issue it's for (None: there's none yet). Its last body
    missing or unreadable starts a fresh history. Any trouble writes no body, so the issue stays as it was, and
    never fails the compare: the other issue's results come first."""
    if out.exists():
        out.unlink()
    issue = None
    try:
        issues = json.loads(issues_path.read_text(encoding="utf-8")) if issues_path and issues_path.exists() else []
        issue = find_issue(issues, title)
        trend = read_trend(issue.get("body")) if issue else None
        if issue and trend is None:
            print(f"::warning::the trend issue #{issue.get('number')} has no saved history to add to; starting afresh")
        new = add_run(trend, current, at=at, run_url=run_url, partial=partial, seconds=seconds)
        body = render_trend(new)
    except Exception as e:  # the history is a look back, never a reason to lose tonight's results
        print(f"::warning::the trend issue's body wasn't written ({type(e).__name__}: {e})")
        return issue
    out.write_text(body, encoding="utf-8")
    kept = read_trend(body)
    kept_runs = len(kept.runs) if kept else 0
    print(f"trend issue body: {len(body):,} of {BODY_LIMIT:,} characters, {kept_runs} runs")
    if kept_runs < len(new.runs):
        print(f"::warning::the trend issue keeps {kept_runs} runs of {len(new.runs)}: more would outgrow GitHub's size")
    for check, found in flipping(new).items():
        for name, said, n in found:
            print(f"flipping: {name} ({check}; {n} changes in its last {FLIP_WINDOW} runs: {said})")
    return issue


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
    ap.add_argument("--github-output", type=Path,
                    help="append issue=, issue_state= and changes= here (and trend_issue= with --trend-body-out)")
    ap.add_argument("--trend-issues", type=Path,
                    help="`gh issue list --label live-trends --json number,title,state,body` output: the history")
    ap.add_argument("--trend-title", default=TREND_TITLE, help="the title of the issue that holds the history")
    ap.add_argument("--trend-body-out", type=Path,
                    help="the trend issue's new body (none written when it can't be: the issue stays as it was)")
    args = ap.parse_args(argv)

    current: dict[str, dict[str, Result]] = {}
    seconds: dict[str, dict[str, float]] = {}
    forms: dict[str, dict[str, dict[str, Any]]] = {}
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
        seconds[check] = run_seconds(check, text)
        if check in _APPLY_CHECKS:
            forms[check] = run_forms(check, text)
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
    saved_forms = read_forms(issue.get("body")) if issue else {}
    reshaped = form_changes(previous, saved_forms, current, forms)
    slow = slower(_last_trend(args.trend_issues, args.trend_title), seconds, partial)
    bots = bots_up(previous, current)
    try:
        body = render_body(state, current, checked_at=now.strftime("%Y-%m-%d %H:%M UTC"), run_url=args.run_url,
                           partial=partial, reached=reached, forms=forms_to_save(saved_forms, state, current, forms))
    except BodyTooLong as e:  # before the comment: the next run compares with the same saved outcomes, and reports again
        print(f"::error::{e}")
        return 1
    args.body_out.write_text(body, encoding="utf-8")
    print(f"issue body: {len(body):,} of {BODY_LIMIT:,} characters")
    for check, name, date in still_back(state, reached):
        print(f"still back: {name} ({check}; last there {date})")
    # (the comment's items: the changes, and the review pages whose form changed and the employers much slower, which
    # come with no change of result; the workflow comments when there are any)
    said = len(changes) + len(reshaped) + len(slow)
    if said:
        args.comment_out.write_text(render_comment(changes, args.run_url, forms=reshaped, slow=slow, bots=bots),
                                    encoding="utf-8")
    elif args.comment_out.exists():
        args.comment_out.unlink()
    for c in changes:
        print(f"changed: {c.check} / {c.employer}: {c.before or '(new)'} -> {c.after or '(no longer checked)'}")
    for f in reshaped:
        print(f"form changed: {f.check} / {f.employer}: {f.before} -> {f.after} boxes (still {f.outcome})")
    for s in slow:
        print(f"slower: {s.check} / {s.employer}: {s.seconds} s, its usual {s.usual} s")
    if bots is not None:
        print(f"::warning::bot checks up: {bots[0]} employers stopped at a bot check or a CAPTCHA (last run {bots[1]})")
    back = went_back(changes)
    for c in back:  # (an annotation on the run, besides the issue's comment)
        print(f"::warning::{c.employer} ({c.check}) no longer reaches the review page: now {c.after}")
    if not changes:
        print("no changes" + (" (nothing saved to compare with)" if previous is None else ""))
    trend_issue = None
    if args.trend_body_out:  # (after the other issue's body and comment, which come out as they did without it)
        trend_issue = write_trend(args.trend_issues, args.trend_body_out, current,
                                  at=now.strftime("%Y-%m-%d %H:%M UTC"), run_url=args.run_url, partial=partial,
                                  seconds=seconds, title=args.trend_title)
    if args.github_output:
        number, issue_state = (issue.get("number") or "", issue.get("state") or "") if issue else ("", "")
        with args.github_output.open("a", encoding="utf-8") as f:
            f.write(f"issue={number}\nissue_state={issue_state}\nchanges={said}\nwent_back={len(back)}\n")
            if args.trend_body_out:
                f.write(f"trend_issue={(trend_issue or {}).get('number') or ''}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
