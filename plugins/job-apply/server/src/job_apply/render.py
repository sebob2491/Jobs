"""Render a Markdown resume or cover letter to a print-ready PDF."""

from __future__ import annotations

import html
import re
from pathlib import Path

import markdown
from playwright.async_api import Error as PlaywrightError, async_playwright

from . import config
from .browser import UNAVAILABLE_HELP, BrowserUnavailable, launch_attempts

KINDS = ("resume", "cover_letter")

_BASE_CSS = """
@page { size: Letter; margin: 0; }
* { box-sizing: border-box; }
body { margin: 0; color: #1a1a1a; font-family: Calibri, Carlito, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
       -webkit-print-color-adjust: exact; }
a { color: inherit; text-decoration: none; }
p { margin: 0 0 0.5em; }
ul { margin: 0.15em 0 0.45em; padding-left: 1.15em; }
li { margin: 0.08em 0; }
strong { font-weight: 600; }
"""

_CSS = {
    "resume": _BASE_CSS + """
body { font-size: 10.5pt; line-height: 1.3; padding: 0.55in 0.65in; }
h1 { font-size: 20pt; margin: 0 0 2pt; letter-spacing: 0.3pt; }
h1 + p { margin-bottom: 8pt; color: #444; }
h2 { font-size: 11pt; text-transform: uppercase; letter-spacing: 0.8pt; margin: 10pt 0 4pt;
     padding-bottom: 2pt; border-bottom: 1px solid #999; }
h3 { font-size: 10.5pt; margin: 6pt 0 1pt; }
h3 em { float: right; font-weight: normal; font-style: normal; color: #444; }
h2, h3 { break-after: avoid; }
li, h3 + p { break-inside: avoid; }
""",
    "cover_letter": _BASE_CSS + """
body { font-size: 11pt; line-height: 1.45; padding: 0.9in 1in; }
h1 { font-size: 16pt; margin: 0 0 2pt; }
h1 + p { color: #444; margin-bottom: 18pt; }
p { margin: 0 0 10pt; }
""",
}


def to_html(markdown_text: str, kind: str, title: str = "") -> str:
    body = markdown.markdown(markdown_text, extensions=["sane_lists", "attr_list"])
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
        f"<style>{_CSS[kind]}</style></head><body>{body}</body></html>"
    )


def count_pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page(?!s)", pdf))


async def render_pdf(page_html: str, dest: Path) -> int:
    """Print HTML to `dest` with a throwaway headless browser; returns the page count."""
    settings = config.Profile.load().settings
    errors = []
    async with async_playwright() as pw:
        for extra in launch_attempts(settings):
            try:
                browser = await pw.chromium.launch(headless=True, **extra)
                break
            except PlaywrightError as e:
                errors.append(f"{extra or 'bundled chromium'}: {str(e).splitlines()[0]}")
        else:
            raise BrowserUnavailable(UNAVAILABLE_HELP + " | ".join(errors))
        try:
            page = await browser.new_page()
            await page.set_content(page_html, wait_until="load")
            pdf = await page.pdf(format="Letter", print_background=True, prefer_css_page_size=True)
        finally:
            await browser.close()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(pdf)
    return count_pages(pdf)
