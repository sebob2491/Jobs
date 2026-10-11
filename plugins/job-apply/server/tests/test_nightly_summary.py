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
    names = [r["company"] for r in ns.records(tmp_path)]
    assert sorted(names) == ["KLA", "Onto Innovation"]
    zipped = tmp_path / "run.zip"
    with zipfile.ZipFile(zipped, "w") as z:
        z.write(tmp_path / "shard-0" / "pipeline.json", "shard-0/pipeline.json")
    assert [r["company"] for r in ns.records(zipped)] == ["Onto Innovation"]
    said = ns.steps(rec)
    assert "== Onto Innovation: Field Service Engineer" in said
    assert "Q: Degree* (listbox) ['BA', 'BS']" in said and "errors: ['Email needs fixing']" in said
    assert "buttons: ['Next', 'Back']" in said and "box: {'label': 'Email*', 'kind': 'text'}" in said
