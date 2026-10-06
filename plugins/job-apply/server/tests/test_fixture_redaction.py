import json

from job_apply.config import Profile
from job_apply.fixtures import clean_html, convert, personal_strings


def test_personal_data_is_redacted(tmp_path):
    secrets = personal_strings(Profile.load())
    assert "Sam Rivera" in secrets and "4805550123" in secrets
    raw = """<html><body><script>track('sam.rivera@example.com')</script>
      <div data-automation-id="legalName" data-ja-id="3" onclick="x()">Sam Rivera</div>
      <input name="phone" value="480-555-0123"><span>Call 4805550123</span>
      <a href="https://www.linkedin.com/in/samrivera">profile</a>
      <label><input type="radio" value="Yes" checked> Yes</label></body></html>"""
    out = clean_html(raw, secrets)
    for leaked in ("Rivera", "sam.rivera", "480-555-0123", "4805550123", "samrivera", "<script", "data-ja-", "onclick"):
        assert leaked.lower() not in out.lower(), leaked
    assert 'data-automation-id="legalName"' in out  # structure the extractor relies on is kept
    assert 'value="Yes"' in out  # radio/checkbox option values are kept


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
