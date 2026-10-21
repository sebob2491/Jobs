"""The person's inbox, for the sign-up codes and confirmation links job sites email them.

Used only with an app password the person saved on the Job Desk (``email_password``), and
only while a job waits on an emailed code or link. The inbox is opened read-only and mail
is fetched with BODY.PEEK, so nothing is marked read or changed. Only mail that arrived
after the desk began waiting, from the job site it's waiting on (its own domain or its job
system's), is looked at, and only the code or link is kept from it (a password reset's, from
when the desk asked for it). The inbox first, and its
Spam (Junk) folder when the inbox has nothing: a site's first mail to an address sometimes lands
there (BrassRing's own page says to look there), and it's the same code from the same sender.

The same checks read the messages Claude hands the desk from the Gmail connector (settings.email_codes, with no app
password: the desk's mail requests), one message at a time over plain data (Mail): its time against the wait's window,
its sender against the job's own, another job's mail by its subject, then the code, link or reset found in it (pick).
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import functools
import html
import imaplib
import math
import re
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
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
    "greenhouse": {"greenhouse.io", "greenhouse-mail.io"},
    "lever": {"lever.co"},
    "paycom": {"paycom.com", "paycomonline.net", "paycomonline.com"},
    "taleo": {"taleo.net", "oracle.com"},
    "brassring": {"brassring.com", "kenexa.com"},  # (IBM Kenexa BrassRing: Kenexa's own domain too)
}
# A refusal says so, whatever else it says ("Too many login failures, try again later" is still one)
_REFUSED = re.compile(r"authenticationfailed|authentication failed|invalid credentials|login failed|"
                      r"incorrect (?:username|password)|bad credentials", re.I)
_BUSY = re.compile(r"unavailable|temporar|try again|later|throttl|too many|limit|overquota|server ?bug", re.I)
# seconds before the wait began: a code sent just as the page asked for it; and, for a password reset's email (`since`
# is when the desk asked for it), the mail service's clock and this computer's apart (the pipeline prefers one that
# came after: RESET_FRESH_WAIT)
LOOK_BACK = 120
NEWEST = 30  # messages looked at per check, newest first

_CODE_WORDS = re.compile(r"verification|one[- ]time|passcode|security code|\bcode\b|\bpin\b|\botp\b", re.I)
_DIGITS = re.compile(r"(?<![\d\-/.:+$])\b(\d{4,8})\b(?![\-/.:]?\d)")
_MIXED = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6,10}\b")
# The word before "code" that makes it another kind: "promo code SAVE20", "ZIP code 85201", "area code 480"
_OTHER_CODE = re.compile(r"\b(?:promo\w*|discount|coupon|zip|postal|area|country|"
                         r"dial(?:ing)?|referral|offer|gift|voucher|"
                         r"tracking|dress|tax|bar|qr|source|course|reference|booking|confirmation number)\s*$", re.I)
# Between the words and a year-like code ("Your code is 2047"): nothing but joining words
_JOINING = re.compile(r"^[\s:\-\u2013\u2026.]*(?:is|was|below)?[\s:\-\u2013\u2026.]*$", re.I)
_LINK_WORDS = re.compile(r"verif|confirm|activat|validat", re.I)
# A password reset's link (settings.manage_accounts: the desk asked the site for one)
_RESET_WORDS = re.compile(r"reset|password|recover|passwd|pwd", re.I)
# A link in the same email that undoes or refuses ("Not you? Deactivate", "unsubscribe")
_UNDOES = re.compile(r"deactivat|unsubscrib|opt-?out", re.I)  # in a link's query: career_ns=account_deactivation
_NOT_THIS_LINK = re.compile(r"deactivat|invalidat|unsubscrib|opt-?out|not-?you|report|declin|reject|cancel", re.I)
# A number named as something else just before it: "(Job 2617841)", "Requisition #2505303"
_NAMED_NUMBER = re.compile(r"(?:\bjob|\breq(?:uisition)?|\bref(?:erence)?)(?:\s+(?:id|code|"
                           r"number|no\.?))?\s*[:#.]?\s*$|#\s*$",
                           re.I)
# Markup that styles a code's characters apart without parting them: <b>48</b><b>2913</b> is one
# code. Only between two characters a code is made of; anywhere else a tag parts the words.
_SPLIT_CODE = re.compile(r"(?<=[0-9A-Z])(?:</?(?:b|strong|span|i|em|u|font|small|big|mark|code|tt|sup|sub)\b[^>]*>)+"
                         r"(?=[0-9A-Z])")
_URL = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)


class MailboxError(Exception):
    """The inbox couldn't be read (a refused app password, no IMAP for the address, a network fault)."""


@dataclass
class Found:
    kind: str  # "code" or "link"
    value: str
    sender: str  # the sender's domain, for the log
    received: float
    spam: bool = False  # it was in the Spam (Junk) folder, not the inbox


# Providers that deliver "name+tag@" to "name@" (plus addressing): that inbox is signed in to as "name@"
PLUS_ADDRESSING = {"gmail.com", "googlemail.com", "icloud.com", "me.com"}


def login_address(address: str) -> str:
    """The address an inbox is signed in to with: a profile's "name+jobs@gmail.com" is "name@gmail.com"'s."""
    local, _, domain = address.strip().rpartition("@")
    if local and domain.lower() in PLUS_ADDRESSING and "+" in local:
        return f"{local.split('+', 1)[0]}@{domain}"
    return address.strip()


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


def _parts(msg: email.message.EmailMessage) -> tuple[list[str], list[str]]:
    """A message's text: its plain-text words, then its HTML's words (an email whose plain
    part leaves the code out has it in the HTML), and the links in its HTML."""
    plain: list[str] = []
    markup: list[str] = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        try:
            body = part.get_content()
        except (LookupError, ValueError):
            continue
        (markup if part.get_content_subtype() == "html" else plain).append(str(body))
    texts = ["\n".join(plain)] if plain else []
    if not markup:
        return texts, []
    words, links = html_words(markup)
    return texts + [words], links


def html_words(markup: list[str]) -> tuple[str, list[str]]:
    """An email's HTML parts as words (a code its markup styles apart read as one, scripts and styles left out) and
    the links in them: the inbox's (_parts) and the Gmail connector's (connector_mail) alike."""
    links = [html.unescape(h) for page in markup for h in re.findall(r"""href\s*=\s*["']([^"']+)""", page, re.I)]
    words = "\n".join(re.sub(r"<[^>]+>", " ", _SPLIT_CODE.sub("", re.sub(r"(?is)<(script|style).*?</\1>", " ", page)))
                      for page in markup)
    return html.unescape(words), links


def find_code(text: str) -> str | None:
    """The code in a sign-up email: digits (or capitals with digits) soon after "code",
    "verification" or "one-time", not a year, a phone number, a price or a job's number.
    One straight after the words ("code: 218335", "code is K7Q2ZP") comes first, wherever
    it is; otherwise the nearest after the first of the words."""
    return _find_code(text)[0]


def _find_code(text: str) -> tuple[str | None, bool]:
    """find_code's answer, and whether it came straight after the words (a sure one)."""
    text = re.sub(r"\s+", " ", text)  # an HTML mail's indentation is no distance
    nearest = None
    for m in _CODE_WORDS.finditer(text):
        if _OTHER_CODE.search(text[max(0, m.start() - 30): m.start()]):
            continue  # a promo code's, a ZIP code's: another kind of code (the review of Oct 15)
        window = text[m.end(): m.end() + 160]
        found = []
        for pattern in (_DIGITS, _MIXED):
            for c in pattern.finditer(window):
                value = c.group(1) if pattern is _DIGITS else c.group(0)
                before = window[:c.start()]
                if pattern is _DIGITS and len(value) == 4 and value.startswith(("19", "20")) \
                        and not _JOINING.match(before):
                    continue  # a year, unless it follows the words straight away
                # (its last few words alone: the pattern ends where the number begins, and the whole text before
                # each number made a long email's read take seconds, the review of 0.3.228)
                if _NAMED_NUMBER.search(text[max(0, m.end() + c.start() - 80):m.end() + c.start()]):
                    continue  # "(Job 2617841)": the job's number, not the code
                found.append((c.start(), value))
        found.sort()  # nearest first, digits or not: "K7Q2ZP. It expires in 1440 minutes"
        for start, value in found:
            if _JOINING.match(window[:start]):
                return value, True
        if found and nearest is None:
            nearest = found[0][1]
    return nearest, False


def find_link(text: str, links: list[str], allowed_link: Callable[[str], bool],
              words: re.Pattern[str] = _LINK_WORDS) -> str | None:
    """A confirmation link in a sign-up email that points back to the job site: one whose
    address itself says verify or confirm before one that only says so in its query (a logo
    link tagged "utm_campaign=email_verification"), and never "Not you? Deactivate". With
    `words`=_RESET_WORDS, a password reset's link instead."""
    def path(url: str) -> str | None:
        try:
            return urlparse(url).path
        except ValueError:  # a template's placeholder ("https://[UNSUBSCRIBE_URL]/"): not a link to open
            return None

    urls = [u.strip().rstrip(".,;") for u in [*links, *_URL.findall(text)]]
    urls = [u for u in urls if (p := path(u)) is not None and not _NOT_THIS_LINK.search(p)
            and not _UNDOES.search(urlparse(u).query) and allowed_link(u)]
    for where in (path, lambda u: u):
        for url in urls:
            if words.search(where(url) or ""):
                return url
    return None


def connect(address: str, password: str) -> tuple[imaplib.IMAP4, str]:
    """The address's IMAP server, signed in with the app password, and its host. MailboxError for a
    refused app password, an address the desk can't read mail for, and a network fault."""
    host = imap_host(address)
    if host is None:
        raise MailboxError(f"the desk can't read mail for {address.rsplit('@', 1)[-1]} addresses")
    try:
        box = imaplib.IMAP4_SSL(host, timeout=20, ssl_context=_tls())
    except ssl.SSLCertVerificationError as e:
        # (something between here and the mail service, or no root certificates: said without the words of a
        # refused app password, which would retire the saved one until a new one is saved)
        raise MailboxError(f"{host}'s certificate didn't check out ({getattr(e, 'verify_message', None) or e}): "
                           f"something between this computer and {host} is answering for it, or this computer has "
                           "no root certificates to check it against; nothing was sent to it") from e
    except OSError as e:
        raise MailboxError(f"couldn't reach {host}: {e}") from e
    try:
        try:
            box.login(address, password)
        except imaplib.IMAP4.abort as e:  # the connection went: a Wi-Fi blip, not the password
            raise MailboxError(f"lost the connection to {host}") from e
        except imaplib.IMAP4.error as e:
            if _BUSY.search(str(e)) and not _REFUSED.search(str(e)):  # Gmail's "[UNAVAILABLE] Temporary System Problem"
                raise MailboxError(f"{host} is busy just now") from e
            raise MailboxError(f"{host} didn't accept the email app password") from e
        except UnicodeError as e:  # app passwords are plain letters and digits
            raise MailboxError("the email app password has characters an app password never has; "
                               "copy it again from your email account's app-password page") from e
    except MailboxError:
        logout(box)
        raise
    return box, host


@functools.lru_cache(maxsize=1)
def _tls() -> ssl.SSLContext:
    """A connection that checks the mail service's certificate and name: imaplib's own checks neither, so anything
    on the way could stand in for the service and take the app password. The system's root certificates, and
    certifi's beside them for a Python installed without any (python.org's on macOS). Made once: every look in
    the inbox connects."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass
    return ctx


def logout(box: imaplib.IMAP4) -> None:
    try:
        box.logout()
    except (imaplib.IMAP4.error, OSError):
        pass


# A line of the server's folder list: its flags, its separator and its name: (\HasNoChildren \Junk) "/" "[Gmail]/Spam"
_LISTED = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>"(?:[^"\\]|\\.)*"|\S+)\s*$')
# The names mail services give that folder, for a server that doesn't flag it \Junk
_JUNK_NAMES = {"spam", "junk", "junk e-mail", "junk email", "bulk mail", "bulk", "[gmail]/spam"}


def junk_folder(box: imaplib.IMAP4) -> str | None:
    """The account's Spam (Junk) folder: the one the server flags \\Junk (Gmail's "[Gmail]/Spam",
    iCloud's "Junk"), else one named as mail services name it. None when it has none."""
    _, listing = box.list()
    named = None
    for line in listing or []:
        m = _LISTED.match(line.decode("utf-8", "replace")) if isinstance(line, bytes) else None
        if m is None or "\\noselect" in m["flags"].lower():
            continue
        name = m["name"]
        if name.startswith('"'):
            name = re.sub(r"\\(.)", r"\1", name[1:-1])
        if "\\junk" in m["flags"].lower():
            return name
        if named is None and name.lower() in _JUNK_NAMES:
            named = name
    return named


def quoted(folder: str) -> str:
    """A folder's name as SELECT takes it: quoted, which imaplib leaves as it is (or, newer, sees is quoted)."""
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def headers(box: imaplib.IMAP4, ids: list[bytes],
            fields: str = "FROM") -> list[tuple[float, bytes, email.message.EmailMessage]]:
    """When each of these messages arrived, its id and the header fields asked for ("FROM SUBJECT"),
    in one round trip."""
    _, heads = box.fetch(b",".join(ids), f"(INTERNALDATE BODY.PEEK[HEADER.FIELDS ({fields})])")  # type: ignore[arg-type]  # imaplib takes bytes too
    out: list[tuple[float, bytes, email.message.EmailMessage]] = []
    for i, part in enumerate(heads):
        if not isinstance(part, tuple):
            continue
        # the date comes before the header, or (as some servers order them) after it
        after = heads[i + 1] if i + 1 < len(heads) and isinstance(heads[i + 1], bytes) else b""
        try:
            stamp = imaplib.Internaldate2tuple(part[0]) or imaplib.Internaldate2tuple(after)  # type: ignore[arg-type]  # bytes: checked above
            received = time.mktime(stamp) if stamp else 0.0
            head = email.message_from_bytes(part[1], policy=email.policy.default)
            mid = part[0].split()[0]
        except Exception:  # an odd message: not one to read
            continue
        out.append((received, mid, head))
    return out


def message_code(msg: email.message.EmailMessage, texts: list[str] | None = None) -> str | None:
    """The code in a message. `texts`: its words (_parts), where they're at hand."""
    if texts is None:
        texts = _parts(msg)[0]
    return _code_in(str(msg.get("Subject") or ""), texts)


def _code_in(subject: str, texts: list[str]) -> str | None:
    """The code in a message with this subject and these words (plain text first, then its HTML's)."""
    # its words before its subject: "Your verification code" in a subject would
    # otherwise read on into the body's first number (the job's)
    # the subject on its own last, then running into the words (a subject "Your
    # verification code" over a body "Use 482913 to verify your email")
    tries = [*texts, subject, subject + "\n" + (texts[0] if texts else "")]
    # a code straight after the words, in any of them, before a nearest guess: the
    # plain text may only name the job's number while the HTML shows the code
    answers = [_find_code(t) for t in tries]
    return next((v for v, sure in answers if v and sure), None) or next((v for v, _ in answers if v), None)


def search(address: str, password: str, since: float, allowed: set[str], want: str,
           allowed_link: Callable[[str], bool] = lambda url: False, before: float | None = None,
           look_back: float = LOOK_BACK, others: Callable[[str, str], bool] | None = None) -> Found | None:
    """The newest code (want="code"), confirmation link (want="link") or password reset link
    (want="reset") from an allowed
    sender that arrived after `since` (a time.time()) less `look_back`, and before `before`
    when given (mail after then is another waiting job's), and not one `others` says is another job's (from its
    sender's domain and its subject). Read-only: nothing is marked read.
    The inbox first; its Spam folder only when the inbox has none."""
    box, host = connect(address, password)
    try:
        args = (since, allowed, want, allowed_link, before, look_back)
        found = _scan(box, "INBOX", *args, others=others)
        if found is None:
            try:
                junk = junk_folder(box)
                found = _scan(box, quoted(junk), *args, signed_only=True, others=others) if junk else None
            except imaplib.IMAP4.error:  # a Spam folder that won't open: the inbox's answer (none) stands
                found = None
            if found is not None:
                found.spam = True
        return found
    except (imaplib.IMAP4.error, OSError) as e:
        raise MailboxError(f"reading {host} failed: {e}") from e
    finally:
        logout(box)


def signed_by_sender(head: email.message.Message) -> bool:
    """Did the mail service find this message really sent from its From address's domain? Its
    Authentication-Results say dmarc=pass, or dkim=pass for that domain (header.d / header.i)."""
    domain = email.utils.parseaddr(str(head.get("From") or ""))[1].rsplit("@", 1)[-1].lower()
    said = " ".join(str(v) for v in head.get_all("Authentication-Results") or []).lower()
    if not domain or not said:
        return False
    if re.search(r"\bdmarc=pass\b", said):
        return True
    return any(d == domain or domain.endswith("." + d) or d.endswith("." + domain)
               for d in re.findall(r"\bdkim=pass\b[^;]*?header\.[di]=@?([a-z0-9.-]+)", said))


def _scan(box: imaplib.IMAP4, folder: str, since: float, allowed: set[str], want: str,
          allowed_link: Callable[[str], bool], before: float | None, look_back: float,
          signed_only: bool = False, others: Callable[[str, str], bool] | None = None) -> Found | None:
    """search's answer from one folder. `signed_only` (the Spam folder, where mail that only claims a
    sender ends up): only mail the mail service found really sent from its sender's domain (DMARC, or
    DKIM signed by that domain, in its Authentication-Results)."""
    box.select(folder, readonly=True)
    day = time.strftime("%d-%b-%Y", time.gmtime(since - look_back - 86400))
    _, data = box.search(None, "SINCE", day)
    ids = (data[0] or b"").split()[-NEWEST:]
    if not ids:
        return None
    # When each arrived and who sent it, in one round trip; the whole message only for the
    # job site's own mail since the wait began
    wanted = []
    fields = "FROM" + (" AUTHENTICATION-RESULTS" if signed_only else "") + (" SUBJECT" if others else "")
    for received, mid, head in headers(box, ids, fields):
        sender = str(head.get("From") or "")
        if signed_only and not signed_by_sender(head):
            continue
        if in_window(sender, str(head.get("Subject") or ""), received, since, allowed, before, look_back, others):
            wanted.append((received, mid, sender))
    for received, mid, sender in sorted(wanted, key=lambda w: w[0], reverse=True):  # newest first
        _, parts = box.fetch(mid, "(BODY.PEEK[])")  # type: ignore[arg-type]  # imaplib takes bytes too
        raw = next((p for p in parts if isinstance(p, tuple)), None)
        if raw is None:
            continue
        try:
            msg = email.message_from_bytes(raw[1], policy=email.policy.default)
            texts, links = _parts(msg)
            value = found_in(want, str(msg.get("Subject") or ""), texts, links, allowed_link)
        except Exception:  # a malformed message: the next one
            continue
        if value:
            return Found(want, value, _found_from(sender), received)
    return None


# ------------------------------------------------- one message's checks, wherever it was read
def _domain(sender: str) -> str:
    """A From header's address's domain ("careers.ti.com"), in lower case."""
    return email.utils.parseaddr(sender)[1].rsplit("@", 1)[-1].lower()


def _found_from(sender: str) -> str:
    """A From header's address's domain as Found keeps it (for the log, and _MAIL_TAKEN's key): as written."""
    return email.utils.parseaddr(sender)[1].rsplit("@", 1)[-1]


def in_window(sender: str, subject: str, received: float, since: float, allowed: set[str], before: float | None,
              look_back: float, others: Callable[[str, str], bool] | None = None) -> bool:
    """Is a message one a waiting job may read its code or link from, by its header alone: arrived after `since` less
    `look_back` and before `before` (when given: mail after then is another waiting job's), from one of the `allowed`
    senders, and not one `others` says is another job's (by its sender's domain and its subject)? The inbox's (_scan)
    and the Gmail connector's (pick) alike."""
    return (received >= since - look_back and (before is None or received < before) and sender_allowed(sender, allowed)
            and not (others is not None and others(_domain(sender), subject)))


def found_in(want: str, subject: str, texts: list[str], links: list[str],
             allowed_link: Callable[[str], bool]) -> str | None:
    """The code (want="code"), confirmation link ("link") or password reset link ("reset") in a message, by its subject,
    its words (plain text first, then its HTML's) and its HTML's links: a link only where `allowed_link` takes it."""
    if want == "code":
        return _code_in(subject, texts)
    words = _RESET_WORDS if want == "reset" else _LINK_WORDS
    return next((v for t in texts or [""] if (v := find_link(t, links, allowed_link, words))), None)


@dataclass
class Mail:
    """A message as the checks read it, wherever it was read: its From header, when it arrived (epoch seconds), its
    subject, its words (plain text first, then its HTML's) and its HTML's links."""
    sender: str
    received: float
    subject: str = ""
    texts: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


def pick(mails: list[Mail], since: float, allowed: set[str], want: str,
         allowed_link: Callable[[str], bool] = lambda url: False, before: float | None = None,
         look_back: float = LOOK_BACK, others: Callable[[str, str], bool] | None = None) -> Found | None:
    """search's answer from messages read elsewhere (the Gmail connector's, handed to the desk by Claude): the newest
    code, link or reset that passes the very checks search makes (in_window, then found_in), or None."""
    wanted = [m for m in mails if in_window(m.sender, m.subject, m.received, since, allowed, before, look_back, others)]
    for m in sorted(wanted, key=lambda m: m.received, reverse=True):  # newest first
        try:
            value = found_in(want, m.subject, m.texts, m.links, allowed_link)
        except Exception:  # an odd message: the next one
            continue
        if value:
            return Found(want, value, _found_from(m.sender), m.received)
    return None


# Characters of a handed message's text (and of its HTML, should it come with one) read: a sign-up email's plain text is
# a few thousand, and the code and its link come early in it (the review of 0.3.228: all read off the event loop, and no
# 200k-character body for the slower patterns to crawl)
MAIL_TEXT_MAX = 20_000
FUTURE_SKEW = 300  # seconds a message's date may run ahead of this computer's clock (one further ahead is no date)
# A date as a person reads it (Gmail's own page: "Tue, Oct 20, 2026, 8:01 AM"), in this computer's time
_HUMAN_DATES = ("%a, %b %d, %Y, %I:%M %p", "%a, %b %d, %Y, %I:%M:%S %p", "%b %d, %Y, %I:%M %p", "%b %d, %Y %I:%M %p",
                "%b %d, %Y, %I:%M:%S %p", "%B %d, %Y, %I:%M %p", "%B %d, %Y %I:%M %p", "%m/%d/%Y, %I:%M %p",
                "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S")


def received_at(said: Any, now: float | None = None) -> float | None:
    """When a message arrived, as a mail service says it, in whole seconds: epoch seconds (or Gmail's internalDate,
    milliseconds), as a number or digits, an RFC 2822 date ("Tue, 20 Oct 2026 08:01:02 -0700"), ISO 8601
    ("2026-10-20T08:01:02-07:00"), or a date as Gmail's page shows it ("Oct 20, 2026, 8:01 AM"). One that names no
    zone is this computer's time. None for anything else, and for a date more than FUTURE_SKEW ahead of now (one that
    would outrank every real message, and never age out of the codes kept as used)."""
    now = time.time() if now is None else now
    when: float | None = None
    if isinstance(said, bool):
        return None
    if isinstance(said, (int, float)) or isinstance(said, str) and re.fullmatch(r"\s*\d{9,14}(\.\d+)?\s*", said):
        value = float(said)
        when = value / 1000 if value > 1e11 else value  # (milliseconds: Gmail's internalDate)
    elif isinstance(said, str) and said.strip():
        text = " ".join(said.replace("\u202f", " ").replace("\xa0", " ").split())
        moment: datetime | None = None
        for pattern in _HUMAN_DATES:  # (before RFC 2822's reader, which takes "8:01 PM" for 8:01 in the morning)
            try:
                moment = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if moment is None and not re.search(r"\b[AP]M\b", text, re.I):
            try:
                moment = email.utils.parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError):
                moment = None
        if moment is None:
            try:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
            except ValueError:
                return None
        when = moment.timestamp()  # (naive: this computer's time, as the person reads it)
    if when is None or not math.isfinite(when) or when <= 0 or when > now + FUTURE_SKEW:
        return None
    return float(round(when))  # (whole seconds: the same message handed in another spelling is the same message)


def connector_mail(message: dict[str, Any]) -> Mail | None:
    """A message as the Gmail connector gives it (and Claude hands it on), read as the inbox's are: its sender (`from`,
    or the connector's `sender`: an address, or {name, email}, its address alone), its date (`internalDate` when it's
    there, Gmail's own; else `date`: received_at), its `subject`, its words (`text`, `plaintext_body` or `body`, then
    `html` or `html_body`'s words, should one come) and its links (in its words, its HTML's, and any given as `links`),
    each MAIL_TEXT_MAX characters at most. None without a sender or a date that reads (its window can't be checked)."""
    who = message.get("from", message.get("sender"))
    if isinstance(who, dict):  # (its address alone: a name may hold another address)
        who = str(who.get("email") or who.get("address") or "")
    given_at = message.get("internalDate")
    when = received_at(given_at) if given_at not in (None, "") else None
    if when is None:
        when = received_at(message.get("date"))
    if not isinstance(who, str) or not who.strip() or when is None:
        return None
    plain = next((v for k in ("text", "plaintext_body", "body") if isinstance(v := message.get(k), str) and v), "")
    markup = next((v for k in ("html", "html_body") if isinstance(v := message.get(k), str) and v), "")
    texts = [plain[:MAIL_TEXT_MAX]] if plain else []
    links: list[str] = []
    if markup:
        words, links = html_words([markup[:MAIL_TEXT_MAX]])
        texts.append(words)
    given = message.get("links")
    if isinstance(given, list):
        links += [str(u) for u in given[:200] if isinstance(u, str)]
    subject = message.get("subject")
    return Mail(who.strip(), when, subject if isinstance(subject, str) else "", texts, links)
