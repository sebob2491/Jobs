"""Reading a sign-up code or confirmation link from the person's inbox."""

import email.utils
import imaplib
import time

import pytest

from job_apply import mailbox
from job_apply.mailbox import MailboxError, find_code, find_link, imap_host, sender_allowed, site_domain


def test_where_mail_is_read_and_whose_it_is():
    assert imap_host("someone@gmail.com") == "imap.gmail.com"
    assert imap_host("someone@outlook.com") == "outlook.office365.com"
    assert imap_host("someone@example.com") is None
    assert site_domain("careers.ti.com") == "ti.com"
    assert site_domain("amat.wd1.myworkdayjobs.com") == "myworkdayjobs.com"
    assert site_domain("jobs.example.co.uk") == "example.co.uk"
    allowed = {"ti.com", "oraclecloud.com"}
    assert sender_allowed("TI Careers <no-reply@careers.ti.com>", allowed)
    assert sender_allowed("noreply@oraclecloud.com", allowed)
    assert not sender_allowed("Deals <offers@not-ti.com>", allowed)  # a look-alike, not a subdomain
    assert not sender_allowed("someone@gmail.com", allowed)


def test_the_code_in_a_sign_up_email():
    assert find_code("Your verification code is 482913. It expires in 10 minutes.") == "482913"
    assert find_code("Use this one-time passcode to continue: 7 3 1") is None
    # not the year, a phone number or a price next to the words
    assert find_code("Security code for your 2026 application: 55021. Questions? Call 480-555-0199.") == "55021"
    assert find_code("Your code\n\n  913 204\n") is None  # split digits aren't one code
    assert find_code("Enter code AB12CD to verify your email") == "AB12CD"
    assert find_code("Thanks for applying to the Field Service Engineer role (R2617841).") is None


def test_a_confirmation_link_back_to_the_job_site():
    links = ["https://www.example.com/unsubscribe", "https://amat.wd1.myworkdayjobs.com/External/verifyEmail?token=abc",
             "https://evil.example.net/verify?token=abc"]
    own = lambda url: "myworkdayjobs.com" in url  # noqa: E731
    assert find_link("", links, own) == "https://amat.wd1.myworkdayjobs.com/External/verifyEmail?token=abc"
    assert find_link("Confirm here: https://evil.example.net/confirm?x=1", [], own) is None
    assert find_link("Nothing to click", ["https://amat.wd1.myworkdayjobs.com/External/jobs"], own) is None


def _message(sender: str, subject: str, body: str, html: bool = False) -> bytes:
    kind = "text/html" if html else "text/plain"
    return (f"From: {sender}\r\nTo: sam@gmail.com\r\nSubject: {subject}\r\n"
            f"Date: {email.utils.formatdate()}\r\nMIME-Version: 1.0\r\nContent-Type: {kind}; charset=utf-8\r\n\r\n"
            f"{body}\r\n").encode()


class FakeImap:
    """An IMAP server with a few messages; records how it was used."""
    instances: list["FakeImap"] = []
    messages: list[tuple[float, bytes]] = []
    accept = True

    def __init__(self, host, timeout=None):
        self.host, self.calls = host, []
        FakeImap.instances.append(self)

    def login(self, user, password):
        self.calls.append(("login", user))
        if not FakeImap.accept:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")

    def select(self, box, readonly=False):
        self.calls.append(("select", box, readonly))
        return "OK", [b"3"]

    def search(self, charset, *criteria):
        self.calls.append(("search",) + criteria)
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(FakeImap.messages)))]

    def fetch(self, mid, parts):
        self.calls.append(("fetch", mid, parts))
        when, raw = FakeImap.messages[int(mid) - 1]
        stamp = imaplib.Time2Internaldate(when).encode()
        return "OK", [(b"%s (INTERNALDATE %s BODY[] {%d}" % (mid, stamp, len(raw)), raw), b")"]

    def logout(self):
        self.calls.append(("logout",))


@pytest.fixture
def imap(monkeypatch):
    FakeImap.instances, FakeImap.messages, FakeImap.accept = [], [], True
    monkeypatch.setattr(mailbox.imaplib, "IMAP4_SSL", FakeImap)
    return FakeImap


def test_the_newest_code_from_the_job_site_since_the_wait_began(imap):
    since = time.time()
    imap.messages = [
        (since - 3600, _message("no-reply@careers.ti.com", "Your code", "Your verification code is 111111")),  # old
        (since + 30, _message("Deals <deals@shop.com>", "Your code", "Your verification code is 222222")),  # not the site
        (since + 40, _message("no-reply@careers.ti.com", "Verify your email",
                              "<p>Your verification code is <b>333333</b></p>", html=True)),
    ]
    found = mailbox.search("sam@gmail.com", "app-password", since, {"ti.com"}, "code")
    assert (found.kind, found.value, found.sender) == ("code", "333333", "careers.ti.com")
    calls = imap.instances[0].calls
    assert ("select", "INBOX", True) in calls  # read-only: nothing is marked read
    assert all(c[2] == "(INTERNALDATE BODY.PEEK[])" for c in calls if c[0] == "fetch")
    assert calls[-1] == ("logout",)
    imap.messages = imap.messages[:2]
    assert mailbox.search("sam@gmail.com", "app-password", since, {"ti.com"}, "code") is None


def test_a_refused_app_password_says_so(imap):
    imap.accept = False
    with pytest.raises(MailboxError, match="app password"):
        mailbox.search("sam@gmail.com", "wrong", time.time(), {"ti.com"}, "code")
    with pytest.raises(MailboxError, match="can't read mail for example.com"):
        mailbox.search("sam@example.com", "x", time.time(), {"ti.com"}, "code")
