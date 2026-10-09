"""Turn a debug snapshot of a real application page into a regression-test fixture.

    uv run --project <plugin>/server python -m job_apply.fixtures <snapshot dir> <name>

The page's DOM is kept (scripts removed) so tests can check that field labels,
kinds and required flags are still extracted correctly. Profile values (name,
email, phone, address, links) are replaced before anything is written. Check the
output for other personal data before committing it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .config import Profile

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "live"
REDACTED = "REDACTED"


NAME_KEYS = ("personal.first_name", "personal.middle_name", "personal.last_name", "personal.preferred_name")


def _spellings(value: str) -> set[str]:
    """How a page may write a value: as is, without its accents, with them as separate marks,
    JSON-escaped (in a data-* attribute) and URL-encoded (in a link or a file name)."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))
    out = {value, plain, unicodedata.normalize("NFD", value), json.dumps(value)[1:-1],
           json.dumps(value)[1:-1].replace("@", "\\u0040"), quote(value, safe=""), quote(value, safe="").replace("%20", "+")}
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
    spelled = {w for v in values for w in _spellings(v)}
    # names of two letters ("Al Wu") are kept too: they're matched as whole words only
    names = {str(prof.get(k)) for k in NAME_KEYS if prof.get(k)}
    # longest first so "Sam Rivera" is replaced before "Sam"
    return sorted({v for v in spelled if len(v) >= 3 or v in names and len(v) >= 2}, key=len, reverse=True)


def _pattern(secret: str) -> str:
    """A value as a pattern that also finds it written another way: a phone number with any
    separators ("(480) 555-0142", "+1 480.555.0142"); words with any spacing or dots
    between them ("742 W. Evergreen Ter."); a short name as a whole word only."""
    if secret.isdigit() and len(secret) >= 7:
        digits = secret[-10:]
        return r"(?:\+?1[\s.\-]*)?" + r"[\s.\-()]*".join(digits)
    words = secret.split()
    body = r"[\s.,\u00a0]+".join(re.escape(w.rstrip(".,")) for w in words) if len(words) > 1 else re.escape(secret)
    return rf"(?<!\w){body}(?!\w)" if len(secret) < 3 else body


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        # a two-letter name only as written ("Al", "Do"): not "all", nor "do" in a question
        text = re.sub(_pattern(s), REDACTED, text, flags=0 if len(s) < 3 else re.I)
    return text


def _redact_page(soup: BeautifulSoup, secrets: list[str]) -> None:
    """Redact the page's words and attribute values, never its tag names or style sheets: a
    last name "Li" mustn't turn <li> into <REDACTED>."""
    for node in soup.find_all(string=True):
        if node.parent is not None and node.parent.name == "style":
            continue
        new = redact(str(node), secrets)
        if new != str(node):
            node.replace_with(new)
    for tag in soup.find_all(True):
        for attr, value in list(tag.attrs.items()):
            if isinstance(value, list):
                tag[attr] = [redact(v, secrets) for v in value]  # type: ignore[assignment]  # bs4 writes a list out as words
            elif isinstance(value, str):
                tag[attr] = redact(value, secrets)


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
        css = _drop_custom_properties(css, keep)
        if css:
            tag.string = css
        else:
            tag.decompose()
    _redact_page(soup, secrets)
    return str(soup)


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
        target = out_dir / (f"{name}.html" if fr["file"] == "page.html" else frame_map.get(fr.get("url", ""), f"{name}.{fr['file']}"))
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
    }
    expect_path = out_dir / f"{name}.expect.json"
    # written with its letters as they are ("José", not "Jos\\u00e9"), so the redaction finds them
    expect_path.write_text(redact(json.dumps(expect, indent=2, ensure_ascii=False), secrets), encoding="utf-8")
    written.append(expect_path)
    return written


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
