"""Profile.load() reads the person's profile again only when one of its files changed (the review of Oct 15: the
desk asks for it many times a step, and each reading parsed profile.yaml and answers.yaml again)."""

import yaml

from job_apply import config


def parses(monkeypatch) -> list[str]:
    """The YAML texts parsed from now on (as files or strings), by their first line."""
    seen: list[str] = []
    real = yaml.safe_load

    def counted(stream, *a, **kw):
        text = stream if isinstance(stream, str) else stream.read()
        seen.append(text.splitlines()[0] if text else "")
        return real(text, *a, **kw)
    monkeypatch.setattr(config.yaml, "safe_load", counted)
    return seen


def test_an_unchanged_profile_is_read_once(job_apply_home, monkeypatch):
    first = config.Profile.load().get("personal.first_name")
    seen = parses(monkeypatch)
    for _ in range(5):
        assert config.Profile.load().get("personal.first_name") == first
    assert seen == []


def test_a_change_to_the_profile_is_read_however_small(job_apply_home):
    path = job_apply_home / "profile.yaml"
    config.Profile.load()
    text = path.read_text(encoding="utf-8")
    name = config.Profile.load().get("personal.first_name")
    other = name[:-1] + ("x" if name[-1] != "x" else "y")  # the same size, written at once: the same time on disk too
    path.write_text(text.replace(f"first_name: {name}", f"first_name: {other}"), encoding="utf-8")
    assert config.Profile.load().get("personal.first_name") == other


def test_an_answer_saved_on_the_desk_is_in_the_next_reading(job_apply_home):
    config.Profile.load()
    config.save_answer("Are you willing to work nights?", "Yes")
    answers = config.Profile.load().get("answers")
    assert answers[0]["answer"] == "Yes" and "nights" in answers[0]["match"]


def test_a_callers_change_to_its_profile_never_reaches_the_next(job_apply_home):
    prof = config.Profile.load()
    prof.data["personal"]["first_name"] = "Changed"
    prof.data.setdefault("answers", []).append({"match": "x", "answer": "y"})
    again = config.Profile.load()
    assert again.get("personal.first_name") != "Changed"
    assert {"match": "x", "answer": "y"} not in (again.get("answers") or [])
    again.data["personal"]["first_name"] = "Changed again"
    assert config.Profile.load().get("personal.first_name") not in ("Changed", "Changed again")


def test_another_home_is_its_own_profile(job_apply_home, tmp_path, monkeypatch):
    config.Profile.load()
    other = tmp_path / "other-home"
    other.mkdir()
    (other / "profile.yaml").write_text("personal:\n  first_name: Other\n", encoding="utf-8")
    monkeypatch.setenv("JOB_APPLY_HOME", str(other))
    assert config.Profile.load().get("personal.first_name") == "Other"
