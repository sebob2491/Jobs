"""The live check's roles (scripts/live_smoke.py's ROLES, --role and each list's role in live_identity.LIST_ROLES): the
kind of job a run looks for, where, and a fake applicant with a background to match. Run in a process of its own:
live_smoke sets the environment it runs in. Nothing in a role is anyone's."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

SERVER = Path(__file__).parent.parent
SCRIPTS = SERVER / "scripts"
_spec = importlib.util.spec_from_file_location("live_identity", SCRIPTS / "live_identity.py")
li = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("live_identity", li)
_spec.loader.exec_module(li)

RUN_ROLES = """
import json, sys
from pathlib import Path
sys.path.insert(0, "scripts")
import yaml
import live_smoke
out = {"roles": sorted(live_smoke.ROLES)}
for name in live_smoke.ROLES:
    for shape in ("plain", "awkward"):
        live_smoke.SHAPE = shape
        live_smoke.use_role(name)
        profile = yaml.safe_load((live_smoke._HOME / "profile.yaml").read_text(encoding="utf-8"))
        resume = Path(profile["documents"]["resume"]).read_bytes().decode("cp1252")
        out[f"{name}/{shape}"] = {"profile": profile, "state": live_smoke.STATE, "query_az": live_smoke.QUERY_AZ,
                                  "query_any": live_smoke.QUERY_ANY, "values": live_smoke.profile_values(),
                                  "resume": resume}
print("RESULT " + json.dumps(out))
"""


def _roles():
    out = subprocess.run([sys.executable, "-c", RUN_ROLES], cwd=SERVER, capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    line = next((x for x in out.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, out.stdout[-2000:] + out.stderr[-3000:]
    return json.loads(line[len("RESULT "):])


def test_every_lists_role_is_a_role_and_finance_stays_paused():
    """Each list --everywhere applies at has a role live_smoke.py knows, and none is finance (paused: the owner's
    call, Oct 10). The Las Vegas list is IT jobs only."""
    text = (SCRIPTS / "live_smoke.py").read_text(encoding="utf-8")
    for role in li.LIST_ROLES.values():
        assert f'    "{role}": {{' in text, role
    assert "finance" not in li.LIST_ROLES.values() and li.LIST_ROLES["las-vegas"] == "it"


def test_the_it_role_is_a_coherent_las_vegas_help_desk_and_sysadmin_applicant():
    """--role it (the Las Vegas list's, live_identity.LIST_ROLES): help desk and systems administrator jobs, searched
    in Nevada first; an applicant who lives in Las Vegas, wants to work there, and whose jobs, school, degree and test
    resume all say IT. Made up: its employers are "Example ..." ones, no other role's, and its address is a public
    building's (Las Vegas' City Hall, as the plain profile's is Chandler's)."""
    got = _roles()
    assert "it" in got["roles"]
    it = got["it/plain"]
    assert it["state"] == "NV" and got["technician/plain"]["state"] == got["hr/plain"]["state"] == "AZ"
    for word in ("help desk", "systems administrator"):
        assert word in it["query_az"] and word in it["query_any"]
    profile = it["profile"]
    assert profile["personal"]["address"]["city"] == "Las Vegas" and profile["personal"]["address"]["state"] == "NV"
    assert profile["personal"]["first_name"] == "Testy" and profile["personal"]["email"].endswith("@example.com")
    assert all(p.endswith(", NV") for p in profile["preferences"]["locations"])
    assert profile["preferences"]["willing_to_relocate"] is True  # (the rest of the fake profile's preferences kept)
    jobs = profile["work_history"]
    assert [j["title"] for j in jobs] == ["Systems Administrator", "Help Desk Technician"]
    assert all(j["company"].startswith("Example ") and j["location"].endswith(", NV") and j["description"]
               for j in jobs)
    assert jobs[0]["end"] == "present" and jobs[1]["end"] < jobs[0]["start"]  # (most recent first, one after another)
    others = {j["company"] for r in ("technician", "hr", "finance")
              for j in got[f"{r}/plain"]["profile"]["work_history"]}
    assert not others & {j["company"] for j in jobs}
    school = profile["education_history"][0]
    assert school["major"] == "Information Technology" and school["end"] <= int(jobs[1]["start"][:4])
    assert profile["education"]["degrees_earned"][-1] == {"level": "bachelor", "field": school["major"]}
    assert "Help Desk Technician, Example Resorts" in it["resume"] and "Information Technology" in it["resume"]
    assert "Las Vegas, NV" in it["resume"] and "not a real applicant" in it["resume"]
    # what its review pages should show (the values audit's), and the other roles' employers never on them
    assert {w["company"] for w in it["values"]["work"]} == {j["company"] for j in jobs}
    assert set(it["values"]["others"]) == others
    for r in ("technician", "hr"):
        assert not {j["company"] for j in jobs} & {w["company"] for w in got[f"{r}/plain"]["values"]["work"]}
    # the awkward shape of it too (Sundays'): its current job with no end date, coursework, no degree claimed
    awkward = got["it/awkward"]["profile"]
    assert awkward["work_history"][0].get("current") is True and "end" not in awkward["work_history"][0]
    assert {s["degree"] for s in awkward["education_history"]} == {""}
    assert awkward["personal"]["address"]["state"] == "NV"
    # the other roles keep the plain profile's Arizona address
    assert got["hr/plain"]["profile"]["personal"]["address"]["state"] == "AZ"
    assert yaml.safe_dump(profile)  # (as the run writes it)


RUN_FAKE = """
import json, sys
sys.path.insert(0, "scripts")
import live_smoke
qs = json.loads(sys.argv[1])
print("RESULT " + json.dumps([[live_smoke.fake_answer(q), live_smoke.asks_ssn(q)] for q in qs]))
"""


def test_the_live_checks_throwaway_answers_fit_the_box_and_never_make_up_an_ssn():
    """Run 147: UCT's UKG Pro asked "How do you rate ...? On a scale of 1-5, 5 being best." in a box that keeps only a
    number, and the run's "Test answer" was emptied again each round; Clark County School District's Taleo refused "0"
    for a Social Security Number. A scale is answered with its top, a number box with a number, and a Social Security
    Number is never made up (None: the run ends there for the test identity, as a person would be asked it)."""
    qs = [{"label": "How do you rate your ability? On a scale of 1-5, 5 being best.", "kind": "text"},
          {"label": "Rate it on a scale from 1 to 10", "kind": "text"},
          {"label": "Your rating", "kind": "text", "numeric": True},
          {"label": "Your rating", "kind": "text", "input_type": "number"},
          {"label": "Social Security Number*", "kind": "text"},
          {"label": "SSN (last 4 digits)", "kind": "text", "numeric": True},
          {"label": "Why do you want this job?", "kind": "textarea"},
          {"label": "Have you ever worked for the Social Security Administration?", "kind": "text"}]
    out = subprocess.run([sys.executable, "-c", RUN_FAKE, json.dumps(qs)], cwd=SERVER, capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    line = next((x for x in out.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, out.stdout[-2000:] + out.stderr[-3000:]
    assert json.loads(line[len("RESULT "):]) == [["5", False], ["10", False], ["1", False], ["1", False],
                                                 [None, True], [None, True], ["Test answer", False],
                                                 ["Test answer", False]]
    # (and the rounds end at one, never answering it: live_smoke's question rounds read asks_ssn)
    text = (SCRIPTS / "live_smoke.py").read_text(encoding="utf-8")
    assert "if any(asks_ssn(q) for q in run.questions):" in text
