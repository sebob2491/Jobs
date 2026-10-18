"""The nightly live check's results in a few lines (scripts/nightly_summary.py): read from the issue's body
as live_compare.py writes it, and from a run's saved results. No network: the body is made here."""

import importlib.util
import json
import sys
import urllib.error
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
_spec = importlib.util.spec_from_file_location("nightly_summary", SCRIPTS / "nightly_summary.py")
ns = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ns)
lc = ns.live_compare

WORKDAY = ("Field Service Engineer · I filled in Workday's Create Account form with your details and saved password. "
           "Fill in anything it still asks for")
READY = "Technician · Filled and waiting on the review page. Check it in the browser, then press Submit."
BOT = "Recruiter · The site is checking that you're a person (a bot check or CAPTCHA). Solve it in the browser window"


def body():
    state = {"pipeline": {"KLA": "needs_you sign_in", "Example | Fab": "ready"},
             "hr": {"Axon": "ready", "Aerotek": "needs_you bot_check"},
             "accounts": {"KLA": "ready"}}
    current = {"pipeline": {"KLA": lc.Result("needs_you sign_in", WORKDAY), "Example | Fab": lc.Result("ready", READY)},
               "hr": {"Axon": lc.Result("ready", READY), "Aerotek": lc.Result("needs_you bot_check", BOT)},
               "accounts": {"KLA": lc.Result("ready", READY)}}
    return lc.render_body(state, current, checked_at="2026-10-11 19:06 UTC", run_url="https://example.com/run/1")


def test_each_checks_outcomes_are_counted_and_listed_one_line_an_employer():
    text = body()
    state = lc.read_state(text)
    assert sorted(ns.counted(state).splitlines()) == ["accounts (1): ready 1", "hr (2): needs_you bot_check 1, ready 1",
                                                      "pipeline (2): needs_you sign_in 1, ready 1"]
    detail = ns.details(text)
    assert set(detail) == {"pipeline", "hr", "accounts"}
    assert detail["pipeline"]["KLA"].startswith("Field Service Engineer · I filled in Workday")
    lines = ns.per_employer("pipeline", state, detail).splitlines()
    assert lines[0].startswith("Example | Fab") and "ready" in lines[0]
    assert "needs_you sign_in" in lines[1] and "I filled in Workday's Create Account" in lines[1]


def test_where_it_stands_tables_say_where_each_employer_stops_and_what_the_person_does():
    text = body()
    md = ns.tables(lc.read_state(text), ns.details(text))
    assert "### Chip employers (technician jobs)" in md and "### Phoenix-area employers (HR jobs)" in md
    assert "| KLA | Account needed (Workday) | Review page | Save its password; it reaches the review page |" in md
    assert "| Example / Fab | Review page | — | Check it, press Submit |" in md  # (a "|" in a name never splits a row)
    assert "| Aerotek | Bot check | — | Solve it; the desk carries on |" in md
    # steps that are the person's by the desk's rules are said as such, not "Stopped (see the run)" (run 51)
    for said, where in (("Workday wants you to take its assessment before the application goes on", "An assessment to take"),
                        ("Oracle Taleo wants its tax credit screening filled in before the application goes on",
                         "A tax credit screening"),
                        ("State of Arizona's site asks for your name and email first", "Sends your name and email first")):
        assert ns.why("needs_you stuck", f"HR Generalist · {said}")[0] == where


def test_a_runs_saved_results_say_each_employers_rounds_and_where_it_stopped(tmp_path):
    rec = {"company": "Onto Innovation", "posting": {"title": "Field Service Engineer"},
           "rounds": [{"status": "needs_you", "need": "stuck", "reason": "The page didn't move on",
                       "log": ["opened", "clicked “Next”"],
                       "questions": [{"label": "Degree*", "kind": "listbox", "options": ["BA", "BS"], "error": None}]}],
           "final_form": {"url": "https://example.com/apply", "errors": ["Email needs fixing"],
                          "actions": ["Next", "Back"], "fields": [{"label": "Email*", "kind": "text"}]}}
    (tmp_path / "shard-0").mkdir()
    (tmp_path / "shard-0" / "pipeline.json").write_text(json.dumps([rec]), encoding="utf-8")
    (tmp_path / "pipeline.json").write_text(json.dumps([{**rec, "company": "KLA"}]), encoding="utf-8")
    # a --parallel run's own pipeline.json holds every group's records: its groups' aren't read again
    assert [r["company"] for r in ns.records(tmp_path)] == ["KLA"]
    (tmp_path / "pipeline.json").unlink()
    assert [r["company"] for r in ns.records(tmp_path)] == ["Onto Innovation"]
    zipped = tmp_path / "run.zip"
    with zipfile.ZipFile(zipped, "w") as z:
        z.write(tmp_path / "shard-0" / "pipeline.json", "shard-0/pipeline.json")
    assert [r["company"] for r in ns.records(zipped)] == ["Onto Innovation"]
    said = ns.steps(rec)
    assert "== Onto Innovation: Field Service Engineer" in said
    assert "Q: Degree* (listbox) ['BA', 'BS']" in said and "errors: ['Email needs fixing']" in said
    assert "buttons: ['Next', 'Back']" in said and "box: {'label': 'Email*', 'kind': 'text'}" in said
    # and the words above a question, where the run recorded them (Taylor Morrison's "Acknowledgment*", run 71)
    rec["rounds"][0]["questions"].append({"label": "Acknowledgment*", "kind": "checkbox", "options": None})
    rec["rounds"][0]["above"] = ["Highest degree", "Terms and Conditions I certify that it's all true."]
    said = ns.steps(rec).splitlines()
    at = said.index("      Q: Acknowledgment* (checkbox) []")
    assert said[at + 1] == "         above it: Terms and Conditions I certify that it's all true.", said
    assert said[at - 1] == "         above it: Highest degree", said
    rec["rounds"][0]["above"] = []  # (a run from before it recorded them)
    assert "above it" not in ns.steps(rec)


COMMENT = """6 changes since the last nightly live check ([run](https://example.com/run/2)):

**Apply pipeline, Phoenix list (HR jobs): where it ended on one posting per employer**

- BDO: `needs_you questions` → `needs_you stuck` (`Senior Associate · Oracle Recruiting Cloud says “Something went wrong. Try again later.” Leave this job for a…`)
- Axon: `needs_you sign_in` → `ready` (`Recruiter · Filled and waiting on the review page. Check it in the browser, then press Submit.`)

**Apply pipeline with the test identity (accounts made and signed in): where it ended**

- Intel: `needs_you stuck` → `needs_you sign_in` (`Engineer · Your saved password didn't sign in on Workday, and I opened its password reset, which isn't…`)
- Edward Jones: `needs_you sign_in` → `needs_you stuck` (`Designer · Edward Jones's site turned the desk's browser away (403 Forbidden). Open the posting in you…`)
- Micron: *new* → `needs_you bot_check` (`Technician · The site is checking that you're a person (a bot check or CAPTCHA). Solve it…`)
- KLA: `crash` → `needs_you questions` (`Technician · 1 question(s) your profile doesn't answer. Answer them here and the desk fills them in`)
"""


def test_the_last_runs_changes_are_sorted_into_the_desks_the_sites_and_the_better():
    """A session reads "look" first: the changes likely the desk's. A bot check, a site's error or a site that
    turns automated browsers away is a site's doing; an employer that got further, or out of a crash, is better."""
    out = ns.triage(COMMENT)
    assert out["look"] == ["accounts: Intel: needs_you stuck -> needs_you sign_in: Sign-in failed after the account was made"]
    assert sorted(out["expected"]) == [
        "accounts: Edward Jones: needs_you sign_in -> needs_you stuck: Site blocks automated browsers",
        "accounts: Micron: new -> needs_you bot_check: Bot check",
        'hr: BDO: needs_you questions -> needs_you stuck: Site error ("Something went wrong")']
    assert sorted(out["better"]) == ["accounts: KLA: crash -> needs_you questions: Questions only you can answer",
                                     "hr: Axon: needs_you sign_in -> ready: Review page"]


# A plausible need for each stop code's outcome (the bucket comes from the code, whatever the need)
_NEED_OF = {"bot_check": "bot_check", "captcha": "captcha", "questions": "questions", "tailor": "tailor",
            "maybe_duplicate": "maybe_duplicate", "your_submit": "your_submit", "account_as_you_apply": "your_submit",
            "email_code": "email_code", "verify_pending": "email_code", "submit_failed": "submit_failed",
            "check_submit": "check_submit", "restarted": "restarted"}


def _coded_comment(reason) -> str:
    """A changes comment with one employer stopped at each stop code, as live_compare writes it from a run's rounds
    (the detail: the posting's title, then "[code] reason")."""
    from job_apply.pipeline import STOP_CODES

    changes = []
    for i, code in enumerate(sorted(STOP_CODES)):
        need = _NEED_OF.get(code, "sign_in" if code.startswith(("account", "sign_in", "reset", "security"))
                            else "stuck")
        rec = {"posting": {"title": "Technician"},
               "rounds": [{"status": "needs_you", "need": need, "code": code, "reason": reason(code)}]}
        result = lc.pipeline_outcome(rec)
        changes.append(lc.Change("accounts", f"Employer {i} {code}", "ready", result.outcome,
                                 result.detail))
    return lc.render_comment(changes)


def _buckets(triaged: dict) -> dict:
    return {bucket: sorted(line.split(": ")[1] for line in lines) for bucket, lines in triaged.items()}


def test_why_has_a_place_for_every_stop_code():
    """T1 (the review of Oct 18): every stop code the desk gives has its own place in why(), and a reason's words never
    outweigh its code (an old comment, without codes, is still read by its words)."""
    from job_apply.pipeline import STOP_CODES

    assert set(ns.CODE_WHY) == STOP_CODES
    for code in STOP_CODES:
        where, what = ns.why("needs_you stuck", f"Technician · [{code}] x")
        assert where and what and where != "Stopped (see the run)", code
    assert ns.why("needs_you stuck", "Technician · [step_not_saved] Something went wrong")[0] == "Step not saved"
    assert ns.why("needs_you stuck", "[bot_check] Your page didn't move on")[0] == "Bot check"  # (no title)
    assert ns.why("needs_you stuck", "Technician · The page didn't move on.")[0] == "Step not saved"  # (no code)
    filled = "Engineer · [account_form_filled] I filled in Workday's Create Account form"
    assert ns.why("needs_you sign_in", filled)[0] == "Account needed (Workday)"
    assert ns.stop_code("Technician · [blank_page] The page stayed blank.") == ("blank_page", "The page stayed blank.")
    assert ns.stop_code("Technician · The page stayed blank.") == ("", "The page stayed blank.")


def test_a_coded_changes_comment_sorts_the_same_whatever_its_reasons_say():
    """T1: "--changes output doesn't change when a message is reworded": the same comment with every reason replaced
    by "x" sorts each employer into the same list, at each stop code."""
    said = {"step_not_saved": "The page didn't move on.", "site_error": "Something went wrong on the site.",
            "bot_check": "The site is checking that you're a person.",
            "account_form_filled": "I filled in Workday's Create Account form with your details and saved password."}
    worded = ns.triage(_coded_comment(lambda code: said.get(code, f"The desk stopped: {code.replace('_', ' ')}.")))
    bare = ns.triage(_coded_comment(lambda code: "x"))
    assert _buckets(worded) == _buckets(bare)
    from job_apply.pipeline import STOP_CODES

    assert sum(len(v) for v in bare.values()) == len(STOP_CODES)
    look = set(_buckets(bare)["look"])
    assert {f"Employer {i} {code}" for i, code in enumerate(sorted(STOP_CODES))
            if code in ("step_not_saved", "no_step_button", "box_refused_answer")} <= look
    assert not any(name.endswith((" bot_check", " site_blocks", " cookie_banner", " captcha")) for name in look)


def test_a_runs_scrubbed_stop_pages_are_imported_into_the_tests(tmp_path):
    """live_smoke.py --fixtures leaves each page a run stopped on, scrubbed, in its folder's fixtures/: imported
    into tests/fixtures/live, test_live_fixtures.py checks every night's stops offline. One already there stays
    unless replaced."""
    run_dir = tmp_path / "live-accounts-0" / "shard-1" / "fixtures"
    run_dir.mkdir(parents=True)
    for name, body in (("pipeline-kla.html", "<p>new</p>"), ("pipeline-kla.frame-1.html", "<p>frame</p>"),
                       ("pipeline-kla.expect.json", '{"fields": []}'),
                       ("pipeline-kla.decision.json", '{"page": "form"}'),
                       ("pipeline-onsemi.html", "<p>o</p>"), ("pipeline-onsemi.expect.json", '{"fields": []}'),
                       ("pipeline-onsemi.decision.json", '{"page": "sign_in"}')):
        (run_dir / name).write_text(body, encoding="utf-8")
    into = tmp_path / "live"
    into.mkdir()
    for name, body in (("pipeline-onsemi.expect.json", '{"old": true}'),
                       ("pipeline-onsemi.decision.json", '{"old": 1}'),
                       ("pipeline-onsemi.frame-9.html", "<p>an old frame</p>"), ("pipeline-onsemi-2.html", "<p>2</p>")):
        (into / name).write_text(body, encoding="utf-8")
    assert ns.import_fixtures(tmp_path, ["kla"], into) == ["pipeline-kla"]
    assert sorted(p.name for p in into.iterdir()) == [
        "pipeline-kla.decision.json", "pipeline-kla.expect.json", "pipeline-kla.frame-1.html", "pipeline-kla.html",
        "pipeline-onsemi-2.html", "pipeline-onsemi.decision.json", "pipeline-onsemi.expect.json",
        "pipeline-onsemi.frame-9.html"]
    assert ns.import_fixtures(tmp_path, [], into) == []  # (onsemi's is there already)
    assert (into / "pipeline-onsemi.decision.json").read_text(encoding="utf-8") == '{"old": 1}'
    # replaced: the run's files alone (what the desk decides there now accepted), never another page's
    assert ns.import_fixtures(tmp_path, [], into, replace=True) == ["pipeline-kla", "pipeline-onsemi"]
    assert (into / "pipeline-onsemi.expect.json").read_text(encoding="utf-8") == '{"fields": []}'
    assert (into / "pipeline-onsemi.decision.json").read_text(encoding="utf-8") == '{"page": "sign_in"}'
    assert sorted(p.name for p in into.glob("pipeline-onsemi*")) == [
        "pipeline-onsemi-2.html", "pipeline-onsemi.decision.json", "pipeline-onsemi.expect.json",
        "pipeline-onsemi.html"]


def test_the_employers_the_desk_goes_all_the_way_at_are_the_accounts_checks_finished_ones():
    """data/desk_handled.yaml (the desk's "Only where the desk goes all the way", Oct 12): the accounts check's
    employers at their review page, or filled up to the site's own last button; not one whose account waits on
    the person's password as they apply, nor one stopped anywhere else."""
    state = {"accounts": {"Intel": "ready", "Arizona Public Service": "needs_you your_submit",
                          "Qorvo": "needs_you your_submit", "Aerotek": "needs_you bot_check"}}
    detail = {"accounts": {"Arizona Public Service": "Analyst · Filled. Its button that sends the application reads "
                                                     "“Apply”: check it, then press it.",
                           "Qorvo": "Technician · Choose a password for its account, then press its Apply."}}
    assert ns.handled(state, detail) == ["Arizona Public Service", "Intel"]


def test_the_counts_name_the_employers_still_back(tmp_path, capsys):
    """An employer the accounts check took to its review page in an earlier run but not in the last is said under
    the counts, with the date it last got there (the issue's body carries it; a quiet night has no comment)."""
    state = {"accounts": {"KLA": "needs_you stuck", "onsemi": "ready"}}
    current = {"accounts": {"KLA": lc.Result("needs_you stuck", "Technician · Workday didn't move on."),
                            "onsemi": lc.Result("ready", "Technician")}}
    text = lc.render_body(state, current, checked_at="2026-10-16 10:47 UTC",
                          reached={"accounts": {"KLA": "2026-10-14", "onsemi": "2026-10-16"}})
    path = tmp_path / "issue.json"
    path.write_text(json.dumps({"body": text}), encoding="utf-8")
    assert ns.main(["--issue-file", str(path)]) == 0
    out = capsys.readouterr().out
    assert "accounts (2): needs_you stuck 1, ready 1" in out
    assert "still back: KLA (accounts; last there 2026-10-14)" in out


def test_an_employer_new_to_a_check_that_goes_as_far_as_it_can_is_listed_as_added():
    """Run 104's thirty-nine chip employers, new to the search check, were filed under "look at" (nothing says
    where a working search stops): an employer new to a check, as far as the check goes, is "added", apart."""
    comment = """3 changes since the last nightly live check:

**Search: does each employer's search work**

- Matheson: *new* \u2192 `works` (`12 in AZ`)
- TSMC Arizona: *new* \u2192 `no search` (`careers page opened`)
- Odd Co: *new* \u2192 `error` (`SearchError: no results block`)
"""
    out = ns.triage(comment)
    assert out["added"] == ["search: Matheson: new -> works: works", "search: TSMC Arizona: new -> no search: no search"]
    assert out["look"] == ["search: Odd Co: new -> error: error"] and out["better"] == [] and out["expected"] == []


def test_a_search_that_went_empty_is_looked_at_and_one_that_came_back_is_better():
    comment = ("**Search: does each employer's search work**\n\n"
               "- USAA: `works` \u2192 `empty` (`none in AZ, none anywhere`)\n"
               "- KPMG: `empty` \u2192 `works` (`none in AZ, 3 anywhere`)\n"
               "- Wells Fargo: `error` \u2192 `works` (`5 in AZ`)\n")
    out = ns.triage(comment)
    assert out["look"] == ["search: USAA: works -> empty: Search found nothing anywhere"]
    assert sorted(out["better"]) == ["search: KPMG: empty -> works: works", "search: Wells Fargo: error -> works: works"]


def test_the_handled_list_is_compared_with_the_last_run(tmp_path):
    """The review of Oct 15: data/desk_handled.yaml drifts from the nightly between releases unnoticed. The summary
    names the employers the last run took all the way that it doesn't list, and the ones it lists that the run
    didn't take there, whatever their case."""
    assert ns.handled_drift(["Air Liquide", "Intel", "KLA"], ["air liquide", "KLA", "Micron"]) == [
        "desk_handled.yaml: 1 all the way in the last run, not listed: Intel",
        "desk_handled.yaml: 1 listed, not all the way in the last run: Micron"]
    assert ns.handled_drift(["KLA"], ["KLA"]) == []
    listed = tmp_path / "desk_handled.yaml"
    listed.write_text('as_of: "run 106"\nemployers:\n  - "No"\n  - "Air Liquide"\n', encoding="utf-8")
    assert ns.listed_handled(listed) == ["No", "Air Liquide"]  # (quoted: "No" a name, not false)
    assert ns.listed_handled(tmp_path / "missing.yaml") == []
    assert ns.listed_handled() and "Air Liquide" in ns.listed_handled()  # (the repository's own)


def test_an_employer_gone_from_a_check_and_a_change_with_no_detail_are_read_too():
    """The review of Oct 15: --changes read only lines with a detail, so an employer a check no longer has ("*no longer
    checked*") and a change written without one vanished from the list a session reads."""
    comment = ("**The test identity's accounts (semiconductor and Phoenix lists)**\n\n"
               "- Intel: `ready` \u2192 *no longer checked*\n"
               "- KLA: `needs_you stuck` \u2192 `ready`\n")
    out = ns.triage(comment)
    assert out["gone"] == ["accounts: Intel: ready -> no longer checked"]
    assert out["better"] == ["accounts: KLA: needs_you stuck -> ready: Review page"]


def test_a_runs_time_is_summed_up_per_job_system():
    """The review of Oct 15: each employer's seconds were recorded and never summarised. --timing gives each job
    system's middle and 90th-percentile time and rounds, and the slowest employers."""
    recs = [{"company": f"W{i}", "seconds": s, "rounds": [{"url": "https://x.wd1.myworkdayjobs.com/en-US/E/job/1"}] * 3}
            for i, s in enumerate((60, 80, 100, 400))]
    recs += [{"company": "G", "seconds": 20, "rounds": [{"url": "https://job-boards.greenhouse.io/x/jobs/1"}]},
             {"company": "untimed"}]
    out = ns.timing(recs, slowest=2)
    assert out[0] == "workday: 4 employer(s), 100 s middle, 400 s at 90%, 3 round(s) middle", out
    assert out[1].startswith("greenhouse: 1 employer(s), 20 s middle")
    assert out[-1] == "slowest: W3 (400 s), W2 (100 s)"


def test_a_job_system_twice_as_slow_as_an_earlier_run_says_so():
    """"2x slower than last week" (the review of Oct 15): --timing with --against an earlier run's artifact names a job
    system whose middle time doubled, not one that was quick either way."""
    def rec(company, seconds, url):
        return {"company": company, "seconds": seconds, "rounds": [{"url": url}]}
    wd, gh = "https://x.wd1.myworkdayjobs.com/en-US/E/job/1", "https://job-boards.greenhouse.io/x/jobs/1"
    earlier = [rec("W1", 50, wd), rec("W2", 60, wd), rec("W3", 70, wd), rec("G", 5, gh)]
    now = [rec("W1", 120, wd), rec("W2", 130, wd), rec("W3", 140, wd), rec("G", 20, gh), rec("O", 300, "https://o.example/x")]
    out = ns.timing(now, earlier=earlier)
    assert out[1] == "workday: 3 employer(s), 130 s middle, 140 s at 90%, 1 round(s) middle (was 60 s: 2.2x slower)", out
    assert out[2].startswith("greenhouse: 1 employer(s), 20 s middle") and "slower" not in out[2]  # (under 30 s)
    assert "slower" not in out[0]  # (a system the earlier run didn't have)
    assert all("slower" not in line for line in ns.timing(now))


def test_a_rounds_pick_list_probe_is_printed_with_it():
    """The live check's look at a pick-only list that showed no choices (live_smoke.pick_list_probe, Edwards Vacuum's
    Protected Veteran in run 113) shows under its round."""
    rec = {"company": "Edwards Vacuum", "posting": {"title": "Field Service Engineer"}, "rounds": [
        {"status": "needs_you", "need": "questions", "reason": "1 question", "log": [],
         "questions": [{"label": "Protected Veteran", "kind": "combobox", "options": [],
                        "error": "ValueError: its list showed no choices"}],
         "pick_lists": [{"label": "Protected Veteran", "before": {"box": None}, "after_10s": {"roles": {"option": 5}}}]}]}
    said = ns.steps(rec).splitlines()
    at = next(i for i, line in enumerate(said) if line.startswith("      Q: Protected Veteran"))
    assert said[at + 1].startswith('      pick list: {"label": "Protected Veteran", "before"'), said


# --- the trend issue: the last runs, and the employers that flip ---------------------------------------------------

def _trend(runs):
    """The trend issue's history from runs of {check: {employer: outcome}}: a run a day from Oct 1, 95 s an employer."""
    trend = None
    for day, run in enumerate(runs, 1):
        current = {check: {name: lc.Result(o) for name, o in found.items()} for check, found in run.items()}
        seconds = {check: {name: 95.0 for name in found} for check, found in run.items()}
        trend = lc.add_run(trend, current, at=f"2026-10-{day:02d} 10:17 UTC", run_url=f"https://example.com/run/{day}",
                           seconds=seconds)
    return trend


def _saved(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_the_trend_says_each_checks_runs_newest_first_and_the_employers_that_flip(tmp_path, capsys):
    """--trend: each check's employers at the review page out of those checked, run by run (a run that didn't check
    one says "-"), with the median seconds; then the employers whose result changed 3 times or more lately."""
    runs = []
    for i in range(10):
        run = {"search": {"USAA": "works"}}
        if i != 4:  # (the accounts check didn't run that night)
            run["accounts"] = {"KLA": "ready" if i % 2 == 0 else "needs_you stuck", "onsemi": "ready"}
        runs.append(run)
    body = lc.render_trend(_trend(runs))
    path = _saved(tmp_path, "trend.json", {"number": 70, "title": "Nightly live trends", "body": body})
    assert ns.main(["--trend", "3", "--trend-file", path]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("the last 3 run(s), newest first")
    assert [line.split() for line in out[1:4]] == [
        [f"2026-10-{day:02d}", "10:17", "UTC", "accounts", "1/2" if day % 2 == 0 else "2/2", "95", "s",
         "search", "1/1", "95", "s"] for day in (10, 9, 8)]
    assert out[4].startswith("flipping (3 changes or more in their last 10 runs")
    assert out[5:] == ["  accounts: KLA RxRx-xRxRx (7 changes)"]
    # all ten (fewer than the 14 it shows), from the list of issues the label gives
    listed = _saved(tmp_path, "issues.json", [{"number": 54, "title": "Nightly live check", "body": "the other"},
                                              {"number": 70, "title": "Nightly live trends", "body": body}])
    assert ns.main(["--trend", "--trend-file", listed]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("the last 10 run(s)") and len(out) == 1 + 10 + 2
    assert out[6].split()[:5] == ["2026-10-05", "10:17", "UTC", "accounts", "-"]
    assert ns.main(["--trend", "--trend-file", _saved(tmp_path, "other.json", {"body": "no history here"})]) == 1
    assert "the trend issue can't be read: its body holds no saved history" in capsys.readouterr().err


def test_an_employer_flips_when_its_result_changed_three_times_in_its_last_ten_runs():
    assert (lc.flips("RxR"), lc.flips("RxRx")) == (2, 3)
    assert lc.flips("RxRxRxRxRx" + "R" * 10) == 0  # (changed often, but not in its last ten runs)
    assert lc.flips("R" * 14 + "RRxRxR") == 4
    assert lc.flips("R-x--R-") == 2  # (the runs it wasn't checked in skipped)
    trend = lc.Trend([], {"accounts": {"KLA": "RRRRRRRxRx", "onsemi": "RRRRRRRRxR", "Intel": "xRxRxRxRxRRRRRRRRRRR"},
                          "search": {"USAA": "RxRxRRRRRR"}})
    assert lc.flipping(trend) == {"accounts": [("KLA", "RRRRRRRxRx", 3)], "search": [("USAA", "RxRxRRRRRR", 4)]}
    assert ns.flippers(trend) == {"KLA": "3 changes in the accounts check's last 10 runs: RRRRRRRxRx"}


def _handled_issue(tmp_path):
    state = {"accounts": {"Intel": "ready", "KLA": "ready", "onsemi": "ready", "Aerotek": "needs_you bot_check"}}
    current = {"accounts": {name: lc.Result(o, READY if o == "ready" else BOT)
                            for name, o in state["accounts"].items()}}
    body = lc.render_body(state, current, checked_at="2026-10-17 10:17 UTC", run_url="https://example.com/run/9")
    return body, _saved(tmp_path, "issue.json", {"body": body})


def test_handled_leaves_out_the_employers_that_flip(tmp_path, capsys):
    """An employer the accounts or pipeline check took all the way some nights only isn't one the desk goes all the way
    at: --handled leaves it out of data/desk_handled.yaml, and says so (on stderr: the YAML stays as it is)."""
    _, issue = _handled_issue(tmp_path)
    trend = lc.Trend([], {"accounts": {"KLA": "RxRxRRRRRR", "Intel": "RRRRRRRRRR", "Aerotek": "xRxRxRxxxx"},
                          "pipeline": {"onsemi": "xRxRxRRRRR"}, "search": {"Intel": "RxRxRxRxRx"}})
    path = _saved(tmp_path, "trend.json", {"body": lc.render_trend(trend)})
    assert ns.main(["--handled", "--issue-file", issue, "--trend-file", path]) == 0
    out, err = capsys.readouterr()
    assert out == 'employers:\n  - "Intel"\n'
    assert err.splitlines() == ["left out: KLA (flips: 4 changes in the accounts check's last 10 runs: RxRxRRRRRR)",
                                "left out: onsemi (flips: 5 changes in the pipeline check's last 10 runs: xRxRxRRRRR)"]
    # and the summary's drift from desk_handled.yaml says a listed one that flips apart, not as gone
    flip = ns.flippers(lc.read_trend(lc.render_trend(trend)))
    assert ns.handled_drift(["Intel", "KLA", "onsemi"], ["Intel", "KLA", "Micron"], flip) == [
        "desk_handled.yaml: 1 listed, not all the way in the last run: Micron",
        "desk_handled.yaml: 1 listed that flip (--handled leaves them out): KLA"]


def test_handled_without_the_trend_issue_is_as_before_with_a_warning(tmp_path, capsys, monkeypatch):
    body, issue = _handled_issue(tmp_path)
    before = 'employers:\n  - "Intel"\n  - "KLA"\n  - "onsemi"\n'
    assert ns.main(["--handled", "--issue-file", issue, "--trend-file", str(tmp_path / "missing.json")]) == 0
    out, err = capsys.readouterr()
    assert out == before
    assert err.startswith("warning: the trend issue isn't available (FileNotFoundError") and "aren't left out" in err

    def offline(url):
        if url.endswith("/issues/54"):
            return {"body": body, "comments": 0}
        raise urllib.error.URLError("no network")
    monkeypatch.setattr(ns, "fetch", offline)
    assert ns.main(["--handled"]) == 0
    out, err = capsys.readouterr()
    assert out == before and "warning: the trend issue isn't available (URLError: <urlopen error no network>)" in err

    def never(url):
        raise AssertionError(f"fetched {url}")
    monkeypatch.setattr(ns, "fetch", never)  # (a saved copy of the issue: nothing is fetched)
    assert ns.main(["--handled", "--issue-file", issue]) == 0
    out, err = capsys.readouterr()
    assert out == before and "--issue-file without --trend-file" in err


# --- what needs a person: the digest (A2, the review of Oct 18) -------------------------------------------------

def _digest_files(tmp_path, *, comment="", accounts=None, probes=None, log=""):
    """The files the nightly's compare job has: the issue's new body (the accounts check's state as given, else none),
    the changes comment, a check's log and the probes' folder."""
    state = {"pipeline": {"KLA": "ready"}} | ({"accounts": accounts} if accounts else {})
    current = {check: {name: lc.Result(outcome, "Technician") for name, outcome in outcomes.items()}
               for check, outcomes in state.items()}
    body = tmp_path / "body.md"
    body.write_text(lc.render_body(state, current, checked_at="2026-10-18 10:00 UTC"), encoding="utf-8")
    (tmp_path / "comment.md").write_text(comment, encoding="utf-8")
    (tmp_path / "run.log").write_text(log, encoding="utf-8")
    args = ["--digest", "--body-file", str(body), "--comment-file", str(tmp_path / "comment.md"),
            "--logs", str(tmp_path / "run.log"), "--run-url", "https://example.com/run/9"]
    if probes is not None:
        (tmp_path / "probes").mkdir()
        (tmp_path / "probes" / "probes.json").write_text(json.dumps(probes), encoding="utf-8")
        args += ["--probes", str(tmp_path / "probes")]
    return args


QUIET = """2 changes since the last nightly live check:

**Apply pipeline, semiconductor list (technician jobs): where it ended on one posting per employer**

- Intel: `needs_you questions` → `ready` (`Technician`)
- Micron: `needs_you stuck` → `needs_you bot_check` (`Technician · [bot_check] The site is checking`)
"""


def test_a_quiet_day_has_no_digest(tmp_path, capsys):
    """Changes that need no one (an employer further on, a bot check), every check finished, the handled list as the
    run, the probes passing and the inbox letting the test identity in: nothing is printed, so nothing is posted."""
    listed = ns.listed_handled()
    probes = {"reset": [{"employer": "Example", "status": "pass", "warning": "", "passes": []}], "tailored": []}
    log = 'LIVE_INBOX {"company": "Acme", "messages": [], "mail_problem": null}\n'
    args = _digest_files(tmp_path, comment=QUIET, accounts={name: "ready" for name in listed}, probes=probes, log=log)
    assert ns.main(args) == 0
    assert capsys.readouterr().out == ""
    assert ns.digest() == "" and ns.digest_from() == ""


def test_each_thing_that_needs_a_person_makes_a_digest(tmp_path, capsys):
    """Any one of them alone is a comment, headed "Needs a person" with the run's link."""
    went = QUIET + ("\n**Went backwards:** 1 employer no longer reaches the review page: KLA (accounts: "
                    "`needs_you stuck`).\n")
    cases = {
        "went": (dict(comment=went), [], "Went backwards: 1 employer no longer reaches the review page: KLA"),
        "unfinished": ({}, ["--unfinished", "accounts hr"], "A check didn't finish: accounts, hr"),
        "drift": (dict(accounts={"Example Not Listed Co": "ready"}), [],
                  "desk_handled.yaml: 1 all the way in the last run, not listed: Example Not Listed Co"),
        "probes": (dict(probes={"reset": [{"employer": "Example Tenant", "status": "fail",
                                           "warning": "pass 2 never signed in", "passes": []}], "tailored": []}), [],
                   "Weekly probes, the desk's failure: Example Tenant: pass 2 never signed in"),
        "inbox": (dict(log='LIVE_INBOX {"company": "Acme", "mail_problem": "The desk couldn\'t read your email: '
                       'imap.example.com didn\'t accept the email app password."}\n'), [],
                  "The test inbox refused its login (LIVE_INBOX at Acme)"),
    }
    for name, (files, more, said) in cases.items():
        folder = tmp_path / name
        folder.mkdir()
        assert ns.main(_digest_files(folder, **files) + more) == 0
        out = capsys.readouterr().out
        assert out.startswith("**Needs a person** ([run](https://example.com/run/9)):\n\n- "), (name, out)
        assert said in out, (name, out)


def test_probes_that_wrote_no_results_and_a_refused_inbox_are_read_as_such(tmp_path):
    (tmp_path / "empty").mkdir()
    said = ns.digest_from(probes_dir=tmp_path / "empty")
    assert "the probes' results can't be read" in said and said.startswith("**Needs a person**:")
    assert ns.inbox_refusals(['LIVE_INBOX {"company": "A", "error": "error: b\'[AUTHENTICATIONFAILED] Invalid\'"}\n'
                              'LIVE_INBOX {"company": "B", "error": "TimeoutError: timed out"}\nLIVE_INBOX not json',
                              'LIVE_INBOX {"company": "A", "mail_problem": "AUTHENTICATIONFAILED"}']) == ["A"]
