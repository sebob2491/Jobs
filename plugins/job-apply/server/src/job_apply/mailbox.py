"""The person's inbox, for the sign-up codes and confirmation links job sites email them.

Used only with an app password the person saved on the Job Desk (``email_password``), and
only while a job waits on an emailed code or link. The inbox is opened read-only and mail
is fetched with BODY.PEEK, so nothing is marked read or changed. Only mail that arrived
after the desk began waiting, from the job site it's waiting on (its own domain or its job
system's), is looked at, and only the code or link is kept from it.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import html
import imaplib
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

# Where each mail service takes IMAP sign-ins with an app password, by the domain of the address.
# Microsoft's (outlook.com, hotmail.com) no longer takes app passwords over IMAP, only its own sign-in.
IMAP_HOSTS = {
    "gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
    "yahoo.com": "imap.mail.yahoo.com", "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com",
    "aol.com": "imap.aol.com",
}
# The addresses each job system sends its codes and links from
ATS_MAIL_DOMAINS = {
    "workday": {"myworkday.com", "workday.com", "myworkdayjobs.com"},
    "oracle_hcm": {"oracle.com", "oraclecloud.com"},
    "successfactors": {"successfactors.com", "successfactors.eu", "sapsf.com", "sap.com"},
    "icims": {"icims.com"},
    "ukg": {"ultipro.com", "ukg.com", "ukg.net"},
    "infor": {"infor.com", "inforcloudsuite.com"},
    "applicantstack": {"applicantstack.com"},
    "eightfold": {"eightfold.ai"},
    "smartrecruiters": {"smartrecruiters.com"},
    "greenhouse": {"greenhouse.io"},
    "lever": {"lever.co"},
    "paycom": {"paycom.com", "paycomonline.net", "paycomonline.com"},
    "taleo": {"taleo.net", "oracle.com"},
}
_BUSY = re.compile(r"unavailable|temporar|try again|later|throttl|too many|limit|overquota|server ?bug", re.I)
LOOK_BACK = 120  # seconds before the wait began: a code sent just as the page asked for it
NEWEST = 30  # messages looked at per check, newest first

_CODE_WORDS = re.compile(r"verification|one[- ]time|passcode|security code|\bcode\b|\bpin\b|\botp\b", re.I)
_DIGITS = re.compile(r"(?<![\d\-/.:+$])\b(\d{4,8})\b(?![\-/.:]?\d)")
_MIXED = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,10}\b")
# Between the words and a year-like code ("Your code is 2047"): nothing but joining words
_JOINING = re.compile(r"^[\s:\-\u2013]*(?:is|was|below)?[\s:\-\u2013]*$", re.I)
_LINK_WORDS = re.compile(r"verif|confirm|activat|validat", re.I)
# A link in the same email that undoes or refuses ("Not you? Deactivate", "unsubscribe")
_NOT_THIS_LINK = re.compile(r"deactivat|invalidat|unsubscrib|opt-?out|not-?you|report|declin|reject|cancel", re.I)
# A number named as something else just before it: "(Job 2617841)", "Requisition #2505303"
_NAMED_NUMBER = re.compile(r"(?:\bjob|\breq(?:uisition)?|\bposition|\bposting|\bref(?:erence)?|#)\s*[:#.]?\s*$", re.I)
# Markup that styles words without parting them: a code drawn as <b>48</b><b>2913</b> is one code
_INLINE_TAGS = re.compile(r"</?(?:b|strong|span|i|em|u|font|small|big|mark|code|tt|sup|sub)\b[^>]*>", re.I)
_URL = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)


class MailboxError(Exception):
    """The inbox couldn't be read (a refused app password, no IMAP for the address, a network fault)."""


@dataclass
class Found:
    kind: str  # "code" or "link"
    value: str
    sender: str  # the sender's domain, for the log
    received: float


def imap_host(address: str) -> str | None:
    return IMAP_HOSTS.get(address.rsplit("@", 1)[-1].lower().strip()) if "@" in address else None


def site_domain(host: str) -> str:
    """A host's site, near enough ("careers.ti.com" -> "ti.com")."""
    parts = [p for p in (host or "").lower().strip(".").split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov"):
        return ".".join(parts[-3:])  # example.co.uk
    return ".".join(parts[-2:])


def sender_allowed(sender: str, allowed: set[str]) -> bool:
    domain = email.utils.parseaddr(sender)[1].rsplit("@", 1)[-1].lower()
    return bool(domain) and any(domain == d or domain.endswith("." + d) for d in allowed)


def _parts(msg: email.message.Message) -> tuple[list[str], list[str]]:
    """A message's text: its plain-text words, then its HTML's words (an email whose plain
    part leaves the code out has it in the HTML), and the links in its HTML."""
    plain, markup = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        try:
            body = part.get_content()
        except (LookupError, ValueError):
            continue
        (markup if part.get_content_subtype() == "html" else plain).append(str(body))
    links = [html.unescape(h) for page in markup for h in re.findall(r"""href\s*=\s*["']([^"']+)""", page, re.I)]
    words = "\n".join(re.sub(r"<[^>]+>", " ", _INLINE_TAGS.sub("", re.sub(r"(?is)<(script|style).*?</\1>", " ", page)))
                      for page in markup)
    texts = ["\n".join(plain)] if plain else []
    return texts + ([html.unescape(words)] if markup else []), links


def _text(msg: email.message.Message) -> tuple[str, list[str]]:
    """A message's words (plain text, or its HTML without tags) and the links in its HTML."""
    texts, links = _parts(msg)
    return (texts[0] if texts else ""), links


def find_code(text: str) -> str | None:
    """The code in a sign-up email: digits (or capitals with digits) soon after "code",
    "verification" or "one-time", not a year, a phone number, a price or a job's number.
    One straight after the words ("code: 218335", "code is K7Q2ZP") comes first, wherever
    it is; otherwise the nearest after the first of the words."""
    text = re.sub(r"\s+", " ", text)  # an HTML mail's indentation is no distance
    nearest = None
    for m in _CODE_WORDS.finditer(text):
        window = text[m.end(): m.end() + 160]
        found = []
        for pattern in (_DIGITS, _MIXED):
            for c in pattern.finditer(window):
                value = c.group(1) if pattern is _DIGITS else c.group(0)
                before = window[:c.start()]
                if pattern is _DIGITS and len(value) == 4 and value.startswith(("19", "20")) \
                        and not _JOINING.match(before):
                    continue  # a year, unless it follows the words straight away
                if _NAMED_NUMBER.search(text[:m.end() + c.start()]):
                    continue  # "(Job 2617841)": the job's number, not the code
                found.append((c.start(), value))
        found.sort()  # nearest first, digits or not: "K7Q2ZP. It expires in 1440 minutes"
        for start, value in found:
            if _JOINING.match(window[:start]):
                return value
        if found and nearest is None:
            nearest = found[0][1]
    return nearest


def find_link(text: str, links: list[str], allowed_link: Callable[[str], bool]) -> str | None:
    """A confirmation link in a sign-up email that points back to the job site: one whose
    address itself says verify or confirm before one that only says so in its query (a logo
    link tagged "utm_campaign=email_verification"), and never "Not you? Deactivate"."""
    urls = [u.strip().rstrip(".,;") for u in [*links, *_URL.findall(text)]]
    urls = [u for u in urls if not _NOT_THIS_LINK.search(urlparse(u).path) and allowed_link(u)]
    for where in (lambda u: urlparse(u).path, lambda u: u):
        for url in urls:
            if _LINK_WORDS.search(where(url)):
                return url
    return None


def search(address: str, password: str, since: float, allowed: set[str], want: str,
           allowed_link: Callable[[str], bool] = lambda url: False, before: float | None = None,
           look_back: float = LOOK_BACK) -> Found | None:
    """The newest code (want="code") or confirmation link (want="link") from an allowed
    sender that arrived after `since` (a time.time()) less `look_back`, and before `before`
    when given (mail after then is another waiting job's). Read-only: nothing is marked read."""
    host = imap_host(address)
    if host is None:
        raise MailboxError(f"the desk can't read mail for {address.rsplit('@', 1)[-1]} addresses")
    try:
        box = imaplib.IMAP4_SSL(host, timeout=20)
    except OSError as e:
        raise MailboxError(f"couldn't reach {host}: {e}") from e
    try:
        try:
            box.login(address, password)
        except imaplib.IMAP4.abort as e:  # the connection went: a Wi-Fi blip, not the password
            raise MailboxError(f"lost the connection to {host}") from e
        except imaplib.IMAP4.error as e:
            if _BUSY.search(str(e)):  # Gmail's "[UNAVAILABLE] Temporary System Problem"
                raise MailboxError(f"{host} is busy just now") from e
            raise MailboxError(f"{host} didn't accept the email app password") from e
        except UnicodeError as e:  # app passwords are plain letters and digits
            raise MailboxError("the email app password has characters an app password never has; "
                               "copy it again from your email account's app-password page") from e
        box.select("INBOX", readonly=True)
        day = time.strftime("%d-%b-%Y", time.gmtime(since - look_back - 86400))
        _, data = box.search(None, "SINCE", day)
        ids = (data[0] or b"").split()[-NEWEST:]
        if not ids:
            return None
        # When each arrived and who sent it, in one round trip; the whole message only for the
        # job site's own mail since the wait began
        _, heads = box.fetch(b",".join(ids), "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM)])")
        wanted = []
        for i, part in enumerate(heads):
            if not isinstance(part, tuple):
                continue
            # the date comes before the header, or (as some servers order them) after it
            after = heads[i + 1] if i + 1 < len(heads) and isinstance(heads[i + 1], bytes) else b""
            try:
                stamp = imaplib.Internaldate2tuple(part[0]) or imaplib.Internaldate2tuple(after)
                received = time.mktime(stamp) if stamp else 0.0
                sender = str(email.message_from_bytes(part[1], policy=email.policy.default).get("From") or "")
                mid = part[0].split()[0]
            except Exception:  # an odd message: not one to read
                continue
            if received >= since - look_back and (before is None or received < before) and sender_allowed(sender, allowed):
                wanted.append((received, mid, sender))
        for received, mid, sender in sorted(wanted, key=lambda w: w[0], reverse=True):  # newest first
            _, parts = box.fetch(mid, "(BODY.PEEK[])")
            raw = next((p for p in parts if isinstance(p, tuple)), None)
            if raw is None:
                continue
            try:
                msg = email.message_from_bytes(raw[1], policy=email.policy.default)
                texts, links = _parts(msg)
                if want == "code":
                    # its words before its subject: "Your verification code" in a subject would
                    # otherwise read on into the body's first number (the job's)
                    value = next((v for t in [*texts, str(msg.get("Subject") or "")] if (v := find_code(t))), None)
                else:
                    value = next((v for t in texts or [""] if (v := find_link(t, links, allowed_link))), None)
            except Exception:  # a malformed message: the next one
                continue
            if value:
                return Found(want, value, email.utils.parseaddr(sender)[1].rsplit("@", 1)[-1], received)
        return None
    except (imaplib.IMAP4.error, OSError) as e:
        raise MailboxError(f"reading {host} failed: {e}") from e
    finally:
        try:
            box.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
