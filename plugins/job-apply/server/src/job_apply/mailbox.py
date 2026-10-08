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
LOOK_BACK = 120  # seconds before the wait began: a code sent just as the page asked for it
NEWEST = 30  # messages looked at per check, newest first

_CODE_WORDS = re.compile(r"verification|one[- ]time|passcode|security code|\bcode\b|\bpin\b|\botp\b", re.I)
_DIGITS = re.compile(r"(?<![\d\-/.:+$])\b(\d{4,8})\b(?![\-/.:]?\d)")
_MIXED = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,10}\b")
# Between the words and a year-like code ("Your code is 2047"): nothing but joining words
_JOINING = re.compile(r"^[\s:\-\u2013]*(?:is|was|below)?[\s:\-\u2013]*$", re.I)
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
    text = re.sub(r"\s+", " ", text)  # an HTML mail's indentation is no distance
    for m in _CODE_WORDS.finditer(text):
        window = text[m.end(): m.end() + 160]
        for pattern in (_DIGITS, _MIXED):
            for c in pattern.finditer(window):
                value = c.group(1) if pattern is _DIGITS else c.group(0)
                if pattern is _DIGITS and len(value) == 4 and value.startswith(("19", "20")) \
                        and not _JOINING.match(window[:c.start()]):
                    continue  # a year, unless it follows the words straight away
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
           allowed_link: Callable[[str], bool] = lambda url: False, before: float | None = None) -> Found | None:
    """The newest code (want="code") or confirmation link (want="link") from an allowed
    sender that arrived after `since` (a time.time()), and before `before` when given (mail
    after then is another waiting job's). Read-only: nothing is marked read."""
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
            if received >= since - LOOK_BACK and (before is None or received < before) and sender_allowed(sender, allowed):
                wanted.append((received, mid, sender))
        for received, mid, sender in sorted(wanted, key=lambda w: w[0], reverse=True):  # newest first
            _, parts = box.fetch(mid, "(BODY.PEEK[])")
            raw = next((p for p in parts if isinstance(p, tuple)), None)
            if raw is None:
                continue
            try:
                msg = email.message_from_bytes(raw[1], policy=email.policy.default)
                text, links = _text(msg)
                if want == "code":
                    value = find_code(str(msg.get("Subject") or "") + "\n" + text)
                else:
                    value = find_link(text, links, allowed_link)
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
