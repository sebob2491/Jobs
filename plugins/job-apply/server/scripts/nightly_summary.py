"""The nightly live check's results in a few lines, for a session to read instead of the issue's
whole body or a run's logs.

    uv run python scripts/nightly_summary.py                       # each check's outcomes, counted
    uv run python scripts/nightly_summary.py --check accounts      # one line per employer of a check
    uv run python scripts/nightly_summary.py --changes             # the last run's changes, sorted: better,
        # expected (a site's or the rules': a bot check, an account step practice runs never take, a site
        # error, no openings), and to look at (likely the desk's: read these first)
    uv run python scripts/nightly_summary.py --tables              # Where It Stands' employer tables (markdown)
    uv run python scripts/nightly_summary.py --handled             # data/desk_handled.yaml's employers (YAML),
        # less the ones that flip (the trend issue's; each left out said on stderr)
    uv run python scripts/nightly_summary.py --trend [N]           # the trend issue's last N runs (14), newest
        # first: each check's employers at the review page / checked (search: works / checked), the median seconds
        # an employer took, and the employers that flip (their result changed 3 times or more in their last 10 runs)
    uv run python scripts/nightly_summary.py --artifacts DIR_OR_ZIP [--companies "KLA,Intel"]
        # each named employer's steps from a run's saved results (a live-* artifact, unzipped or not):
        # every round's status, reason, last log lines, questions (with their choices) and the page's
        # errors, its buttons and its unanswered boxes (live_smoke.py's "final_form")
    uv run python scripts/nightly_summary.py --import-fixtures DIR_OR_ZIP [--companies "KLA"] [--replace]
        # the pages a run stopped on, already scrubbed (its fixtures/ folders), into tests/fixtures/live:
        # tests/test_live_fixtures.py then checks every night's stops offline
    uv run python scripts/nightly_summary.py --probes DIR_OR_ZIP [--companies "KLA"]
        # the weekly probes' results (a live-probes artifact: scripts/live_probes.py): the desk's failures and what
        # couldn't be checked this week (the site's or the person's), each under its own heading, then each reset
        # tenant's passes, step by step, and each tailored-resume employer's verdict; with --companies, those runs'
        # steps too

The issue ("Nightly live check", #54) is read over GitHub's public API, no sign-in needed; `--issue-file`
reads a saved copy instead. The trend issue ("Nightly live trends", label live-trends: the last 60 runs) is
read the same way, found by its label; `--trend-file` reads a saved copy (its API JSON). With `--issue-file`,
the trend comes only from `--trend-file`: nothing is fetched. A run's artifacts need a signed-in download (the
GitHub tools' artifact link, fetched with curl), then `--artifacts` reads them here.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_audit  # (scripts/live_audit.py, beside this one: the review pages' values)
import live_compare  # (scripts/live_compare.py, beside this one)
import live_probes  # (scripts/live_probes.py, beside this one: the weekly probes' results)

REPO = "sebob2491/Jobs"
HANDLED_FILE = Path(__file__).resolve().parents[2] / "data" / "desk_handled.yaml"
ISSUE = 54
TREND_LABEL = "live-trends"  # (the trend issue's own label: the issue above has live-check)
CHECKS = ("pipeline", "hr", "accounts", "search", "finance")
HANDLED_FLIPS = ("accounts", "pipeline")  # the checks whose flipping employers --handled leaves out


def fetch(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "job-apply-nightly-summary"})
    with urllib.request.urlopen(request, timeout=30) as response:  # (a fixed https address)
        return json.load(response)


def issue_body(path: str | None = None) -> str:
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data["body"] if isinstance(data, dict) else str(data)
    return fetch(f"https://api.github.com/repos/{REPO}/issues/{ISSUE}")["body"]


def last_comment() -> str:
    issue = fetch(f"https://api.github.com/repos/{REPO}/issues/{ISSUE}")
    n = issue.get("comments") or 0
    if not n:
        return ""
    page = fetch(f"https://api.github.com/repos/{REPO}/issues/{ISSUE}/comments?per_page=1&page={n}")
    return page[0]["body"] if page else ""


# --- the trend issue: the last runs, and the employers that flip ----------------------------------------------

class NoTrend(Exception):
    """The trend issue couldn't be read (its reason in its message)."""


def load_trend(path: str | None = None, *, offline: bool = False) -> live_compare.Trend:
    """The trend issue's history: from a saved copy (the issue's API JSON, or a list of issues as the API or
    `gh issue list --json` gives them), else fetched by its label (none when `offline`). NoTrend when it can't be."""
    if not path and offline:
        raise NoTrend("--issue-file without --trend-file: nothing is fetched")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8")) if path else \
            fetch(f"https://api.github.com/repos/{REPO}/issues?labels={TREND_LABEL}&state=all&per_page=10")
    except (OSError, ValueError) as e:  # (no network, an HTTP error, a file that isn't there or isn't JSON)
        raise NoTrend(f"{type(e).__name__}: {e}") from e
    if isinstance(data, list):
        issue = live_compare.find_issue(data, live_compare.TREND_TITLE)
        if issue is None:
            raise NoTrend(f"no issue titled {live_compare.TREND_TITLE!r} with the label {TREND_LABEL}")
        data = issue
    trend = live_compare.read_trend(data.get("body") if isinstance(data, dict) else str(data))
    if trend is None:
        raise NoTrend("its body holds no saved history")
    return trend


def trend_lines(trend: live_compare.Trend, runs: int = live_compare.TREND_SHOWN) -> list[str]:
    """The last `runs` runs, newest first: each check's employers at the review page out of those checked (search: works
    out of those checked) and the median seconds an employer took; then the employers that flip."""
    shown = trend.runs[-runs:][::-1] if runs > 0 else []
    checks = live_compare._ordered({c for run in shown for c in run["checks"]})
    out = [f"the last {len(shown)} run(s), newest first: each check's employers at the review page / checked (search: "
           "works / checked), and the median seconds an employer took (- a check that didn't finish or didn't run)"]
    for run in shown:
        cells = []
        for check in checks:
            one = run["checks"].get(check)
            said = f"{one['reached']}/{one['total']}" if one else "-"
            if one and one.get("median_seconds") is not None:
                said += f" {one['median_seconds']} s"
            cells.append(f"{check} {said:<13}")
        out.append(f"{run['at']}  " + " ".join(cells).rstrip())
    flip = live_compare.flipping(trend)
    if not flip:
        out.append(f"flipping: none ({live_compare.FLIP_MIN} changes or more in an employer's last "
                   f"{live_compare.FLIP_WINDOW} runs)")
    else:
        out.append(f"flipping ({live_compare.FLIP_MIN} changes or more in their last {live_compare.FLIP_WINDOW} runs; "
                   "R at the review page or search works, x not, - not checked; oldest first):")
        for check, found in flip.items():
            out += [f"  {check}: {name} {said} ({n} changes)" for name, said, n in found]
    return out


def flippers(trend: live_compare.Trend, checks: tuple[str, ...] = HANDLED_FLIPS) -> dict[str, str]:
    """The employers that flip in these checks ({employer: why}), as --handled leaves them out."""
    out: dict[str, str] = {}
    for check, found in live_compare.flipping(trend).items():
        if check in checks:
            for name, said, n in found:
                why_ = f"{n} changes in the {check} check's last {live_compare.FLIP_WINDOW} runs: {said}"
                out[name] = f"{out[name]}; {why_}" if name in out else why_
    return out


def details(body: str) -> dict[str, dict[str, str]]:
    """Each check's employers' detail column, as the issue's tables show it, keyed by check."""
    titles = {live_compare._title(check): check for check in CHECKS}
    out: dict[str, dict[str, str]] = {}
    check = None
    for line in body.splitlines():
        if line.startswith("### "):
            heading = line[4:].strip()
            check = next((c for t, c in titles.items() if heading.startswith(t[:40])), None)
            continue
        m = re.match(r"^\| (.+?) \| `(.+?)` \| `?(.*?)`? \|$", line)
        if check and m and m.group(1) != "Employer":
            out.setdefault(check, {})[m.group(1)] = m.group(3)
    return out


def counted(state: dict[str, dict[str, str]]) -> str:
    lines = []
    for check, outcomes in state.items():
        tally = Counter(outcomes.values())
        lines.append(f"{check} ({len(outcomes)}): " + ", ".join(f"{o} {n}" for o, n in sorted(tally.items(), key=lambda t: (-t[1], t[0]))))
    return "\n".join(lines)


def per_employer(check: str, state: dict[str, dict[str, str]], detail: dict[str, dict[str, str]],
                 width: int = 110) -> str:
    rows = []
    for name in sorted(state.get(check) or {}, key=str.lower):
        said = (detail.get(check) or {}).get(name, "")
        said = said.split(" · ", 1)[-1]
        rows.append(f"{name[:28]:28} {state[check][name]:24} {said[:width]}")
    return "\n".join(rows)


# --- Where It Stands' tables ---------------------------------------------------------

def why(outcome: str, said: str) -> tuple[str, str]:
    """Where it stopped and what the person does there, in the page's words, from an outcome and its detail."""
    d = said.split(" · ", 1)[-1]
    if outcome == "ready":
        return "Review page", "Check it, press Submit"
    if outcome == "no postings":
        return "No opening found", "Nothing to apply to"
    if outcome == "empty":  # (the search check's: answered, and found nothing anywhere)
        return "Search found nothing anywhere", "Being looked into"
    if "bot_check" in outcome:
        return "Bot check", "Solve it; the desk carries on"
    if "your_submit" in outcome:
        if "button that sends" in d:
            return "Filled; its send button reads \"Apply\"", "Check it, press Apply"
        return "Account made as you apply", "Choose a password, press its Apply"
    if "sign_in" in outcome or "email_code" in outcome:
        m = re.search(r"filled in (.+?)'s Create Account", d)
        if "CAPTCHA" in d:
            return f"Account form ({m.group(1) if m else 'site'}) with a CAPTCHA", "Solve the CAPTCHA, create the account"
        if m:
            return f"Account needed ({m.group(1)})", "The desk makes the account with its saved password"
        if "didn't sign in" in d:
            return "Sign-in failed after the account was made", "Being looked into"
        if "wants something" in d:
            return "Account form wants more", "Being looked into"
        system = re.search(r"\b(BrassRing|Eightfold|Amazon|Workday|SuccessFactors|Oracle)\b", d)
        return f"Account needed ({system.group(1) if system else 'sign-in'})", "Sign in or create the account"
    if "shows a notice" in d:
        return "AI or privacy notice", "Accepted for you (accept_notices); practice runs leave it"
    if "way on is" in d or "Acknowledge" in d:
        return "Privacy notice to accept", "Accepted for you (accept_notices)"
    if "cookie banner" in d:
        return "Cookie banner with no decline", "Choose in the banner (or turn on accept_cookies)"
    if "Something went wrong" in d:
        return "Site error (\"Something went wrong\")", "Being looked into"
    if "site's own trouble" in d:  # (the site's error page where the posting should be, "Content missing", an empty page)
        return "Site error (the site's own page)", "Reload or open the posting later, then Resume"
    # steps that are the person's by the desk's rules (run 51: Circle K's and Taylor Morrison's assessments, Kforce's
    # screening, the State of Arizona's name and email sent first)
    if "take its assessment" in d:
        return "An assessment to take", "Take it, then press Resume"
    if "tax credit screening" in d:
        return "A tax credit screening", "Fill it in, then press Resume"
    if "name and email first" in d:
        return "Sends your name and email first", "Press its Apply yourself"
    if "Degree list" in d:
        return "Degree list without the profile's degree", "Choose the closest honest one"
    if "turned the desk" in d or "blocked" in d:
        return "Site blocks automated browsers", "Apply in your own browser"
    if "couldn't find the button" in d:
        return "No step button found", "Being looked into"
    if "didn't take your profile's answer" in d:
        return "A box refused the profile's answer", "Being looked into"
    if "didn't move on" in d:
        return "Step not saved", "Being looked into"
    if "question" in d:
        return "Questions only you can answer", "Answer them once; remembered"
    if "no application opened" in d:
        return "Apply opened no application", "Being looked into"
    if "asks about your past jobs" in d:
        return "Past jobs' details the profile lacks", "Add them to your profile once"
    if outcome == "crash":
        return "The check itself failed", "Being looked into"
    if outcome.startswith("needs_you"):
        return "Stopped (see the run)", "Being looked into"
    return outcome, ""


def handled(state: dict[str, dict[str, str]], detail: dict[str, dict[str, str]]) -> list[str]:
    """The employers where the accounts check took the test applicant all the way: to the review page, or filled
    up to the site's own last button (data/desk_handled.yaml, the desk's "Only where the desk goes all the way").
    Not one whose account is made as the person applies: that waits on their password."""
    said = detail.get("accounts") or {}
    return sorted((name for name, outcome in (state.get("accounts") or {}).items()
                   if why(outcome, said.get(name, ""))[0] == "Review page"
                   or why(outcome, said.get(name, ""))[0].startswith("Filled;")), key=str.casefold)


def listed_handled(path: Path = HANDLED_FILE) -> list[str]:
    """data/desk_handled.yaml's employers, as written (none when it can't be read)."""
    import yaml  # (the server's own dependency: this script runs in its environment)
    try:
        names = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("employers") or []
    except (OSError, yaml.YAMLError, AttributeError):
        return []
    return [str(n) for n in names if n]


def handled_drift(now: list[str], listed: list[str], flipping: Iterable[str] = ()) -> list[str]:
    """data/desk_handled.yaml against the last run (the review of Oct 15): employers the accounts check now takes all
    the way that it doesn't list, and ones it lists that the last run didn't take there (a bot check or a site's
    error that night, or a real step back). Refreshed with --handled before a release, which leaves out the
    employers that flip (`flipping`, from the trend issue): those are said apart, and only when listed."""
    flips = {n.casefold() for n in flipping}
    went, have = {n.casefold() for n in now}, {n.casefold() for n in listed}
    new = [n for n in now if n.casefold() not in have and n.casefold() not in flips]
    not_now = [n for n in listed if n.casefold() not in went and n.casefold() not in flips]
    listed_flipping = [n for n in listed if n.casefold() in flips]
    out = []
    if new:
        out.append(f"desk_handled.yaml: {len(new)} all the way in the last run, not listed: {', '.join(new)}")
    if not_now:
        out.append(f"desk_handled.yaml: {len(not_now)} listed, not all the way in the last run: {', '.join(not_now)}")
    if listed_flipping:
        out.append(f"desk_handled.yaml: {len(listed_flipping)} listed that flip (--handled leaves them out): "
                   + ", ".join(listed_flipping))
    return out


def tables(state: dict[str, dict[str, str]], detail: dict[str, dict[str, str]]) -> str:
    """Markdown tables of where each employer stops: the chip list's (with the test account's column) and the
    Phoenix HR list's, as Where It Stands shows them."""
    accounts = state.get("accounts") or {}

    def account(name: str) -> str:
        if name not in accounts:
            return "—"
        return why(accounts[name], (detail.get("accounts") or {}).get(name, ""))[0]

    def doing(name: str, check: str) -> str:
        if accounts.get(name) == "ready":
            return "Save its password; it reaches the review page"
        if name in accounts:
            return why(accounts[name], (detail.get("accounts") or {}).get(name, ""))[1]
        return why(state[check][name], (detail.get(check) or {}).get(name, ""))[1]

    out = []
    for check, title in (("pipeline", "Chip employers (technician jobs)"), ("hr", "Phoenix-area employers (HR jobs)")):
        if not state.get(check):
            continue
        out += [f"### {title}", "", "| Employer | Practice run (no account) | With a test account | What you do |",
                "|---|---|---|---|"]
        for name in sorted(state[check], key=str.lower):
            where = why(state[check][name], (detail.get(check) or {}).get(name, ""))[0]
            out.append(f"| {name.replace('|', '/')} | {where} | {account(name)} | {doing(name, check)} |")
        out.append("")
    return "\n".join(out)


# --- the last run's changes, sorted ---------------------------------------------------------

# (an employer gone from a check, "*no longer checked*", and a change with no detail are lines too: the review of Oct 15
# found both dropped)
_CHANGE = re.compile(r"^- (.+?): (?:`(.+?)`|\*new\*) \u2192 (?:`(.+?)`|\*no longer checked\*)(?: \(`(.*)`\))?\s*$")
# where a stop is a site's or the rules' doing, not the desk's: no fix of the desk's moves it on
_EXPECTED = re.compile(r"^(Bot check|No opening found|Site blocks automated browsers|Site error|Account needed|"
                       r"Account form \(.+\) with a CAPTCHA|Account made as you apply|Cookie banner with no decline|"
                       r"AI or privacy notice|Privacy notice to accept)")
_FURTHER = {"ready", "needs_you your_submit"}  # as far as a run goes: the person's own Submit (or Apply) is next
_BROKEN = {"failed", "crash", "error", "empty"}  # (a pipeline's that broke; a search's that found nothing or failed)


def _check_of(heading: str) -> str:
    """A changes comment's heading as its check's short name."""
    h = heading.lower()
    return ("accounts" if "test identity" in h else "hr" if "(hr jobs)" in h else "finance" if "finance" in h
            else "pipeline" if "semiconductor" in h else "search" if "search" in h else h[:20])


def triage(comment: str) -> dict[str, list[str]]:
    """The last run's changes (live_compare.py's comment) in four lists: "better" (an employer got further),
    "expected" (it stops where a site or the product's rules stop it: a bot check, an account step a practice
    run never takes, a site's error, no openings), "added" (an employer new to a check, as far as the check goes:
    a list grown, a check widened, run 104's thirty-nine chip employers new to the search check), "gone" (one a check
    no longer has) and "look" (likely the desk's: the ones to read first). Each line: "<check>: <employer>: <old> ->
    <new>: <where it stops>"."""
    out: dict[str, list[str]] = {"look": [], "expected": [], "better": [], "added": [], "gone": []}
    check = ""
    for line in comment.splitlines():
        if line.startswith("**"):
            check = _check_of(line.strip("*"))
            continue
        m = _CHANGE.match(line)
        if not m:
            continue
        name, old, new, detail = m.group(1), m.group(2) or "new", m.group(3), m.group(4) or ""
        if new is None:  # (a list or a check that no longer has it)
            out["gone"].append(f"{check}: {name}: {old} -> no longer checked")
            continue
        where, _ = why(new, detail)
        said = f"{check}: {name}: {old} -> {new}: {where}"
        if old == "new" and new in (*_FURTHER, "works", "no search"):
            out["added"].append(said)
        elif new in _FURTHER or (old in _BROKEN and new not in _BROKEN):
            out["better"].append(said)
        elif _EXPECTED.match(where):
            out["expected"].append(said)
        else:
            out["look"].append(said)
    return out


# --- a run's saved results ------------------------------------------------------------------

def records(where: Path) -> Iterator[dict[str, Any]]:
    """Every employer record in a run's saved results: a live-* artifact's zip or folder (its
    pipeline.json files, shards too)."""
    # (a --parallel run's own pipeline.json holds its groups' records: then theirs aren't read again)
    if where.is_file() and where.suffix == ".zip":
        with zipfile.ZipFile(where) as z:
            names = [n for n in z.namelist() if n.endswith("pipeline.json") and "/pipeline/" not in n]
            for name in ["pipeline.json"] if "pipeline.json" in names else names:
                yield from _listed(json.loads(z.read(name)))
        return
    paths = [where / "pipeline.json"] if (where / "pipeline.json").is_file() else \
        [p for p in sorted(where.rglob("pipeline.json")) if p.parent.name != "pipeline"]
    for path in paths:
        yield from _listed(json.loads(path.read_text(encoding="utf-8")))


def _listed(data: Any) -> list[dict[str, Any]]:
    return [r for r in (data if isinstance(data, list) else data.get("records", [])) if isinstance(r, dict)]


def steps(rec: dict[str, Any], log_lines: int = 6) -> str:
    """One employer's run: each round, and what the page showed when it ended."""
    out = [f"== {rec.get('company')}: {(rec.get('posting') or {}).get('title', '')}"]
    if rec.get("crash"):
        out.append(f"  crash: {rec['crash']}")
    for i, r in enumerate(rec.get("rounds") or []):
        out.append(f"  [{i}] {r.get('status')} {r.get('need') or ''}: {str(r.get('reason') or '')[:400]}")
        out += [f"      log: {line[:200]}" for line in (r.get("log") or [])[-log_lines:]]
        above = r.get("above") or []
        for i, q in enumerate(r.get("questions") or []):
            options = q.get("options") or []
            out.append(f"      Q: {str(q.get('label'))[:160]} ({q.get('kind')}) {options[:12]}"
                       + (f" error: {q['error']}" if q.get("error") else ""))
            if i < len(above) and above[i]:
                out.append(f"         above it: {above[i][-300:]}")
        for probe in r.get("pick_lists") or []:  # (live_smoke.pick_list_probe)
            out.append(f"      pick list: {json.dumps(probe)[:2500]}")
    final = rec.get("final_form") or {}
    if final:
        out.append(f"  page: {final.get('url', '')[:150]}")
        if final.get("errors"):
            out.append(f"  errors: {final['errors'][:6]}")
        if final.get("actions"):
            out.append(f"  buttons: {final['actions'][:40]}")
        for f in final.get("fields") or []:
            out.append(f"  box: {f}")
    for probe in rec.get("lists") or []:
        out.append(f"  list probe: {json.dumps(probe)[:1500]}")
    return "\n".join(out)


_PROBE_RUN = re.compile(r"(?:^|/)((?:reset|tailored)-[^/]+)/pipeline\.json$")


def probe_records(where: Path, wanted: list[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """The weekly probes' runs in a live-probes artifact (a zip or its folder): (the run's folder, e.g. reset-kla-1,
    its employer's record), for the employers named in `wanted` (any part of a name)."""
    found: list[tuple[str, Any]] = []
    if where.is_file() and where.suffix == ".zip":
        with zipfile.ZipFile(where) as z:
            found = [(m.group(1), json.loads(z.read(n))) for n in sorted(z.namelist()) if (m := _PROBE_RUN.search(n))]
    elif where.is_dir():
        found = [(p.parent.name, json.loads(p.read_text(encoding="utf-8"))) for p in sorted(where.rglob("pipeline.json"))
                 if _PROBE_RUN.search(p.relative_to(where).as_posix())]
    for run, data in found:
        for rec in _listed(data):
            if any(w in str(rec.get("company", "")).lower() for w in wanted):
                yield run, rec


LIVE_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "live"


def import_fixtures(where: Path, wanted: list[str], into: Path = LIVE_FIXTURES, replace: bool = False) -> list[str]:
    """The scrubbed pages in a run's fixtures/ folders (a zip or a folder, its groups' too) copied into the
    tests' live fixtures: each one's files (<name>.html, its frames, <name>.expect.json, and <name>.decision.json:
    what the desk decided there, which test_live_fixtures.py decides again). A fixture already there is left as it is
    unless `replace`: then its files are the run's alone (none left of the page it had, a frame or a decision the
    new capture hasn't), which is how a change to what the desk decides on a page is accepted. The names copied."""
    files: dict[str, bytes] = {}
    if where.is_file() and where.suffix == ".zip":
        with zipfile.ZipFile(where) as z:
            for n in z.namelist():
                if "/fixtures/" in f"/{n}" and not n.endswith("/"):
                    files.setdefault(Path(n).name, z.read(n))
    else:
        for path in sorted(where.rglob("fixtures/*")):
            if path.is_file():
                files.setdefault(path.name, path.read_bytes())
    names = sorted({n.split(".", 1)[0] for n in files if n.endswith(".expect.json")})
    names = [n for n in names if not wanted or any(w in n for w in wanted)]
    copied = []
    into.mkdir(parents=True, exist_ok=True)
    for name in names:
        if (into / f"{name}.expect.json").exists() and not replace:
            continue
        for old in into.glob(f"{name}.*"):
            old.unlink()
        for fname, data in files.items():
            if fname.split(".", 1)[0] == name:
                (into / fname).write_bytes(data)
        copied.append(name)
    return copied


SLOWER = 2.0  # a job system's middle time this many times an earlier run's is said (the review of Oct 15)
SLOWER_FLOOR = 30  # seconds: a system that took less than this in the middle isn't said, however much slower


def timing(recs: list[dict[str, Any]], slowest: int = 5, earlier: list[dict[str, Any]] | None = None) -> list[str]:
    """How long each job system's employers took in a run (the seconds each record keeps): the middle and the 90th
    percentile, and the rounds, per system; then the slowest employers. The review of Oct 15: the time was recorded and
    never summarised, so a site growing slower showed nowhere. With `earlier` (a run of the week before, say), a system
    whose middle is SLOWER times that run's, and SLOWER_FLOOR seconds or more, says so."""
    from job_apply.ats import detect_ats

    def system(r: dict[str, Any]) -> str:
        rounds = r.get("rounds") or []
        url = (rounds[-1].get("url") if rounds else None) or (r.get("posting") or {}).get("url")
        return detect_ats(url) if url else "unknown"

    def pct(values: list[float], q: float) -> float:
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]

    def middles(of: list[dict[str, Any]]) -> dict[str, float]:
        grouped: dict[str, list[float]] = {}
        for r in of:
            if isinstance(r.get("seconds"), (int, float)):
                grouped.setdefault(system(r), []).append(float(r["seconds"]))
        return {name: pct(secs, 0.5) for name, secs in grouped.items()}

    timed = [r for r in recs if isinstance(r.get("seconds"), (int, float))]
    by: dict[str, list[dict[str, Any]]] = {}
    for r in timed:
        by.setdefault(system(r), []).append(r)
    before = middles(earlier) if earlier is not None else {}
    out = []
    for name, group in sorted(by.items(), key=lambda kv: -pct([r["seconds"] for r in kv[1]], 0.5)):
        secs = [float(r["seconds"]) for r in group]
        rounds = [len(r.get("rounds") or []) for r in group]
        middle, was = pct(secs, 0.5), before.get(name)
        slower = (f" (was {was:.0f} s: {middle / was:.1f}x slower)"
                  if was and middle >= SLOWER_FLOOR and middle >= SLOWER * was else "")
        out.append(f"{name}: {len(group)} employer(s), {middle:.0f} s middle, {pct(secs, 0.9):.0f} s at 90%, "
                   f"{pct([float(x) for x in rounds], 0.5):.0f} round(s) middle{slower}")
    if timed:
        out.append("slowest: " + ", ".join(f"{r.get('company')} ({float(r['seconds']):.0f} s)"
                                            for r in sorted(timed, key=lambda r: -float(r["seconds"]))[:slowest]))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--issue-file", help="a saved copy of the issue (its API JSON), instead of fetching it")
    ap.add_argument("--check", choices=CHECKS, help="one line per employer of this check")
    ap.add_argument("--changes", action="store_true", help="the last run's changes (the issue's last comment)")
    ap.add_argument("--tables", action="store_true", help="Where It Stands' employer tables, in markdown")
    ap.add_argument("--handled", action="store_true",
                    help="the employers the accounts check took all the way, for data/desk_handled.yaml (less the ones "
                    "that flip in the accounts or pipeline check: each said on stderr)")
    ap.add_argument("--trend", type=int, nargs="?", const=live_compare.TREND_SHOWN, metavar="N",
                    help=f"the last N runs (default {live_compare.TREND_SHOWN}), newest first, from the trend issue; "
                    "and the employers that flip")
    ap.add_argument("--trend-file", help="with --trend or --handled: a saved copy of the trend issue (its API JSON), "
                    "instead of fetching it")
    ap.add_argument("--artifacts", type=Path, help="a run's live-* artifact (zip or unzipped folder)")
    ap.add_argument("--companies", default="",
                    help="with --artifacts or --import-fixtures: these employers only (comma-separated)")
    ap.add_argument("--import-fixtures", type=Path, help="a run's artifact: copy its scrubbed stop pages into the tests")
    ap.add_argument("--replace", action="store_true", help="with --import-fixtures: replace fixtures already there")
    ap.add_argument("--timing", type=Path, help="a run's artifact: the time each job system's employers took")
    ap.add_argument("--against", type=Path, help="with --timing: an earlier run's artifact (a week before: artifacts "
                    "are kept 7 days); a job system twice as slow as there says so")
    ap.add_argument("--probes", type=Path, help="a run's live-probes artifact (zip or folder): the weekly probes' "
                    "results; with --companies, those employers' runs step by step too")
    args = ap.parse_args(argv)
    if args.probes:
        try:
            results = live_probes.read_results(args.probes)
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            print(f"the probes' results can't be read: {e}", file=sys.stderr)
            return 1
        for line in live_probes.summary_lines(results):
            print(line)
        wanted = [w.strip().lower() for w in args.companies.split(",") if w.strip()]
        for run, rec in probe_records(args.probes, wanted) if wanted else ():
            print(f"-- {run}")
            print(steps(rec))
        return 0
    if args.trend is not None:
        try:
            trend = load_trend(args.trend_file, offline=bool(args.issue_file))
        except NoTrend as e:
            print(f"the trend issue can't be read: {e}", file=sys.stderr)
            return 1
        for line in trend_lines(trend, args.trend):
            print(line)
        return 0
    if args.timing:
        for line in timing(list(records(args.timing)), earlier=list(records(args.against)) if args.against else None):
            print(line)
        return 0
    if args.import_fixtures:
        wanted = [re.sub(r"[^a-z0-9]+", "-", w.strip().lower()).strip("-") for w in args.companies.split(",") if w.strip()]
        for name in import_fixtures(args.import_fixtures, wanted, replace=args.replace):
            print(name)
        return 0
    if args.artifacts:
        wanted = [w.strip().lower() for w in args.companies.split(",") if w.strip()]
        chosen = {str(r.get("company")): r for r in records(args.artifacts)
                  if not wanted or any(w in str(r.get("company", "")).lower() for w in wanted)}
        for rec in chosen.values():
            print(steps(rec))
        if args.artifacts.is_dir():  # (the review pages' values against the profile's: live_audit.py)
            for line in live_audit.values_findings("values", chosen, args.artifacts):
                print(line)
        return 0
    if args.changes:
        sorted_ = triage(last_comment())
        for key, title in (("look", "Look at (likely the desk's)"), ("expected", "Expected (a site's or the rules')"),
                           ("better", "Better"), ("added", "Added (new to a check, as far as it goes)"),
                           ("gone", "Gone (no longer checked)")):
            print(f"{title}: {len(sorted_[key])}")
            for said in sorted_[key]:
                print(f"  {said}")
        return 0
    body = issue_body(args.issue_file)
    state = live_compare.read_state(body) or {}
    if args.handled:
        names = handled(state, details(body))
        try:  # (an employer that flips gets there some nights only: the desk can't count on it)
            flip = flippers(load_trend(args.trend_file, offline=bool(args.issue_file)))
        except NoTrend as e:
            print(f"warning: the trend issue isn't available ({e}): the employers that flip aren't left out",
                  file=sys.stderr)
            flip = {}
        for name in names:
            if name in flip:
                print(f"left out: {name} (flips: {flip[name]})", file=sys.stderr)
        quoted = [json.dumps(n) for n in names if n not in flip]  # (any name, quoted: YAML reads "No" as false)
        print("employers:\n" + "\n".join(f"  - {n}" for n in quoted))
    elif args.tables:
        print(tables(state, details(body)))
    elif args.check:
        print(per_employer(args.check, state, details(body)))
    else:
        m = re.search(r"Last checked: (.+?) · \[run\]\((.+?)\)", body)
        if m:
            print(f"last run: {m.group(1)}  {m.group(2)}")
        print(counted(state))
        for check, name, date in live_compare.still_back(state, live_compare.read_reached(body)):
            print(f"still back: {name} ({check}; last there {date})")
        if state.get("accounts"):
            try:  # (as --handled leaves them out; without the trend issue, as before)
                flip = flippers(load_trend(args.trend_file, offline=bool(args.issue_file)))
            except NoTrend:
                flip = {}
            for line in handled_drift(handled(state, details(body)), listed_handled(), flip):
                print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
