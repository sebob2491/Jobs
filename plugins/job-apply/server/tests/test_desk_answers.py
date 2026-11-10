"""The Your answers page's Answers the desk remembers, the server's half (the design's D5, D7's tests 14 and 15):
GET /api/answers lists what answers.yaml holds, POST /api/answers/change and /api/answers/forget change or remove one
entry and no other. Made-up people and employers only: the repo is public."""

import json

import yaml
from test_desk_settings import _calls, _desk

from job_apply import config

HEADER = "# Answers you gave in the Job Desk."
# (an ID kept for one employer, a question answered for every employer, one left empty there, a common question's
# answer, and the same question answered for another employer: newest first, as the desk keeps them)
ENTRIES = [
    {"match": config.question_pattern("Employee ID"), "answer": "WW123456", "question": "Employee ID",
     "from": "Example Fab", "at": "Example Fab"},
    {"match": config.question_pattern("Are you willing to work weekends?"), "answer": "Yes, every other weekend",
     "question": "Are you willing to work weekends?"},
    {"match": config.question_pattern("Middle name"), "answer": None, "question": "Middle name", "at": "Example Litho"},
    {"match": "(?i)highest.*education", "answer": "Some college (no degree)", "bank": "education_level"},
    {"match": config.question_pattern("Employee ID"), "answer": "EX-778899", "question": "Employee ID",
     "at": "Example Litho"},
]


def _write(home, entries=ENTRIES) -> None:
    config._write_answers({}, [dict(e) for e in entries])
    assert (home / "answers.yaml").read_text(encoding="utf-8").startswith(HEADER)


def _kept(home) -> list:
    return yaml.safe_load((home / "answers.yaml").read_text(encoding="utf-8"))["answers"]


def _id(entry: dict) -> str:
    return f"{entry['match']}\n{entry.get('at', '')}"


def test_answers_lists_the_remembered_ones_newest_first_ids_marked_and_none_in_the_state(srv, job_apply_home):
    """D7 test 14: /api/answers lists what answers.yaml holds, newest first, without the common questions' answers,
    and marks an ID as private; none of it is in /api/state."""
    _write(job_apply_home)
    [(status, listed), (state_status, state)] = _calls(_desk(srv), [("GET", "/api/answers", None),
                                                                   ("GET", "/api/state", None)])
    assert status == 200 and state_status == 200
    assert listed["answers"] == [
        {"id": _id(ENTRIES[0]), "question": "Employee ID", "answer": "WW123456", "at": "Example Fab", "private": True},
        {"id": _id(ENTRIES[1]), "question": "Are you willing to work weekends?", "answer": "Yes, every other weekend",
         "at": "", "private": False},
        {"id": _id(ENTRIES[2]), "question": "Middle name", "answer": None, "at": "Example Litho", "private": False},
        {"id": _id(ENTRIES[4]), "question": "Employee ID", "answer": "EX-778899", "at": "Example Litho",
         "private": True},
    ]
    shown = json.dumps(state)
    for kept in ("WW123456", "EX-778899", "Yes, every other weekend", "Are you willing to work weekends"):
        assert kept not in shown, kept
    assert "answers" not in state


def test_answers_with_none_remembered_and_a_file_that_doesnt_read(srv, job_apply_home):
    """No answers.yaml: an empty list. One that doesn't parse: a 400 saying so, the file left as it is. Without the
    page's key, 403."""
    assert _calls(_desk(srv), [("GET", "/api/answers", None)]) == [(200, {"answers": []})]
    assert [s for s, _ in _calls(_desk(srv), [("GET", "/api/answers", None)], token=False)] == [403]
    (job_apply_home / "answers.yaml").write_text("answers: [unclosed\n", encoding="utf-8")
    [(status, said)] = _calls(_desk(srv), [("GET", "/api/answers", None)])
    assert status == 400 and "has a typo" in said["error"]
    assert (job_apply_home / "answers.yaml").read_text(encoding="utf-8") == "answers: [unclosed\n"


def test_change_replaces_that_entrys_answer_and_no_other(srv, job_apply_home):
    """D7 test 15: change gives that entry its new answer, for the same question and the same employer, as the newest;
    the other employer's answer to the same question, every other entry and the file's comment stay as written. An
    empty answer is a 400 in D5's words, changing nothing."""
    _write(job_apply_home)
    said = _calls(_desk(srv), [
        ("POST", "/api/answers/change", {"id": _id(ENTRIES[4]), "answer": "  EX-000111 "}),
        ("POST", "/api/answers/change", {"id": _id(ENTRIES[1]), "answer": ""}),
        ("POST", "/api/answers/change", {"id": _id(ENTRIES[1]), "answer": "   "}),
        ("GET", "/api/answers", None)])
    assert said[0] == (200, {"ok": True})
    assert said[1] == said[2] == (400, {"error": "Type an answer, or press Forget."})
    kept = _kept(job_apply_home)
    assert kept == [{**ENTRIES[4], "answer": "EX-000111"}, *ENTRIES[:4]]
    assert (job_apply_home / "answers.yaml").read_text(encoding="utf-8").startswith(HEADER)
    assert said[3][1]["answers"][0] == {"id": _id(ENTRIES[4]), "question": "Employee ID", "answer": "EX-000111",
                                        "at": "Example Litho", "private": True}


def test_forget_removes_that_entry_and_no_other(srv, job_apply_home):
    """D7 test 15: forget takes out that entry alone: the same question's answer for another employer, every other
    entry and the file's comment stay."""
    _write(job_apply_home)
    assert _calls(_desk(srv), [("POST", "/api/answers/forget", {"id": _id(ENTRIES[0])})]) == [(200, {"ok": True})]
    assert _kept(job_apply_home) == ENTRIES[1:]
    assert (job_apply_home / "answers.yaml").read_text(encoding="utf-8").startswith(HEADER)
    assert _calls(_desk(srv), [("POST", "/api/answers/forget", {"id": _id(ENTRIES[2])})]) == [(200, {"ok": True})]
    assert _kept(job_apply_home) == [ENTRIES[1], ENTRIES[3], ENTRIES[4]]


def test_an_unknown_id_is_a_404_a_common_question_isnt_listed_here_and_the_key_is_needed(srv, job_apply_home):
    """D7 test 15: an id that names no remembered answer (one forgotten already, a common question's, none, not text)
    gets 404 and changes nothing; an unknown action 404; and without the page's key, 403, nothing changed."""
    _write(job_apply_home)
    before = (job_apply_home / "answers.yaml").read_bytes()
    bank_id = f"{ENTRIES[3]['match']}\n"
    asks = []
    for entry_id in ("no such question\n", bank_id, _id(ENTRIES[0]) + " ", None, ["x"], 7):
        asks += [("POST", "/api/answers/change", {"id": entry_id, "answer": "Something"}),
                 ("POST", "/api/answers/forget", {"id": entry_id})]
    said = _calls(_desk(srv), [*asks, ("POST", "/api/answers/rename", {"id": _id(ENTRIES[0])})])
    assert [s for s, _ in said] == [404] * (len(asks) + 1), said
    assert said[0][1] == {"error": "That answer isn't remembered any more."}
    assert (job_apply_home / "answers.yaml").read_bytes() == before
    for ask in (("POST", "/api/answers/change", {"id": _id(ENTRIES[0]), "answer": "Something"}),
                ("POST", "/api/answers/forget", {"id": _id(ENTRIES[0])}), ("GET", "/api/answers", None)):
        assert [s for s, _ in _calls(_desk(srv), [ask], token=False)] == [403]
    assert (job_apply_home / "answers.yaml").read_bytes() == before
