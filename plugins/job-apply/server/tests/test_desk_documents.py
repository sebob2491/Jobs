"""The Settings page's place for the profile's other documents (Resume and documents): /api/profile/document takes one
file at a time for documents.transcripts, .references and .certificates, or takes one out of the list; the state's
document names list them. Made-up files and names only: the repo is public."""

import base64
import io
import json
import zipfile
from pathlib import Path

import pytest
import yaml
from test_desk_settings import _calls, _desk
from test_documents import _box

from job_apply import autofill, config, profile_setup
from job_apply.autofill import resolve_field

PDF = b"%PDF-1.4\n% a made-up certificate\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"made up"
JPG = b"\xff\xd8\xff\xe0" + b"made up"
KINDS = ("transcripts", "references", "certificates")
COMMENTED = """# My profile: made-up
personal:
  first_name: Sam   # (made up)
  last_name: Rivera
documents:
  # the resume, first
  resume: {resume}
  cover_letter: ""   # none yet
  certificates: ["{old}"]   # my own
preferences:
  titles: [Technician]
"""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _docx() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(zipfile.ZipInfo("word/document.xml", (2026, 1, 1, 0, 0, 0)), "<w:document/>")
    return out.getvalue()


def _add(kind: str, name: str, raw: bytes = PDF) -> tuple[str, str, dict]:
    return ("POST", "/api/profile/document", {"kind": kind, "name": name, "data": _b64(raw)})


def _names(srv) -> dict:
    return _calls(_desk(srv), [("GET", "/api/state", None)])[0][1]["profile"]["documents"]


def _written(home: Path) -> dict:
    return yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8"))["documents"]


def test_one_file_a_kind_is_added_and_read_back_by_name_and_by_the_profile(srv, job_apply_home):
    asks = [_add("transcripts", "Example-Transcript.pdf"), _add("references", "Example-References.docx", _docx()),
            _add("certificates", "Example-Certificate.png", PNG)]
    said = _calls(_desk(srv), asks)
    assert [s for s, _ in said] == [200, 200, 200], said
    assert said[2][1]["documents"] == {"resume": "resume.pdf", "cover_letter": None,
                                       "transcripts": ["Example-Transcript.pdf"],
                                       "references": ["Example-References.docx"],
                                       "certificates": ["Example-Certificate.png"]}
    assert _names(srv) == said[2][1]["documents"]
    prof = config.Profile.load()
    for kind, name in zip(KINDS, ("Example-Transcript.pdf", "Example-References.docx", "Example-Certificate.png")):
        assert [p.name for p in prof.document_files(kind)] == [name]
        assert prof.document_files(kind)[0].parent == job_apply_home / "documents" / kind
        assert isinstance(_written(job_apply_home)[kind], list)
    assert (job_apply_home / "documents" / "certificates" / "Example-Certificate.png").read_bytes() == PNG
    assert str(job_apply_home) not in json.dumps(said)  # (names, never paths)
    assert (job_apply_home / "resume.pdf").read_bytes().startswith(b"%PDF")


def test_a_second_file_is_added_beside_the_first_and_the_same_name_replaces_it(srv, job_apply_home):
    _calls(_desk(srv), [_add("certificates", "Example-Diploma.pdf")])
    _calls(_desk(srv), [_add("certificates", "Example-License.jpg", JPG)])
    assert _names(srv)["certificates"] == ["Example-License.jpg", "Example-Diploma.pdf"]  # (the newest first)
    newer = PDF + b"newer"
    said = _calls(_desk(srv), [_add("certificates", "Example-Diploma.pdf", newer)])
    assert said[0][0] == 200
    assert _names(srv)["certificates"] == ["Example-License.jpg", "Example-Diploma.pdf"]  # (not twice)
    assert (job_apply_home / "documents" / "certificates" / "Example-Diploma.pdf").read_bytes() == newer
    assert len(_written(job_apply_home)["certificates"]) == 2


def test_an_eleventh_file_is_refused_with_a_sentence_and_a_replacement_is_not_a_new_file(srv, job_apply_home):
    _calls(_desk(srv), [_add("transcripts", f"Example-{n}.pdf") for n in range(10)])
    assert len(_names(srv)["transcripts"]) == 10
    before = (job_apply_home / "profile.yaml").read_bytes()
    said = _calls(_desk(srv), [_add("transcripts", "Example-Eleventh.pdf")])
    assert said == [(400, {"error": "Your transcripts already has 10 files, the most the desk keeps: remove one "
                                    "first"})]
    assert (job_apply_home / "profile.yaml").read_bytes() == before
    assert not (job_apply_home / "documents" / "transcripts" / "Example-Eleventh.pdf").exists()
    assert _calls(_desk(srv), [_add("transcripts", "Example-3.pdf", PDF + b"x")])[0][0] == 200  # (one of the ten)


@pytest.mark.parametrize("body, said", [
    (_add("certificates", "notes.txt", b"hello")[2], "Pick your certificates as a PDF, Word (.docx), JPG or PNG file"),
    (_add("certificates", "tool.exe")[2], "Pick your certificates as a PDF, Word (.docx), JPG or PNG file"),
    (_add("certificates", "no-type")[2], "Pick your certificates as a PDF, Word (.docx), JPG or PNG file"),
    (_add("transcripts", "a.pdf", b"MZ not a pdf")[2], "That file isn't a PDF inside: pick the file itself, or save it "
                                                       "as a PDF"),
    (_add("transcripts", "a.png", PDF)[2], "That file isn't a PNG inside: pick the file itself, or save it as a PDF"),
    (_add("transcripts", "a.jpeg", PDF)[2], "That file isn't a JPEG inside: pick the file itself, or save it as a "
                                            "PDF"),
    (_add("transcripts", "a.docx", b"PK not a zip")[2],
     "That file isn't a Word document inside: save it as .docx or PDF and pick that"),
    (_add("transcripts", "a.pdf", b"")[2], "That file is empty"),
    ({"kind": "transcripts", "name": "a.pdf", "data": "%%% not base64"},
     "The file didn't come through whole: pick it again"),
    ({"kind": "diplomas", "name": "a.pdf", "data": _b64(PDF)}, "That kind of document isn't saved here."),
    ({"kind": "transcripts", "remove": True}, "Say which file to remove."),
    ({"kind": "transcripts", "remove": "Nothing-Here.pdf"}, "That file isn't in the list any more."),
])
def test_a_refused_file_says_why_and_changes_nothing(srv, job_apply_home, body, said):
    before = (job_apply_home / "profile.yaml").read_bytes()
    assert _calls(_desk(srv), [("POST", "/api/profile/document", body)]) == [(400, {"error": said})]
    assert (job_apply_home / "profile.yaml").read_bytes() == before
    assert not (job_apply_home / "documents").exists()


def test_a_file_too_big_is_refused(srv, job_apply_home, monkeypatch):
    monkeypatch.setattr(profile_setup, "RESUME_MOST", 64)
    said = _calls(_desk(srv), [_add("references", "a.pdf", PDF + b"x" * 64)])
    assert said == [(400, {"error": "That file is bigger than 0 MB: pick a smaller copy"})]


def test_a_name_with_a_path_in_it_is_made_safe(srv, job_apply_home):
    for name in ("../../evil/Example-Cert.pdf", "C:\\Users\\someone\\Example-Cert2.pdf", ".hidden.pdf",
                 "we<ird>:na|me?.PDF"):
        said = _calls(_desk(srv), [_add("certificates", name)])
        assert said[0][0] == 200, (name, said)
    saved = sorted(p.name for p in (job_apply_home / "documents" / "certificates").iterdir())
    assert saved == ["Example-Cert.pdf", "Example-Cert2.pdf", "hidden.pdf", "we_ird_na_me_.pdf"], saved
    assert sorted(job_apply_home.parent.rglob("Example-Cert*.pdf")) == [
        job_apply_home / "documents" / "certificates" / "Example-Cert.pdf",
        job_apply_home / "documents" / "certificates" / "Example-Cert2.pdf"]
    assert not any(p.name == "evil" for p in job_apply_home.parent.rglob("*"))


def test_a_file_named_for_the_resume_never_replaces_the_resume(srv, job_apply_home):
    before = (job_apply_home / "resume.pdf").read_bytes()
    _calls(_desk(srv), [_add("transcripts", "resume.pdf", PDF + b"other")])
    assert (job_apply_home / "resume.pdf").read_bytes() == before
    assert (job_apply_home / "documents" / "transcripts" / "resume.pdf").read_bytes() == PDF + b"other"


def test_remove_takes_the_file_out_of_the_list_and_leaves_it_on_disk(srv, job_apply_home):
    _calls(_desk(srv), [_add("references", "Example-A.pdf"), _add("references", "Example-B.pdf")])
    said = _calls(_desk(srv), [("POST", "/api/profile/document", {"kind": "references", "remove": "Example-B.pdf"})])
    assert said[0][0] == 200 and said[0][1]["documents"]["references"] == ["Example-A.pdf"]
    assert _names(srv)["references"] == ["Example-A.pdf"]
    assert (job_apply_home / "documents" / "references" / "Example-B.pdf").read_bytes() == PDF
    assert len(_written(job_apply_home)["references"]) == 1
    _calls(_desk(srv), [("POST", "/api/profile/document", {"kind": "references", "remove": "Example-A.pdf"})])
    assert _written(job_apply_home)["references"] == [] and _names(srv)["references"] == []
    assert (job_apply_home / "documents" / "references" / "Example-A.pdf").exists()


def test_the_profiles_comments_and_other_keys_survive_and_the_list_is_written_in_place(srv, job_apply_home):
    old = job_apply_home / "old-diploma.pdf"
    old.write_bytes(PDF)
    path = job_apply_home / "profile.yaml"
    path.write_text(COMMENTED.format(resume=job_apply_home / "resume.pdf", old=old.as_posix()), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    _calls(_desk(srv), [_add("certificates", "Example-Certificate.pdf"), _add("transcripts", "Example-T.pdf")])
    after = path.read_text(encoding="utf-8")
    for kept in ("# My profile: made-up", "first_name: Sam   # (made up)", "  # the resume, first",
                 'cover_letter: ""   # none yet', "preferences:\n  titles: [Technician]", "   # my own"):
        assert kept in after, kept
    # (only the two lines of the lists changed or were added: nothing else was rewritten)
    assert set(before.splitlines()) - set(after.splitlines()) == {f'  certificates: ["{old.as_posix()}"]   # my own'}
    data = yaml.safe_load(after)
    assert [Path(p).name for p in data["documents"]["certificates"]] == ["Example-Certificate.pdf", "old-diploma.pdf"]
    assert [Path(p).name for p in data["documents"]["transcripts"]] == ["Example-T.pdf"]
    assert data["preferences"] == {"titles": ["Technician"]} and data["personal"]["first_name"] == "Sam"
    # a file the person named themselves is kept by add, and taken out by its name
    _calls(_desk(srv), [("POST", "/api/profile/document", {"kind": "certificates", "remove": "old-diploma.pdf"})])
    assert [Path(p).name for p in _written(job_apply_home)["certificates"]] == ["Example-Certificate.pdf"]
    assert old.exists() and "# My profile: made-up" in path.read_text(encoding="utf-8")


def test_the_route_needs_the_pages_key_and_a_profile(srv, job_apply_home):
    asks = [_add("certificates", "a.pdf"), ("POST", "/api/profile/document", {"kind": "certificates", "remove": "a"})]
    before = (job_apply_home / "profile.yaml").read_bytes()
    assert [s for s, _ in _calls(_desk(srv), asks, token=False)] == [403, 403]
    assert (job_apply_home / "profile.yaml").read_bytes() == before and not (job_apply_home / "documents").exists()
    for ask in asks:
        said = _calls(_desk(srv), [ask], started=lambda: (job_apply_home / "profile.yaml").unlink(missing_ok=True))
        assert said == [(400, {"error": "Set up your profile first."})], said


def test_a_box_asking_for_a_certificate_gets_the_file_added_through_the_route(srv, job_apply_home):
    """The existing autofill path (documents.certificates) attaches what the page added: a box for certificates
    gets the file; a box for a transcript gets none while only a certificate is there."""
    assert resolve_field(_box("Certificates"), config.Profile.load(), {}, 3) is None  # (nothing yet)
    said = _calls(_desk(srv), [_add("certificates", "Example-Certificate.pdf")])
    assert said[0][0] == 200
    target = job_apply_home / "documents" / "certificates" / "Example-Certificate.pdf"
    prof = config.Profile.load()
    got = resolve_field(_box("Certificates"), prof, {}, 3)
    assert (got.value, got.rule) == (str(target), "documents.certificates")
    assert autofill.documents_asked(_box("Certificates")) == ["certificates"]
    assert resolve_field(_box("Unofficial Transcripts"), prof, {}, 3) is None
    assert prof.missing_documents() == []


def test_the_stop_for_a_missing_certificate_points_to_the_page(srv, job_apply_home):
    from job_apply.pipeline import Applier, Run

    box = {"label": "Please attach your certificate", "kind": "file"}
    applier = Applier(srv)
    stopped = Run(1)
    assert applier._stop_for_what_is_left(stopped, "", [], [box])
    assert stopped.reason == ("The form wants your certificates (“Please attach your certificate”). Add it in "
                              "Settings, under Resume and documents, or attach it in the desk's browser window, "
                              "then press Carry on."), stopped.reason


def test_the_stop_says_the_same_on_a_desk_on_its_own_and_inside_claude(srv, job_apply_home):
    from job_apply.pipeline import Applier, Run

    box = {"label": "If available, please attach a copy of your transcript(s), diploma(s), and/or certificate(s) "
                    "here.*", "kind": "file"}
    said = []
    for standalone in (False, True):
        applier = Applier(srv)
        applier.set_standalone(standalone)
        run_ = Run(1)
        assert applier._stop_for_what_is_left(run_, "", [], [box])
        said.append(run_.reason)
    assert said[0] == said[1] and "Add it in Settings, under Resume and documents, or attach it in the desk's " \
        "browser window, then press Carry on." in said[0], said
    assert "ask Claude" not in said[0] and "documents." not in said[0]
