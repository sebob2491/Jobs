"""The nightly live check's results in a few lines (scripts/nightly_summary.py): read from the issue's body
as live_compare.py writes it, and from a run's saved results. No network: the body is made here."""

import importlib.util
import json
import sys
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


def test_a_runs_scrubbed_stop_pages_are_imported_into_the_tests(tmp_path):
    """live_smoke.py --fixtures leaves each page a run stopped on, scrubbed, in its folder's fixtures/: imported
    into tests/fixtures/live, test_live_fixtures.py checks every night's stops offline. One already there stays
    unless replaced."""
    run_dir = tmp_path / "live-accounts-0" / "shard-1" / "fixtures"
    run_dir.mkdir(parents=True)
    for name, body in (("pipeline-kla.html", "<p>new</p>"), ("pipeline-kla.frame-1.html", "<p>frame</p>"),
                       ("pipeline-kla.expect.json", '{"fields": []}'), ("pipeline-onsemi.html", "<p>o</p>"),
                       ("pipeline-onsemi.expect.json", '{"fields": []}')):
        (run_dir / name).write_text(body, encoding="utf-8")
    into = tmp_path / "live"
    into.mkdir()
    (into / "pipeline-onsemi.expect.json").write_text('{"old": true}', encoding="utf-8")
    assert ns.import_fixtures(tmp_path, ["kla"], into) == ["pipeline-kla"]
    assert sorted(p.name for p in into.iterdir()) == ["pipeline-kla.expect.json", "pipeline-kla.frame-1.html",
                                                      "pipeline-kla.html", "pipeline-onsemi.expect.json"]
    assert ns.import_fixtures(tmp_path, [], into) == []  # (onsemi's is there already)
    assert ns.import_fixtures(tmp_path, [], into, replace=True) == ["pipeline-kla", "pipeline-onsemi"]
    assert (into / "pipeline-onsemi.expect.json").read_text(encoding="utf-8") == '{"fields": []}'


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
