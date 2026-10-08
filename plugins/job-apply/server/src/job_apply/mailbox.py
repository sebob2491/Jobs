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

# Where each mail service takes IMAP sign-ins, by the domain of the address
IMAP_HOSTS = {
    "gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com", "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com", "msn.com": "outlook.office365.com",
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
LOOK_BACK = 120  # seconds before the wait began: a code sent just as the page asked for it
NEWEST = 30  # messages looked at per check, newest first

_CODE_WORDS = re.compile(r"verification|one[- ]time|passcode|security code|\bcode\b|\bpin\b|\botp\b", re.I)
_DIGITS = re.compile(r"(?<![\d\-/.:+$])\b(\d{4,8})\b(?![\-/.:]?\d)")
_MIXED = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,10}\b")
_LINK_WORDS = re.compile(r"verif|confirm|activat|validat", re.I)
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


def _text(msg: email.message.Message) -> tuple[str, list[str]]:
    """A message's words (plain text, or its HTML without tags) and the links in its HTML."""
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
    if plain:
        return "\n".join(plain), links
    words = "\n".join(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", page)) for page in markup)
    return html.unescape(words), links


def find_code(text: str) -> str | None:
    """The code in a sign-up email: digits (or capitals with digits) soon after "code",
    "verification" or "one-time", not a year, a phone number or a price."""
    for m in _CODE_WORDS.finditer(text):
        window = text[m.start(): m.end() + 160]
        for pattern in (_DIGITS, _MIXED):
            for c in pattern.finditer(window):
                value = c.group(1) if pattern is _DIGITS else c.group(0)
                if pattern is _DIGITS and len(value) == 4 and value.startswith(("19", "20")):
                    continue  # a year
                return value
    return None


def find_link(text: str, links: list[str], allowed_link: Callable[[str], bool]) -> str | None:
    """A confirmation link in a sign-up email that points back to the job site."""
    for url in [*links, *_URL.findall(text)]:
        url = url.strip().rstrip(".,;")
        if _LINK_WORDS.search(url) and allowed_link(url):
            return url
    return None


def search(address: str, password: str, since: float, allowed: set[str], want: str,
           allowed_link: Callable[[str], bool] = lambda url: False) -> Found | None:
    """The newest code (want="code") or confirmation link (want="link") from an allowed
    sender that arrived after `since` (a time.time()). Read-only: nothing is marked read."""
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
        except imaplib.IMAP4.error as e:
            raise MailboxError(f"{host} didn't accept the email app password") from e
        box.select("INBOX", readonly=True)
        day = time.strftime("%d-%b-%Y", time.gmtime(since - LOOK_BACK - 86400))
        _, data = box.search(None, "SINCE", day)
        ids = (data[0] or b"").split()[-NEWEST:]
        for mid in reversed(ids):
            _, parts = box.fetch(mid, "(INTERNALDATE BODY.PEEK[])")
            raw = next((p for p in parts if isinstance(p, tuple)), None)
            if raw is None:
                continue
            stamp = imaplib.Internaldate2tuple(raw[0])
            received = time.mktime(stamp) if stamp else 0.0
            if received < since - LOOK_BACK:
                continue
            msg = email.message_from_bytes(raw[1], policy=email.policy.default)
            sender = str(msg.get("From") or "")
            if not sender_allowed(sender, allowed):
                continue
            text, links = _text(msg)
            subject = str(msg.get("Subject") or "")
            if want == "code":
                value = find_code(subject + "\n" + text)
            else:
                value = find_link(text, links, allowed_link)
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
