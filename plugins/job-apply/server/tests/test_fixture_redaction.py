import json

from job_apply.config import Profile
from job_apply.fixtures import clean_html, convert, personal_strings, redact


def test_personal_data_is_redacted(tmp_path):
    secrets = personal_strings(Profile.load())
    assert "Sam Rivera" in secrets and "4805550123" in secrets
    raw = """<html><body><script>track('sam.rivera@example.com')</script>
      <div data-automation-id="legalName" data-ja-id="3" onclick="x()">Sam Rivera</div>
      <input name="phone" value="480-555-0123"><span>Call 4805550123</span>
      <a href="https://www.linkedin.com/in/samrivera">profile</a>
      <label><input type="radio" value="Yes" checked> Yes</label>
      <link rel="stylesheet" href="https://cdn.example.com/site.css"><img src="https://cdn.example.com/logo.png"></body></html>"""
    out = clean_html(raw, secrets)
    for leaked in ("Rivera", "sam.rivera", "480-555-0123", "4805550123", "samrivera", "<script", "data-ja-", "onclick"):
        assert leaked.lower() not in out.lower(), leaked
    assert 'data-automation-id="legalName"' in out  # structure the extractor relies on is kept
    assert 'value="Yes"' in out  # radio/checkbox option values are kept
    assert "cdn.example.com" not in out  # nothing loads from the network when a test opens it


def test_fixture_css_is_trimmed_and_offline():
    raw = """<html><head><style>@charset "UTF-8";@import url("https://cdn.example.com/a.css");
      @font-face{font-family:X;src:url(https://fonts.example.com/x.woff2)}
      :root{--used:#123;--chain:var(--used);--unused:#456}
      .field{color:var(--chain);background:url('https://cdn.example.com/bg.png') no-repeat}
      .badge-module_badge__P84rY{color:blue}
      .field::before{content:"}"}
      .field:not(:focus) > span{display:none}
      .md\\:hidden{display:none}
      @media (max-width:600px){.gone{display:none}.field>span{display:block}}
      </style></head><body>
      <div class="field" style="background-image:url(//cdn.example.com/i.png)"><span>x</span></div>
      <div class="md:hidden">menu</div>
      <iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>
      <svg><use href="https://cdn.example.com/s.svg#i"></use></svg>
      <a href="https://example.com/jobs">jobs</a></body></html>"""
    out = clean_html(raw, [])
    for remote in ("cdn.example.com", "fonts.example.com", "recaptcha"):
        assert remote not in out, remote
    # rules for elements on the page stay, including whatever hides or shows them
    for kept in (".field{color:var(--chain);background:none no-repeat}", "--used:#123", "--chain:var(--used)",
                 '.field::before{content:"}"}', ".field:not(:focus) > span{display:none}", ".md\\:hidden{display:none}",
                 "@media (max-width:600px){.field>span{display:block}}", 'href="https://example.com/jobs"'):
        assert kept in out, kept
    for dropped in ("badge-module", ".gone", "--unused", "@font-face", "@import"):
        assert dropped not in out, dropped


def test_convert_writes_expectations(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "page.html").write_text("<html><body><label for=a>Email</label><input id=a></body></html>", encoding="utf-8")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": "https://acme.wd1.myworkdayjobs.com/External/apply?email=sam.rivera@example.com",
        "frames": [{"file": "page.html", "url": "https://acme.wd1.myworkdayjobs.com/External/apply"}],
        "fields": [{"id": "1", "kind": "text", "label": "Email", "required": False, "value": "sam.rivera@example.com"}],
    }), encoding="utf-8")
    convert(snap, "acme-email", tmp_path / "out", Profile.load())
    expect = json.loads((tmp_path / "out" / "acme-email.expect.json").read_text(encoding="utf-8"))
    assert expect["fields"] == [{"label": "Email", "kind": "text", "required": False}]  # no values
    assert "sam.rivera" not in expect["source_url"]


def test_personal_data_written_another_way_is_redacted_too():
    """Made-up applicant. Phones with any separators, the email encoded or by its name alone,
    handles without the address, accented names however written, two-letter names, the
    person's answers, and tokens in links and meta tags."""
    prof = Profile({"personal": {
        "first_name": "José", "last_name": "Núñez", "email": "sunflower77@example.org", "phone": "480-555-0142",
        "address": {"line1": "742 W Evergreen Ter", "postal_code": "85226"},
        "linkedin_url": "https://www.linkedin.com/in/qh-ex-12345", "github_url": "https://github.com/zz-octo-998"}})
    raw = """<html><head><meta name="csrf-token" content="CSRFTOKEN-1"><meta charset="utf-8"></head><body>
      <p>(480) 555-0142 · 480.555.0142 · +1 480 555 0142</p>
      <a href="/confirm?email=sunflower77%40example.org&amp;sid=SESSIONID-2">confirm</a>
      <form action="/submit?token=FORMTOKEN-3"></form>
      <div data-props='{"name":"Jos\\u00e9 N\\u00fa\\u00f1ez"}' data-state="eyJlbWFpbCI6InN1bmZsb3dlcjc3In0="></div>
      <p>Signed in as sunflower77 · linkedin.com/in/qh-ex-12345 · github.com/zz-octo-998</p>
      <p>Jose_Nunez_Resume.pdf · Jos%C3%A9_N%C3%BA%C3%B1ez.pdf · 742 W. Evergreen Ter.</p>
      <textarea name="why">Six years keeping etch tools running.</textarea>
      <select name="race"><option>Decline</option><option selected>Hispanic or Latino</option></select>
      <label><input type="radio" name="vet" value="Protected veteran" checked> Protected veteran</label>
      <div role="checkbox" aria-checked="true" aria-label="Yes, I have a disability"></div>
    </body></html>"""
    out = clean_html(raw, personal_strings(prof))
    for leaked in ("555-0142", "555 0142", "555.0142", "sunflower77", "SESSIONID-2", "FORMTOKEN-3", "CSRFTOKEN-1",
                   "\\u00e9", "eyJlbWFpbCI6", "qh-ex-12345", "zz-octo-998", "Jose_Nunez", "Jos%C3%A9", "Evergreen",
                   "Six years", "selected", "checked", 'aria-checked="true"'):
        assert leaked.lower() not in out.lower(), leaked
    assert 'value="Protected veteran"' in out and "<option>Hispanic or Latino</option>" in out  # the choices stay
    assert 'charset="utf-8"' in out
    short = clean_html("<p>You're all set, Al!</p><p>Wu, Al</p><p>Alabama Wuhan</p>",
                       personal_strings(Profile({"personal": {"first_name": "Al", "last_name": "Wu"}})))
    assert "Al!" not in short and "Wu," not in short and "Alabama Wuhan" in short


def test_a_value_is_taken_out_as_a_whole_word_and_wherever_its_one():
    """A first name "Rob" came out of "Problem" as "PREDACTEDlem": garbled, and the name easy to
    guess. A value is taken out only as a whole word, but that's wherever it is one: in an email,
    a link, a file name, an attribute, run into the next name, and a number not inside a longer one."""
    prof = Profile({"personal": {"first_name": "Rob", "last_name": "Hall", "email": "rob.hall77@example.org",
                                 "address": {"line1": "742 W. Evergreen Ter.", "postal_code": "85201"}}})
    raw = """<html><body><h1>Problem? Robotics shall meet the Challenge</h1>
      <p>Rob Hall, ROB HALL, robhall · RobHall.pdf · Rob_Hall_Resume.pdf · Resume%20Rob%20Hall.pdf</p>
      <p>rob.hall77@example.org · 742 W Evergreen Terrace · ZIP 85201-1234 · order 1852011</p>
      <a href="https://example.com/jobs/Rob-Hall-123" data-user="Hall">x</a></body></html>"""
    out = clean_html(raw, personal_strings(prof))
    assert "Problem? Robotics shall meet the Challenge" in out and "order 1852011" in out
    for kept in ("REDACTED, REDACTED, REDACTED ·", "REDACTED.pdf", "REDACTED_Resume.pdf", "Resume%20REDACTED.pdf",
                 "ZIP REDACTED-1234", 'href="https://example.com/jobs/REDACTED-123"', 'data-user="REDACTED"'):
        assert kept in out, kept
    for leaked in ("Rob ", "Hall", "robhall", "hall77", "Evergreen"):
        assert leaked not in out, leaked


def test_a_surname_after_an_initial_is_taken_out_of_a_user_name_a_file_name_or_a_link():
    """Taken out as a whole word only, a last name "Doe" stayed in "jdoe1987", "JDoe_Resume.pdf", an
    email and a link, which a problem report would show. In the name of something (no spaces, its
    words joined by a digit, "_", "@", "/" or ".") a value goes wherever it is, with the initial or two
    it runs on into, so no half-word is left to guess it by. Prose keeps its words whole, a link its
    longer ones ("dashboard" for "Ash"), and a ZIP code inside a longer number is still another number."""
    secrets = personal_strings(Profile({"personal": {"first_name": "Jane", "last_name": "Doe",
                                                     "address": {"postal_code": "85201"}}}))
    for named in ("jdoe1987", "JDoe_Resume.pdf", "jdoe@example.com", "https://x.example/u/jdoe"):
        assert "doe" not in redact(named, secrets).lower(), named
    said = redact("Signed in as jdoe1987 (JDoe_Resume.pdf), order-1852011.pdf, https://x.example/u/jdoe", secrets)
    assert said == "Signed in as REDACTED1987 (REDACTED_Resume.pdf), order-1852011.pdf, https://x.example/u/REDACTED"
    page = clean_html('<p>Signed in as jdoe1987</p><a href="https://x.example/u/jdoe" data-file="JDoe_CV.pdf">me</a>',
                      secrets)
    assert "doe" not in page.lower(), page
    secrets = personal_strings(Profile({"personal": {"first_name": "Rob", "last_name": "Lee", "middle_name": "Ray",
                                                     "preferred_name": "Ash"}}))
    prose = "Problem? Don't flee the gray washing. Robotics, Leeway, Raymond and Ashes."
    assert redact(prose, secrets) == prose
    assert redact("https://example.com/careers/dashboard", secrets) == "https://example.com/careers/dashboard"
    secrets = personal_strings(Profile({"personal": {"first_name": "Ted", "last_name": "Hall"}}))
    assert redact("Ted_Hall_Resume.pdf", secrets) == "REDACTED_Resume.pdf"  # not "Ted" again in the REDACTED


def test_a_short_name_leaves_the_longer_words_of_a_name_of_something_whole():
    """In the name of something, a value went with every letter it ran on into: a first name "Tom"
    turned "custom_field" into "REDACTED_field" and "Ted" "?status=accepted" into "?status=REDACTED",
    so a problem report lost what it needed and a fixture's ids weren't the site's. A short value goes
    there only with an initial or two ("jdoe1987", "JDoe_Resume.pdf", and "JDoeResume.pdf", whose next
    word starts with a capital), a long one wherever it is ("samrivera87"), and a full name run
    together still goes ("janedoe99")."""
    for first, named in (("Tom", "custom_field"), ("Ted", "?status=accepted"), ("Ann", "announcement_bar"),
                         ("Ted", "formField-selected_2"), ("Dan", "abundant_2")):
        assert redact(named, personal_strings(Profile({"personal": {"first_name": first}}))) == named, named
    page = '<div class="announcement_bar" data-view="?status=accepted" id="formField-selected_2">custom_field</div>'
    names = {"first_name": "Ted", "last_name": "Ann", "preferred_name": "Tom"}
    assert clean_html(page, personal_strings(Profile({"personal": names}))) == page
    secrets = personal_strings(Profile({"personal": {"first_name": "Jane", "last_name": "Doe"}}))
    for named in ("jdoe1987", "JDoe_Resume.pdf", "jdoe@example.com", "https://x.example/u/jdoe", "JDoeResume.pdf",
                  "jdoes_2", "janedoe99", "jane_doe_2"):
        assert "doe" not in redact(named, secrets).lower(), named
    assert redact("jdoe1987, JDoeResume.pdf", secrets) == "REDACTED1987, REDACTEDResume.pdf"
    secrets = personal_strings(Profile({"personal": {"first_name": "Jane", "last_name": "Rivera"}}))
    assert redact("samrivera87", secrets) == "REDACTED87"


def test_expectations_keep_accented_names_findable_and_drop_query_strings(tmp_path):
    prof = Profile({"personal": {"first_name": "José", "last_name": "Núñez", "email": "sunflower77@example.org"}})
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "page.html").write_text("<html><body><label for=a>x</label><input id=a></body></html>", encoding="utf-8")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": "https://careers-x.icims.com/jobs/1/submit?jtsid=SESSIONID-abc&e=sunflower77%40example.org",
        "frames": [{"file": "page.html", "url": "https://careers-x.icims.com/jobs/1/submit"}],
        "fields": [{"id": "1", "kind": "checkbox", "label": "I, José Núñez, certify that this is true", "required": True}],
    }), encoding="utf-8")
    convert(snap, "probe", tmp_path / "out", prof)
    text = (tmp_path / "out" / "probe.expect.json").read_text(encoding="utf-8")
    assert "Jos" not in text and "SESSIONID" not in text and "sunflower77" not in text
    assert json.loads(text)["source_url"] == "https://careers-x.icims.com/jobs/1/submit"


def test_a_short_surname_or_an_everyday_email_name_leaves_the_page_whole():
    """A last name "Li" doesn't turn <li> into <REDACTED>; an email "resume@..." or a website
    ".../portfolio" doesn't take every "resume" or "portfolio" off the page; "#" links stay."""
    prof = Profile({"personal": {"first_name": "Mei", "last_name": "Li", "email": "resume@example.org",
                                 "website": "https://mei-example.org/portfolio"}})
    raw = """<html><body><ul><li>Upload your resume</li><li>Add a portfolio</li></ul>
      <p>Thanks, Mei Li</p><a href="#">Back to top</a><a href="#section-2">More</a></body></html>"""
    out = clean_html(raw, personal_strings(prof))
    assert out.count("<li>") == 2 and "Upload your resume" in out and "Add a portfolio" in out, out
    assert "Mei" not in out and "Li<" not in out and " Li" not in out
    assert 'href="#"' in out


def test_a_link_loses_its_session_path_parameter_too():
    """Taleo and other Java sites put the session in the path (";jsessionid=..."), which
    stayed in scrubbed links and in problem reports."""
    from job_apply.fixtures import _without_query

    assert _without_query("https://x.taleo.net/careersection/jobapply.ftl;jsessionid=AB12?job=1#top") == \
        "https://x.taleo.net/careersection/jobapply.ftl"
    assert _without_query("https://example.com/jobs/12/apply") == "https://example.com/jobs/12/apply"


def test_anyone_elses_details_and_a_sessions_are_taken_out_of_a_captured_page():
    """The review of Oct 15 planted a third party's details in a page and ran the scrubber: everything of the profile's
    went, and everything else stayed (a recruiter's email and phone, a birth date, an SSN's shape, a candidate id, a
    32-hex session, a short token, query strings in a frame's address, a formaction, a data-href and a CSS url(), a
    frame's srcdoc, an HTML comment, a button's value, an SVG's words). None survives now; the page's own ties (ids,
    labels' for) stay."""
    planted = {
        "recruiter": "jane.recruiter@acme-staffing.example", "phone": "(602) 555-0147", "dob": "04/12/1988",
        "ssn": "123-45-6789", "candidate": "C0098812", "session": "9f86d081884c7d659a2feaa0c55ad015",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl", "frame_token": "tok=abc123secret",
        "formaction_token": "sid=77aa88bb", "href_token": "auth=zz99", "css_token": "sig=qq11",
        "srcdoc": "srcdoc-secret-words", "comment": "debug user=jane.recruiter", "button": "buttonsecret@acme-staffing.example",
        "svg": "svg-person@acme-staffing.example",
    }
    raw = f"""<html><body>
      <!-- {planted["comment"]} -->
      <p>Questions? Email {planted["recruiter"]} or call {planted["phone"]}.</p>
      <p>Date of birth: {planted["dob"]}. SSN {planted["ssn"]}. Candidate ID: {planted["candidate"]}</p>
      <div data-session="{planted["session"]}" data-token="{planted["jwt"]}"></div>
      <iframe src="/apply/frame.html?{planted["frame_token"]}" title="Application"></iframe>
      <iframe srcdoc="{planted["srcdoc"]}"></iframe>
      <form><label for="f0123456789abcdef0123456789abcdef">Name</label>
        <input id="f0123456789abcdef0123456789abcdef">
        <button formaction="/save?{planted["formaction_token"]}" value="{planted["button"]}">Save</button></form>
      <span data-href="/next?{planted["href_token"]}">Next</span>
      <div style="background: url('/img/bg.png?{planted["css_token"]}')"></div>
      <svg><text>{planted["svg"]}</text></svg>
      <p>Requisition 10557 posted 10/16/2026. Call 1-800 for help.</p>
    </body></html>"""
    out = clean_html(raw, [])
    for what, value in planted.items():
        assert value not in out, (what, value)
    assert 'for="f0123456789abcdef0123456789abcdef"' in out and 'id="f0123456789abcdef0123456789abcdef"' in out
    assert "Requisition 10557 posted 10/16/2026" in out  # (a job's number and a date are no one's)
    assert "frame.html" in out and "/img/bg.png" in out


def test_the_scrubber_keeps_the_pages_ties_and_its_questions_and_takes_a_click_to_call_number():
    """The review of 0.3.189: a class list's long hex went from the page but not its style sheet, "userId_1234" and
    "userId_5678" became one id, an aria-label "Relocate?" lost its question mark (read as an address), and a
    click-to-call link's number written without separators ("tel:+16025550147") stayed. An attribute that ties the
    page together keeps its value but for an email or a token; only an address loses its query; a tel: number goes."""
    hexes = "a" * 32
    raw = f"""<html><head><style>.x-{hexes} {{ color: red }}</style></head><body>
      <div class="x-{hexes} box">Box</div>
      <label for="userId_1234">One</label><input id="userId_1234">
      <label for="userId_5678">Two</label><input id="userId_5678">
      <input aria-label="Relocate?" title="Why?"><img src="/logo.png?v=3"><button formaction="?token=zz88">Go</button>
      <a href="tel:+16025550147">Call us</a><a href="tel:6025550147">Or here</a></body></html>"""
    out = clean_html(raw, [])
    assert f'class="x-{hexes} box"' in out and f".x-{hexes}" in out
    assert 'id="userId_1234"' in out and 'id="userId_5678"' in out and 'for="userId_5678"' in out
    assert 'aria-label="Relocate?"' in out and 'title="Why?"' in out
    assert "v=3" not in out and "zz88" not in out
    assert "6025550147" not in out and 'href="tel:5555550100"' in out


def test_the_expectations_are_scrubbed_like_their_page(tmp_path):
    """The review of 0.3.189: convert scrubbed a label's third party on the page but not in its .expect.json, so the
    fixture failed its own test and the JSON kept the details."""
    snap = tmp_path / "snap"
    snap.mkdir()
    labels = ["Mobile phone (e.g. 602-555-4567)", "Questions? Email jane.doe@acme-staffing.example", "User ID 6x30abc"]
    (snap / "page.html").write_text("".join(f'<label for="f{i}">{label}</label><input id="f{i}">'
                                            for i, label in enumerate(labels)), encoding="utf-8")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": "https://x.example/apply/9f86d081884c7d659a2feaa0c55ad015",
        "frames": [{"file": "page.html", "url": "https://x.example/apply"}],
        "fields": [{"label": label, "kind": "text"} for label in labels]}), encoding="utf-8")
    convert(snap, "third", tmp_path / "out", secrets=[])
    page = (tmp_path / "out" / "third.html").read_text(encoding="utf-8")
    written = (tmp_path / "out" / "third.expect.json").read_text(encoding="utf-8")
    for leaked in ("602-555-4567", "jane.doe", "6x30abc", "9f86d081884c7d659a2feaa0c55ad015"):
        assert leaked not in written, leaked
    for field in json.loads(written)["fields"]:
        assert f">{field['label']}<" in page, field["label"]


def test_the_shapes_the_review_of_oct_18_found_surviving_are_taken_out(tmp_path):
    """The review of Oct 18 planted more on a page: a third party's phone as ten bare digits after a phone word and with
    no separator after its area code, the person's own phone written with slashes, birth dates with the month's name,
    and a license's, a passport's, an EIN's, an IBAN's and a card's numbers after their labels. All stayed, in the page
    and in its .expect.json; none does now. (Every value made up.)"""
    planted = {
        "bare phone": "Phone: 6025550188", "call us": "call us at +16025550177", "no gap": "(602)5550166",
        "own phone": "480/555/0142", "born": "Born 14 March 1990", "birthday": "Birthday: March 14, 1990",
        "born on": "born on 3rd of Sept. 1985", "license": "Driver's License #: D12345678",
        "passport": "Passport number: X98765432", "ein": "EIN: 12-3456789",
        "iban": "IBAN: DE89 3704 0044 0532 0130 00", "card": "Card number: 4111 1111 1111 1111",
    }
    values = {"bare phone": "6025550188", "call us": "6025550177", "no gap": "5550166", "own phone": "480/555/0142",
              "born": "14 March 1990", "birthday": "March 14, 1990", "born on": "Sept. 1985", "license": "D12345678",
              "passport": "X98765432", "ein": "3456789", "iban": "3704 0044", "card": "1111 1111"}
    prof = Profile({"personal": {"first_name": "Jordan", "last_name": "Quill", "phone": "480-555-0142"}})
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "page.html").write_text("<html><body>" + "".join(
        f'<label for="f{i}">{what}</label><input id="f{i}"><p title="{text}">{text}</p>'
        for i, (what, text) in enumerate(planted.items())) + "</body></html>", encoding="utf-8")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": "https://x.example/apply", "frames": [{"file": "page.html", "url": "https://x.example/apply"}],
        "fields": [{"label": f"{what} ({text})", "kind": "text"} for what, text in planted.items()]}), encoding="utf-8")
    convert(snap, "shapes", tmp_path / "out", prof)
    page = (tmp_path / "out" / "shapes.html").read_text(encoding="utf-8")
    written = (tmp_path / "out" / "shapes.expect.json").read_text(encoding="utf-8")
    for what, value in values.items():
        assert value not in page and value not in written, (what, value)
    # (the labels stay, and what isn't anyone's: a requisition's ten digits, a word that only starts like a label)
    for kept in ("Phone: ", "Driver's License #: ", "IBAN: ", "Card number: ", "Born "):
        assert kept in page and kept in written, kept
    kept = clean_html("<p>Requisition 6025550188. Licensed in 2019. Cardinal 12345. Born to lead.</p>", [])
    assert "Requisition 6025550188. Licensed in 2019. Cardinal 12345. Born to lead." in kept
