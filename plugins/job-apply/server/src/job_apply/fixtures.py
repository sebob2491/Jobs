"""Turn a debug snapshot of a real application page into a regression-test fixture.

    uv run --project <plugin>/server python -m job_apply.fixtures <snapshot dir> <name>

The page's DOM is kept (scripts removed) so tests can check that field labels,
kinds and required flags are still extracted correctly. Profile values (name,
email, phone, address, links) are replaced before anything is written. Check the
output for other personal data before committing it.
"""

from __future__ import annotations

import argparse
import copy
import functools
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment

from .config import Profile

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "live"
REDACTED = "REDACTED"


NAME_KEYS = ("personal.first_name", "personal.middle_name", "personal.last_name", "personal.preferred_name")


def _spellings(value: str) -> set[str]:
    """How a page may write a value: as is, without its accents, with them as separate marks,
    JSON-escaped (in a data-* attribute) and URL-encoded (in a link or a file name)."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))
    out = {value, plain, unicodedata.normalize("NFD", value), json.dumps(value)[1:-1],
           json.dumps(value)[1:-1].replace("@", "\\u0040"), quote(value, safe=""),
           quote(value, safe="").replace("%20", "+")}
    return {v for v in out if v}


def _identifying(word: str) -> bool:
    """A user name or handle, not an everyday word that a page would also use: one with a
    digit or a separator in it, or a long one."""
    return len(word) >= 3 and (bool(re.search(r"[\d._-]", word)) or len(word) >= 10)


def personal_strings(prof: Profile) -> list[str]:
    keys = [*NAME_KEYS, "personal.email", "personal.phone", "personal.address.line1", "personal.address.line2",
            "personal.address.postal_code", "personal.linkedin_url", "personal.github_url", "personal.website"]
    values = [str(prof.get(k)) for k in keys if prof.get(k)]
    if prof.full_name:
        values.append(prof.full_name)
    email = str(prof.get("personal.email") or "")
    local = email.split("@")[0] if "@" in email else ""
    if _identifying(local):
        values.append(local)  # "Signed in as sunflower77" (not an everyday word: "resume@...")
    for key in ("personal.linkedin_url", "personal.github_url", "personal.website"):
        url = str(prof.get(key) or "").strip().rstrip("/")
        if url:
            values.append(re.sub(r"^https?://(www\.)?", "", url, flags=re.I))  # "linkedin.com/in/someone"
            handle = url.rsplit("/", 1)[-1]
            # a profile's handle; a website's last segment only when it isn't a word ("/portfolio")
            if len(handle) >= 4 and "." not in handle and (key != "personal.website" or _identifying(handle)):
                values.append(handle)
    phone = re.sub(r"\D", "", str(prof.get("personal.phone") or ""))
    if len(phone) >= 7:
        values.append(phone)
    ids = prof.get("history.employee_ids")  # an employer's ID for the person
    if isinstance(ids, dict):
        values += [str(v).strip() for v in ids.values() if v not in (None, "") and len(str(v).strip()) >= 3]
    spelled = {w for v in values for w in _spellings(v)}
    # names of two letters ("Al Wu") are kept too: they're matched as whole words only
    names = {str(prof.get(k)) for k in NAME_KEYS if prof.get(k)}
    # longest first so "Sam Rivera" is replaced before "Sam"
    return sorted({v for v in spelled if len(v) >= 3 or v in names and len(v) >= 2}, key=len, reverse=True)


_LETTER = r"[^\W\d_]"
_HUMP = r"(?-i:(?<=[a-z])(?=[A-Z]))"  # a small letter, then a capital: where "JaneDoe" turns to its next word
# A run of text without spaces, quotes, brackets or commas that joins its words with a digit, "_", "@",
# "%", "/", ".", "=" or "+" names something (a user name, an email, a file name, a link, an id) rather
# than being a word of prose: "jdoe1987", "JDoe_Resume.pdf", "jdoe@example.com", "x.example/u/jdoe"
_RUN = re.compile(r"[^\s\"'()<>\[\]{},;|]+")
_JOINED = re.compile(r"\d|[^\W_][_@%/.=+][^\W_]")


def _edge(c: str, ahead: bool, humps: bool, inside: bool = False) -> str:
    """What mustn't be beside a value's first character (or, `ahead`, its last) for it to be a
    whole word: a digit beside a digit, or a letter beside a letter, unless the case turns
    there ("JaneDoe": `humps`). Anything else is the end of a word, an underscore too
    ("Jane_Doe_Resume.pdf"), so a value in an email, an address or a file name is still found.
    `inside` the name of something (`_RUN`), a letter may run on into it: "jdoe1987"."""
    if c.isdecimal():
        return r"(?!\d)" if ahead else r"(?<!\d)"
    if not c.isalpha() or inside:
        return ""  # "(480)", "Jr.": nothing to run on into
    plain = rf"(?!{_LETTER})" if ahead else rf"(?<!{_LETTER})"
    return rf"(?:{plain}|{_HUMP})" if humps else plain


@functools.lru_cache(maxsize=1024)  # asked for each value in every text and attribute of a page
def _pattern(secret: str, inside: bool = False) -> str:
    """A value as a pattern that finds it as a whole word only (never "Ash" in "washing", nor a
    ZIP code in a longer number), and written another way too: a phone number with any
    separators ("(480) 555-0142", "+1 480.555.0142", "480/555/0142", "020 7946 0958"); words with any spacing,
    dots, dashes or underscores between them, or none ("742 W. Evergreen Ter.", "Jane_Doe",
    "janedoe"). Several words may run on at the end, as an address written out does
    ("742 W Evergreen Terrace"). `inside`: for the name of something (`_RUN`), where a surname
    often follows an initial ("jdoe1987"), the value with the letters it runs on into, so none are
    left to guess it by; only a digit beside a digit keeps it apart. A short value (under 5 letters
    or digits) only with an initial or two beside it ("jdoe", "JDoe", its word ending where the case
    turns too: "JDoeResume"), since in a longer word it's that word's, not the person's: not "Tom" in
    "custom_field", nor "Ted" in "status=accepted". A longer one goes wherever it is ("samrivera87")."""
    if secret.isdigit() and len(secret) >= 7:  # its country code (or the US one), or a 0 before it, may be left off
        apart = r"[\s.\-()/]*"  # (and "480/555/0142": the review of Oct 18)
        return (rf"(?<!\d)(?:\+?{apart.join(secret[:-10] or '1')}[\s.\-]*|0[\s.\-]*)?{apart.join(secret[-10:])}"
                r"(?!\d)")
    words = [w.rstrip(".,") for w in secret.split()] if len(secret.split()) > 1 else [secret]
    words = [w for w in words if w] or [secret]
    humps = len(secret) >= 3  # a two-letter name only as a word of its own: not "Do" in "toDo"
    body = (_edge(words[0][0], False, humps, inside) + r"[\s.,_\u00a0-]*".join(map(re.escape, words))
            + (_edge(words[-1][-1], True, humps, inside) if len(words) == 1 else ""))
    if not inside:
        return body
    if sum(c.isalnum() for c in secret) >= 5:
        return rf"(?<!{_LETTER}){_LETTER}*?(?:{body}){_LETTER}*"
    # up to 2 letters beside it, before or after, in a word that begins and ends where letters do or the case turns
    start, end = rf"(?:(?<!{_LETTER})|{_HUMP})", rf"(?:(?!{_LETTER})|{_HUMP})"
    return rf"{start}(?:{_LETTER}{{0,2}}(?:{body})|{_LETTER}?(?:{body}){_LETTER}|(?:{body}){_LETTER}{{2}}){end}"


def _in_names(text: str, find: re.Pattern[str]) -> str:
    """`text` with what `find` finds taken out of each name of something in it (`_RUN`), not its prose."""
    return _RUN.sub(lambda m: find.sub(REDACTED, m[0]) if _JOINED.search(m[0]) else m[0], text)


def redact(text: str, secrets: list[str]) -> str:
    """`text` with each of `secrets` taken out as a whole word (`_pattern`), and also where it runs
    into other letters in the name of something (`_RUN`), with those letters when they're an initial
    or two or the value is long: "jdoe1987" and "JDoe_Resume.pdf" become "REDACTED1987" and
    "REDACTED_Resume.pdf" for a surname "Doe", and "samrivera87" "REDACTED87" for "Rivera", while
    "custom_field" keeps a name "Tom" and "Problem" keeps "Rob"."""
    for s in secrets:
        # a two-letter name only as written ("Al", "Do"): not "all", nor "do" in a question
        text = re.sub(_pattern(s), REDACTED, text, flags=0 if len(s) < 3 else re.I)
        inside = _pattern(s, inside=True)
        if len(s) >= 3 and re.search(_LETTER, s) and re.search(inside, text, flags=re.I):
            # what's taken out already stays as it is: not "Ted" in "REDACTED_Resume.pdf"
            text = _in_names(text, re.compile(rf"{REDACTED}|{inside}", re.I))
    return text


# What a captured page may hold of other people or of a session, whoever's profile captured it (the review of Oct 15: a
# third party's email and phone, an SSN's or a birth date's shape, a candidate id, a token all survived the scrubber,
# which knew only the profile's own strings): each taken out wherever it's written, words or attributes
_EMAIL = re.compile(r"\b[\w.%+-]+@(?!example\.(?:com|org|net)\b)[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b", re.I)
# (and one with no separator after its area code, "(602)5550188": the review of Oct 18)
_PHONE = re.compile(r"(?<![\w-])(?:\+?1[-. ]?)?(?:\(?\d{3}\)?[-. ]\d{3}[-. ]|\(\d{3}\)\d{3}[-. ]?)\d{4}(?![\w-])")
# Ten digits with no separator, or one only after the area code, after a word that says they're a phone's ("Phone:
# 6025550188", "Mobile: +1 6025550188", "Phone (optional) 602-5550188", "call us at +16025550188"): elsewhere they may
# be a requisition's or an order's number
_PHONE_SAID = re.compile(r"\b((?:phone|mobile|cell|telephone|tel|fax|call|text|sms|whatsapp)\b"
                         r"(?:\W{1,3}(?:number|no|us|me|at|on|is)\b)*(?:\s*\([^()]{0,30}\))?\W{1,4}?)"
                         r"(?:\+?1[\s.-]?)?\d{3}[\s.-]?\d{7}(?!\d)", re.I)
_SSN = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
          r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
# A birth date in digits, or with its month's name ("Born 14 March 1990", "Birthday: March 14, 1990": the review of
# Oct 18)
_BIRTH_DATE = re.compile(r"\b((?:date of birth|birth ?date|birthday|born(?: on)?|d\.?o\.?b\.?)\W{0,3})"
                         r"(?:[\d/.-]{6,10}|\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH + r",?\s+\d{4}"
                         r"|" + _MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})(?!\d)", re.I)
_CANDIDATE_ID = re.compile(r"\b((?:candidate|applicant|application|user|profile|person) ?(?:id|number|no\.?|#)\W{0,3})"
                           r"(?=[\w-]*\d)[\w-]{4,}", re.I)  # (one with a digit in it: never the next label's words)
# A license's, a passport's, an EIN's or a card's number after its label ("Driver's License #: D1234567", "Driver
# License: AZ D12345678", "Card number: 4111 1111 1111 1111": the review of Oct 18), as _CANDIDATE_ID: its words with a
# digit in them, groups and all (and a state's code before a license's). A card only as a credit or debit card or
# with its number's word: never "card-header-v2"
_LABELLED_ID = re.compile(r"\b((?:(?:driver[’']?s?\s+)?licen[cs]e|passport|(?-i:EIN)|employer identification|"
                          r"(?:credit|debit)\s+card)\b(?:\s*(?:(?:id|number|no)\b\.?|#))?\W{0,3}"
                          r"|\bcard\s*(?:(?:number|no)\b\.?|#)\W{0,3})"
                          r"(?:(?-i:[A-Z]{2})\s+)?(?=[\w-]*\d)[\w-]{4,}(?:[ ](?=[\w-]*\d)[\w-]+)*", re.I)
# An IBAN after its label, its groups with letters too ("IBAN: GB82 WEST 1234 5698 7654 32")
_IBAN = re.compile(r"\b(IBAN(?:\s*(?:number|no\.?|#))?\W{0,3})[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{1,4}){2,8}\b")
_JWT = re.compile(r"\beyJ[\w-]{4,}\.[\w-]{4,}(?:\.[\w-]*)?")
# a click-to-call link's number, written with no separators ("tel:+16025550147": the review of 0.3.189)
_TEL = re.compile(r"\b(tel:\s*)\+?\d[\d().\s-]{6,}\d", re.I)  # (a "Tel: " label keeps its space)
_LONG_HEX = re.compile(r"\b[0-9a-f]{32,}\b", re.I)


def scrub_others(text: str) -> str:
    """`text` without the details of anyone or any session a page can carry, whoever captured it (an attribute that
    ties the page's parts together keeps more: _scrub_value)."""
    text = _EMAIL.sub("someone@example.com", text)
    text = _JWT.sub(REDACTED, text)
    text = _SSN.sub("000-00-0000", text)
    text = _TEL.sub(r"\g<1>5555550100", text)
    text = _PHONE.sub("555-555-0100", text)
    text = _PHONE_SAID.sub(r"\g<1>5555550100", text)
    text = _BIRTH_DATE.sub(lambda m: m.group(1) + "01/01/2000", text)
    text = _CANDIDATE_ID.sub(lambda m: m.group(1) + REDACTED, text)
    text = _LABELLED_ID.sub(lambda m: m.group(1) + REDACTED, text)
    text = _IBAN.sub(lambda m: m.group(1) + REDACTED, text)
    return _LONG_HEX.sub(REDACTED, text)


# Attributes that tie a page's parts together by their values: kept as they are, long hex, ids and all (only an email
# or a token taken out: the review of 0.3.189 found "userId_1234" and "userId_5678" made one id, and a class list's
# long hex taken out of the page but not its style sheet)
_TIES = {"id", "for", "name", "class", "form", "list", "headers", "aria-labelledby", "aria-describedby",
         "aria-controls", "aria-owns", "aria-activedescendant", "rel", "accept-charset"}
# An address whose query may carry a session or a person (a link's own are handled where it's made offline): taken
# without it. A whole address, or one with a query of name=value, in any attribute; a path from the site's root only in
# one that holds an address (never "Relocate?" in an aria-label: the review of 0.3.189)
_URL_VALUE = re.compile(r"^(?:https?:)?//|^[\w./-]+\?[^=\s]*=")
_PATH_VALUE = re.compile(r"^/[^/]")
_URL_ATTRS = {"src", "href", "action", "formaction", "poster", "data", "cite", "background", "xlink:href", "srcset"}


def _scrub_value(attr: str, value: str, secrets: list[str]) -> str:
    """An attribute's value without the profile's strings or anyone else's details: in one that ties the page's parts
    together, only an email or a token, so the ties hold."""
    value = redact(value, secrets)
    if attr in _TIES:
        return _EMAIL.sub("someone@example.com", _JWT.sub(REDACTED, value))
    return scrub_others(value)
_CSS_URL_QUERY = re.compile(r"""(url\(\s*["']?[^"'()?\s]*)\?[^"')]*(["']?\s*\))""", re.I)


def _redact_page(soup: BeautifulSoup, secrets: list[str]) -> None:
    """Redact the page's words and attribute values, never its tag names or style sheets: a
    last name "Li" mustn't turn <li> into <REDACTED>. And anyone else's details (scrub_others)."""
    for node in soup.find_all(string=True):
        if isinstance(node, Comment):
            node.extract()  # (a comment can hold anything: a session, a debug dump, an address)
            continue
        if node.parent is not None and node.parent.name == "style":
            continue
        new = scrub_others(redact(str(node), secrets))
        if new != str(node):
            node.replace_with(new)
    for tag in soup.find_all(True):
        tag.attrs.pop("srcdoc", None)  # (a frame's whole page in an attribute)
        for attr, value in list(tag.attrs.items()):
            if isinstance(value, list):
                tag[attr] = [_scrub_value(attr, v, secrets) for v in value]  # type: ignore[assignment]  # bs4 writes a list out as words
            elif isinstance(value, str):
                v = value.strip()
                if attr == "style":
                    value = _CSS_URL_QUERY.sub(r"\1\2", value)
                elif ("?" in v and " " not in v
                        and (_URL_VALUE.match(v) or (attr in _URL_ATTRS or attr.startswith("data-"))
                             and _PATH_VALUE.match(v)
                             or attr in _URL_ATTRS and v.startswith("?"))):
                    value = _without_query(v)
                tag[attr] = _scrub_value(attr, value, secrets)


_KEEP_META = re.compile(r"^(charset|viewport|content-type)$", re.I)
_OPAQUE = re.compile(r"^eyJ[\w+/=-]+$|^[\w+/=-]{80,}$")  # base64 state, tokens


def _without_query(url: str) -> str:
    """A link without its query, fragment and path parameters: session ids, tokens and the
    person's email."""
    if url.startswith("#"):
        return "#"  # a link to the page itself stays one ("" would reload it)
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    path = re.sub(r";[^/]*", "", parts.path)  # ";jsessionid=..." path parameters too
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


REMOTE = ("http:", "https:", "//")
# Attributes that make the browser fetch something as soon as the page opens.
FETCH_ATTRS = ("src", "srcset", "poster", "data", "background", "href", "xlink:href")

_CSS_STRUCTURE = re.compile(
    r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|url\((?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^)"'])*\)|[{};]""", re.I)
_CSS_COMMENT = re.compile(r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(/\*.*?\*/)""", re.S)
_CSS_REMOTE_URL = re.compile(
    r"""url\(\s*(?:"(?:https?:)?//[^"]*"|'(?:https?:)?//[^']*'|(?:https?:)?//[^)"'\s]*)\s*\)""", re.I)
_CSS_GROUPS = ("@media", "@supports", "@layer", "@container", "@scope", "@document", "@-moz-document")
_PSEUDO_ELEMENT = re.compile(r"::[\w-]+(?:\([^()]*\))?|:(?:before|after|first-line|first-letter)\b", re.I)
_CSS_STRING = re.compile(r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'""")
_CUSTOM_PROP = re.compile(r"(?<![\w-])(--[\w-]+)\s*:([^;{}]*)")
_VAR_REF = re.compile(r"var\(\s*(--[\w-]+)")


def _split_top(text: str, sep: str) -> list[str]:
    """Split on sep where it isn't inside brackets or quotes."""
    parts, depth, start, quote = [], 0, 0, ""
    for i, c in enumerate(text):
        if quote:
            quote = "" if c == quote and text[i - 1] != "\\" else quote
        elif c in "\"'":
            quote = c
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def _used_custom_properties(css: str) -> set[str]:
    """Custom properties (--name) that something actually reads, directly or through another one."""
    values: dict[str, list[str]] = {}
    for name, value in _CUSTOM_PROP.findall(css):
        values.setdefault(name, []).append(value)
    used = set(_VAR_REF.findall(_CUSTOM_PROP.sub("", css)))
    todo = list(used)
    while todo:
        for value in values.get(todo.pop(), []):
            for name in set(_VAR_REF.findall(value)) - used:
                used.add(name)
                todo.append(name)
    return used


def _drop_custom_properties(css: str, keep: set[str]) -> str:
    out = []
    for prelude, block in _css_rules(css):
        if block is None:
            out.append(prelude + ";")
        elif prelude.lower().startswith(_CSS_GROUPS):
            out.append(f"{prelude}{{{_drop_custom_properties(block, keep)}}}")
        else:
            if not prelude.startswith("@") and not any(m.group() in "{}" for m in _CSS_STRUCTURE.finditer(block)):
                kept = [d for d in _split_top(block, ";")
                        if not (m := re.match(r"(--[\w-]+)\s*:", d)) or m.group(1) in keep]
                block = ";".join(kept)
            out.append(f"{prelude}{{{block}}}")
    return "".join(out)


def _css_rules(css: str) -> list[tuple[str, str | None]]:
    """Top-level (prelude, block) pairs; the block is None for statements like @import."""
    rules: list[tuple[str, str | None]] = []
    depth, start, body = 0, 0, 0
    prelude = ""
    for m in _CSS_STRUCTURE.finditer(css):
        tok = m.group()
        if tok == "{":
            if depth == 0:
                prelude, body = css[start:m.start()].strip(), m.end()
            depth += 1
        elif tok == "}":
            if depth == 0:
                start = m.end()
                continue
            depth -= 1
            if depth == 0:
                rules.append((prelude, css[body:m.start()]))
                start = m.end()
        elif tok == ";" and depth == 0:
            if css[start:m.start()].strip():
                rules.append((css[start:m.start()].strip(), None))
            start = m.end()
    return rules


class _CssPruner:
    """Keeps only the CSS a saved page needs.

    Rules whose selectors match nothing on the page are dropped (sites ship whole
    component libraries, megabytes of them), and so are fonts, @import and remote
    url()s, which would otherwise load from the network whenever a test opens the
    page. Rules for elements that are there stay, so whatever hides or shows a
    field still applies.
    """

    def __init__(self, soup: BeautifulSoup):
        self.soup = soup
        self.tags: set[str] = set()
        self.classes: set[str] = set()
        self.ids: set[str] = set()
        for tag in soup.find_all(True):
            self.tags.add(tag.name.lower())
            classes: str | list[str] = tag.get("class") or []
            self.classes.update(c.lower() for c in ([classes] if isinstance(classes, str) else classes))
            if tag.get("id"):
                self.ids.add(str(tag["id"]).lower())

    def prune(self, css: str) -> str:
        css = _CSS_COMMENT.sub(lambda m: "" if m.group(1) else m.group(), css)
        return _CSS_REMOTE_URL.sub("none", self._prune(css))

    def _prune(self, css: str) -> str:
        out = []
        for prelude, block in _css_rules(css):
            low = prelude.lower()
            if block is None:
                if not low.startswith(("@import", "@charset")):
                    out.append(prelude + ";")
            elif low.startswith("@font-face"):
                continue
            elif low.startswith(_CSS_GROUPS):
                inner = self._prune(block)
                if inner:
                    out.append(f"{prelude}{{{inner}}}")
            elif low.startswith("@") or self.used(prelude):
                out.append(f"{prelude}{{{block}}}")
        return "".join(out)

    def used(self, selectors: str) -> bool:
        for sel in _split_top(selectors, ","):
            plain = _PSEUDO_ELEMENT.sub("", sel).strip()
            if not plain or plain[-1] in ">+~":
                plain += "*"
            if not self._may_match(plain):
                continue
            try:
                if self.soup.select_one(plain) is not None:
                    return True
            except Exception:
                return True  # can't evaluate it here, so keep it
        return False

    def _may_match(self, sel: str) -> bool:
        """Cheap test first: every class, id and tag the selector requires must be on the page."""
        bare = re.sub(r"\[[^\]]*\]", "", _CSS_STRING.sub('""', sel))
        while True:  # what's inside :not(), :is(), :has() isn't required
            shorter = re.sub(r"\([^()]*\)", "", bare)
            if shorter == bare:
                break
            bare = shorter
        for kind, name in re.findall(r"([.#])((?:\\.|[\w-])+)", bare):
            if "\\" not in name and name.lower() not in (self.classes if kind == "." else self.ids):
                return False
        if "\\" in bare:  # an escape like "\61 bc" can end in what looks like a tag name
            return True
        return all(t.lower() in self.tags for t in re.findall(r"(?:^|[\s>+~])([a-zA-Z][\w-]*)", bare))


def clean_html(raw: str, secrets: list[str], frame_map: dict[str, str] | None = None, base_url: str = "") -> str:
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.find_all(["script", "noscript", "base"]):
        tag.decompose()
    for tag in soup.find_all("meta", attrs={"http-equiv": re.compile("^refresh$", re.I)}):
        tag.decompose()
    # meta tags carry CSRF tokens and session ids; only the page's charset and viewport are kept
    for tag in soup.find_all("meta"):
        if not (tag.get("charset") or _KEEP_META.match(str(tag.get("name") or tag.get("http-equiv") or ""))):
            tag.decompose()
    # what the person typed or chose: free-text answers, picked options, ticked boxes
    for tag in soup.find_all("textarea"):
        tag.string = ""
    for tag in soup.find_all(attrs={"contenteditable": re.compile("^(true|)$", re.I)}):  # type: ignore[call-overload]  # bs4 stubs want a name too
        tag.string = ""
    # Keep fixtures hermetic: nothing may load from the network when a test opens them.
    for tag in soup.find_all("link"):
        if str(tag.get("href", "")).startswith(REMOTE):
            tag.decompose()
    for tag in soup.find_all(["img", "source", "video", "audio"]):
        tag.attrs.pop("src", None)
        tag.attrs.pop("srcset", None)
    for tag in soup.find_all(True):
        for attr in [a for a in tag.attrs if a.startswith("data-ja-") or a.startswith("on")]:
            del tag[attr]
        if tag.name == "input" and tag.get("type") not in ("radio", "checkbox", "submit", "button"):
            tag.attrs.pop("value", None)
        for attr in ("checked", "selected", "aria-checked", "aria-selected"):
            tag.attrs.pop(attr, None)
        for attr in [a for a in tag.attrs if a.startswith("data-") and _OPAQUE.match(str(tag.get(a) or ""))]:
            del tag[attr]
        if tag.name in ("a", "area", "form", "link"):
            for attr in ("href", "action"):
                if tag.get(attr):
                    tag[attr] = _without_query(str(tag[attr]))
        if tag.name == "iframe" and frame_map and tag.get("src"):
            absolute = urljoin(base_url, str(tag["src"]))
            if absolute in frame_map:
                tag["src"] = frame_map[absolute]
        if tag.name not in ("a", "area", "link", "form"):
            for attr in FETCH_ATTRS:
                if str(tag.get(attr, "")).strip().lower().startswith(REMOTE):
                    del tag[attr]
        if tag.get("style"):
            tag["style"] = _CSS_REMOTE_URL.sub("none", str(tag["style"]))
    pruner = _CssPruner(soup)
    sheets = [(tag, pruner.prune(tag.get_text())) for tag in soup.find_all("style")]
    # Design-token sheets define thousands of --variables; keep the ones something reads.
    keep = _used_custom_properties(
        "\n".join(css for _, css in sheets) + "\n" + "\n".join(str(t["style"]) for t in soup.find_all(style=True)))
    for tag, css in sheets:
        css = _CSS_URL_QUERY.sub(r"\1\2", _drop_custom_properties(css, keep))  # (a local address's query too)
        if css:
            tag.string = css
        else:
            tag.decompose()
    _redact_page(soup, secrets)
    return str(soup)


# Buttons a fixture's expectation keeps: the ones that move an application on, not a site's navigation
_STEP_WORDS = re.compile(r"\b(next|continue|submit|apply|sign ?in|log ?in|create|account|save|accept|agree|"
                         r"acknowledge|read|upload|review|start)\b", re.I)


def convert(snapshot: Path, name: str, out_dir: Path = DEFAULT_DIR, prof: Profile | None = None,
            secrets: list[str] | None = None) -> list[Path]:
    """Scrub a saved snapshot into `out_dir`: `{name}.html`, its frames, and `{name}.expect.json`.
    `secrets` (default: the profile's personal strings) are what's taken out."""
    meta = json.loads((snapshot / "snapshot.json").read_text(encoding="utf-8"))
    secrets = secrets if secrets is not None else personal_strings(prof or Profile.load())
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    frame_map: dict[str, str] = {}
    for fr in meta.get("frames", []):
        if fr["file"] != "page.html" and fr.get("url"):
            frame_map[fr["url"]] = f"{name}.{fr['file']}"
    for fr in meta.get("frames", []):
        src = snapshot / fr["file"]
        if not src.exists():
            continue
        target = out_dir / (f"{name}.html" if fr["file"] == "page.html"
                            else frame_map.get(fr.get("url", ""), f"{name}.{fr['file']}"))
        html = clean_html(src.read_text(encoding="utf-8"), secrets, frame_map, fr.get("url") or meta.get("url", ""))
        target.write_text(html, encoding="utf-8")
        written.append(target)

    expect: dict[str, Any] = {
        "source_url": redact(_without_query(meta.get("url", "")), secrets),
        "note": meta.get("note", ""),
        "fields": [
            {k: f[k] for k in ("label", "kind", "required") if k in f}
            for f in meta.get("fields", [])
            if f.get("label")
        ],
        # the buttons that move an application on, as the form reader read them (a privacy statement's link
        # Amkor's site drew longer than it took a button's words to be went unread, live check Oct 11)
        "actions": sorted({str(a.get("text") or "").strip() for a in meta.get("actions", [])
                           if _STEP_WORDS.search(str(a.get("text") or "")) and len(str(a.get("text") or "")) <= 120}),
    }
    expect_path = out_dir / f"{name}.expect.json"
    # written with its letters as they are ("José", not "Jos\\u00e9"), so the redaction finds them
    # (and anyone else's details, as on the page: else a label the page has scrubbed reads differently here, and the
    # labels' third parties stay in it: the review of 0.3.189)
    expect_path.write_text(scrub_others(redact(json.dumps(expect, indent=2, ensure_ascii=False), secrets)),
                           encoding="utf-8")
    written.append(expect_path)
    return written


# Decision replay (a page's <name>.decision.json): what the desk decides on a saved page, recorded by the live check
# (scripts/live_smoke.py --fixtures) and decided again offline by tests/test_live_fixtures.py, so a change to the
# pipeline or the answer rules that decides otherwise on a page a nightly stopped on is seen. Scripts are taken out
# of the saved pages, so what a site draws with them isn't replayed: only what the saved HTML shows.
DECISION_SUFFIX = ".decision.json"
# The keys compared (the "run" entry is what the live run itself said there, kept to read, not decided again)
DECISION_KEYS = ("page", "next", "captcha", "security_asked", "account_and_application", "job_search", "fields")
REPLAY_RESUME = "jobdesk-test-resume.pdf"  # (live_identity.RESUME_NAME: the run's own test resume's name)
# The applicant the decisions are made for: the live check's technician (scripts/live_profile.py's FAKE_PROFILE, which
# tests/test_live_fixtures.py keeps this the same as) with a fixed address and a test resume of its own
REPLAY_PROFILE: dict[str, Any] = {
    "personal": {
        "first_name": "Testy", "last_name": "McTestface", "email": "testy.mctestface@example.com",
        "phone": "480-555-0199", "phone_country_code": "+1",
        "address": {"line1": "175 S Arizona Ave", "city": "Chandler", "state": "AZ", "postal_code": "85225",
                    "country": "United States"},
        "linkedin_url": "https://www.linkedin.com/in/example-test-profile", "drivers_license": True,
    },
    "documents": {"resume": None},
    "work_authorization": {"authorized_to_work": True, "requires_sponsorship": False, "us_person": True,
                           "us_citizen": True, "over_18": True},
    "preferences": {"how_did_you_hear": "Company Website", "willing_to_relocate": True,
                    "willing_to_travel": "Yes", "flexible_schedule": True,
                    "locations": ["Phoenix, AZ", "Chandler, AZ", "Tempe, AZ"]},
    "eeo": {"gender": "Decline to self-identify", "hispanic_latino": "Decline to self-identify",
            "race": "Decline to self-identify", "veteran": "I don't wish to answer",
            "disability": "I do not want to answer"},
    "work_history": [{"title": "Equipment Technician", "company": "Example Fab", "location": "Chandler, AZ",
                      "start": "2021-03", "end": "present",
                      "description": "Maintained and repaired process equipment; ran preventive maintenance."}],
    "education_history": [{"school": "Arizona State University", "degree": "Bachelor of Science",
                           "major": "Electrical Engineering", "start": 2016, "end": 2020}],
    "education": {"highest_degree": "Bachelor's Degree",
                  "degrees_earned": [{"level": "high_school"},
                                     {"level": "bachelor", "field": "Electrical Engineering"}]},
    "history": {"us_government_employee": False, "us_military": False},
    "settings": {"submit_mode": "dry_run", "browser_channel": "chromium", "headless": True, "accept_cookies": True},
}


def replay_profile(folder: Path) -> Profile:
    """REPLAY_PROFILE, with its test resume written into `folder` (the rules answer a file box only with a file
    that's there)."""
    resume = folder / REPLAY_RESUME
    if not resume.exists():
        folder.mkdir(parents=True, exist_ok=True)
        resume.write_bytes(b"%PDF-1.4\n% a test resume for replaying the desk's decisions\n")
    data = copy.deepcopy(REPLAY_PROFILE)
    data["documents"] = {"resume": str(resume)}
    return Profile(data)


async def read_saved(srv: Any, html: Path) -> tuple[dict[str, Any], str]:
    """A saved page opened in the server's browser and read as the Job Desk reads a page (Applier._look): the form
    without its lists' choices, the first 4000 characters of its words, and whether a CAPTCHA's pictures show."""
    await srv.browser.goto(html.resolve().as_uri())
    data = await srv.inspect_form(include_dropdown_options=False)
    text = await srv.page_text(4000)
    data["challenge"] = await srv.browser.challenge_showing()
    return data, text


def field_decision(field: dict[str, Any], prof: Profile, file_inputs: int = 1) -> str:
    """What the answer rules do with a box (autofill.resolve_field, for no job in particular): "filled by <rule>",
    "skipped by <rule>" (the rule leaves it empty on purpose), "asked" (no answer, and it's required: the person's)
    or "skipped" (no answer, and it isn't required)."""
    from .autofill import SKIP, resolve_field

    ans = resolve_field(field, prof, {}, file_inputs)
    if ans is None:
        return "asked" if field.get("required") else "skipped"
    if ans.value in (None, "", SKIP):
        return f"skipped by {ans.rule}"
    return f"filled by {ans.rule}"


def decide(data: dict[str, Any], text: str, prof: Profile) -> dict[str, Any]:
    """What the desk decides on a page it has read (read_saved's `data` and `text`): the page's kind
    (pipeline.classify), the button it would press on (pick_next's), whether a CAPTCHA is on it, security questions
    are asked, an account form is the whole application, or it's a job search, and each box's answer (field_decision),
    by "<label> (<kind>)", numbered from the second of the same ("#2")."""
    from . import pipeline

    kind = pipeline.classify(data, text)
    step = pipeline.pick_next(data.get("actions") or [], kind == "form")
    fields = data.get("fields") or []
    file_inputs = sum(1 for f in fields if f.get("kind") == "file")
    decided: dict[str, str] = {}
    for f in fields:
        key = f"{str(f.get('label') or f.get('name') or '').strip()} ({f.get('kind')})"
        n, base = 1, key
        while key in decided:
            n += 1
            key = f"{base} #{n}"
        decided[key] = field_decision(f, prof, file_inputs)
    return {
        "page": kind,
        "next": str(step.get("text") or "").strip() if step else None,
        "captcha": pipeline._captcha_on(data),
        "security_asked": pipeline._security_asked(data),
        "account_and_application": pipeline._account_and_application(data),
        "job_search": pipeline._job_search(data),
        "fields": decided,
    }


def decision_differences(name: str, was: dict[str, Any], now: dict[str, Any]) -> list[str]:
    """Where a page's decision now differs from the one recorded, each said with the page and the key."""
    out = []
    for key in DECISION_KEYS:
        if key == "fields":
            old, new = was.get("fields") or {}, now.get("fields") or {}
            for label in sorted(set(old) | set(new)):
                if old.get(label) != new.get(label):
                    out.append(f"{name}: fields[{label!r}]: was {old.get(label, '(no such box)')!r}, "
                               f"now {new.get(label, '(no such box)')!r}")
        elif was.get(key) != now.get(key):
            out.append(f"{name}: {key}: was {was.get(key)!r}, now {now.get(key)!r}")
    return out


def scrubbed_decision(decision: dict[str, Any], secrets: list[str] | None = None) -> dict[str, Any]:
    """A decision as its file keeps it: `secrets` and anyone else's details taken out, as from its expect.json (so one
    decided again offline is compared as it would be written)."""
    return json.loads(scrub_others(redact(json.dumps(decision, ensure_ascii=False), secrets or [])))


def write_decision(path: Path, decision: dict[str, Any], secrets: list[str] | None = None) -> Path:
    """A page's decision written beside its expect.json (scrubbed_decision)."""
    path.write_text(json.dumps(scrubbed_decision(decision, secrets), indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot", type=Path, help="a debug/<timestamp> folder from a job's application folder")
    ap.add_argument("name", help="fixture name, e.g. workday-my-information")
    ap.add_argument("--out", type=Path, default=DEFAULT_DIR)
    args = ap.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.name):
        ap.error("name must be lowercase letters, digits, - or _")
    for p in convert(args.snapshot, args.name, args.out):
        print(p)
    print("\nEdit the .expect.json to keep only fields you've checked, and look the HTML over for "
          "personal data the profile didn't cover before committing.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
