"""The profile's other documents (documents.transcripts, .references, .certificates) and its references: list: read
like the resume, matched to the file boxes that ask for each and to no other, a reference list laid out from the
entries where there's no file, and the references' details kept out of reports and saved pages. Made-up files and
names only."""

import json
import zipfile
from pathlib import Path

import pytest
import yaml
from conftest import PROFILE, browser_available, run

from job_apply import autofill, config, doctor, render, report
from job_apply.autofill import resolve_field
from job_apply.config import Profile
from job_apply.fixtures import clean_html, personal_strings
from job_apply.pipeline import Run

# made up, each: no one's
REFERENCES = [{"name": "Morgan Quillby", "relationship": "Former supervisor", "company": "Tarnwell Systems",
               "phone": "602-555-0177", "email": "mquillby42@example.org"},
              {"name": "Avery Brightwater", "relationship": "Coworker", "phone": "(480) 555-0166"}]


def _files(tmp_path, *names: str) -> list[Path]:
    out = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"%PDF-1.4\n% a made-up document\n")
        out.append(path)
    return out


def _box(label: str, **more) -> dict:
    return {"id": "f", "kind": "file", "label": label, "value": "", **more}


def test_the_profile_names_more_documents_each_a_file_or_a_list(tmp_path, monkeypatch):
    """documents.transcripts, .references and .certificates are read like documents.resume: a path with ~ expanded,
    or a list of them; one named that isn't there is never given, and is said (profile_gaps: the desk and doctor)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # (Windows' home)
    transcript, cert, diploma = _files(tmp_path, "transcript.pdf", "cert.pdf", "diploma.pdf")
    prof = Profile({"documents": {"transcripts": "~/transcript.pdf", "references": "~/gone.pdf",
                                  "certificates": [str(cert), "~/diploma.pdf", str(tmp_path / "lost.pdf"), 7]}})
    assert prof.document_files("transcripts") == [transcript]
    assert prof.document_files("certificates") == [cert, diploma]  # (in the order written; never a number)
    assert prof.document_files("references") == []
    assert prof.missing_documents() == [("references", tmp_path / "gone.pdf"),
                                        ("certificates", tmp_path / "lost.pdf")]
    gaps = [g for g in prof.profile_gaps() if g.startswith("documents.")]
    assert gaps == [f"documents.references (file not found: {tmp_path / 'gone.pdf'})",
                    f"documents.certificates (file not found: {tmp_path / 'lost.pdf'})"]
    # none named: nothing to say (most forms never ask)
    assert not Profile({"documents": {"transcripts": None}}).missing_documents()
    assert not [g for g in Profile({}).profile_gaps() if g.startswith("documents.")]
    assert Profile({"documents": {"certificates": {"a": "b"}}}).document_paths("certificates") == []


def test_the_references_are_the_persons_own_entries_as_written():
    prof = Profile({"references": [*REFERENCES, {"relationship": "no name"}, "Pat", {"name": "  Kim   Lee ",
                                                                                      "phone": None}]})
    assert prof.references() == [REFERENCES[0], REFERENCES[1], {"name": "Kim Lee"}]
    assert Profile({"references": {"name": "x"}}).references() == []  # (a list, or none)


def test_a_box_for_transcripts_references_or_certificates_gets_the_profiles_own(tmp_path):
    """NSHE's Workday (UNLV, Nevada State University, College of Southern Nevada) requires "Please upload your
    Unofficial Transcripts." and "Please upload your Reference Contact Information.": they stopped the live check with
    profile_gap, the profile having no place for either. Each gets the profile's own file now."""
    # (a certificate whose file's name says no kind: the profile's one, for any certificate, license or diploma box
    # that names no particular one)
    resume, transcript, older, refs, cert = _files(tmp_path, "resume.pdf", "transcript.pdf", "older.pdf", "refs.pdf",
                                                   "my-credential.pdf")
    prof = Profile({"documents": {"resume": str(resume), "transcripts": [str(transcript), str(older)],
                                  "references": str(refs), "certificates": [str(cert)]}})
    workday = {"section": "Application Questions 1 of 3", "multiple": True}
    asked = resolve_field(_box("Please upload your Unofficial Transcripts.*", **workday), prof, {}, 3)
    assert (asked.value, asked.rule) == ([str(transcript), str(older)], "documents.transcripts")
    single = resolve_field(_box("Unofficial Transcripts"), prof, {}, 3)  # (a box that takes one: the first)
    assert (single.value, single.rule) == (str(transcript), "documents.transcripts")
    for label in ("Please upload your Reference Contact Information.*", "References", "Professional References",
                  "Reference List"):
        got = resolve_field(_box(label, **workday), prof, {}, 3)
        assert (got.value, got.rule) == (str(refs), "documents.references"), label
    for label in ("Certificates", "Certifications"):
        got = resolve_field(_box(label), prof, {}, 3)
        assert (got.value, got.rule) == (str(cert), "documents.certificates"), label
    # (a file whose name says no kind is a certificate proper: never in a box naming a license or a diploma)
    for label in ("Professional License", "Diploma", "Licence", "Licenses / Certifications"):
        assert resolve_field(_box(label), prof, {}, 3) is None, label
    # a box named by its section alone ("Attach" under "Transcripts")
    assert resolve_field(_box("Attach", section="Transcripts"), prof, {}, 3).rule == "documents.transcripts"
    # by its own words: a transcript's box in a section that names the cover letter never gets the cover letter
    letter = _files(tmp_path, "letter.pdf")[0]
    with_letter = Profile({"documents": {**prof.data["documents"], "cover_letter": str(letter)}})
    got = resolve_field(_box("Transcripts", section="Cover Letter and Other Documents"), with_letter, {}, 3)
    assert got.rule == "documents.transcripts"


def test_a_box_naming_several_takes_what_the_profile_has(tmp_path):
    """Las Vegas Valley Water District's Workday: "If available, please attach a copy of your transcript(s),
    diploma(s), and/or certificate(s) here.*". A box that takes several files gets every one of them the profile has;
    one that takes one file gets the first kind it names that the profile has."""
    transcript, cert, diploma = _files(tmp_path, "transcript.pdf", "cert.pdf", "diploma.pdf")
    label = "If available, please attach a copy of your transcript(s), diploma(s), and/or certificate(s) here.*"
    both = Profile({"documents": {"transcripts": str(transcript), "certificates": [str(cert), str(diploma)]}})
    several = resolve_field(_box(label, multiple=True), both, {}, 2)
    # (in the order it names them: transcripts, diplomas, certificates)
    assert several is not None and several.value == [str(transcript), str(diploma), str(cert)]
    assert several.rule == "documents.transcripts+certificates"
    one = resolve_field(_box(label), both, {}, 2)
    assert (one.value, one.rule) == (str(transcript), "documents.transcripts")
    certs_only = Profile({"documents": {"certificates": [str(cert), str(diploma)]}})
    one = resolve_field(_box(label), certs_only, {}, 2)
    assert (one.value, one.rule) == (str(diploma), "documents.certificates")
    several = resolve_field(_box(label, multiple=True), certs_only, {}, 2)
    assert (several.value, several.rule) == ([str(diploma), str(cert)], "documents.certificates")
    assert resolve_field(_box(label, multiple=True), Profile({}), {}, 2) is None  # (none: the person's)
    assert autofill.documents_asked(_box(label)) == ["transcripts", "certificates"]  # (in the order it names them)


def test_a_box_for_something_else_never_gets_one_of_these_nor_one_of_these_the_resume(tmp_path):
    """Never a transcript, reference list or certificate in a box asking for something else (a resume, a cover
    letter, a writing sample, a portfolio, a DD-214, an ID, a letter of recommendation), and never the resume in a box
    asking for one of these."""
    resume, letter, transcript, refs, cert = _files(tmp_path, "resume.pdf", "letter.pdf", "transcript.pdf",
                                                    "refs.pdf", "cert.pdf")
    every = Profile({"documents": {"resume": str(resume), "cover_letter": str(letter), "transcripts": str(transcript),
                                   "references": str(refs), "certificates": str(cert)}})
    ours = {str(transcript), str(refs), str(cert)}
    for label in ("Writing sample", "Portfolio", "DD-214 (Certificate of Release or Discharge from Active Duty)",
                  "Photo ID", "Copy of Driver's License", "Letter of Recommendation", "Letters of Reference",
                  "Birth Certificate", "Veterans' Preference Certificate", "Writing sample or transcripts"):
        for files in (1, 3):
            got = resolve_field(_box(label, multiple=True), every, {}, files)
            given = [] if got is None else got.value if isinstance(got.value, list) else [got.value]
            assert not set(given) & ours, (label, got)
    assert resolve_field(_box("Resume/CV*"), every, {}, 3).value == str(resume)  # (a resume's box: the resume)
    assert resolve_field(_box("Cover Letter"), every, {}, 3).value == str(letter)
    no_resume = Profile({"documents": {"certificates": str(cert), "transcripts": str(transcript)}})
    assert resolve_field(_box("Resume/CV*", multiple=True), no_resume, {}, 3) is None
    assert resolve_field(_box("Resume and transcripts"), no_resume, {}, 1) is None
    # the resume alone: a page's one upload asking for one of these is never given the resume
    only_resume = Profile({"documents": {"resume": str(resume)}})
    for label in ("Please upload your Unofficial Transcripts.", "Please upload your Reference Contact Information.",
                  "Certifications", "Attach"):
        section = "Licenses and certifications" if label == "Attach" else ""
        assert resolve_field(_box(label, section=section), only_resume, {}, 1) is None, label


def test_a_reference_list_made_for_the_job_is_what_its_box_gets(tmp_path):
    """With no file for a reference list, the page the desk made in the job's folder from the profile's references:
    entries goes up; the profile's own file, where it names one, comes first."""
    folder = tmp_path / "0001-job"
    folder.mkdir()
    made = _files(folder, "Sam_Rivera_References.pdf", "Sam_Rivera_Resume.pdf")[0]
    (folder / "Sam_Rivera_References.md").write_text(render.references_markdown(Profile({"references": REFERENCES})),
                                                    encoding="utf-8")  # (what it was made of, as references_page keeps)
    job = {"folder": str(folder)}
    box = _box("Please upload your Reference Contact Information.*")
    got = resolve_field(box, Profile({"references": REFERENCES}), job, 2)
    assert (got.value, got.rule) == (str(made), "documents.references")
    own = _files(tmp_path, "my-references.pdf")[0]
    assert resolve_field(box, Profile({"documents": {"references": str(own)}}), job, 2).value == str(own)
    assert autofill.tailored_document(job, "resume").endswith("Sam_Rivera_Resume.pdf")  # (each its own)


def test_the_references_page_says_only_the_persons_entries():
    prof = Profile({**PROFILE, "references": [*REFERENCES, {"name": "Lee *Star* [x]_y"}]})
    md = render.references_markdown(prof)
    assert md.startswith("# Sam Rivera\n\nsam.rivera@example.com · 480-555-0123 · Chandler, AZ\n\n## References\n\n")
    assert ("**Morgan Quillby**  \nFormer supervisor, Tarnwell Systems  \n602-555-0177 · mquillby42@example.org"
            in md)
    assert "**Avery Brightwater**  \nCoworker  \n(480) 555-0166" in md
    assert r"**Lee \*Star\* \[x\]\_y**" in md  # (the person's words as written: never markup)
    assert render.references_markdown(Profile(PROFILE)) == ""  # no entries: no page (nothing is made up)


@pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")
def test_a_reference_list_renders_as_one_page_in_the_jobs_folder(srv, job_apply_home):
    data = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump({**data, "references": REFERENCES}),
                                                 encoding="utf-8")
    job = srv.add_job(url="https://example.com/jobs/3", title="Lab Technician", company="Example College")["job"]
    out = run(srv.render_document("references", render.references_markdown(Profile.load()), job_id=job["id"]))
    pdf = Path(out["path"])
    assert pdf.name == "Sam_Rivera_References.pdf" and pdf.read_bytes().startswith(b"%PDF")
    assert out["pages"] == 1 and "warning" not in out


def test_the_page_is_made_only_where_a_box_asks_and_the_profile_has_entries_and_no_file(srv, job_apply_home,
                                                                                     monkeypatch):
    made = []

    async def fake_render(page_html, dest):
        made.append(dest.name)
        dest.write_bytes(b"%PDF-1.4\n")
        return 1

    monkeypatch.setattr(srv, "render_pdf", fake_render)
    job = srv.tracker().get(srv.add_job(url="https://example.com/jobs/4", title="Clerk", company="Example County")
                            ["job"]["id"])
    refs_box = _box("Please upload your Reference Contact Information.*", required=True)
    with_entries = Profile({**PROFILE, "references": REFERENCES})
    assert not run(srv.references_page([refs_box], Profile(PROFILE), job))  # (no entries: nothing made up)
    assert not run(srv.references_page([_box("Resume/CV")], with_entries, job))  # (no box asking)
    assert not run(srv.references_page([refs_box], with_entries, None))  # (no job's folder)
    own = _files(job_apply_home, "own-refs.pdf")[0]
    assert not run(srv.references_page([refs_box], Profile({**with_entries.data, "documents": {
        "references": str(own)}}), job))  # (the person's own file comes first)
    assert not made
    assert run(srv.references_page([refs_box], with_entries, job))
    assert made == ["Sam_Rivera_References.pdf"]
    assert (Path(job["folder"]) / "Sam_Rivera_References.md").read_text(encoding="utf-8") \
        == render.references_markdown(with_entries)
    assert not run(srv.references_page([refs_box], with_entries, job))  # (laid out once: the same entries)
    changed = Profile({**PROFILE, "references": REFERENCES[:1]})
    assert run(srv.references_page([refs_box], changed, job)) and len(made) == 2  # (again, as they changed)

    async def broken(page_html, dest):
        raise RuntimeError("the page wouldn't print")

    monkeypatch.setattr(srv, "render_pdf", broken)  # (never the fill's trouble: the box stays the person's)
    assert not run(srv.references_page([refs_box], Profile({**PROFILE, "references": REFERENCES[1:]}), job))


def test_the_references_details_are_kept_out_of_a_saved_page_and_a_report(job_apply_home, tmp_path):
    """The repo is public, and a report is filed in it: a saved page (a fixture) and a report never hold a
    reference's name, phone or email (nor, in a report, where they work), however the page writes them."""
    prof = Profile({**PROFILE, "references": REFERENCES})
    secrets = personal_strings(prof)
    page = ("<html><body><h2>References</h2><label>Reference Name</label><input value='Morgan Quillby'>"
            "<label>First</label><input value='Morgan'><label>Last</label><input value='Quillby'>"
            "<input value='602.555.0177'><input value='mquillby42@example.org'><p>Avery Brightwater, 480-555-0166</p>"
            "<p>Tarnwell Systems</p></body></html>")
    out = clean_html(page, secrets)
    for private in ("Morgan", "Quillby", "555.0177", "6025550177", "mquillby42", "Avery", "Brightwater",
                    "555-0166"):
        assert private not in out, private
    assert "Reference Name" in out and "References" in out  # (the page's own words stay)
    # a report: its text, its saved pages, and never the reference list the desk made for the job
    folder = tmp_path / "0009-example-county-clerk"
    snap = folder / "debug" / "20261020-120000-000"
    snap.mkdir(parents=True)
    (snap / "page.html").write_text(page, encoding="utf-8")
    (snap / "snapshot.json").write_text(json.dumps({"url": "https://example.wd1.myworkdayjobs.com/x/apply",
                                                    "title": "References: Morgan Quillby", "fields": []}),
                                        encoding="utf-8")
    (folder / "Sam_Rivera_References.pdf").write_bytes(b"%PDF-1.4 Morgan Quillby 602-555-0177")
    (folder / "Sam_Rivera_References.md").write_text("**Morgan Quillby**  \n602-555-0177", encoding="utf-8")
    job = {"id": 9, "title": "Clerk", "company": "Example County", "ats": "workday", "folder": str(folder),
           "url": "https://example.wd1.myworkdayjobs.com/x"}
    r = Run(9, "Clerk", "Example County", status="needs_you", need="stuck",
            reason="The page says: Morgan Quillby (mquillby42@example.org) can't be reached at 602-555-0177",
            log=["uploaded your reference list (a page the desk made from the references in your profile) to "
                 "“Please upload your Reference Contact Information”", "Tarnwell Systems said no"])
    built = report.build(job, r, prof)
    with zipfile.ZipFile(built["zip"]) as z:
        names = z.namelist()
        everything = "".join(z.read(n).decode("utf-8") for n in names)
    assert not any(n.endswith((".pdf", ".md")) and n != "report.md" for n in names)  # (only the text and pages)
    assert "uploaded your reference list" in everything
    for private in ("Morgan", "Quillby", "555-0177", "6025550177", "mquillby42", "Avery", "Brightwater",
                    "555-0166", "Tarnwell"):
        assert private not in everything and private not in built["issue_url"], private


def test_doctor_says_a_named_document_that_isnt_there_and_nothing_about_one_not_named(job_apply_home):
    data = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    prof = Profile({**data, "documents": {**data["documents"],
                                          "transcripts": str(job_apply_home / "Sam transcript.pdf")}})
    [said] = doctor.check_documents(prof)
    assert said.status == doctor.ADVICE and "documents.transcripts" in said.say and "isn't there" in said.say
    assert "Sam" not in said.say + said.fix  # (never the file's name: it may be the person's)
    assert doctor.check_documents(Profile(data)) == [] and doctor.check_documents(None) == []
    assert config.EXTRA_DOCUMENTS["references"] in doctor.check_documents(
        Profile({**data, "documents": {"references": "/nowhere/refs.pdf"}}))[0].say


def test_several_files_go_into_a_box_that_takes_several_and_only_there(srv, tmp_path):
    """A box that takes several files (`multiple`: Workday's) gets them all at once, once a page; a box that takes one
    is never given several (the fill says so, and the box stays empty for the person)."""
    transcript, cert = _files(tmp_path, "transcript.pdf", "cert.pdf")
    page = tmp_path / "uploads.html"
    page.write_text("<!doctype html><html><body><form>"
                    "<label for='m'>Transcripts and certificates</label><input type='file' id='m' multiple>"
                    "<label for='s'>Certificate</label><input type='file' id='s'></form></body></html>",
                    encoding="utf-8")
    run(srv.browser.goto(page.as_uri()))
    ids = {f["label"]: f["id"] for f in run(srv.browser.inspect(include_dropdown_options=False))["fields"]}
    both = [str(transcript), str(cert)]
    done = run(srv.browser.fill([{"id": ids["Transcripts and certificates"], "value": both},
                                 {"id": ids["Certificate"], "value": both}]))
    assert done[0]["ok"] and done[0]["result"] == "uploaded transcript.pdf, cert.pdf", done
    assert not done[1]["ok"] and "one file at a time" in done[1]["error"], done
    tab = run(srv.browser.page())
    held = run(tab.evaluate(
        "() => [...document.querySelectorAll('input[type=file]')].map((b) => [...b.files].map((f) => f.name))"))
    assert held == [["transcript.pdf", "cert.pdf"], []], held
    again = run(srv.browser.fill([{"id": ids["Transcripts and certificates"], "value": both}]))
    assert again[0]["result"] == "already uploaded on this page", again
    gone = run(srv.browser.fill([{"id": ids["Transcripts and certificates"],
                                  "value": [str(transcript), str(tmp_path / "no.pdf")]}]))
    assert not gone[0]["ok"] and "FileNotFoundError" in gone[0]["error"], gone


def test_the_desk_says_a_named_document_that_isnt_there(srv, job_apply_home):
    import time

    from conftest import person_browser

    from job_apply.desk import Desk

    gone = job_apply_home / "transcript-2024.pdf"
    data = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    data["documents"]["transcripts"] = str(gone)
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#notices .notice")
                said = await page.inner_text("#notices")
                await browser.close()
                return said
        finally:
            await desk.stop()

    said = run(go())
    assert f"your transcripts file where documents.transcripts says (nothing is at {gone})" in said, said


# --- the review of the first round (its probes' cases: a battery of file boxes, the stale page, the scrubber) ------

def _every(tmp_path: Path) -> tuple[Profile, dict[str, str]]:
    """A profile with every document, and each file's path by its name (the review's battery)."""
    names = ("resume.pdf", "letter.pdf", "transcript.pdf", "refs.pdf", "cert.pdf", "diploma.pdf")
    paths = {p.name: str(p) for p in _files(tmp_path, *names)}
    return Profile({"documents": {"resume": paths["resume.pdf"], "cover_letter": paths["letter.pdf"],
                                  "transcripts": paths["transcript.pdf"], "references": paths["refs.pdf"],
                                  "certificates": [paths["cert.pdf"], paths["diploma.pdf"]]}}), paths


# Boxes that ask for something else than the profile's transcripts, reference list or certificates: a letter someone
# else wrote, a form to sign, a job's reference number, proof of eligibility or identity (label, section)
SOMETHING_ELSE = [
    ("Letter(s) of Reference", ""), ("Letters of Reference", ""), ("Reference Letter(s)", ""),
    ("Letter of reference or recommendation", ""), ("Letters from your references", ""),
    ("Reference letters and transcripts", ""), ("Reference Release Form", ""), ("Signed Reference Waiver", ""),
    ("Reference Authorization Form", ""), ("Reference Questionnaire", ""), ("Reference check consent form", ""),
    ("Transcript request form", ""), ("Transcript release authorization", ""), ("Job Reference Number", ""),
    ("Attach", "Job Reference: R12345 Lab Technician"), ("Upload a file", "Reference Code 4421 - Process Technician"),
    ("Upload Attachment", "Reference Information"), ("Professional License Number", ""), ("Certificate Number", ""),
    ("Copy of Driver License", ""), ("Upload a copy of your valid drivers license", ""),
    ("Driver’s License (front and back)", ""), ("Commercial Driver's License (CDL)", ""), ("CDL license", ""),
    ("Driving licence", ""), ("Copy of license (driver)", ""), ("Naturalization Certificate", ""),
    ("Certificate of Citizenship", ""),
    ("Proof of citizenship (birth certificate, passport, or naturalization certificate)", ""),
    ("Certificate of Indian Blood (CIB)", ""), ("Tribal Enrollment Certificate", ""),
    ("Schedule A certification letter", ""), ("Marriage certificate", ""), ("Fingerprint Clearance Card", ""),
    ("Certificate of Release or Discharge from Active Duty", ""), ("DD214", ""),
    ("Veteran preference documentation (DD-214 or VA letter)", ""), ("Disability certification for Schedule A", ""),
    ("Medical certificate", ""), ("Certificate of Insurance", ""),
    ("Certification of disability (VA rating letter)", ""),
    ("Upload copy of your Social Security card or birth certificate", ""),
    ("Upload your I-9 documents (license, passport)", ""), ("Portfolio or certifications", ""), ("Photo", ""),
    ("Work permit", ""), ("Writing sample", ""), ("Writing sample or transcripts", ""),
]


@pytest.mark.parametrize("label, section", SOMETHING_ELSE)
def test_a_box_for_something_else_is_never_given_these(tmp_path, label, section):
    """The review: a letter of reference ("Letter(s) of Reference", "Letters from your references") or a form to sign
    ("Reference Release Form", "Transcript request form") got the reference list or the transcript; a box under a
    job's "Job Reference: R12345" got the reference list; and proofs of eligibility or identity (citizenship, a
    Certificate of Indian Blood, Schedule A, a medical or marriage certificate, a CDL) got a certificate. None of them
    gets one of these, in a box that takes one file or several."""
    prof, paths = _every(tmp_path)
    ours = {paths[n] for n in ("transcript.pdf", "refs.pdf", "cert.pdf", "diploma.pdf")}
    for multiple in (False, True):
        for files in (1, 2):
            box = _box(label, section=section, **({"multiple": True} if multiple else {}))
            got = resolve_field(box, prof, {}, files)
            given = [] if got is None else got.value if isinstance(got.value, list) else [got.value]
            assert not set(given) & ours, (label, section, multiple, files, got)


@pytest.mark.parametrize("label, section, want", [
    ("Please upload your Reference Contact Information.*", "Application Questions 1 of 3", "refs.pdf"),
    ("References", "", "refs.pdf"), ("Professional References", "", "refs.pdf"), ("Reference List", "", "refs.pdf"),
    ("Attachments", "References", "refs.pdf"), ("Attach", "Professional References", "refs.pdf"),
    ("Unofficial Transcripts", "", "transcript.pdf"), ("Upload", "Transcripts", "transcript.pdf"),
    ("High school diploma or GED certificate", "", "diploma.pdf"), ("Copy of degree or diploma", "", "diploma.pdf"),
    ("Copy of Degree", "", "diploma.pdf"), ("Proof of degree (diploma or transcript)", "", "diploma.pdf"),
    ("Certificate of completion of training", "", "cert.pdf"), ("Certificates", "", "cert.pdf"),
])
def test_a_box_gets_the_kind_of_document_it_names(tmp_path, label, section, want):
    """The review: certificates hold diplomas, licenses and certificates proper, and with [cert.pdf, diploma.pdf] a
    "High school diploma or GED certificate" box got cert.pdf. A one-file box gets a file whose name says the kind it
    names; a reference list only by the box's own words, or a section about references as people."""
    prof, paths = _every(tmp_path)
    got = resolve_field(_box(label, section=section), prof, {}, 2)
    assert got is not None and got.value == paths[want], (label, got)


def test_a_certificate_box_takes_only_a_file_that_says_it_is_what_the_box_names(tmp_path):
    """Of several certificates, none whose name says the kind (or the particular one) the box names: the person's to
    attach. One certificate whose name says no kind at all goes into a box naming no particular one."""
    transcript, cert, diploma, teaching, nursing, plain, other = _files(
        tmp_path, "transcript.pdf", "cert.pdf", "diploma.pdf", "Teaching_Certificate.pdf", "Nursing-License-2024.pdf",
        "credential.pdf", "scan2.pdf")

    def given(label: str, certificates: list[Path], **more) -> str | list[str] | None:
        prof = Profile({"documents": {"transcripts": str(transcript), "certificates": [str(p) for p in certificates]}})
        got = resolve_field(_box(label, **more), prof, {}, 2)
        return None if got is None else got.value

    # a transcript before a certificate whose name says another kind ("Proof of degree (diploma or transcript)")
    assert given("Proof of degree (diploma or transcript)", [cert]) == str(transcript)
    # a box naming a particular one: only a file whose name says so
    assert given("Teaching certificate", [cert, diploma]) is None
    assert given("Teaching certificate", [cert, teaching]) == str(teaching)
    assert given("Nursing license", [cert, diploma]) is None and given("Nursing license", [nursing]) == str(nursing)
    assert given("Food Handler Certificate", [plain]) is None
    # one certificate whose name says no kind: a certificate proper, for a certificate's box naming no particular one;
    # never a license's or a diploma's (the second review: OSHA30.pdf alone went into "High School Diploma")
    assert given("Certifications", [plain]) == str(plain)
    for label in ("Professional License", "High School Diploma", "Diploma/GED", "Copy of diploma", "Copy of License",
                  "Licenses", "Licence", "Copy of degree or diploma"):
        assert given(label, [plain]) is None and given(label, [plain], multiple=True) is None, label
    assert given("Proof of degree (diploma or transcript)", [plain], multiple=True) == str(transcript)
    assert given("Professional License", [cert]) is None
    assert given("Copy of degree or diploma", [cert]) is None
    # several, none saying the kind: the person's in a box that takes one; all of them in one that takes several (for
    # certificates alone: never in one naming a license too, the third review's rule)
    assert given("Certificates", [plain, other]) is None
    assert given("Certifications", [plain, other], multiple=True) == [str(plain), str(other)]
    assert given("Licenses / Certifications", [plain, other], multiple=True) is None


@pytest.mark.parametrize("label, section", [("Resume or CV", "Import your profile"),
                                            ("Resume", "Autofill your application with your resume"),
                                            ("Resume", "Parse resume")])
def test_a_resume_box_under_a_section_that_reads_the_resume_is_still_left_empty(tmp_path, label, section):
    """The review (a regression of the first round): a box's label naming a document made it its own words alone, so
    a "Resume" box under "Autofill your application with your resume", beside the one that attaches it, got the resume
    again, and the site refilled the form from its own reading. Its section still says it reads the resume."""
    prof, _ = _every(tmp_path)
    got = resolve_field(_box(label, section=section), prof, {}, 2)
    assert got is not None and got.value == autofill.SKIP, got


def test_a_page_made_of_references_since_changed_or_taken_out_never_goes_up(srv, job_apply_home, monkeypatch):
    """The review: a reference taken off, then a render that failed (a browser's hiccup), and the page made before,
    with that reference on it, went up, for good (its Markdown, written before the render, matched the new entries);
    and with every reference taken out of the profile the page made before still went up. A failed render takes the
    old page with it, the next fill makes it again, and a made page goes up only while it holds the entries as they
    are."""
    a = {"name": "Morgan Quillby", "phone": "602-555-0177"}
    b = {"name": "Avery Brightwater", "phone": "480-555-0166"}

    async def fake_render(page_html, dest):
        dest.write_bytes(("%PDF " + page_html).encode("utf-8"))
        return 1

    async def broken(page_html, dest):
        raise RuntimeError("the browser went away")

    monkeypatch.setattr(srv, "render_pdf", fake_render)
    job = srv.tracker().get(srv.add_job(url="https://example.com/jobs/6", title="Clerk", company="Example County")
                            ["job"]["id"])
    box = _box("Please upload your Reference Contact Information.*", required=True)
    both = Profile({**PROFILE, "references": [a, b]})
    assert run(srv.references_page([box], both, job))
    assert "Brightwater" in Path(resolve_field(box, both, job, 1).value).read_text(encoding="utf-8")
    only_a = Profile({**PROFILE, "references": [a]})
    monkeypatch.setattr(srv, "render_pdf", broken)
    assert not run(srv.references_page([box], only_a, job))
    assert resolve_field(box, only_a, job, 1) is None  # (the old page, with B on it, never goes up)
    monkeypatch.setattr(srv, "render_pdf", fake_render)
    assert run(srv.references_page([box], only_a, job))  # (made again, with the browser back)
    page = Path(resolve_field(box, only_a, job, 1).value).read_text(encoding="utf-8")
    assert "Quillby" in page and "Brightwater" not in page
    # every reference taken out: the page made before isn't theirs to send any more
    none = Profile(PROFILE)
    assert not run(srv.references_page([box], none, job)) and resolve_field(box, none, job, 1) is None
    # nor a page written by hand that isn't the entries as they are
    (Path(job["folder"]) / "Sam_Rivera_References.md").write_text("# Sam Rivera\n\n## References\n\n**Someone**\n",
                                                                  encoding="utf-8")
    assert resolve_field(box, only_a, job, 1) is None


def test_the_stop_says_why_there_is_no_page_of_the_references_the_profile_has(srv, job_apply_home):
    """The review: with references: entries whose page couldn't be made (or ran past a page), the stop told the person
    to add references under references:, which they had. It says what happened instead."""
    from job_apply.pipeline import Applier

    data = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump({**data, "references": REFERENCES}), encoding="utf-8")
    job = srv.tracker().get(srv.add_job(url="https://example.com/jobs/7", title="Clerk", company="Example County")
                            ["job"]["id"])
    applier = Applier(srv)
    refs = {"label": "Please upload your Reference Contact Information.*", "kind": "file"}
    failed = Run(job["id"])
    assert applier._stop_for_what_is_left(failed, "", [], [refs])
    assert ("add documents.references to your profile (the desk couldn't make its page of the references in your "
            "profile just now: Carry on tries again)") in failed.reason, failed.reason
    assert "under references:" not in failed.reason
    Path(job["folder"]).mkdir(parents=True, exist_ok=True)
    (Path(job["folder"]) / "Sam_Rivera_References.too-long").touch()
    long = Run(job["id"])
    assert applier._stop_for_what_is_left(long, "", [], [refs])
    assert "the references in your profile run past one page" in long.reason, long.reason


def test_every_fill_of_the_desks_says_the_documents_it_uploaded(srv, monkeypatch):
    """The review: only the main fill said an upload of the profile's other documents in the job's log; the fills
    after a page's errors, a refill of emptied boxes, a resume put up again and an account page's fill didn't. Every
    fill goes through one place now, which says them."""
    import re

    from job_apply import pipeline
    from job_apply.pipeline import Applier

    sources = "\n".join(p.read_text(encoding="utf-8")
                        for p in sorted(Path(pipeline.__file__).parent.glob("pipeline*.py")))
    assert re.findall(r"\bsrv\.autofill\(", sources) == ["srv.autofill("], "the desk fills only in _profile_fill"
    applier = Applier(srv)

    async def fake_autofill(job_id=None, **how):
        return {"filled": [{"id": "f", "label": "Unofficial Transcripts.", "value": ["/x/a.pdf", "/x/b.pdf"],
                            "from": "documents.transcripts", "uploaded": True},
                           {"id": "r", "label": "Resume", "value": "/x/r.pdf", "from": "documents.resume",
                            "uploaded": True}], "failed": [], "needs_input": [], "how": how}

    monkeypatch.setattr(srv, "autofill", fake_autofill)
    r = Run(3)
    result = run(applier._profile_fill(r, redo=["f"]))
    assert result["how"] == {"redo": ["f"]}
    assert r.log == ["uploaded your transcripts (2 files) to “Unofficial Transcripts”"], r.log


def test_the_references_page_never_makes_a_list_quote_or_heading_of_the_persons_words():
    prof = Profile({**PROFILE, "references": [{"name": "Kim Lee", "relationship": "- coworker", "company": "x"},
                                              {"name": "Pat Doe", "relationship": "> manager"},
                                              {"name": "Lou Roe", "relationship": "# 1 fan"},
                                              {"name": "Ann Poe", "relationship": "1. supervisor"}]})
    page = render.to_html(render.references_markdown(prof), "references", title="x")
    assert "<li>" not in page and "<blockquote>" not in page and page.count("<h1>") == 1, page
    for said in ("- coworker", "&gt; manager", "# 1 fan", "1. supervisor"):
        assert said in page, (said, page)


def test_a_reference_is_taken_out_by_name_and_a_pages_own_words_and_workings_stay(job_apply_home):
    """The review: each word of a reference's name came out of every word and attribute of a page, any case:
    data-automation-id="formField-…" became "formREDACTED-…" (and broke reading the page again), "street_address"
    "REDACTED_address", and "May", "Will" and "Grace" went from a page's sentences. A reference's whole name goes (as
    written, any case, titles or not); the surname alone only as written, in the page's words, and never an everyday
    word; a word of the name only as a box's whole value ("Jo"); and a reference's employer like "Retired" stays."""
    prof = Profile({"personal": {"first_name": "Sam", "last_name": "Rivera"},
                    "references": [{"name": "Dr. Grace Field", "company": "Retired"},
                                   {"name": "Will May", "phone": "602-555-0177"},
                                   {"name": "Jo Street"}, {"name": "Morgan Quillby", "company": "Tarnwell Systems"}]})
    secrets = personal_strings(prof)
    page = ('<html><body><div data-automation-id="formField-legalNameSection_firstName" id="quillby-1" '
            'class="css-field"><label for="i1">Street Address</label><input id="i1" name="street_address">'
            '<p>This field is required. You will be contacted in May. Grace period applies. Will you relocate?</p>'
            '<p>Dr. Grace Field and grace field, Will May, morgan quillby and Quillby wrote; quillby is not a name.</p>'
            '<span class="pill">Jo</span><p>Jo is short for Joanne. Job</p><span title="Quillby">x</span>'
            '<button data-automation-id="pageFooterNextButton">Next</button></div></body></html>')
    out = clean_html(page, secrets)
    for kept in ('data-automation-id="formField-legalNameSection_firstName"', 'name="street_address"',
                 'id="quillby-1"', "Street Address", "This field is required", "You will be contacted in May",
                 "Grace period applies", "Will you relocate?", "quillby is not a name", "Jo is short for Joanne. Job",
                 'data-automation-id="pageFooterNextButton"'):
        assert kept in out, (kept, out)
    for gone in ("Grace Field", "grace field", "Will May", "morgan quillby", "Quillby wrote", 'title="Quillby"',
                 ">Jo<"):
        assert gone not in out, (gone, out)
    said = report._scrubber(report.report_strings(prof))
    assert said("Retired from Tarnwell Systems. We will call you.") == "Retired from REDACTED. We will call you."


# The file boxes on the suite's own pages (the review's dump of every fixture with one: label, section, file boxes on
# the page, whether it takes several), but the NSHE page made for these documents
FIXTURE_FILE_BOXES = [
    ("Cover Letter", None, 2, False), ("Resume/CV *", None, 2, False), ("Resume/CV *", None, 1, False),
    ("Resume/CV*", None, 2, False), ("Upload a File * Select here to upload.", None, 1, False),
    ("Accepted file format: .pdf, .doc, .docx, . tct, .rtf", "Resume", 1, False), ("Choose File", None, 1, False),
    ("Accepted file format: doc, docx or pdf", "Resume", 2, False), ("", "Additional Personal Information", 2, False),
    ("Select the file to attach", None, 1, False), ("Accepted file format: .rtf, .txt, .doc, .docx or .pdf",
                                                    "Resume", 1, False),
    ("Import your profile from resume", None, 4, False), ("Upload Resume", None, 4, True),
    ("Upload Cover Letter", None, 4, True), ("Upload Attachment", None, 4, True),
    ("Accepted file format: .pdf, .doc or .docx", "Resume", 1, False), ("*Resume", None, 1, False),
    ("Resume", None, 1, False), ("Upload Resume", None, 1, False),
    ("Accepted file format: .pdf, .doc or .docx", None, 1, False), ("files_QuickApply", None, 1, False),
    ("File upload. Press enter to browse for a file", None, 1, False), ("Upload Resume/CV", "Upload options", 3, False),
    ("Upload Additional Attachments", None, 3, False), ("Second resume (optional)", None, 3, False),
    ("*Resume:", None, 1, False), ("*First Name:", None, 1, False), ("Cover Letter", None, 2, False),
    ("Resume", None, 2, False), ("Cover letter", None, 1, False), ("Upload a file (5MB max)*", "Resume/CV", 1, False),
    ("Upload a file (5MB max)", "Resume/CV", 1, False),
]


@pytest.mark.parametrize("label, section, files, multiple", FIXTURE_FILE_BOXES)
def test_the_suites_own_file_boxes_get_what_they_got_before(tmp_path, label, section, files, multiple):
    """None of the file boxes on the suite's pages asks for a transcript, a reference list or a certificate: with them
    all in the profile, each gets what it gets with only a resume and a cover letter."""
    prof, paths = _every(tmp_path)
    plain = Profile({"documents": {k: prof.data["documents"][k] for k in ("resume", "cover_letter")}})
    box = _box(label, section=section, **({"multiple": True} if multiple else {}))
    every, before = resolve_field(box, prof, {}, files), resolve_field(box, plain, {}, files)
    assert (every and (every.value, every.rule)) == (before and (before.value, before.rule)), (label, every, before)


@pytest.mark.parametrize("where", ["dialog", "icims"])
def test_several_files_never_go_into_a_sites_upload_dialog_or_an_icims_box(srv, tmp_path, where):
    """The review: a box that takes several files inside a site's own upload dialog (whose Upload sends one) or
    iCIMS's "<name>_File" box (which sends its form and keeps one name) was given several, and said to have them. Those
    are the person's: the fill says so, the box stays empty, and nothing is said to have gone up."""
    transcript, cert = _files(tmp_path, "transcript.pdf", "cert.pdf")
    box = ("<div role='dialog' aria-label='Upload documents'><label for='m'>Transcripts and certificates</label>"
           "<input type='file' id='m' multiple><button type='button'>Upload</button></div>" if where == "dialog" else
           "<label for='m_File'>Transcripts and certificates</label><input type='file' id='m_File' multiple>"
           "<input type='hidden' id='m_FileName' value=''>")
    page = tmp_path / "uploads.html"
    page.write_text(f"<!doctype html><html><body><form>{box}</form></body></html>", encoding="utf-8")
    run(srv.browser.goto(page.as_uri()))
    [field] = [f for f in run(srv.browser.inspect(include_dropdown_options=False))["fields"] if f["kind"] == "file"]
    [done] = run(srv.browser.fill([{"id": field["id"], "value": [str(transcript), str(cert)]}]))
    assert not done["ok"] and "one file at a time" in done["error"], done
    tab = run(srv.browser.page())
    assert run(tab.evaluate("() => document.querySelector('input[type=file]').files.length")) == 0


def test_the_replay_decides_as_the_live_run_with_its_test_transcript_and_reference(tmp_path):
    from job_apply import fixtures

    prof = fixtures.replay_profile(tmp_path)
    [transcript] = prof.document_files("transcripts")
    assert transcript.name == fixtures.REPLAY_TRANSCRIPT and prof.references()
    got = resolve_field(_box("Please upload your Unofficial Transcripts.*", required=True), prof, {}, 2)
    assert got is not None and got.value == str(transcript)


# --- the second review: words that leave a real box empty, generic certificates, the scrubber's leftovers ----------

@pytest.mark.parametrize("label, want", [
    ("Please provide a list of three professional references with their names, titles, phone numbers and email "
     "addresses.", "refs.pdf"),
    ("Professional References (3)", "refs.pdf"),
    ("Upload your list of references. Reference checks will be conducted for finalists.", "refs.pdf"),
    ("Upload your reference list. By uploading, you consent to us contacting your references.", "refs.pdf"),
    ("Upload your references using the form below", "refs.pdf"),
    ("Please upload your unofficial transcripts. Official transcripts will be required upon request.",
     "transcript.pdf"),
    ("Copy of transcripts (we may request originals later)", "transcript.pdf"),
    ("Unofficial transcripts (PDF form)", "transcript.pdf"),
    ("Please upload your transcripts with the application form", "transcript.pdf"),
    ("Upload transcripts from an accredited or authorized institution", "transcript.pdf"),
    ("Please check that your transcript is legible before uploading", "transcript.pdf"),
    ("Upload your license, required to verify eligibility", "license.pdf"),
    ("Upload your current licenses and certifications (include license numbers)", "license.pdf"),
])
def test_a_box_that_only_says_such_words_elsewhere_still_gets_its_document(tmp_path, label, want):
    """The second review: the words of a form to sign, a number or eligibility, read anywhere in a box's words, left
    real boxes empty ("…phone numbers and email addresses", "Reference checks will be conducted", "you consent to us
    contacting your references", "upon request", "PDF form", "verify eligibility", "include license numbers"). They
    keep a box from these documents only beside the document's word ("Reference Release Form", "Transcript request
    form", "Certificate Number"), or as what a proof is of ("Certificate of Citizenship")."""
    paths = {p.name: str(p) for p in _files(tmp_path, "transcript.pdf", "refs.pdf", "license.pdf", "cert.pdf")}
    prof = Profile({"documents": {"transcripts": paths["transcript.pdf"], "references": paths["refs.pdf"],
                                  "certificates": [paths["license.pdf"], paths["cert.pdf"]]}})
    got = resolve_field(_box(label), prof, {}, 2)
    assert got is not None and got.value == paths[want], (label, got)


@pytest.mark.parametrize("label", ["Upload certifications listed in your application",
                                   "Please attach any certificates that support your application",
                                   "Industry certifications (e.g., CompTIA A+)", "Upload any certifications",
                                   "Professional certifications (if applicable)", "Certificates of completion"])
def test_a_certificate_box_naming_none_in_particular_gets_the_certificates(tmp_path, label):
    """The second review: any word in a box's label was read as naming a particular certificate ("listed", "support",
    "CompTIA" in an example), so these boxes got nothing. Only the words right before the certificate's word name one
    ("Food Handler Certificate", "IPC-A-610 certification"). A box that takes several gets every certificate (a file
    whose name says it's one, or says no kind); one that takes one, a file whose name says it's one, else the one whose
    name says nothing, if it's the only such."""
    named, osha, forklift = _files(tmp_path, "forklift-certificate.pdf", "OSHA30.pdf", "Forklift.pdf")

    def given(certificates: list[Path], multiple: bool = False) -> str | list[str] | None:
        prof = Profile({"documents": {"certificates": [str(p) for p in certificates]}})
        got = resolve_field(_box(label, **({"multiple": True} if multiple else {})), prof, {}, 2)
        return None if got is None else got.value

    assert given([named]) == str(named) and given([osha]) == str(osha)
    assert given([osha, forklift]) is None  # (two whose names say nothing, in a box that takes one: the person's)
    assert given([osha, forklift], multiple=True) == [str(osha), str(forklift)]
    assert given([osha, named]) == str(named) and given([osha, named], multiple=True) == [str(named), str(osha)]


def test_a_box_naming_a_particular_certificate_takes_only_a_file_that_says_so(tmp_path):
    ipc, comptia, forklift = _files(tmp_path, "IPC_certificate.pdf", "CompTIA_A+.pdf", "forklift-certificate.pdf")
    prof = Profile({"documents": {"certificates": [str(forklift), str(ipc), str(comptia)]}})
    for label, want in (("IPC-A-610 certification", ipc), ("CompTIA A+ certification", comptia),
                        ("Copy of your Forklift Certificate", forklift)):
        got = resolve_field(_box(label), prof, {}, 2)
        assert got is not None and got.value == str(want), (label, got)
    assert resolve_field(_box("Food Handler Certificate"), prof, {}, 2) is None


def test_a_box_naming_a_document_the_profile_has_is_never_said_to_have_no_place(srv, job_apply_home, tmp_path):
    """The second review: a required box whose words name the reference list but that the desk doesn't give it to (an
    "Attach" under "Reference Information", a "Reference Release Form") was said to want "a file your profile has no
    place for", with documents.references set. It says the desk doesn't attach it by itself; and, with nothing for it
    in the profile, as before."""
    from job_apply.pipeline import Applier

    applier = Applier(srv)
    boxes = [{"label": "Attach", "section": "Reference Information", "kind": "file"},
             {"label": "Reference Release Form*", "kind": "file"}]
    bare = Run(1)
    assert applier._stop_for_what_is_left(bare, "", [], boxes)
    assert bare.reason.startswith("The form wants a file your profile has no place for: “Attach” and “Reference "
                                  "Release Form”. Attach them"), bare.reason
    refs = _files(tmp_path, "refs.pdf")[0]
    data = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    data["documents"]["references"] = str(refs)
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    held = Run(2)
    assert applier._stop_for_what_is_left(held, "", [], boxes)
    assert held.reason.startswith("The form wants a file the desk doesn't attach by itself: “Attach” and “Reference "
                                  "Release Form”. Attach them"), held.reason
    assert "no place" not in held.reason and "documents.resume" not in held.reason


def test_doctor_says_to_name_certificates_for_what_they_are(job_apply_home, tmp_path):
    named, osha, scan = _files(tmp_path, "forklift-certificate.pdf", "OSHA30.pdf", "scan2.pdf")
    data = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    assert [c.name for c in doctor.check_documents(Profile({**data, "documents": {"certificates": [str(named)]}}))] \
        == []
    [said] = doctor.check_documents(Profile({**data, "documents": {"certificates": [str(named), str(osha),
                                                                                    str(scan)]}}))
    assert said.name == "certificate_names" and said.status == doctor.ADVICE and "2 certificate files" in said.say
    assert "OSHA" not in said.say + said.fix and "scan2" not in said.say + said.fix  # (never a file's name)
    assert "diploma, license or certificate" in said.fix


def test_a_references_surname_in_capitals_goes_and_a_lists_choices_stay(job_apply_home):
    """The second review's leftovers: a surname alone in capitals ("QUILLBY, MORGAN") stayed; and a reference's first
    name that is a list's choice too (a month, a country: "May", "Jordan") came out of the page's lists as their whole
    value."""
    prof = Profile({"references": [{"name": "Morgan Quillby"}, {"name": "May Jordan"}]})
    out = clean_html("<html><body><p>QUILLBY, MORGAN</p><select><option>May</option><option>June</option></select>"
                     "<select><option>Jordan</option><option>Kenya</option></select><p>May Jordan</p>"
                     "<span>Quillby</span></body></html>", personal_strings(prof))
    assert "QUILLBY" not in out and ">Quillby<" not in out and "May Jordan" not in out, out
    assert "<option>May</option>" in out and "<option>Jordan</option>" in out, out


# --- the third review: proofs and forms that came back with the loosened words, a certificate "of" something --------

# A proof, a form, a letter or a number: never one of the profile's documents (the third review's probe, excl3.py)
NOT_THEIRS = [
    "Reference Form", "Release of references", "Authorization to contact references",
    "References: please sign the attached release", "Transcript Request Form (signed)", "Official transcript request",
    "Teaching license verification", "Proof of certification of eligibility", "Birth certificate copy",
    "Consent to contact references", "Signed authorization to release transcripts",
    "Authorization for release of transcripts", "Reference Release Form", "Signed Reference Waiver",
    "Reference Authorization Form", "Reference Questionnaire", "Reference check consent form",
    "Transcript request form", "Transcript release authorization", "Letter(s) of Reference", "Letters of Reference",
    "Reference letters (2)",
    "Letters from your references", "Letter of recommendation", "Job reference", "Job Reference Number",
    "Reference ID", "Professional License Number", "Certificate Number", "Naturalization Certificate",
    "Certificate of Citizenship", "Certificate of Indian Blood (CIB)", "Tribal Enrollment Certificate",
    "Schedule A certification letter", "Disability Certificate", "Disability certification for Schedule A",
    "Marriage certificate", "Medical certificate", "Certificate of Insurance",
    "Certification of disability (VA rating letter)",
    "Proof of citizenship (birth certificate, passport, or naturalization certificate)",
    "IVP fingerprint card or certificate", "Fingerprint clearance card", "Copy of CDL license",
    "Copy of Driver License",
    "Driver's License (front and back)", "DD-214 (Certificate of Release or Discharge from Active Duty)",
    "Veterans' Preference Certificate", "Photo ID", "Upload copy of your Social Security card or birth certificate",
    "Certificate of eligibility", "Eligibility certificate",
    "Proof of eligibility to work (certificate of naturalization)",
    "Employment eligibility verification", "Upload your naturalization papers", "Citizenship documentation",
    "Certificate of Indian Blood", "CIB certificate", "Tribal ID or Certificate of Degree of Indian Blood",
    "Certificate of Degree of Indian Blood (CDIB)", "Indian Preference certification",
    "Native American Preference: certificate", "Proof of tribal membership (certificate)", "Death certificate",
    "Medical clearance certificate", "Security clearance certification", "Insurance card", "Reference Form (signed)",
    "Please complete and upload the reference form", "Upload the signed reference check form",
    "Transcript authorization",
    "Request transcripts", "Signed release for references", "Reference verification form",
    "Waiver of right to review references", "Reference Questionnaires (2)", "Reference Forms", "Reference Waivers",
    "Reference Checks", "Transcript Authorization Form", "Transcript Requests", "Certificate of Vaccination",
    "Certificate of Immunization", "Certificate of Good Standing", "TB test certificate", "Drug screen certificate",
    "Background check certificate", "Police clearance certificate",
]
# ...and boxes that ask for one, whatever else they say (the probe's)
THEIRS = [
    "Please upload a list of three professional references with their names, titles, phone numbers and email "
    "addresses.",
    "Please upload a list of three professional references. Reference checks will be conducted for finalists.",
    "Upload your reference list. By uploading, you consent to us contacting your references.",
    "Please upload your unofficial transcripts. Official transcripts will be required upon request.",
    "Copy of transcripts (we may request originals later)", "Unofficial transcripts (PDF form)",
    "Applicants must submit transcripts with the application form",
    "Unofficial transcripts are required to verify eligibility for this position",
    "Upload your current licenses and certifications (include license numbers and expiration dates)",
    "Please check that your transcript is legible before uploading", "Upload your references using the form below",
    "Professional References (3)", "References (3 required)",
    "Upload transcripts from an accredited or authorized institution", "Teaching certificate", "Nursing license",
    "Certified Medical Assistant (CMA) certificate", "Life and Health Insurance License",
    "Please upload your Unofficial Transcripts.", "Please upload your Reference Contact Information.",
    "If available, please attach a copy of your transcript(s), diploma(s), and/or certificate(s) here.",
    "Copy of your teaching certificate or certification eligibility", "High school diploma or GED",
    "Transcripts (ID number must be visible)",
    "References (we will request your consent before contacting them)",
    "Reference list (we check references for finalists only)",
]


@pytest.mark.parametrize("label", NOT_THEIRS)
def test_a_proof_a_form_or_a_number_never_gets_these(tmp_path, label):
    """The third review: with the exclusions tied to the document's word, proofs of tribal enrollment came back
    ("Certificate of Degree of Indian Blood (CDIB)" got diploma.pdf, or all three in a box that takes several; "CIB
    certificate", "Indian Preference certification"), with a vaccination's, an immunization's or good standing's
    certificate (a certificate whose name said no kind went in), and forms to sign said otherwise ("Authorization to
    contact references", "References: please sign the attached release", "Reference Forms", "Reference Checks",
    "Transcript Requests")."""
    assert autofill.documents_asked(_box(label)) == [], label
    files = {p.name: str(p) for p in _files(tmp_path, "transcript.pdf", "refs.pdf", "cert.pdf", "diploma.pdf",
                                            "OSHA30.pdf", "license.pdf")}
    prof = Profile({"documents": {"transcripts": files["transcript.pdf"], "references": files["refs.pdf"],
                                  "certificates": [files[n] for n in ("cert.pdf", "diploma.pdf", "OSHA30.pdf",
                                                                      "license.pdf")]}})
    ours = {files[n] for n in ("transcript.pdf", "refs.pdf", "cert.pdf", "diploma.pdf", "OSHA30.pdf", "license.pdf")}
    for multiple in (False, True):
        got = resolve_field(_box(label, **({"multiple": True} if multiple else {})), prof, {}, 2)
        given = [] if got is None else got.value if isinstance(got.value, list) else [got.value]
        assert not set(given) & ours, (label, multiple, got)


@pytest.mark.parametrize("label", THEIRS)
def test_a_box_asking_for_these_still_names_them(label):
    assert autofill.documents_asked(_box(label)), label


def test_a_certificate_of_something_particular_takes_only_a_file_named_for_it(tmp_path):
    """The third review: "Certificate of X" names X ("Certificate of Achievement in Welding": welding), as "X
    Certificate" does; a certificate of something general ("Certificate of Completion") names nothing in particular.
    And a degree is the diploma kind only as the box's subject ("Copy of degree", "Degree certificate")."""
    welding, osha, cert, diploma = _files(tmp_path, "welding-certificate.pdf", "OSHA30.pdf", "cert.pdf",
                                          "diploma.pdf")

    def given(label: str, certificates: list[Path]) -> str | None:
        got = resolve_field(_box(label), Profile({"documents": {"certificates": [str(p) for p in certificates]}}),
                            {}, 2)
        return None if got is None else str(got.value)

    assert given("Certificate of Achievement in Welding", [osha, welding]) == str(welding)
    assert given("Certificate of Achievement in Welding", [osha]) is None
    assert given("Certificate of Completion", [osha]) == str(osha)
    assert given("Copy of degree", [cert, diploma]) == str(diploma)
    assert given("Degree certificate", [cert, diploma]) == str(diploma)


def test_a_references_one_word_name_that_is_a_lists_choice_leaves_the_list_alone(job_apply_home):
    """The third review: a reference named with one word that is a state's or a country's ("Georgia") came out of every
    "Georgia" on a page, its State list's choice too."""
    out = clean_html("<html><body><select><option>Georgia</option><option>Arizona</option></select>"
                     "<p>Georgia is lovely</p></body></html>",
                     personal_strings(Profile({"references": [{"name": "Georgia"}]})))
    assert "<option>Georgia</option>" in out and "Georgia is lovely" in out, out


# --- the fourth review: a diploma's box that also says "certificate", military transcripts, a name surname first ----

DIPLOMA_CERTIFICATES = ["High school diploma or GED certificate", "Copy of high school diploma or GED certificate",
                        "Diploma/GED certificate", "High School Diploma/GED Certificate (required)", "GED certificate",
                        "Diploma certificate", "Degree certificate", "Degree certification", "Certificate of Degree",
                        "Certification of degree completion", "Diploma or certificate of high school equivalency"]


@pytest.mark.parametrize("label", DIPLOMA_CERTIFICATES)
def test_a_diplomas_box_that_says_certificate_takes_only_a_diploma(tmp_path, label):
    """The fourth review: a certificate whose name says no kind (OSHA30.pdf, CPR_card.pdf) went into "High school
    diploma or GED certificate" (the most common label of all), "GED certificate", "Certificate of Degree"; and with
    [forklift-certificate.pdf, diploma.pdf], "Certificate of Degree" got the forklift certificate, and a box that takes
    several got both. "Certificate" beside a diploma's word is that diploma's: only a file named for a diploma goes."""
    forklift, diploma, osha, cpr = _files(tmp_path, "forklift-certificate.pdf", "diploma.pdf", "OSHA30.pdf",
                                          "CPR_card.pdf")

    def given(certificates: list[Path], multiple: bool) -> str | list[str] | None:
        prof = Profile({"documents": {"certificates": [str(p) for p in certificates]}})
        got = resolve_field(_box(label, **({"multiple": True} if multiple else {})), prof, {}, 2)
        return None if got is None else got.value

    for multiple in (False, True):
        assert given([osha], multiple) is None and given([cpr], multiple) is None, (label, multiple)
        assert given([forklift, osha], multiple) is None, (label, multiple)
        assert given([forklift, diploma, osha], multiple) == str(diploma), (label, multiple)


def test_a_box_naming_a_diploma_or_a_license_and_a_certificate_takes_a_certificate_only_beside_one(tmp_path):
    """"Degree or certificate" names both: a box that takes one file gets only a diploma; one that takes several, the
    diploma and the files named as certificates, never one whose name says no kind. So with a license and a
    certificate ("Professional license or certification")."""
    forklift, diploma, osha, license_ = _files(tmp_path, "forklift-certificate.pdf", "diploma.pdf", "OSHA30.pdf",
                                               "nursing-license.pdf")

    def given(label: str, certificates: list[Path], multiple: bool = False) -> str | list[str] | None:
        prof = Profile({"documents": {"certificates": [str(p) for p in certificates]}})
        got = resolve_field(_box(label, **({"multiple": True} if multiple else {})), prof, {}, 2)
        return None if got is None else got.value

    assert given("Degree or certificate", [forklift, diploma, osha]) == str(diploma)
    assert given("Degree or certificate", [forklift, diploma, osha], True) == [str(diploma), str(forklift)]
    assert given("Degree or certificate", [forklift, osha]) is None
    assert given("Degree or certificate", [forklift, osha], True) == str(forklift)
    assert given("Degree or certificate", [osha], True) is None
    label = "Professional license or certification"
    assert given(label, [forklift, license_, osha]) == str(license_)
    assert given(label, [forklift, license_, osha], True) == [str(license_), str(forklift)]
    assert given(label, [osha]) is None and given(label, [osha], True) is None


@pytest.mark.parametrize("label", ["Military transcript (JST)", "Joint Services Transcript (JST)",
                                   "AARTS/CCAF transcript", "SMART transcript",
                                   "Military transcripts (JST) or college transcripts",
                                   "Military training certificates"])
def test_a_military_transcript_is_never_the_college_one(tmp_path, label):
    prof, paths = _every(tmp_path)
    assert autofill.documents_asked(_box(label)) == [], label
    got = resolve_field(_box(label), prof, {}, 2)
    assert got is None or got.value not in (paths["transcript.pdf"], paths["cert.pdf"], paths["diploma.pdf"]), got


def test_a_references_name_written_surname_first_is_taken_out_too(job_apply_home):
    """The fourth review: a reference's surname that is a place ("Denise Washington") is never taken out alone, so a
    review page's "Washington, Denise" kept the whole name. The name surname first goes too, any case."""
    out = clean_html("<html><body><p>Washington, Denise</p><p>WASHINGTON DENISE</p><p>washington denise</p>"
                     "<select><option>Washington</option><option>Oregon</option></select></body></html>",
                     personal_strings(Profile({"references": [{"name": "Denise Washington"}]})))
    assert "Denise" not in out and "DENISE" not in out and "denise" not in out, out
    assert "<option>Washington</option>" in out, out


@pytest.mark.parametrize("name, kind", [
    ("GED_2015.pdf", "diploma"), ("diploma2019.pdf", "diploma"), ("BS_degree.pdf", "diploma"),
    ("nursing-license.pdf", "license"), ("Licensure_RN.pdf", "license"), ("forklift-certificate.pdf", "certificate"),
    ("CPR_cert.pdf", "certificate"), ("acknowledged_cert.pdf", "certificate"),
    ("managed-services-certificate.pdf", "certificate"), ("Rugged_terrain_cert.pdf", "certificate"),
    ("Acknowledged_training.pdf", None), ("diplomat_award.pdf", None), ("Concert.pdf", None),
    ("unlicensed_notes.pdf", None),
])
def test_a_files_name_says_its_kind_only_in_whole_words(tmp_path, name, kind):
    """The fifth review: a kind's word was read anywhere in a file's name, so managed-services-certificate.pdf,
    Acknowledged_training.pdf, Rugged_terrain_cert.pdf ("ged") and diplomat_award.pdf ("diploma") read as diplomas and
    went into "High school diploma or GED certificate", and Concert.pdf as a certificate. Only as whole words (digits
    beside them are fine: GED_2015.pdf, diploma2019.pdf)."""
    path = _files(tmp_path, name)[0]
    kinds = {"diploma": "High school diploma or GED certificate", "license": "Professional License",
             "certificate": "Certificates"}
    prof = Profile({"documents": {"certificates": [str(path), str(_files(tmp_path, "other-scan.pdf")[0])]}})
    for asked, label in kinds.items():
        got = resolve_field(_box(label), prof, {}, 2)
        assert (got is not None and got.value == str(path)) == (asked == kind), (name, label, got)
    assert autofill.certificate_kind_unsaid(path) == (kind is None), name
