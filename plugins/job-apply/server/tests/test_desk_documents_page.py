"""The Settings page's Resume and documents panel: under the usual cover letter, a row each for the transcripts, the
reference list and the certificates and licenses, each listing its files' names with a small Remove and an Add a file
button. Each test opens the real page in a browser, with the desk's real routes. Made-up files only: the repo is
public."""

import base64
import json

import pytest
from conftest import browser_available, open_desk, person_browser, run
from test_desk_buttons import _idle_desk

from job_apply import config

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

PDF = b"%PDF-1.4\n% a made-up document\n"
SAVED = "() => !document.querySelector('[data-saving]')"
ROWS = {
    "transcripts": ("Transcripts", "Optional. They go in where a site asks for a transcript."),
    "references": ("Reference list", "Optional. It goes in where a site asks for a list of references as a file."),
    "certificates": ("Certificates and licenses", "Optional. Diplomas, licenses and certificates: name each file for "
                                                  "what it is, so the right one goes in where a site asks."),
}
_READ = """() => Object.fromEntries(['transcripts', 'references', 'certificates'].map((k) => {
  const $ = (id) => document.getElementById(id);
  const row = $(`set-${k}-row`);
  return [k, {name: $(`set-${k}-name`).textContent, said: $(`set-${k}-said`).textContent,
    files: [...row.querySelectorAll('.docfile span')].map((s) => s.textContent),
    removes: [...row.querySelectorAll('[data-doc-name]')].map((b) => b.textContent),
    add: $(`set-${k}`).textContent, accept: $(`set-${k}-pick`).accept, hidden: $(`set-${k}-pick`).hidden,
    err: [...row.querySelectorAll('.set-err')].filter((e) => !e.hidden).map((e) => e.textContent)}];
}))"""


def _posts(page) -> list[dict]:
    posted: list[dict] = []
    page.on("request", lambda r: posted.append(json.loads(r.post_data or "{}"))
            if r.method == "POST" and r.url.endswith("/api/profile/document") else None)
    return posted


async def _pick(page, kind: str, name: str, data: bytes = PDF, mime: str = "application/pdf") -> None:
    async with page.expect_file_chooser() as chooser:
        await page.click(f"#set-{kind}")
    await (await chooser.value).set_files({"name": name, "mimeType": mime, "buffer": data})
    await page.wait_for_function(SAVED)
    await page.wait_for_timeout(300)


def _with_desk(srv, steps):
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/documents")
                out = await steps(page)
                await browser.close()
                return out
        finally:
            await desk.stop()

    return run(go())


def test_the_panel_is_named_resume_and_documents_and_draws_the_three_rows_with_their_sentences(srv, job_apply_home):
    async def steps(page):
        return {"read": await page.evaluate(_READ), "h": await page.inner_text("#panel-documents-h"),
                "link": await page.inner_text("a[href='#settings/documents']"),
                "order": await page.evaluate("""() => [...document.querySelectorAll('#panel-documents .setrow')]
                    .map((r) => r.id || (r.querySelector('.t') || r).id)""")}

    seen = _with_desk(srv, steps)
    assert (seen["h"], seen["link"]) == ("Resume and documents", "Resume and documents")
    for kind, (title, said) in ROWS.items():
        row = seen["read"][kind]
        assert (row["name"], row["said"]) == (title, said), row
        assert (row["files"], row["add"], row["hidden"], row["err"]) == ([], "Add a file", True, []), row
        assert row["accept"] == ".pdf,.docx,.jpg,.jpeg,.png"
    order = seen["order"]
    assert order.index("set-letter-name") < order.index("set-transcripts-row") < order.index("set-references-row") \
        < order.index("set-certificates-row") < order.index("set-tailor-row"), order


def test_the_files_the_profile_has_are_listed_by_name_each_with_a_remove(srv, job_apply_home):
    folder = job_apply_home / "documents" / "certificates"
    folder.mkdir(parents=True)
    for name in ("Example-Diploma.pdf", "Example-License.png"):
        (folder / name).write_bytes(PDF)
    text = (job_apply_home / "profile.yaml").read_text(encoding="utf-8")
    both = [str(folder / "Example-Diploma.pdf"), str(folder / "Example-License.png")]
    config.write_text_atomic(job_apply_home / "profile.yaml",
                             config.set_yaml_value(text, ("documents", "certificates"), both))

    seen = _with_desk(srv, lambda page: page.evaluate(_READ))
    assert seen["certificates"]["files"] == ["Example-Diploma.pdf", "Example-License.png"], seen
    assert seen["certificates"]["removes"] == ["Remove", "Remove"]
    assert seen["transcripts"]["files"] == [] and seen["references"]["files"] == []
    assert str(job_apply_home) not in json.dumps(seen)


def test_add_a_file_posts_its_kind_name_and_bytes_and_the_line_comes(srv, job_apply_home):
    async def steps(page):
        posted = _posts(page)
        await _pick(page, "references", "Example-References.pdf")
        await _pick(page, "references", "Example-More.png", b"\x89PNG\r\n\x1a\nx", "image/png")
        return {"posted": posted, "read": await page.evaluate(_READ)}

    seen = _with_desk(srv, steps)
    assert seen["posted"] == [
        {"kind": "references", "name": "Example-References.pdf", "data": base64.b64encode(PDF).decode()},
        {"kind": "references", "name": "Example-More.png", "data": base64.b64encode(b"\x89PNG\r\n\x1a\nx").decode()}]
    assert seen["read"]["references"]["files"] == ["Example-More.png", "Example-References.pdf"], seen["read"]
    assert seen["read"]["transcripts"]["files"] == []
    assert [p.name for p in config.Profile.load().document_files("references")] == [
        "Example-More.png", "Example-References.pdf"]


def test_remove_posts_the_name_and_the_line_goes_and_the_file_stays(srv, job_apply_home):
    async def steps(page):
        await _pick(page, "transcripts", "Example-A.pdf")
        await _pick(page, "transcripts", "Example-B.pdf")
        posted = _posts(page)
        await page.click("#set-transcripts-row [data-doc-name='Example-B.pdf']")
        await page.wait_for_function(SAVED)
        await page.wait_for_timeout(300)
        return {"posted": posted, "read": await page.evaluate(_READ)}

    seen = _with_desk(srv, steps)
    assert seen["posted"] == [{"kind": "transcripts", "remove": "Example-B.pdf"}]
    assert seen["read"]["transcripts"]["files"] == ["Example-A.pdf"]
    assert (job_apply_home / "documents" / "transcripts" / "Example-B.pdf").exists()


def test_a_refused_add_shows_the_routes_sentence_and_adds_no_line(srv, job_apply_home):
    async def steps(page):
        await _pick(page, "certificates", "notes.txt", b"hello", "text/plain")
        return await page.evaluate(_READ)

    seen = _with_desk(srv, steps)
    assert seen["certificates"]["err"] == ["Pick your certificates as a PDF, Word (.docx), JPG or PNG file"], seen
    assert seen["certificates"]["files"] == [] and seen["transcripts"]["err"] == []


def test_focus_goes_to_the_next_files_remove_or_to_add_a_file_and_is_seen(srv, job_apply_home):
    active = "() => document.activeElement.dataset.docName || document.activeElement.id"

    async def steps(page):
        for name in ("Example-A.pdf", "Example-B.pdf", "Example-C.pdf"):
            await _pick(page, "certificates", name)
        seen = {"order": await page.evaluate("""() => [...document.querySelectorAll(
            '#set-certificates-row [data-doc-name]')].map((b) => b.dataset.docName)""")}  # (the newest first)
        # the keyboard: Tab to the first Remove, which shows a focus ring; Enter takes it out
        await page.focus("#set-letter")
        for _ in range(3):  # (the cover letter's Choose a file, then the next rows' controls come before)
            await page.keyboard.press("Tab")
            if await page.evaluate("() => document.activeElement.dataset.docName === 'Example-C.pdf'"):
                break
        seen["tabbed"] = await page.evaluate(active)
        seen["ring"] = await page.evaluate("() => [document.activeElement.matches(':focus-visible'), "
                                           "getComputedStyle(document.activeElement).outlineStyle]")
        await page.keyboard.press("Enter")
        await page.wait_for_function(SAVED)
        await page.wait_for_timeout(300)
        seen["after first"] = await page.evaluate(active)
        await page.keyboard.press("Enter")  # (that one, Example-B: its place taken by Example-A)
        await page.wait_for_function(SAVED)
        await page.wait_for_timeout(300)
        seen["after second"] = await page.evaluate(active)
        await page.keyboard.press("Enter")  # (the last)
        await page.wait_for_function(SAVED)
        await page.wait_for_timeout(300)
        seen["after last"] = await page.evaluate(active)
        return seen

    seen = _with_desk(srv, steps)
    assert seen["order"] == ["Example-C.pdf", "Example-B.pdf", "Example-A.pdf"], seen
    assert seen["tabbed"] == "Example-C.pdf" and seen["ring"] == [True, "solid"], seen
    assert seen["after first"] == "Example-B.pdf", seen
    assert seen["after second"] == "Example-A.pdf", seen
    assert seen["after last"] == "set-certificates", seen
