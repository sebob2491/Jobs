"""What the desk learned from the person: the words of a button they pressed to move an application on, at a stop where
the desk found no step button (stop code no_step_button), kept for the same employer's site and the same page.

learned.json, in the person's job-apply folder, theirs alone. Each entry holds only:
  {"place": the employer's place (place_key: pipeline_accounts.account_place, a host with a tenant on a shared one,
            then "|" and the employer's name as company_key gives it: one host serves many employers),
   "system": the job system's key, or "",
   "page": a short hash of the page's headings and its boxes' labels (never the words themselves, nor the address),
   "button": the button's words (60 characters at most),
   "on": the day, "YYYY-MM-DD", "worked": presses that moved the page on, "failed": ones that didn't,
   "employer": the employer's name as the tracker has it, for the page's list (an entry kept without one shows its
               site instead)}
Never a path or query of an address, a box's value, anything typed, an email or a name.

Every function reads the file again and writes it whole, under one lock, and never raises: a file that is missing,
empty or doesn't read is "nothing learned". The pipeline decides what may be learned (pipeline._may_learn); this
module only keeps it, and holds the small script that hears which button the person pressed (listen / mute).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import threading
import unicodedata
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from . import config

LEARNED_FILE = "learned.json"
MOST = 500  # entries kept: the oldest by day go first
WORDS_LONGEST = 60  # characters of a button's words kept
_LOCK = threading.Lock()  # (the desk's jobs at once, the page's routes)
NAME_LONGEST = 80
_KEYS = ("place", "system", "page", "button", "on", "worked", "failed")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_PAGE = re.compile(r"[0-9a-f]{16}")


def words_of(text: Any) -> str:
    """A button's words as kept and compared: its visible text, without the characters that show nothing (Unicode's
    format characters: a zero-width space or a soft hyphen inside "Sub\u200bmit"), whitespace collapsed,
    WORDS_LONGEST characters."""
    shown = "".join(c for c in str(text or "") if unicodedata.category(c) != "Cf")
    return " ".join(shown.split())[:WORDS_LONGEST].strip()


def same_words(a: Any, b: Any) -> bool:
    """Are these a button's same words (case and spaces aside)? Never for empty ones."""
    x = words_of(a).casefold()
    return bool(x) and x == words_of(b).casefold()


def page_key(headings: Iterable[Any], labels: Iterable[Any]) -> str:
    """A page's key: 16 hex digits of a hash of its headings and its boxes' labels (sorted), without its address."""
    shape = json.dumps([[str(h) for h in headings], sorted(str(x) for x in labels)], ensure_ascii=False)
    return hashlib.sha256(shape.encode("utf-8")).hexdigest()[:16]


def place_of(url: str) -> str:
    """Where this address's employer is kept (pipeline_accounts.account_place); a page saved on this computer (a test's)
    by a hash of its folder, never the folder: no path of any address is kept."""
    from .pipeline_accounts import account_place

    place = account_place(url)
    if place.startswith("file:"):
        return "file:" + hashlib.sha256(place.encode("utf-8")).hexdigest()[:16]
    return place


def company_key(company: Any) -> str:
    """An employer's name as a learned press is kept by: lower-case, its letters and digits alone ("Acme, Inc." is
    "acmeinc")."""
    return "".join(c for c in str(company or "").casefold() if c.isalnum())


def place_key(url: str, company: Any) -> str:
    """Where a learned press is kept: the address's place (place_of) and the job's employer (company_key), as
    "place|company". A job system many employers share is one place for all of them (jobs.smartrecruiters.com, a
    Taleo's ?org=), so the employer is named too. "" with either unknown: then nothing is learned or used."""
    place, company = place_of(url), company_key(company)
    return f"{place}|{company}" if place and company else ""


def shown_place(place: str) -> str:
    """A kept place as the page shows it: the address's part alone (the employer's name is the tracker's)."""
    return place.partition("|")[0]


def entry_id(place: str, page: str) -> str:
    """An entry's id for the page's routes: opaque (a hash of its place and page)."""
    return hashlib.sha256(f"{place}\n{page}".encode()).hexdigest()[:16]


def _valid(entry: Any) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("place"), str) and entry["place"] != ""
            and isinstance(entry.get("system"), str) and isinstance(entry.get("page"), str)
            and _PAGE.fullmatch(entry["page"]) is not None
            and isinstance(entry.get("button"), str) and words_of(entry["button"]) == entry["button"] != ""
            and isinstance(entry.get("on"), str) and _DAY.fullmatch(entry["on"]) is not None
            and type(entry.get("worked")) is int and type(entry.get("failed")) is int)


def _name(employer: Any) -> str:
    """An employer's name as kept for the page's list: one line, NAME_LONGEST characters; "" for anything else."""
    if not isinstance(employer, str):
        return ""
    shown = "".join(c for c in employer if unicodedata.category(c) != "Cf")
    return " ".join(shown.split())[:NAME_LONGEST].strip()


def _read() -> list[dict[str, Any]]:
    """The entries as kept (each only its own keys); [] where there's no file or it doesn't read."""
    try:
        got = json.loads((config.home() / LEARNED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(got, list):
        return []
    return [{k: e[k] for k in _KEYS} | ({"employer": _name(e["employer"])} if _name(e.get("employer")) else {})
            for e in got if _valid(e)]


def _write(entries: list[dict[str, Any]]) -> None:
    entries = sorted(entries, key=lambda e: e["on"])[-MOST:]  # (the oldest by day go first; a stable sort)
    config.ensure_home()
    config.write_text_atomic(config.home() / LEARNED_FILE, json.dumps(entries, indent=1))


def _find(entries: list[dict[str, Any]], place: str, page: str) -> dict[str, Any] | None:
    return next((e for e in entries if e["place"] == place and e["page"] == page), None)


def remember(place: str, system: str, page: str, button: str, today: str, employer: str = "") -> bool:
    """Keep that the person pressed `button` to go on from this page at this place, on `today`. The same page's earlier
    entry is replaced (its counts start again when the words differ). False when it wasn't kept: empty words, a place
    or page that isn't one, or the file can't be written."""
    words = words_of(button)
    if not (words and place and _PAGE.fullmatch(page or "") and _DAY.fullmatch(today or "")):
        return False
    with _LOCK, contextlib.suppress(OSError, ValueError, TypeError):
        entries = _read()
        old = _find(entries, place, page)
        if old is not None:
            entries.remove(old)
        same = old is not None and same_words(old["button"], words)
        entries.append({"place": place, "system": system if isinstance(system, str) else "", "page": page,
                        "button": words, "on": today, "worked": old["worked"] if same and old else 0,
                        "failed": 0} | ({"employer": _name(employer)} if _name(employer) else {}))
        _write(entries)
        return True
    return False


def lookup(place: str, page: str) -> dict[str, Any] | None:
    """The entry for this place and page, or None."""
    with _LOCK:
        entry = _find(_read(), place, page)
        return dict(entry) if entry else None


def worked(place: str, page: str) -> None:
    """A learned press moved the page on."""
    _bump(place, page, "worked")


def failed(place: str, page: str) -> None:
    """A learned press didn't move the page on: the entry is dropped at the second."""
    _bump(place, page, "failed")


def _bump(place: str, page: str, which: str) -> None:
    with _LOCK, contextlib.suppress(OSError, ValueError, TypeError):
        entries = _read()
        entry = _find(entries, place, page)
        if entry is None:
            return
        entry[which] += 1
        if which == "failed" and entry["failed"] >= 2:
            entries.remove(entry)
        _write(entries)


def forget(place: str, page: str) -> bool:
    """Drop the entry for this place and page. True when there was one."""
    with _LOCK, contextlib.suppress(OSError, ValueError, TypeError):
        entries = _read()
        entry = _find(entries, place, page)
        if entry is None:
            return False
        entries.remove(entry)
        _write(entries)
        return True
    return False


def forget_id(which: str) -> bool:
    """Drop the entry with this id (entry_id), as the page's Forget asks. True when there was one."""
    with _LOCK, contextlib.suppress(OSError, ValueError, TypeError):
        entries = _read()
        entry = next((e for e in entries if entry_id(e["place"], e["page"]) == which), None)
        if entry is None:
            return False
        entries.remove(entry)
        _write(entries)
        return True
    return False


def all() -> list[dict[str, Any]]:
    """Every entry, newest day first."""
    with _LOCK:
        return sorted(_read(), key=lambda e: e["on"], reverse=True)


# --------------------------------------------------------- hearing the person's press in the paused tab
# The page's one reporting function (Playwright's expose_binding on the tab, the run's own: a binding per page) and the
# script that calls it. The script listens for `click` in the capture phase and reports ONLY the visible words (60
# characters at most) of the nearest button, link or role=button the click was on (an input button's value): never an
# aria-label or a title (which can say anything, a name or a file's), nothing for any other element, no key or input
# event, no value of a box, no coordinate, no address. mute() takes the listener out of the page again when the stop
# ends, and what it put on `window` with it; a page navigated meanwhile has no listener at all (its script is gone with
# it). The binding itself stays on the tab (Playwright can't take one off): the pipeline ignores what it says while no
# such stop waits
BINDING = "__jdHand"
LISTEN_JS = """() => {
  if (typeof window.__jdHandOff === 'function') window.__jdHandOff();
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const heard = (e) => {
    try {
      if (!e.isTrusted || !(e.target instanceof Element)) return;
      const b = e.target.closest('button, input[type="button"], input[type="submit"], a, [role="button"]');
      if (!b) return;
      const said = b.tagName === 'INPUT' ? b.value : b.innerText;
      const words = clean(said).slice(0, %d);
      if (words && typeof window.%s === 'function') window.%s(words);
    } catch (_) { /* never the page's trouble */ }
  };
  document.addEventListener('click', heard, true);
  window.__jdHandOff = () => {
    document.removeEventListener('click', heard, true);
    try { delete window.__jdHandOff; } catch (_) { window.__jdHandOff = undefined; }
  };
}""" % (WORDS_LONGEST, BINDING, BINDING)
MUTE_JS = "() => { if (typeof window.__jdHandOff === 'function') window.__jdHandOff(); }"
_WAIT = 4.0  # seconds a frame is given for either script


def _origin(url: Any) -> tuple[str, str]:
    """An address's origin as compared here: its scheme and its host with its port."""
    try:
        parts = urlsplit(str(url or ""))
        return parts.scheme.lower(), parts.netloc.lower()
    except ValueError:
        return "", ""


def _frames(page: Any) -> list[Any]:
    """The tab's frames a script may go into: its main frame, and frames of the main frame's own origin (never another
    site's frame in it: what's pressed there isn't the employer's form's), not a detached one, a blank one, nor a
    CAPTCHA's or a media player's."""
    from .browser import _CAPTCHA_FRAME, _MEDIA_FRAME

    out = []
    try:
        main = page.main_frame
        own = _origin(main.url)
    except Exception:
        return []
    for frame in page.frames:
        try:
            if frame.is_detached():
                continue
            if frame is not main and (not frame.url or frame.url == "about:blank" or _MEDIA_FRAME.match(frame.url)
                                      or _origin(frame.url) != own):
                continue
            if _CAPTCHA_FRAME.match(frame.url):
                continue
        except Exception:
            continue
        out.append(frame)
    return out


async def _in_frames(page: Any, script: str) -> None:
    for frame in _frames(page):
        with contextlib.suppress(Exception, asyncio.TimeoutError):
            await asyncio.wait_for(frame.evaluate(script), _WAIT)


async def listen(page: Any, bound: Any, on_words: Callable[[Any, Any], Any]) -> Any:
    """Start hearing, in `page`'s every frame Playwright reaches, which button the person presses: the tab's binding
    exposed if it isn't (`bound`: the page it was exposed on already; returns the page now bound), then the script
    installed. `on_words(source, words)` is called for each report from a frame of the main frame's origin. Whatever
    goes wrong, nothing is heard."""

    def own(source: Any, words: Any) -> Any:
        # (the binding is reachable from every frame of the tab, another site's too: only a report from a frame of the
        # main frame's own origin is passed on)
        try:
            frame = source["frame"] if isinstance(source, dict) else getattr(source, "frame", None)
            tab = source["page"] if isinstance(source, dict) else getattr(source, "page", None)
            if frame is None or tab is None or _origin(frame.url) != _origin(tab.main_frame.url):
                return None
        except Exception:
            return None
        return on_words(source, words)

    if bound is not page:
        try:
            await page.expose_binding(BINDING, own)
        except Exception as e:  # (a binding the tab already has, from this run's earlier stop: the same callback)
            if "already" not in str(e).lower():
                return bound
        bound = page
    await _in_frames(page, LISTEN_JS)
    return bound


async def mute(page: Any) -> None:
    """Take the listener out of every frame it went into, and what it put on `window` (the binding stays on the tab:
    the pipeline ignores a report that comes when no stop is waiting)."""
    if page is not None:
        await _in_frames(page, MUTE_JS)

