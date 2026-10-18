"""The live check's awkward profile (live_smoke.py --profile-shape awkward, the design of Oct 18, A7): the profile
itself, the switch, Sundays' row in the live-nightly workflow, and nightly_summary.py --compare-shapes, which says where
it got less far than the plain profile. No browser, no network."""

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from job_apply import autofill, fixtures
from job_apply.config import Profile

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
WORKFLOW = Path(__file__).resolve().parents[4] / ".github" / "workflows" / "live-nightly.yml"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lp = _load("live_profile")
la = _load("live_audit")
ns = _load("nightly_summary")


# --- the profile ------------------------------------------------------------------------------------------------------

def test_the_awkward_profile_differs_from_the_plain_one_only_as_described():
    """Its job held with no end date, two coursework entries at one school with no degree, a GED as the one credential,
    a second phone; everything else the plain profile's, which (and the replay profile beside it) stays as it was."""
    plain = copy.deepcopy(lp.FAKE_PROFILE)
    shaped = lp.awkward()
    assert lp.FAKE_PROFILE == plain  # (built from a copy)
    assert lp.shaped(lp.FAKE_PROFILE, "plain") == plain and lp.shaped(lp.FAKE_PROFILE, "awkward") == shaped
    with pytest.raises(ValueError, match="no profile shape named 'odd'"):
        lp.shaped(lp.FAKE_PROFILE, "odd")
    changed = {k for k in {*plain, *shaped} if plain.get(k) != shaped.get(k)}
    assert changed == {"personal", "work_history", "education_history", "education"}
    assert shaped["personal"] == {**plain["personal"], "alternate_phone": lp.AWKWARD_PHONE}
    assert lp.AWKWARD_PHONE != plain["personal"]["phone"] and "-555-01" in lp.AWKWARD_PHONE  # (no one's number)
    (job,) = shaped["work_history"]
    assert "end" not in job and job["current"] is True
    assert {k: v for k, v in job.items() if k != "current"} == {k: v for k, v in plain["work_history"][0].items()
                                                                 if k != "end"}
    first, second = shaped["education_history"]
    assert first["school"] == second["school"] == lp.AWKWARD_SCHOOL and first["degree"] == second["degree"] == ""
    assert first["major"] == plain["education_history"][0]["major"] and first["start"] > second["end"]
    assert shaped["education"] == {"highest_degree": "GED", "degrees_earned": [{"level": "ged"}]}
    # the replay profile is still the plain one's, apart from the email (test_live_fixtures checks it too)
    replay = json.loads(json.dumps(fixtures.REPLAY_PROFILE))
    assert {k: v for k, v in replay.items() if k != "personal"} == \
        {k: v for k, v in json.loads(json.dumps(plain)).items() if k != "personal"}


def test_the_awkward_profile_with_a_roles_background_keeps_the_roles_job_and_field():
    hr = {**lp.FAKE_PROFILE, "work_history": [{"title": "HR Coordinator", "company": "Example Staffing",
                                               "start": "2021-03", "end": "present"}],
          "education_history": [{"school": "Arizona State University", "degree": "Bachelor of Science",
                                 "major": "Business Administration", "start": 2016, "end": 2020}]}
    shaped = lp.awkward(hr)
    assert shaped["work_history"] == [{"title": "HR Coordinator", "company": "Example Staffing", "start": "2021-03",
                                       "current": True}]
    assert {e["major"] for e in shaped["education_history"]} == {"Business Administration"}
    assert [e["degree"] for e in shaped["education_history"]] == ["", ""]  # (the role's degree is gone)


def test_the_awkward_profile_is_one_the_desk_reads_and_never_claims_a_degree_from(tmp_path):
    """It passes the profile's own checks (nothing required missing, no settings warning; its schools without a degree
    are the one gap more than the plain profile's), its job reads as still held, and the desk answers its highest
    education with the GED, never a degree."""
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n%%EOF\n")
    plain = Profile({**copy.deepcopy(lp.FAKE_PROFILE), "documents": {"resume": str(resume)}})
    shaped = Profile({**lp.awkward(), "documents": {"resume": str(resume)}})
    assert shaped.missing_required() == [] and shaped.settings.warnings == []
    gaps = [g for g in shaped.profile_gaps() if g not in plain.profile_gaps()]
    assert gaps == [f"education_history degree: {lp.AWKWARD_SCHOOL}; {lp.AWKWARD_SCHOOL}"]
    assert autofill.undated_jobs(shaped) == []  # (its job is still held: no end date wanted)
    field = {"id": "x", "kind": "text", "label": "Highest level of education"}
    assert autofill.resolve_field(field, shaped, {}).value == "GED"
    assert autofill.resolve_field({**field, "label": "Phone number"}, shaped, {}).value == \
        lp.FAKE_PROFILE["personal"]["phone"]
    assert autofill.resolve_field({**field, "label": "Alternate phone number"}, shaped, {}) is None


# --- live_smoke.py --profile-shape ------------------------------------------------------------------------------------

def _smoke(tmp_path, *args):
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("LIVE_TEST_", "JOB_APPLY_SECRET_", "JOB_APPLY_LIVE_TEST_"))}
    out = subprocess.run([sys.executable, str(SCRIPTS / "live_smoke.py"), *args, "--out", str(tmp_path / "out")],
                         env={**clean, "TMPDIR": str(tmp_path)}, capture_output=True, text=True, timeout=120,
                         encoding="utf-8")
    return out.returncode, out.stdout + out.stderr


@pytest.mark.parametrize("args,said", [
    (["--profile-shape", "awkward"], "--profile-shape awkward goes with --pipeline"),  # (the search check)
    (["--pipeline", "--profile-shape", "awkward", "--fixtures"], "--profile-shape awkward goes without --fixtures"),
    (["--pipeline", "--profile-shape", "awkward", "--test-identity"],
     "--profile-shape awkward goes without --test-identity")])
def test_live_smoke_refuses_the_awkward_shape_with_what_it_cant_go_with(tmp_path, args, said):
    code, out = _smoke(tmp_path, *args)
    assert code == 2 and said in out and "Nothing was run" in out, out
    assert not (tmp_path / "out").exists()


def test_live_smoke_refuses_a_shape_it_doesnt_know(tmp_path):
    code, out = _smoke(tmp_path, "--pipeline", "--profile-shape", "odd")
    assert code == 2 and "invalid choice: 'odd'" in out, out


def test_live_smoke_applies_with_the_awkward_profile(tmp_path):
    """Everything up to the first employer (--companies matches none, so no browser starts): the profile it writes
    in the run's home is the awkward one, and its test resume says the job is still held and no degree."""
    code, said = _smoke(tmp_path, "--pipeline", "--fake-passwords", "--profile-shape", "awkward",
                        "--companies", "no-such-employer")
    assert code == 0, said
    assert "Profile shape: awkward" in said
    (home,) = [h for h in tmp_path.glob("job-apply-live-*") if (h / "profile.yaml").exists()]
    written, expected = yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8")), lp.awkward()
    assert {k: v for k, v in written.items() if k not in ("documents", "personal")} == \
        {k: v for k, v in expected.items() if k not in ("documents", "personal")}
    # (the email is each run's own: live_profile.FAKE_EMAIL is made as it's imported)
    assert written["personal"] == {**expected["personal"], "email": written["personal"]["email"]}
    assert lp.FAKE_EMAIL_SHAPE.fullmatch(written["personal"]["email"])
    resume = Path(written["documents"]["resume"]).read_bytes()
    assert b"\\(2021-03 to present\\)" in resume and b"None" not in resume  # (a PDF string's brackets, escaped)
    assert f"(Electrical Engineering, {lp.AWKWARD_SCHOOL} \\(2018 to 2019\\)) Tj".encode() in resume
    # and plain, as before: the fake profile
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    code, said = _smoke(plain_dir, "--pipeline", "--companies", "no-such-employer")
    assert code == 0 and "Profile shape" not in said, said
    (home,) = [h for h in plain_dir.glob("job-apply-live-*") if (h / "profile.yaml").exists()]
    written = yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8"))
    assert written["education_history"] == lp.FAKE_PROFILE["education_history"]
    assert written["work_history"] == lp.FAKE_PROFILE["work_history"]


# --- the values audit with a GED ----------------------------------------------------------------------------------

def test_a_ged_holders_review_page_showing_the_ged_is_no_overstated_degree():
    """degree_key reads a page's "GED" (and "High School Diploma or GED") as high_school's: a profile whose one
    credential is a GED isn't warned for it; an associate's beside its coursework school is still said."""
    profile = {"levels": ["ged"], "education": [{"school": lp.AWKWARD_SCHOOL, "start": "2018", "end": "2019"}]}
    page = f"<h4>Education</h4><p>School or University {lp.AWKWARD_SCHOOL}</p><p>Degree {{}}</p><p>From 2018</p>"
    assert list(la.page_findings("awkward/KLA", la.page_text(page.format("High School Diploma or GED")), profile)) == []
    found = list(la.page_findings("awkward/KLA", la.page_text(page.format("Associate of Applied Science")), profile))
    assert len(found) == 1 and "shows a associate degree" in found[0], found


# --- nightly_summary.py --compare-shapes -----------------------------------------------------------------------------

PLAIN_PROFILE = {"levels": ["bachelor", "high_school"], "work": [{"start": "2021-03", "end": "present"}],
                 "education": [{"school": "Arizona State University", "degree": "Bachelor of Science", "start": "2016",
                                "end": "2020"}]}
AWKWARD_PROFILE = {"levels": ["ged"], "work": [{"start": "2021-03"}],
                   "education": [{"school": lp.AWKWARD_SCHOOL, "start": "2018", "end": "2019"},
                                 {"school": lp.AWKWARD_SCHOOL, "start": "2015", "end": "2016"}]}


def _rec(company, status="ready", need="", reason="", profile=None):
    return {"company": company, "posting": {"title": "Technician"}, "profile": profile,
            "rounds": [{"status": status, "need": need, "reason": reason, "log": [], "questions": []}]}


def _page(school, degree, start="03/2021", to=""):
    return (f"<h4>Work Experience</h4><p>Job Title Technician</p><p>From {start}</p>{to}<h4>Education</h4>"
            f"<p>School or University {school}</p><p>Degree {degree}</p><h4>Resume</h4>")


def _artifact(folder, recs, pages):
    """A --parallel run's artifact as the check's machine writes it: its pipeline.json, and each employer's page in
    its group's folder."""
    folder.mkdir(parents=True)
    (folder / "pipeline.json").write_text(json.dumps(recs), encoding="utf-8")
    for name, html in pages.items():
        where = folder / "shard-1" / "pipeline" / la.slug(name)
        where.mkdir(parents=True)
        (where / "page.html").write_text(html, encoding="utf-8")
    return folder


def _stand_ins(tmp_path):
    plain = _artifact(tmp_path / "live-pipeline", [
        _rec("KLA", profile=PLAIN_PROFILE), _rec("ASML", profile=PLAIN_PROFILE), _rec("Intel", profile=PLAIN_PROFILE),
        _rec("Amkor", profile=PLAIN_PROFILE), _rec("onsemi", "needs_you", "stuck", "The page didn't move on",
                                                    profile=PLAIN_PROFILE), _rec("NXP", profile=PLAIN_PROFILE)],
        {"KLA": _page("Arizona State University", "Bachelor of Science"),
         # (a job from a date the tenant kept from an earlier run: the plain profile's warning too, so not the shape's)
         "ASML": _page("Arizona State University", "Bachelor of Science", start="02/2021"),
         "Intel": _page("Arizona State University", "Bachelor of Science"),
         "Amkor": _page("Arizona State University", "Bachelor of Science"),
         "NXP": _page("Arizona State University", "Bachelor of Science")})
    awkward = _artifact(tmp_path / "shape-awkward", [
        _rec("KLA", "needs_you", "questions", "Degree*: the profile has no degree for this school",
             profile=AWKWARD_PROFILE),
        _rec("ASML", profile=AWKWARD_PROFILE), _rec("Intel", profile=AWKWARD_PROFILE),
        {**_rec("Amkor", profile=AWKWARD_PROFILE), "rounds": [], "crash": "TimeoutError: the page never loaded"},
        _rec("onsemi", profile=AWKWARD_PROFILE)],
        {"ASML": _page(lp.AWKWARD_SCHOOL, "Some College", start="02/2021"),
         # the desk claimed a degree for coursework: the "never overstate" rule, the awkward profile's alone
         "Intel": _page(lp.AWKWARD_SCHOOL, "Associate of Applied Science"),
         "onsemi": _page(lp.AWKWARD_SCHOOL, "Some College")})
    return plain, awkward


def test_the_shapes_compare_says_where_the_awkward_profile_got_less_far_and_what_only_it_was_warned_for(tmp_path,
                                                                                                         capsys):
    plain, awkward = _stand_ins(tmp_path)
    assert ns.main(["--compare-shapes", str(plain), str(awkward)]) == 0
    said = capsys.readouterr().out.splitlines()
    assert said == [
        "shapes: the plain profile reached the review page at 5 of 6 employers, the awkward one at 3 of 5",
        "less far with the awkward profile: 2",
        "  Amkor: plain at the review page; awkward crash: TimeoutError: the page never loaded",
        "  KLA: plain at the review page; awkward needs_you questions: Degree*: the profile has no degree for this "
        "school",
        "not checked with the awkward profile: 1: NXP",
        "further with the awkward profile: 1",
        "  onsemi: awkward at the review page; plain needs_you stuck: The page didn't move on",
        "values audit warned only for the awkward profile: 1",
        f"  Intel: beside {lp.AWKWARD_SCHOOL} the review page shows a associate degree (“Associate of Applied”); "
        "the profile's levels: ged, high_school",
    ], said
    # zipped as GitHub zips an artifact (its folder's contents at the top): the same
    zips = []
    for folder in (plain, awkward):
        zipped = tmp_path / f"{folder.name}.zip"
        with zipfile.ZipFile(zipped, "w") as z:
            for path in folder.rglob("*"):
                if path.is_file():
                    z.write(path, path.relative_to(folder).as_posix())
        zips.append(str(zipped))
    assert ns.main(["--compare-shapes", *zips]) == 0
    assert capsys.readouterr().out.splitlines() == said


def test_the_shapes_compare_says_so_when_a_side_has_nothing(tmp_path, capsys):
    plain, _ = _stand_ins(tmp_path)
    (tmp_path / "empty").mkdir()
    assert ns.main(["--compare-shapes", str(plain), str(tmp_path / "empty")]) == 1
    assert "nothing to compare" in capsys.readouterr().err
    (tmp_path / "bad.zip").write_text("not a zip", encoding="utf-8")
    assert ns.main(["--compare-shapes", str(plain), str(tmp_path / "bad.zip")]) == 1
    assert "can't be read" in capsys.readouterr().err


# --- Sundays' row in the live-nightly workflow --------------------------------------------------------------------

def _flow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_awkward_row_is_the_pipeline_check_in_practice_with_no_accounts_and_its_own_artifact():
    flow = _flow()
    check = flow["jobs"]["check"]
    rows = [r for r in check["strategy"]["matrix"]["include"] if r["check"] == "awkward"]
    assert len(rows) == 1
    (row,) = rows
    args = row["args"].split()
    assert args[:1] == ["--pipeline"] and args[args.index("--profile-shape") + 1] == "awkward"
    assert not {"--test-identity", "--everywhere", "--security-answers", "--fixtures", "--plus"} & set(args)
    assert check["env"]["JOB_APPLY_NEVER_SUBMIT"] == "1"
    # its artifact is never live-*: the compare job (which writes the issue) downloads live-* alone
    assert row["artifact"] == "shape-awkward"
    compare = flow["jobs"]["compare"]
    (download,) = [s for s in compare["steps"] if "download-artifact" in s.get("uses", "")]
    assert download["with"]["pattern"] == "live-*" and not row["artifact"].startswith("live-")
    assert "awkward" not in compare["steps"][[s.get("name") for s in compare["steps"]].index(
        "Compare with the last run")]["run"]
    (upload,) = [s for s in check["steps"] if "upload-artifact" in s.get("uses", "")]
    assert upload["with"]["name"] == "${{ matrix.artifact || format('live-{0}', matrix.slot || matrix.check) }}"
    assert upload["if"] == "always() && (matrix.check != 'awkward' || steps.setup.outputs.ready == 'true')"
    # the account secrets go to the accounts check alone
    run = next(s for s in check["steps"] if s.get("name") == "Run the check")
    assert "matrix.check == 'accounts'" in run["env"]["LIVE_TEST_EMAIL_PASSWORD"]
    assert "matrix.check == 'accounts'" in run["env"]["LIVE_TEST_SITE_PASSWORD"]


def test_the_shapes_job_compares_in_the_runs_summary_alone():
    flow = _flow()
    shapes = flow["jobs"]["shapes"]
    assert shapes["needs"] == ["fresh", "check"] and shapes["if"] == "always() && needs.fresh.outputs.awkward == 'true'"
    assert shapes["permissions"] == {"contents": "read"}  # (no issues: write, so nothing of it reaches an issue)
    names = [s["with"]["name"] for s in shapes["steps"] if "download-artifact" in s.get("uses", "")]
    assert names == ["live-pipeline", "shape-awkward"]
    (step,) = [s for s in shapes["steps"] if "--compare-shapes" in s.get("run", "")]
    assert "--compare-shapes live-pipeline shape-awkward" in step["run"] and "GITHUB_STEP_SUMMARY" in step["run"]
    assert step["continue-on-error"] is True and "gh " not in json.dumps(shapes)
    assert flow["jobs"]["fresh"]["outputs"]["awkward"] == "${{ steps.last.outputs.awkward }}"
    assert "awkward" in flow.get("on", flow.get(True))["workflow_dispatch"]["inputs"]["checks"]["description"]


def _bash_step(tmp_path, run, env):
    output, summary = tmp_path / "output", tmp_path / "summary"
    output.unlink(missing_ok=True)
    full = {**{k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP")}, **env,
            "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}
    out = subprocess.run(["bash", "-e", "-c", run], env=full, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").split())


bash = pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None,
                          reason="the workflow's steps run in bash on Linux (Windows' may be another's)")


@bash
@pytest.mark.parametrize("event,schedule,checks,day,awkward", [
    ("workflow_dispatch", "", "", "7", "true"),  # the 3:17 AM run, dispatched, on a Sunday
    ("workflow_dispatch", "", "", "1", "false"),  # on a Monday
    ("workflow_dispatch", "", "pipeline", "7", "false"),  # a probe of other checks, even on a Sunday
    ("workflow_dispatch", "", "awkward", "3", "true"),  # by hand, any day
    ("workflow_dispatch", "", "pipeline, awkward", "3", "true"),
    ("schedule", "17 10 * * *", "", "7", "true"),  # the fallback schedule's run, nothing written in 12 hours
    ("schedule", "17 10 * * *", "", "6", "false"),
    ("schedule", "43 13 * * 0", "", "7", "false")])  # the weekly probes' run: the probes alone
def test_the_fresh_job_runs_the_awkward_check_on_sundays_and_by_hand(tmp_path, event, schedule, checks, day, awkward):
    step = _flow()["jobs"]["fresh"]["steps"][0]
    # (gh stands in: no issue written yet, so a scheduled run goes ahead)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    said = _bash_step(tmp_path, step["run"], {
        "EVENT": event, "SCHEDULE": schedule, "CHECKS": checks, "WEEKLY": step["env"]["WEEKLY"], "REPO": "o/r",
        "GH_TOKEN": "", "DAY": day, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"})
    assert said["awkward"] == awkward, said
    if awkward == "true":
        assert said["go"] == "true"  # (its row is one of the check job's: that job runs)


@bash
@pytest.mark.parametrize("check,checks,awkward,ready", [
    ("awkward", "", "true", "true"),
    ("awkward", "", "false", "false"),  # the other six days
    ("awkward", "pipeline", "false", "false"),
    ("awkward", "awkward", "true", "true"),
    ("pipeline", "", "false", "true"),  # the other checks as before
    ("pipeline", "awkward", "true", "false")])
def test_the_awkward_row_runs_only_when_the_fresh_job_says(tmp_path, check, checks, awkward, ready):
    step = next(s for s in _flow()["jobs"]["check"]["steps"] if s.get("name") == "Is it set up")
    assert step["env"]["AWKWARD"] == "${{ needs.fresh.outputs.awkward }}"
    said = _bash_step(tmp_path, step["run"], {"CHECK": check, "CHECKS": checks, "AWKWARD": awkward,
                                              "HAS_SECRETS": "false"})
    assert said == {"ready": ready}, said
