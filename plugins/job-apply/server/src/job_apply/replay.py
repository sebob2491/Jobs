"""Offline replay of a saved page: what the Job Desk reads and decides there, without the site.

    job-apply-doctor --replay <a debug folder | a fixture's .html> [--profile <profile.yaml>] [--json]

The page is opened in Playwright's bundled Chromium (JOB_APPLY_CHROMIUM_PATH's, where that's set) with no network:
every request but for the page's own files is refused, so nothing is fetched from the site or sent to it. It's read as
the desk reads a page (the form reader's boxes, buttons, errors and dialogs, and whether a CAPTCHA shows), then decided
on as the desk decides (job_apply.fixtures.decide: the page's kind, the button it would press, the CAPTCHA, security
question, account-and-application and job-search reads), with the answer rules' plan for each box (autofill
.plan_autofill: filled by which rule, asked, or left and why) and the verdict on any agreement on it (a terms gate's
button, a notice over the page: agreed to with settings.accept_notices, or the person's, and what it holds that's never
agreed to). A value is never shown: a box's answer is said by its rule's name ("<phone>").

Whatever is named, what's opened is a copy scrubbed first, as a test fixture is (job_apply.fixtures.clean_html: scripts
out, nothing that loads from the network, the person's details taken out: the desk's own profile's and any named one's);
a debug folder (a job's debug/<time>, or one a stop saved: debug/<time>-stop) is made into one with its frames
(fixtures.convert). And the browser runs no script, and has a proxy that isn't there for anything it would send before a
request is refused. So what's replayed is what the saved HTML shows, without what a site draws with its scripts. The
answers are the live check's test applicant's (fixtures.REPLAY_PROFILE) unless a profile is named.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

from playwright.async_api import Browser, ProxySettings, Route, WebSocketRoute, async_playwright

from . import config, fixtures
from .browser import BrowserSession

LOCAL_SCHEMES = ("data", "blob", "about")  # (and the saved page's own files: OfflineSession._only_local)
REPLAY_APPLICANT = "the live check's test applicant"
# Where the replay's browser sends what it would send to the network: nowhere (a port nothing listens on, here)
NO_PROXY: ProxySettings = {"server": "http://127.0.0.1:9"}


class OfflineSession(BrowserSession):
    """The desk's browser session on a page of this computer's alone: a fresh, headless context where every request to
    the network is refused (each one counted in `refused`), no web socket is opened, and of this computer's files only
    those in the saved page's `folder` are opened. `browser`: one
    already running to open the context in (the tests'); else Playwright's bundled Chromium is started, and stopped
    with the session."""

    def __init__(self, browser: Browser | None = None) -> None:
        super().__init__()
        self._given = browser
        self._browser: Browser | None = None
        self.refused: list[str] = []
        self.local: list[str] = []  # (the saved page's files served: the page and its frames)
        self.folder: Path | None = None  # the saved page's folder: the only files it may open

    async def _launch(self) -> None:
        if self._given is not None:
            browser = self._given
        else:
            self._pw = await async_playwright().start()
            browser = self._browser = await self._pw.chromium.launch(
                headless=True, executable_path=os.environ.get("JOB_APPLY_CHROMIUM_PATH") or None, proxy=NO_PROXY)
        # (no script runs, and whatever the browser opens before a request is routed (a frame's connection) goes to a
        # proxy that isn't there: the review of T10 found a frame's address connected to, and a script's WebRTC)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block",
                                        accept_downloads=False, java_script_enabled=False, proxy=NO_PROXY)
        await ctx.route("**/*", self._only_local)
        await ctx.route_web_socket(re.compile(r".*"), self._no_socket)
        ctx.set_default_timeout(15000)
        self._ctx = ctx
        self._page = await ctx.new_page()

    async def _only_local(self, route: Route) -> None:
        try:
            await self._answer(route)
        except Exception:  # (a request left unanswered would hold the page's load: refused instead)
            with contextlib.suppress(Exception):
                await route.abort()

    async def _answer(self, route: Route) -> None:
        url = route.request.url
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme == "file":
            # (the saved page and its own files beside it alone; a page or frame as UTF-8, as it was written: one that
            # doesn't name its characters is otherwise guessed at, and now and then read as Windows' Western, its words
            # mangled)
            file = Path(url2pathname(parts.path))
            if self.folder is None or not file.resolve().is_relative_to(self.folder) or not file.is_file():
                await route.abort("accessdenied")  # (nothing of the network: a file not there, or not the page's)
            elif file.suffix.lower() in (".html", ".htm"):
                self.local.append(url)
                await route.fulfill(status=200, body=file.read_bytes(), content_type="text/html; charset=utf-8")
            else:
                self.local.append(url)
                await route.continue_()
        elif scheme in LOCAL_SCHEMES:
            await route.continue_()
        else:
            self.refused.append(url)
            await route.abort("internetdisconnected")

    async def _no_socket(self, ws: WebSocketRoute) -> None:
        self.refused.append(ws.url)
        await ws.close()

    async def close(self) -> None:
        await super().close()  # (the context, and Playwright where it was started here, which ends its browser)
        self._browser = None


class _Reader:
    """What fixtures.read_saved reads a page through: the server's tools' way (inspect_form, page_text), on a session
    of the replay's own."""

    def __init__(self, session: BrowserSession) -> None:
        self.browser = session

    async def inspect_form(self, include_dropdown_options: bool = True) -> dict[str, Any]:
        from .ats import detect_ats

        data = await self.browser.inspect(include_dropdown_options)
        data["ats"] = detect_ats(data["url"])
        return data

    async def page_text(self, max_chars: int = 8000) -> str:
        return await self.browser.visible_text(max_chars)


def _page_of(path: Path, work: Path, secrets: list[str]) -> tuple[Path, str]:
    """The page to open for `path`, always a copy scrubbed into `work` (scripts out, nothing of the network, `secrets`
    taken out: fixtures.clean_html): a debug folder's (or the page.html in one) as a fixture is made from it
    (fixtures.convert, its frames mapped to their files), else the .html with the frames saved beside it under its
    name (a fixture's "<name>.frame-1.html"). And the address it was saved from (the fixture's expect.json's, the
    snapshot's: without its query)."""
    folder = path if path.is_dir() else path.parent if path.name == "page.html" else None
    if folder is not None and (folder / "snapshot.json").is_file():
        fixtures.convert(folder, "replay", work, secrets=secrets)
        page, expect = work / "replay.html", work / "replay.expect.json"
    elif path.is_dir():
        raise ValueError(f"{path.name} isn't a saved page's folder (it has no snapshot.json)")
    elif path.is_file() and path.suffix.lower() in (".html", ".htm"):
        work.mkdir(parents=True, exist_ok=True)
        expect = path.with_name(f"{path.stem}.expect.json")
        for source in [path, *sorted(path.parent.glob(f"{glob.escape(path.stem)}.frame-*.htm*"))]:
            raw = source.read_text(encoding="utf-8", errors="replace")
            # (a test fixture, its expect.json beside it, was cleaned as it was made: only the details taken out, as
            # cleaning it again reads otherwise (its answered radios, its links); any other page is cleaned first)
            said = fixtures.redact(raw, secrets) if expect.is_file() else fixtures.clean_html(raw, secrets)
            (work / source.name).write_text(said, encoding="utf-8")
        page = work / path.name
    else:
        raise ValueError(f"{path.name} isn't a saved page (a debug folder, or a fixture's .html)")
    if not page.is_file():
        raise ValueError(f"{path.name} has no page saved in it")
    address = json.loads(expect.read_text(encoding="utf-8")).get("source_url") if expect.is_file() else ""
    return page, fixtures.redact(str(address or ""), secrets)


def _secrets(profile: config.Profile | None) -> list[str]:
    """What a replay takes out of the page before it's opened: the person's details as the desk's own profile has
    them, and the named profile's too (never only the named one's: the review of T10 found a debug folder replayed
    with another profile printing the person's own name)."""
    found: set[str] = set()
    for prof in (None, profile):
        try:
            found |= set(fixtures.personal_strings(prof if prof is not None else config.Profile.load()))
        except (OSError, ValueError):  # (the desk's own profile unreadable: the named one's still)
            continue
    return sorted(found, key=len, reverse=True)



def _box_plans(data: dict[str, Any], prof: config.Profile) -> list[dict[str, Any]]:
    """Each box on the page with what the answer rules would do with it (plan_autofill, for no job in particular):
    "fill" by its rule (the value said as "<rule>", never itself), "ask", or "left" and why."""
    from .autofill import SKIP, plan_autofill, resolve_field

    fields = data.get("fields") or []
    plan = plan_autofill(fields, prof, {})
    filled = {str(t["id"]): t for t in plan["to_fill"]}
    asked = {str(q["id"]): q for q in plan["needs_input"]}
    judged, already = set(map(str, plan.get("judged") or [])), set(map(str, plan["already_filled"]))
    file_inputs = sum(1 for f in fields if f.get("kind") == "file")
    out = []
    for f in fields:
        fid = str(f.get("id"))
        box: dict[str, Any] = {"label": " ".join(str(f.get("label") or f.get("name") or "").split()),
                               "kind": f.get("kind"), "required": bool(f.get("required"))}
        if fid in filled:
            rule = str(filled[fid].get("rule") or "")
            box.update(plan="fill", rule=rule, value=f"<{rule}>")
        elif fid in asked:
            box.update(plan="ask", why="the profile's answer isn't one of its choices" if asked[fid].get("unmatched")
                       else "no answer in the profile")
        elif fid in judged:
            box.update(plan="left", why="on the site's own choice, which the profile doesn't change")
        elif fid in already:
            box.update(plan="left", why="shows an answer already")
        elif f.get("aside"):
            box.update(plan="left", why="a sign-up beside the application")
        elif f.get("disabled"):
            box.update(plan="left", why="greyed out")
        elif f.get("readonly"):
            box.update(plan="left", why="read-only")
        elif f.get("kind") == "password":
            box.update(plan="left", why="a password: the desk's own step (sign-in or account)")
        elif (ans := resolve_field(f, prof, {}, file_inputs)) is not None and ans.value in (None, "", SKIP):
            box.update(plan="left", why=f"its rule ({ans.rule}) leaves it empty")
        else:
            box.update(plan="left", why="no answer, and it isn't required")
        out.append(box)
    return out


def _agreements(data: dict[str, Any], words: str) -> dict[str, Any]:
    """The verdicts on what the page asks to be agreed to, as the desk's rules give them: a page's button that agrees
    (pipeline._agreement_button) and whether it's a terms gate the desk presses (_terms_gate, with
    settings.accept_notices), and a dialog over the page that nothing else answers (_notice) and whether it may be
    agreed to (_agreeable_notice); each with the first kind of what's never agreed to that its words hold
    (never.NEVER's)."""
    from . import pipeline
    from .never import ANYWHERE, _never_for_them

    out: dict[str, Any] = {"agreement": None, "notice": None}
    button = pipeline._agreement_button(data)
    if button is not None:
        about = " ".join([*(data.get("headings") or []), str(data.get("title") or "")])
        gate = pipeline._terms_gate(button, data, words) or pipeline._attestation_gate(button, data, words)
        out["agreement"] = {
            "button": " ".join(str(button.get("text") or "").split()),
            "terms_gate": gate,
            "verdict": "agreed to for the person (with settings.accept_notices)" if gate else "the person's",
            "never": _never_for_them(f"{button.get('text') or ''} {about}", only=pipeline._AGREEMENT_KINDS)
            or _never_for_them(words, only=ANYWHERE)}
    notice = pipeline._notice(data)
    if notice is not None:
        heading = pipeline._notice_name(notice)
        text = str(notice.get("text") or "")
        agreeable, _ = pipeline._agreeable_notice(heading, text, False)
        out["notice"] = {
            "heading": heading, "agreeable": agreeable,
            "verdict": "agreed to for the person (with settings.accept_notices)" if agreeable else "the person's",
            "never": _never_for_them(text, only=ANYWHERE) or _never_for_them(heading, only=pipeline._AGREEMENT_KINDS)}
    return out


async def replay(path: Path, profile: Path | None = None, browser: Browser | None = None) -> dict[str, Any]:
    """Open the saved page at `path` offline and say what the desk reads and decides there (the module's doc).
    `profile`: a profile file to answer from (else the live check's test applicant). `browser`: one running already to
    open it in (else the bundled Chromium is started for it)."""
    path = Path(path)
    if profile is not None and not Path(profile).is_file():
        raise FileNotFoundError(f"there's no profile file at {Path(profile).name}")
    with tempfile.TemporaryDirectory(prefix="job-apply-replay-") as tmp:
        work = Path(tmp)
        prof = config.Profile.load(Path(profile)) if profile else fixtures.replay_profile(work / "applicant")
        # (the page is scrubbed of the person's own details, the desk's profile's, and the named one's: _secrets)
        page, address = _page_of(path, work / "page", _secrets(prof if profile else None))
        session = OfflineSession(browser)
        session.folder = page.parent.resolve()
        try:
            data, said = await fixtures.read_saved(_Reader(session), page)
            words = await session.visible_text(40000)  # (what the desk's terms gate reads: the page's words, whole)
        finally:
            await session.close()
        decision = fixtures.decide(data, said, prof)
        return {
            "file": path.name,
            "address": address,
            "applicant": "the profile named" if profile else REPLAY_APPLICANT,
            "refused": len(session.refused),
            **{k: decision[k] for k in fixtures.DECISION_KEYS if k != "fields"},
            "errors": [" ".join(str(e).split()) for e in data.get("errors") or []],
            "buttons": [" ".join(str(a.get("text") or "").split()) + (" (greyed out)" if a.get("disabled") else "")
                        for a in data.get("actions") or []],
            "dialogs": [" ".join(str(d.get("heading") or d.get("text") or "")[:80].split())
                        for d in data.get("dialogs") or []],
            "boxes": _box_plans(data, prof),
            **_agreements(data, words),
        }


def _yes(value: Any) -> str:
    return "yes" if value else "no"


def text(result: dict[str, Any]) -> str:
    """A replay's result as job-apply-doctor --replay prints it."""
    out = [f"Replay of {result['file']} (offline: {result['refused']} request(s) to the network refused)",
           *([f"Saved from: {result['address']}"] if result.get("address") else []),
           f"Answered for: {result['applicant']}",
           f"Page: {result['page']}" + (f"; the button the desk presses: “{result['next']}”"
                                        if result.get("next") else "; no button the desk presses"),
           f"CAPTCHA: {_yes(result['captcha'])}; security questions: {_yes(result['security_asked'])}; "
           f"an account form that is the application: {_yes(result['account_and_application'])}; "
           f"a job search: {_yes(result['job_search'])}"]
    out.append("Errors: " + (" | ".join(result["errors"]) if result["errors"] else "none"))
    out.append("Buttons: " + (" | ".join(result["buttons"]) if result["buttons"] else "none"))
    if result["dialogs"]:
        out.append("Dialogs over the page: " + " | ".join(result["dialogs"]))
    for name, what in (("agreement", "button"), ("notice", "heading")):
        found = result.get(name)
        if found:
            out.append(f"{'Agreement' if name == 'agreement' else 'Notice'} “{found[what]}”: {found['verdict']}"
                       + (f"; it holds what's never agreed to ({found['never']})" if found.get("never") else ""))
    out.append(f"Boxes ({len(result['boxes'])}):")
    for box in result["boxes"]:
        head = (f"  {box['plan']:<4}  {box['label'] or '(no label)'} ({box['kind']}"
                f"{', required' if box['required'] else ''})")
        out.append(head + (f" = {box['value']}" if box["plan"] == "fill" else f": {box['why']}"))
    return "\n".join(out)
