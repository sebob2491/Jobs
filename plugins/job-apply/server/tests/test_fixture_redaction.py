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
    (snap / "page.html").write_text("<html><body><label for=a>Email</label><input id=a></body></html>")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": "https://acme.wd1.myworkdayjobs.com/External/apply?email=sam.rivera@example.com",
        "frames": [{"file": "page.html", "url": "https://acme.wd1.myworkdayjobs.com/External/apply"}],
        "fields": [{"id": "1", "kind": "text", "label": "Email", "required": False, "value": "sam.rivera@example.com"}],
    }))
    convert(snap, "acme-email", tmp_path / "out", Profile.load())
    expect = json.loads((tmp_path / "out" / "acme-email.expect.json").read_text())
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
    words joined by a digit, "_", "@", "/" or ".") a value goes wherever it is, with the letters it
    runs on into, so no half-word is left to guess it by. Prose keeps its words whole, and a ZIP
    code inside a longer number is still another number."""
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
    assert redact("https://example.com/careers/dashboard", secrets) == "https://example.com/careers/REDACTED"
    secrets = personal_strings(Profile({"personal": {"first_name": "Ted", "last_name": "Hall"}}))
    assert redact("Ted_Hall_Resume.pdf", secrets) == "REDACTED_Resume.pdf"  # not "Ted" again in the REDACTED


def test_expectations_keep_accented_names_findable_and_drop_query_strings(tmp_path):
    prof = Profile({"personal": {"first_name": "José", "last_name": "Núñez", "email": "sunflower77@example.org"}})
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "page.html").write_text("<html><body><label for=a>x</label><input id=a></body></html>")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": "https://careers-x.icims.com/jobs/1/submit?jtsid=SESSIONID-abc&e=sunflower77%40example.org",
        "frames": [{"file": "page.html", "url": "https://careers-x.icims.com/jobs/1/submit"}],
        "fields": [{"id": "1", "kind": "checkbox", "label": "I, José Núñez, certify that this is true", "required": True}],
    }))
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
