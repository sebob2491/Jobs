"""What the desk learned from the person: the words of a button they pressed to move an application on, at a stop where
the desk found no step button (stop code no_step_button), kept for the same employer's site and the same page.

learned.json, in the person's job-apply folder, theirs alone. Each entry holds only:
  {"place": the employer's place (pipeline_accounts.account_place: a host, with a tenant on a shared one),
   "system": the job system's key, or "",
   "page": a short hash of the page's headings and its boxes' labels (never the words themselves, nor the address),
   "button": the button's words (60 characters at most),
   "on": the day, "YYYY-MM-DD", "worked": presses that moved the page on, "failed": ones that didn't}
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
from collections.abc import Callable, Iterable
from typing import Any

from . import config

LEARNED_FILE = "learned.json"
MOST = 500  # entries kept: the oldest by day go first
WORDS_LONGEST = 60  # characters of a button's words kept
_LOCK = threading.Lock()  # (the desk's jobs at once, the page's routes)
_KEYS = ("place", "system", "page", "button", "on", "worked", "failed")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_PAGE = re.compile(r"[0-9a-f]{16}")


def words_of(text: Any) -> str:
    """A button's words as kept and compared: its visible text, whitespace collapsed, WORDS_LONGEST characters."""
    return " ".join(str(text or "").split())[:WORDS_LONGEST].strip()


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


def _read() -> list[dict[str, Any]]:
    """The entries as kept (each only its own keys); [] where there's no file or it doesn't read."""
    try:
        got = json.loads((config.home() / LEARNED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(got, list):
        return []
    return [{k: e[k] for k in _KEYS} for e in got if _valid(e)]


def _write(entries: list[dict[str, Any]]) -> None:
    entries = sorted(entries, key=lambda e: e["on"])[-MOST:]  # (the oldest by day go first; a stable sort)
    config.ensure_home()
    config.write_text_atomic(config.home() / LEARNED_FILE, json.dumps(entries, indent=1))


def _find(entries: list[dict[str, Any]], place: str, page: str) -> dict[str, Any] | None:
    return next((e for e in entries if e["place"] == place and e["page"] == page), None)


def remember(place: str, system: str, page: str, button: str, today: str) -> bool:
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
                        "failed": 0})
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
# characters at most) of the nearest button, link or role=button the click was on: nothing for any other element, no
# key or input event, no value, no coordinate, no address. It reports only while window.__jdHandOn is true, which
# mute() sets false when the stop ends; a page navigated meanwhile has no listener at all (its script is gone with it)
BINDING = "__jdHand"
LISTEN_JS = """() => {
  window.__jdHandOn = true;
  if (window.__jdHandHears) return;
  window.__jdHandHears = true;
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  document.addEventListener('click', (e) => {
    try {
      if (!window.__jdHandOn || !e.isTrusted || !(e.target instanceof Element)) return;
      const b = e.target.closest('button, input[type="button"], input[type="submit"], a, [role="button"]');
      if (!b) return;
      const said = b.innerText || b.textContent || (b.tagName === 'INPUT' ? b.value : '');
      const words = clean(said || b.getAttribute('aria-label') || '').slice(0, %d);
      if (words && typeof window.%s === 'function') window.%s(words);
    } catch (_) { /* never the page's trouble */ }
  }, true);
}""" % (WORDS_LONGEST, BINDING, BINDING)
MUTE_JS = "() => { window.__jdHandOn = false; }"
_WAIT = 4.0  # seconds a frame is given for either script


def _frames(page: Any) -> list[Any]:
    """The tab's frames a script may go into: not a detached one, a blank one but the main frame, nor a CAPTCHA's or a
    media player's."""
    from .browser import _CAPTCHA_FRAME, _MEDIA_FRAME

    out = []
    for frame in page.frames:
        try:
            if frame.is_detached():
                continue
            if frame is not page.main_frame and (not frame.url or frame.url == "about:blank"
                                                  or _MEDIA_FRAME.match(frame.url)):
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
    installed. `on_words(source, words)` is called for each report. Whatever goes wrong, nothing is heard."""
    if bound is not page:
        try:
            await page.expose_binding(BINDING, on_words)
        except Exception as e:  # (a binding the tab already has, from this run's earlier stop: the same callback)
            if "already" not in str(e).lower():
                return bound
        bound = page
    await _in_frames(page, LISTEN_JS)
    return bound


async def mute(page: Any) -> None:
    """Stop the listener reporting, in every frame (the binding stays on the tab: the pipeline ignores a report that
    comes when no stop is waiting)."""
    if page is not None:
        await _in_frames(page, MUTE_JS)

