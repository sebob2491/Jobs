"""The nightly live check's results in a few lines, for a session to read instead of the issue's
whole body or a run's logs.

    uv run python scripts/nightly_summary.py                       # each check's outcomes, counted
    uv run python scripts/nightly_summary.py --check accounts      # one line per employer of a check
    uv run python scripts/nightly_summary.py --changes             # the last run's changes, sorted: better,
        # expected (a site's or the rules': a bot check, an account step practice runs never take, a site
        # error, no openings), and to look at (likely the desk's: read these first)
    uv run python scripts/nightly_summary.py --tables              # Where It Stands' employer tables (markdown)
    uv run python scripts/nightly_summary.py --handled             # data/desk_handled.yaml's employers (YAML)
    uv run python scripts/nightly_summary.py --artifacts DIR_OR_ZIP [--companies "KLA,Intel"]
        # each named employer's steps from a run's saved results (a live-* artifact, unzipped or not):
        # every round's status, reason, last log lines, questions (with their choices) and the page's
        # errors, its buttons and its unanswered boxes (live_smoke.py's "final_form")
    uv run python scripts/nightly_summary.py --import-fixtures DIR_OR_ZIP [--companies "KLA"] [--replace]
        # the pages a run stopped on, already scrubbed (its fixtures/ folders), into tests/fixtures/live:
        # tests/test_live_fixtures.py then checks every night's stops offline

The issue ("Nightly live check", #54) is read over GitHub's public API, no sign-in needed; `--issue-file`
reads a saved copy instead. A run's artifacts need a signed-in download (the GitHub tools' artifact link,
fetched with curl), then `--artifacts` reads them here.
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
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_compare  # noqa: E402  (scripts/live_compare.py, beside this one)

REPO = "sebob2491/Jobs"
ISSUE = 54
CHECKS = ("pipeline", "hr", "accounts", "search", "finance")


def fetch(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "job-apply-nightly-summary"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (a fixed https address)
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

_CHANGE = re.compile(r"^- (.+?): (?:`(.+?)`|\*new\*) \u2192 `(.+?)` \(`(.*)`\)\s*$")
# where a stop is a site's or the rules' doing, not the desk's: no fix of the desk's moves it on
_EXPECTED = re.compile(r"^(Bot check|No opening found|Site blocks automated browsers|Site error|Account needed|"
                       r"Account form \(.+\) with a CAPTCHA|Account made as you apply|Cookie banner with no decline|"
                       r"AI or privacy notice|Privacy notice to accept)")
_FURTHER = {"ready", "needs_you your_submit"}  # as far as a run goes: the person's own Submit (or Apply) is next
_BROKEN = {"failed", "crash"}


def _check_of(heading: str) -> str:
    """A changes comment's heading as its check's short name."""
    h = heading.lower()
    return ("accounts" if "test identity" in h else "hr" if "(hr jobs)" in h else "finance" if "finance" in h
            else "pipeline" if "semiconductor" in h else "search" if "search" in h else h[:20])


def triage(comment: str) -> dict[str, list[str]]:
    """The last run's changes (live_compare.py's comment) in three lists: "better" (an employer got further),
    "expected" (it stops where a site or the product's rules stop it: a bot check, an account step a practice
    run never takes, a site's error, no openings), and "look" (likely the desk's: the ones to read first).
    Each line: "<check>: <employer>: <old> -> <new>: <where it stops>"."""
    out: dict[str, list[str]] = {"look": [], "expected": [], "better": []}
    check = ""
    for line in comment.splitlines():
        if line.startswith("**"):
            check = _check_of(line.strip("*"))
            continue
        m = _CHANGE.match(line)
        if not m:
            continue
        name, old, new, detail = m.group(1), m.group(2) or "new", m.group(3), m.group(4)
        where, _ = why(new, detail)
        said = f"{check}: {name}: {old} -> {new}: {where}"
        if new in _FURTHER or (old in _BROKEN and new not in _BROKEN):
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
        for q in r.get("questions") or []:
            options = q.get("options") or []
            out.append(f"      Q: {str(q.get('label'))[:160]} ({q.get('kind')}) {options[:12]}"
                       + (f" error: {q['error']}" if q.get("error") else ""))
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


LIVE_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "live"


def import_fixtures(where: Path, wanted: list[str], into: Path = LIVE_FIXTURES, replace: bool = False) -> list[str]:
    """The scrubbed pages in a run's fixtures/ folders (a zip or a folder, its groups' too) copied into the
    tests' live fixtures: each one's files (<name>.html, its frames, <name>.expect.json). A fixture already
    there is left as it is unless `replace`. The names copied."""
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
        for fname, data in files.items():
            if fname.split(".", 1)[0] == name:
                (into / fname).write_bytes(data)
        copied.append(name)
    return copied


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--issue-file", help="a saved copy of the issue (its API JSON), instead of fetching it")
    ap.add_argument("--check", choices=CHECKS, help="one line per employer of this check")
    ap.add_argument("--changes", action="store_true", help="the last run's changes (the issue's last comment)")
    ap.add_argument("--tables", action="store_true", help="Where It Stands' employer tables, in markdown")
    ap.add_argument("--handled", action="store_true",
                    help="the employers the accounts check took all the way, for data/desk_handled.yaml")
    ap.add_argument("--artifacts", type=Path, help="a run's live-* artifact (zip or unzipped folder)")
    ap.add_argument("--companies", default="",
                    help="with --artifacts or --import-fixtures: these employers only (comma-separated)")
    ap.add_argument("--import-fixtures", type=Path, help="a run's artifact: copy its scrubbed stop pages into the tests")
    ap.add_argument("--replace", action="store_true", help="with --import-fixtures: replace fixtures already there")
    args = ap.parse_args(argv)
    if args.import_fixtures:
        wanted = [re.sub(r"[^a-z0-9]+", "-", w.strip().lower()).strip("-") for w in args.companies.split(",") if w.strip()]
        for name in import_fixtures(args.import_fixtures, wanted, replace=args.replace):
            print(name)
        return 0
    if args.artifacts:
        wanted = [w.strip().lower() for w in args.companies.split(",") if w.strip()]
        for rec in records(args.artifacts):
            if not wanted or any(w in str(rec.get("company", "")).lower() for w in wanted):
                print(steps(rec))
        return 0
    if args.changes:
        sorted_ = triage(last_comment())
        for key, title in (("look", "Look at (likely the desk's)"), ("expected", "Expected (a site's or the rules')"),
                           ("better", "Better")):
            print(f"{title}: {len(sorted_[key])}")
            for said in sorted_[key]:
                print(f"  {said}")
        return 0
    body = issue_body(args.issue_file)
    state = live_compare.read_state(body) or {}
    if args.handled:
        quoted = [json.dumps(n) if re.search(r"""[:#'"]""", n) else n for n in handled(state, details(body))]
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
