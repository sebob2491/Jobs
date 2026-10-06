from pathlib import Path

import pytest
from conftest import browser_available, run

from job_apply.render import count_pages, to_html

RESUME = """# Sam Rivera
Chandler, AZ · 480-555-0123 · sam.rivera@example.com

## Experience
### Equipment Technician — Intel, Chandler AZ *Mar 2021 – Present*
- Maintained 300mm etch and deposition tools
- Led PMs on vacuum and RF subsystems

## Education
### BS Electrical Engineering — Arizona State University *2020*
"""


def test_markdown_to_html():
    page = to_html(RESUME, "resume", title="Sam Rivera Resume")
    assert "<h1>Sam Rivera</h1>" in page
    assert "<em>Mar 2021 – Present</em>" in page
    assert "<li>Maintained 300mm etch and deposition tools</li>" in page
    assert "@page" in page


@pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")
def test_render_document_into_job_folder(srv):
    job = srv.add_job(url="https://example.com/jobs/1", title="FSE", company="Example Litho")["job"]
    out = run(srv.render_document("resume", RESUME, job_id=job["id"]))
    pdf = Path(out["path"])
    assert pdf.name == "Sam_Rivera_Resume.pdf" and pdf.read_bytes().startswith(b"%PDF")
    assert out["pages"] == 1 and "warning" not in out
    assert Path(out["source"]).read_text() == RESUME

    letter = run(srv.render_document("cover_letter", "# Sam Rivera\n\n" + "Dear team,\n\n" + "Long paragraph. " * 900,
                                     job_id=job["id"]))
    assert letter["pages"] > 1 and "warning" in letter

    # the tailored files are what autofill uploads
    from job_apply.autofill import resolve_field
    from job_apply.config import Profile

    folder_job = srv.get_job(job["id"])["job"]
    f = {"id": "1", "kind": "file", "label": "Resume", "value": ""}
    assert resolve_field(f, Profile.load(), folder_job).value == str(pdf)
    f = {"id": "2", "kind": "file", "label": "Cover Letter", "value": ""}
    assert resolve_field(f, Profile.load(), folder_job, 2).value.endswith("Sam_Rivera_Cover_Letter.pdf")


def test_count_pages():
    assert count_pages(b"<< /Type /Pages /Kids [1 0 R 2 0 R] >> << /Type /Page >> << /Type/Page >>") == 2


@pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")
def test_render_default_resume_into_home(srv, job_apply_home):
    out = run(srv.render_document("resume", RESUME, default=True))
    assert Path(out["path"]) == job_apply_home / "Sam_Rivera_Resume.pdf"
    assert Path(out["path"]).read_bytes().startswith(b"%PDF")


def test_a_too_long_tailored_resume_holds_its_job(srv, monkeypatch):
    """render_document flags a tailored resume over 2 pages, so the desk doesn't send it
    while Claude tightens it; the next render, within the limit, clears the flag."""
    from job_apply.pipeline import tailored_ready

    pages = iter([3, 1])

    async def fake_render(page_html, dest):
        dest.write_bytes(b"%PDF-1.4\n")
        return next(pages)

    monkeypatch.setattr(srv, "render_pdf", fake_render)
    job = srv.add_job(url="https://example.com/jobs/2", title="FSE", company="Example Litho")["job"]
    out = run(srv.render_document("resume", RESUME, job_id=job["id"]))
    assert "warning" in out and not tailored_ready(srv.tracker().get(job["id"]))
    out = run(srv.render_document("resume", RESUME, job_id=job["id"]))
    assert "warning" not in out and tailored_ready(srv.tracker().get(job["id"]))

