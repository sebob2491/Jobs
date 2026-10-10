import asyncio
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
    assert Path(out["source"]).read_text(encoding="utf-8") == RESUME

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
    # the long letter isn't sent, and doesn't hold the job either: only the resume's length does
    assert resolve_field(f, Profile.load(), folder_job, 2) is None
    from job_apply.pipeline import tailored_ready
    assert tailored_ready(folder_job)
    run(srv.render_document("cover_letter", "# Sam Rivera\n\nDear team,\n\nA short letter.", job_id=job["id"]))
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



def test_a_resume_is_rendered_as_written_not_as_markup():
    """Words in angle brackets stay on the page, no HTML or script from pasted text gets in,
    "#1" isn't a heading and "24*7" isn't italics; links stay links."""
    page = to_html("# Sam Rivera\n<sam@example.com> \u00b7 <https://linkedin.com/in/sam>\n\n"
                   "- #1 technician in the region\n- Supported 24*7 coverage on 3*NXE tools\n"
                   "- Know <PLC> and <HMI> well <script>alert(1)</script> <img src=https://x.example/a.png>\n"
                   "#1 in the region\n", "resume")
    body = page.split("<body>", 1)[1]
    assert "&lt;PLC&gt;" in body and "&lt;HMI&gt;" in body
    assert "<script" not in body and "<img" not in body
    assert "24*7 coverage on 3*NXE" in body and "<h1>1 in the region" not in body
    assert "<h1>Sam Rivera</h1>" in body and 'href="https://linkedin.com/in/sam"' in body and "mailto" in html_unescape(body)


def html_unescape(text):
    import html
    return html.unescape(text)


def test_a_pdf_that_fails_to_write_leaves_the_last_good_one(tmp_path, monkeypatch):
    """A full disk while writing mustn't leave a cut-off PDF under the real name."""
    import job_apply.render as render

    dest = tmp_path / "Sam_Rivera_Resume.pdf"
    dest.write_bytes(b"%PDF-1.4 the last good one %%EOF")
    real = Path.write_bytes

    def disk_full(self, data):
        if self.name.endswith(".part"):
            real(self, data[:10])
            raise OSError(28, "No space left on device")
        return real(self, data)

    class FakePage:
        async def route(self, *a, **k): pass
        async def set_content(self, *a, **k): pass
        async def pdf(self, **k): return b"%PDF-1.4 " + b"x" * 1000 + b" %%EOF"

    class FakeBrowser:
        async def new_page(self, **k): return FakePage()
        async def close(self): pass

    class FakeChromium:
        async def launch(self, **k): return FakeBrowser()

    class FakePw:
        chromium = FakeChromium()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    monkeypatch.setattr(render, "async_playwright", lambda: FakePw())
    monkeypatch.setattr(Path, "write_bytes", disk_full)
    with pytest.raises(OSError):
        asyncio.run(render.render_pdf("<p>x</p>", dest))
    assert dest.read_bytes() == b"%PDF-1.4 the last good one %%EOF"
    assert not (tmp_path / "Sam_Rivera_Resume.pdf.part").exists()


def test_a_resume_keeps_its_line_breaks_code_spans_and_drops_notes():
    page = to_html("Sam Rivera<br>Chandler, AZ\n\n<!-- tailor this for ASML -->\n"
                   "- Wrote `a < b` checks for tool interlocks\n", "resume")
    body = page.split("<body>", 1)[1]
    assert "<br" in body and "&lt;br" not in body
    assert "tailor this" not in body
    assert "<code>a &lt; b</code>" in body and "&amp;lt;" not in body


def test_a_too_long_resume_for_the_current_job_holds_it_too(srv, monkeypatch):
    """With no job_id, render_document writes to the current job's folder: the too-long
    marker goes there too, or autofill would upload the 3-page resume."""
    from job_apply.pipeline import tailored_ready

    async def fake_render(page_html, dest):
        dest.write_bytes(b"%PDF-1.4\n")
        return 3

    monkeypatch.setattr(srv, "render_pdf", fake_render)
    job = srv.add_job(url="https://example.com/jobs/3", title="FSE", company="Example Litho")["job"]
    srv.browser.current_job_id = job["id"]
    out = run(srv.render_document("resume", RESUME))
    assert "warning" in out and not tailored_ready(srv.tracker().get(job["id"]))
