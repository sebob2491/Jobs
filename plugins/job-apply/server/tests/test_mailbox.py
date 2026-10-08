"""Reading a sign-up code or confirmation link from the person's inbox."""

import email.utils
import imaplib
import time

import pytest

from job_apply import mailbox
from job_apply.mailbox import MailboxError, find_code, find_link, imap_host, sender_allowed, site_domain


def test_where_mail_is_read_and_whose_it_is():
    assert imap_host("someone@gmail.com") == "imap.gmail.com"
    assert imap_host("someone@outlook.com") is None  # Microsoft takes no app passwords over IMAP now
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
    # a four-digit code that looks like a year, right after the words
    assert find_code("Your verification code is 2047") == "2047"
    assert find_code("Your code: 1998. It expires soon.") == "1998"
    # an HTML mail's table layout: the code in the next cell, after lots of indentation
    cell = "Your verification code is" + " " * 40 + "\n" * 6 + " " * 120 + "\n" + " " * 80 + "837201"
    assert find_code(cell) == "837201"


def test_the_job_systems_own_mail_as_sent_live():
    """The shapes of real sign-up mail (Oct 2026): Oracle sends from a long workflow address
    and names the requisition before the code; Workday's links come from otp.workday.com."""
    oracle = mailbox.ATS_MAIL_DOMAINS["oracle_hcm"]
    assert sender_allowed("x.fa.sender@workflow.email.us-phoenix-1.ocs.oraclecloud.com", oracle)
    assert sender_allowed("Send-Only.example@otp.workday.com", mailbox.ATS_MAIL_DOMAINS["workday"])
    assert sender_allowed("confirm@eightfold.ai", mailbox.ATS_MAIL_DOMAINS["eightfold"])
    assert find_code("We need you to confirm your identity so your application can be considered for the "
                     "position of Equipment Technician - Pump/Abatement - 2505303.\n\nConfirm your identity "
                     "using this code: 218335.\n\nThe code will expire in 10 minutes.") == "218335"
    assert find_code("Verify your email We just need to verify your email address. Enter the following code "
                     "below to complete the verification process: 109409 The code will expire after 15 minutes.") == "109409"
    link = "https://example.wd1.myworkdayjobs.com/External/activate/abc123/?redirect=%2FExternal%2Fjob%2F1"
    assert find_link(f"Click this link to confirm your email address {link}", [], lambda u: "myworkdayjobs" in u) == link


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

    date_last = False  # the server sends a message's date after its header, not before

    def fetch(self, mids, parts):
        self.calls.append(("fetch", mids, parts))
        out = []
        for mid in mids.decode().split(","):
            when, raw = FakeImap.messages[int(mid) - 1]
            stamp = imaplib.Time2Internaldate(when).encode()
            if "HEADER.FIELDS" in parts:
                head = b"".join(line + b"\r\n" for line in raw.split(b"\r\n") if line.lower().startswith(b"from:")) + b"\r\n"
                if FakeImap.date_last:
                    out += [(b"%s (BODY[HEADER.FIELDS (FROM)] {%d}" % (mid.encode(), len(head)), head),
                            b" INTERNALDATE %s)" % stamp]
                else:
                    out += [(b"%s (INTERNALDATE %s BODY[HEADER.FIELDS (FROM)] {%d}" % (mid.encode(), stamp, len(head)),
                             head), b")"]
            else:
                out += [(b"%s (BODY[] {%d}" % (mid.encode(), len(raw)), raw), b")"]
        return "OK", out

    def logout(self):
        self.calls.append(("logout",))


@pytest.fixture
def imap(monkeypatch):
    FakeImap.instances, FakeImap.messages, FakeImap.accept, FakeImap.date_last = [], [], True, False
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
    fetches = [c for c in calls if c[0] == "fetch"]
    assert all("BODY.PEEK[" in c[2] for c in fetches)
    # who sent each, in one round trip; the whole message only for the site's mail since the wait
    assert [(c[1], c[2]) for c in fetches] == [(b"1,2,3", "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM)])"),
                                              (b"3", "(BODY.PEEK[])")]
    assert calls[-1] == ("logout",)
    imap.messages = imap.messages[:2]
    assert mailbox.search("sam@gmail.com", "app-password", since, {"ti.com"}, "code") is None
    imap.messages, imap.date_last = imap.messages + [(since + 50, _message(
        "no-reply@careers.ti.com", "Verify", "Your verification code is 444444"))], True
    assert mailbox.search("sam@gmail.com", "app-password", since, {"ti.com"}, "code").value == "444444"


def test_a_malformed_message_is_passed_over(imap, monkeypatch):
    since = time.time()
    imap.messages = [
        (since + 10, _message("no-reply@careers.ti.com", "Verify", "Your verification code is 555555")),
        (since + 20, _message("no-reply@careers.ti.com", "Verify", "Your verification code is 666666")),
    ]
    real = mailbox._text

    def text(msg):  # the newest one can't be read
        if "666666" in str(msg.get_payload()):
            raise IndexError("a broken header")
        return real(msg)

    monkeypatch.setattr(mailbox, "_text", text)
    assert mailbox.search("sam@gmail.com", "app-password", since, {"ti.com"}, "code").value == "555555"


def test_a_refused_app_password_says_so(imap):
    imap.accept = False
    with pytest.raises(MailboxError, match="app password"):
        mailbox.search("sam@gmail.com", "wrong", time.time(), {"ti.com"}, "code")
    with pytest.raises(MailboxError, match="can't read mail for example.com"):
        mailbox.search("sam@example.com", "x", time.time(), {"ti.com"}, "code")


def test_mail_after_another_jobs_wait_began_is_that_jobs(imap):
    """Workday sends every employer's codes from one address: a code that came after a later
    job began waiting on its own is that job's, not this one's."""
    since = time.time()
    imap.messages = [
        (since + 10, _message("acme@otp.workday.com", "Verify", "Your verification code is 111111")),
        (since + 60, _message("other@otp.workday.com", "Verify", "Your verification code is 222222")),
    ]
    workday = mailbox.ATS_MAIL_DOMAINS["workday"]
    assert mailbox.search("sam@gmail.com", "pw", since, workday, "code").value == "222222"
    assert mailbox.search("sam@gmail.com", "pw", since, workday, "code", before=since + 50).value == "111111"
