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


def test_a_password_resets_link_is_found_only_when_one_was_asked_for():
    """settings.manage_accounts asks a site for a password reset: its email's link is the reset
    page's, never a sign-up confirmation's, and a confirmation search never opens a reset link."""
    own = lambda url: "myworkdayjobs" in url  # noqa: E731
    reset = "https://amat.wd1.myworkdayjobs.com/External/passwordReset?token=abc"
    links = ["https://amat.wd1.myworkdayjobs.com/External/jobs", reset]
    assert find_link("Reset your password with the link below", links, own, mailbox._RESET_WORDS) == reset
    assert find_link("Reset your password with the link below", links, own) is None
    assert find_link("", ["https://evil.example.net/resetPassword?t=1"], own, mailbox._RESET_WORDS) is None


def _message(sender: str, subject: str, body: str, html: bool = False) -> bytes:
    kind = "text/html" if html else "text/plain"
    return (f"From: {sender}\r\nTo: sam@gmail.com\r\nSubject: {subject}\r\n"
            f"Date: {email.utils.formatdate()}\r\nMIME-Version: 1.0\r\nContent-Type: {kind}; charset=utf-8\r\n\r\n"
            f"{body}\r\n").encode()


class FakeImap:
    """An IMAP server with a few messages (its inbox's, and its Spam folder's); records how it was used."""
    instances: list["FakeImap"] = []
    messages: list[tuple[float, bytes]] = []
    spam: list[tuple[float, bytes]] = []
    junk_name: str | None = "[Gmail]/Spam"  # what the server calls its Spam folder (None: it has none)
    junk_flagged = True  # and whether it flags it \Junk, as Gmail does
    spam_broken = False  # a Spam folder that won't open
    accept = True
    login_error: Exception | None = None  # what the server does to a sign-in, when not a refusal

    def __init__(self, host, timeout=None):
        self.host, self.calls, self.folder = host, [], "INBOX"
        FakeImap.instances.append(self)

    def login(self, user, password):
        self.calls.append(("login", user))
        password.encode("ascii")  # as imaplib sends it
        if FakeImap.login_error is not None:
            raise FakeImap.login_error
        if not FakeImap.accept:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")

    def list(self, directory="", pattern="*"):
        self.calls.append(("list",))
        lines = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
                 b'(\\HasNoChildren) "/" "[Gmail]/Sent Mail"']
        if FakeImap.junk_name:
            flag = b" \\Junk" if FakeImap.junk_flagged else b""
            lines.append(b'(\\HasNoChildren%s) "/" "%s"' % (flag, FakeImap.junk_name.encode()))
        return "OK", lines

    def select(self, box, readonly=False):
        self.calls.append(("select", box, readonly))
        self.folder = box.strip('"')
        return "OK", [b"3"]

    def _mail(self):
        if self.folder == "INBOX":
            return FakeImap.messages
        if FakeImap.spam_broken:
            raise imaplib.IMAP4.error("command SEARCH illegal in state AUTH")
        assert self.folder == FakeImap.junk_name, self.folder
        return FakeImap.spam

    def search(self, charset, *criteria):
        self.calls.append(("search",) + criteria)
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self._mail())))]

    date_last = False  # the server sends a message's date after its header, not before

    def fetch(self, mids, parts):
        self.calls.append(("fetch", mids, parts))
        out = []
        for mid in mids.decode().split(","):
            when, raw = self._mail()[int(mid) - 1]
            stamp = imaplib.Time2Internaldate(when).encode()
            if "HEADER.FIELDS" in parts:
                names = tuple(f.lower().encode() + b":" for f in parts.split("(")[-1].rstrip(")]").split())
                head = b"".join(line + b"\r\n" for line in raw.split(b"\r\n") if line.lower().startswith(names)) + b"\r\n"
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


    @classmethod
    def reset(cls):
        cls.instances, cls.messages, cls.accept, cls.date_last = [], [], True, False
        cls.spam, cls.junk_name, cls.junk_flagged, cls.spam_broken = [], "[Gmail]/Spam", True, False
        cls.login_error = None


@pytest.fixture
def imap(monkeypatch):
    FakeImap.reset()
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
    real = mailbox._parts

    def parts(msg):  # the newest one can't be read
        if "666666" in str(msg.get_payload()):
            raise IndexError("a broken header")
        return real(msg)

    monkeypatch.setattr(mailbox, "_parts", parts)
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


def test_the_code_not_a_job_number_a_minute_count_or_a_phone_number():
    """A code straight after the words comes first, wherever it is; a number named as the
    job's isn't the code; a subject's code words don't read on into the body."""
    oracle = ("We need you to confirm your identity so your application can be considered for the "
              "position of Equipment Technician - Pump/Abatement - 2505303.\n\nConfirm your identity "
              "using this code: 218335.\n\nThe code will expire in 10 minutes.")
    assert find_code("Your verification code\n" + oracle) == "218335"
    assert find_code("Your one-time passcode\nThanks for applying for Field Service Engineer (Job 2617841). "
                     "Enter 482913 on the page to continue.") == "482913"
    assert find_code("Your verification code is K7Q2ZP. It expires in 1440 minutes.") == "K7Q2ZP"
    assert find_code("Your security code is AB12CD. Need help? Call us on 800 555 0199.") == "AB12CD"


def test_a_code_split_by_its_markup_or_only_in_the_html_is_read(imap):
    from email.message import EmailMessage

    since = time.time()
    both = EmailMessage()
    both["From"], both["Subject"] = "no-reply@careers.ti.com", "Verify your email"
    both.set_content("Hi Sam, your code is in the HTML version of this email.")
    both.add_alternative("<p>Your verification code is <b>55</b><b>1234</b></p>", subtype="html")
    imap.messages = [(since + 10, bytes(both))]
    assert mailbox.search("sam@gmail.com", "app-password", since, {"ti.com"}, "code").value == "551234"


def test_the_confirmation_link_not_a_tagged_logo_or_a_deactivate_link():
    own = lambda u: u.startswith("https://careers.acme.com/")  # noqa: E731
    verify = "https://careers.acme.com/account/verify?token=abc"
    assert find_link("", ["https://careers.acme.com/?utm_campaign=email_verification", verify], own) == verify
    activate = "https://careers.acme.com/account/activate?token=abc"
    assert find_link("", ["https://careers.acme.com/account/deactivate?token=abc", activate], own) == activate


@pytest.mark.parametrize("error", [imaplib.IMAP4.abort("socket error: EOF"),
                                   imaplib.IMAP4.error("[UNAVAILABLE] Temporary System Problem. Try again later.")])
def test_a_mail_service_hiccup_isnt_a_refused_password(imap, error):
    """A dropped connection or a busy server says so: the desk tries the same password again
    (a refusal would have it wait for a new one that isn't needed)."""
    imap.login_error = error
    with pytest.raises(MailboxError) as e:
        mailbox.search("sam@gmail.com", "abcd efgh ijkl mnop", time.time(), {"ti.com"}, "code")
    assert "app password" not in str(e.value)


def test_an_app_password_with_odd_characters_is_said_to_be_wrong(imap):
    with pytest.raises(MailboxError, match="app password"):
        mailbox.search("sam@gmail.com", "p\u00e4sswort", time.time(), {"ti.com"}, "code")


def test_mail_from_before_the_wait_can_be_left_out(imap):
    """With another job already waiting on the same sender, mail from just before this one's
    wait began is that job's: no looking back."""
    since = time.time()
    imap.messages = [(since - 60, _message("acme@otp.workday.com", "Verify", "Your verification code is 111111"))]
    assert mailbox.search("sam@gmail.com", "pw", since, {"workday.com"}, "code").value == "111111"
    assert mailbox.search("sam@gmail.com", "pw", since, {"workday.com"}, "code", look_back=0) is None


def test_codes_from_greenhouses_mail_domain_and_an_employers_own_eightfold_site():
    from job_apply.ats import detect_ats

    assert sender_allowed("no-reply@us.greenhouse-mail.io", mailbox.ATS_MAIL_DOMAINS["greenhouse"])
    assert detect_ats("https://careers.lamresearch.com/careers/job/1") == "eightfold"  # so eightfold.ai's mail is its


def test_a_number_named_as_the_jobs_however_its_named_and_a_code_for_the_position():
    """"Requisition code: 2505303" and "Job ID 2617841" are the job's numbers, whatever the
    words; "the code for this position: 482913" is the code."""
    assert find_code("Requisition code: 2505303\nYour verification code is 218335") == "218335"
    assert find_code("Verification code\nJob ID 2617841\n\n482913") == "482913"
    assert find_code("Your verification code for this position: 482913") == "482913"
    assert find_code("Your verification code is... 2047") == "2047"  # a year-like code, straight after


def _alternative(subject: str, plain: str, markup: str) -> bytes:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"], msg["Subject"] = "no-reply@careers.ti.com", subject
    msg.set_content(plain)
    msg.add_alternative(markup, subtype="html")
    return bytes(msg)


def test_the_code_wherever_the_email_puts_it(imap):
    """In one span beside its words' span; in the HTML while the plain text only names another
    number; under a subject that has the words while the body has only the code."""
    since = time.time()
    imap.messages = [(since + 10, _alternative("Verify", "Hi Sam, see the HTML version.",
                                               "<span>Your verification code</span><span>482913</span>"))]
    assert mailbox.search("sam@gmail.com", "pw", since, {"ti.com"}, "code").value == "482913"
    imap.messages = [(since + 10, _alternative(
        "Verify", "Your verification code is in this email.\nApplication 2617841 received.",
        "<p>Your verification code is <b>551234</b></p><p>Application 2617841 received.</p>"))]
    assert mailbox.search("sam@gmail.com", "pw", since, {"ti.com"}, "code").value == "551234"
    imap.messages = [(since + 10, _message("no-reply@careers.ti.com", "Your verification code",
                                           "Use 482913 to verify your email address."))]
    assert mailbox.search("sam@gmail.com", "pw", since, {"ti.com"}, "code").value == "482913"


def test_a_placeholder_link_or_a_deactivation_in_a_links_query_isnt_opened():
    own = lambda u: u.startswith("https://careers.acme.com/")  # noqa: E731
    verify = "https://careers.acme.com/account/verify?token=abc"
    assert find_link("", ["https://[UNSUBSCRIBE_URL]/", verify], own) == verify
    confirm = "https://careers.acme.com/careers?career_ns=email_verification&t=1"
    assert find_link("", ["https://careers.acme.com/careers?career_ns=account_deactivation&t=1", confirm], own) == confirm


def test_a_refusal_that_also_says_try_later_is_a_refusal(imap):
    imap.login_error = imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials. Too many failed attempts, "
                                           "try again later.")
    with pytest.raises(MailboxError, match="app password"):
        mailbox.search("sam@gmail.com", "abcd efgh ijkl mnop", time.time(), {"ti.com"}, "code")


def test_brassrings_passcode_mail_from_its_own_domains(imap):
    """Edward Jones' BrassRing (the live check's test identity, Oct 2026) emails a passcode with the
    subject "Your Passcode". The desk's list of each job system's mail domains had none for BrassRing,
    so only brassring.com and the employer's own site were read: mail from Kenexa's domain (IBM Kenexa
    BrassRing) was never looked at."""
    from job_apply.ats import detect_ats

    url = "https://sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=26235&siteid=5374"
    assert detect_ats(url) == "brassring"
    brassring = mailbox.ATS_MAIL_DOMAINS["brassring"]
    assert sender_allowed("Edward Jones Careers <no-reply@mail.brassring.com>", brassring)
    assert sender_allowed("noreply@kenexa.com", brassring)
    assert not sender_allowed("noreply@not-kenexa.com", brassring)
    since = time.time()
    imap.messages = [(since + 8, _message("Edward Jones <noreply@kenexa.com>", "Your Passcode",
                                          "Your passcode is 482913. It will expire in 10 minutes."))]
    found = mailbox.search("sam@gmail.com", "app-password", since, brassring, "code")
    assert (found.value, found.sender, found.spam) == ("482913", "kenexa.com", False)


@pytest.mark.parametrize("body, markup", [
    ("Your passcode is 482913. It will expire in 10 minutes.", False),
    ("Hello,\n\nUse the passcode below to validate your email address.\n\n482913\n\nIt expires in 10 minutes.", False),
    ("<p>Your passcode is <b>482913</b>.</p><p>Edward Jones, 12555 Manchester Rd, St. Louis</p>", True),
    ("<table><tr><td>Your Passcode</td></tr><tr><td><span>482913</span></td></tr></table>", True),
])
def test_a_passcode_email_is_read_as_a_code(imap, body, markup):
    """BrassRing's mail has the subject "Your Passcode" (its page says so): its passcode is read from a
    plain or an HTML body, whether the words are in the body or only in the subject."""
    since = time.time()
    imap.messages = [(since + 8, _message("Edward Jones <noreply@brassring.com>", "Your Passcode", body, html=markup))]
    assert mailbox.search("sam@gmail.com", "app-password", since, {"brassring.com"}, "code").value == "482913"


def test_a_code_in_the_spam_folder_is_read_when_the_inbox_has_none(imap):
    """BrassRing's page says "Be sure to check your Spam or Junk Mail folder if you do not see it in your
    Inbox": the desk only ever looked in the inbox. Spam is looked in (read-only, as the inbox, and for the
    same site's mail since the wait began) when the inbox has no code; a code in the inbox comes first."""
    since = time.time()
    spam = "[Gmail]/Spam"
    imap.messages = [(since + 5, _message("Deals <deals@shop.com>", "Your code", "Your verification code is 222222"))]
    imap.spam = [
        (since - 3600, _message("noreply@brassring.com", "Your Passcode", "Your passcode is 111111")),  # before the wait
        (since + 9, _message("Offers <offers@shop.com>", "Your Passcode", "Your passcode is 333333")),  # not the site
        (since + 10, _message("Edward Jones <noreply@brassring.com>", "Your Passcode", "Your passcode is 482913")),
    ]
    found = mailbox.search("sam@gmail.com", "app-password", since, {"brassring.com"}, "code")
    assert (found.kind, found.value, found.sender, found.spam) == ("code", "482913", "brassring.com", True)
    calls = imap.instances[0].calls
    assert ("select", "INBOX", True) in calls and ("select", f'"{spam}"', True) in calls  # both read-only
    assert all("BODY.PEEK[" in c[2] for c in calls if c[0] == "fetch")
    assert calls[-1] == ("logout",)
    # a code in the inbox is the answer: its Spam folder isn't opened
    imap.messages.append((since + 20, _message("noreply@brassring.com", "Your Passcode", "Your passcode is 555555")))
    found = mailbox.search("sam@gmail.com", "app-password", since, {"brassring.com"}, "code")
    assert (found.value, found.spam) == ("555555", False)
    assert ("select", f'"{spam}"', True) not in imap.instances[-1].calls
    # nothing in either: nothing
    imap.messages, imap.spam = [], imap.spam[:2]
    assert mailbox.search("sam@gmail.com", "app-password", since, {"brassring.com"}, "code") is None


@pytest.mark.parametrize("name, flagged", [("[Gmail]/Spam", True), ("Junk", True), ("Bulk Mail", True),
                                           ("Junk E-mail", False), ("Spam", False)])
def test_the_spam_folder_is_found_by_its_flag_or_its_name(imap, name, flagged):
    imap.junk_name, imap.junk_flagged = name, flagged
    since = time.time()
    imap.spam = [(since + 10, _message("noreply@brassring.com", "Your Passcode", "Your passcode is 482913"))]
    found = mailbox.search("sam@gmail.com", "app-password", since, {"brassring.com"}, "code")
    assert found is not None and found.value == "482913" and found.spam
    assert ("select", f'"{name}"', True) in imap.instances[0].calls


def test_an_account_with_no_spam_folder_or_one_that_wont_open_is_just_the_inbox(imap):
    since = time.time()
    imap.messages = [(since + 5, _message("Deals <deals@shop.com>", "Your code", "Your verification code is 222222"))]
    imap.junk_name = None
    assert mailbox.search("sam@gmail.com", "app-password", since, {"brassring.com"}, "code") is None
    imap.junk_name, imap.spam_broken = "[Gmail]/Spam", True
    assert mailbox.search("sam@gmail.com", "app-password", since, {"brassring.com"}, "code") is None
    imap.messages = [(since + 5, _message("noreply@brassring.com", "Your Passcode", "Your passcode is 482913"))]
    assert mailbox.search("sam@gmail.com", "app-password", since, {"brassring.com"}, "code").value == "482913"


def test_a_confirmation_link_in_the_spam_folder_is_found_too(imap):
    since = time.time()
    own = lambda url: url.startswith("https://careers.acme.com/")  # noqa: E731
    verify = "https://careers.acme.com/account/verify?token=abc"
    imap.spam = [(since + 10, _message("no-reply@careers.acme.com", "Confirm your email",
                                       f"Confirm your email address: {verify}"))]
    found = mailbox.search("sam@gmail.com", "app-password", since, {"acme.com"}, "link", own)
    assert (found.kind, found.value, found.spam) == ("link", verify, True)
