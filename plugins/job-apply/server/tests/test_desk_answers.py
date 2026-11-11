"""The Your answers page's Answers the desk remembers, the server's half (the design's D5, D7's tests 14 and 15):
GET /api/answers lists what answers.yaml holds, POST /api/answers/change and /api/answers/forget change or remove one
entry and no other. Made-up people and employers only: the repo is public."""

import json
import threading
import time

import pytest
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
        {"id": _id(ENTRIES[0]), "question": "Employee ID", "answer": None, "empty": False, "at": "Example Fab",
         "private": True},
        {"id": _id(ENTRIES[1]), "question": "Are you willing to work weekends?", "answer": "Yes, every other weekend",
         "empty": False, "at": "", "private": False},
        {"id": _id(ENTRIES[2]), "question": "Middle name", "answer": None, "empty": True, "at": "Example Litho",
         "private": False},
        {"id": _id(ENTRIES[4]), "question": "Employee ID", "answer": None, "empty": False, "at": "Example Litho",
         "private": True},
    ]
    # (an ID's value never in the list, wherever it might be: the page asks for it with Show)
    whole = json.dumps(listed)
    assert "WW123456" not in whole and "EX-778899" not in whole
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
    """D7 test 15: change gives that entry its new answer, for the same question and the same employer, where it is;
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
    assert kept == [*ENTRIES[:4], {**ENTRIES[4], "answer": "EX-000111"}]  # (where it was: the list keeps its order)
    assert (job_apply_home / "answers.yaml").read_text(encoding="utf-8").startswith(HEADER)
    assert said[3][1]["answers"][-1] == {"id": _id(ENTRIES[4]), "question": "Employee ID", "answer": None,
                                         "empty": False, "at": "Example Litho", "private": True}
    assert "EX-000111" not in json.dumps(said[3][1])


def test_show_gives_that_ids_value_alone_and_refuses_the_rest(srv, job_apply_home, caplog):
    """/api/answers/show: the one private entry's value, uncached; another private entry's value never with it; a 404
    for an id that names none, a 400 for an entry that isn't private (the page has it) or was left empty; 403 without
    the key; no value in an error or a log line; and /api/state holds none of it."""
    import logging

    caplog.set_level(logging.DEBUG)
    _write(job_apply_home)
    said = _calls(_desk(srv), [
        ("POST", "/api/answers/show", {"id": _id(ENTRIES[0])}),
        ("POST", "/api/answers/show", {"id": _id(ENTRIES[4])}),
        ("POST", "/api/answers/show", {"id": _id(ENTRIES[1])}),
        ("POST", "/api/answers/show", {"id": _id(ENTRIES[2])}),
        ("POST", "/api/answers/show", {"id": "no such question\n"}),
        ("POST", "/api/answers/show", {"id": f"{ENTRIES[3]['match']}\n"}),
        ("GET", "/api/state", None)])
    assert said[0] == (200, {"answer": "WW123456"}) and said[1] == (200, {"answer": "EX-778899"})
    assert said[2] == (400, {"error": "That answer isn't hidden: it's on the page already."})
    assert said[3] == (400, {"error": "That answer was left empty."})
    assert [s for s, _ in said[4:6]] == [404, 404]
    errors = json.dumps(said[2:6]) + json.dumps(said[6][1]) + "\n".join(r.getMessage() for r in caplog.records)
    for value in ("WW123456", "EX-778899", "Yes, every other weekend"):
        assert value not in errors, value
    assert [s for s, _ in _calls(_desk(srv), [("POST", "/api/answers/show", {"id": _id(ENTRIES[0])})],
                                 token=False)] == [403]


def test_show_is_never_cached(srv, job_apply_home):
    from conftest import run
    from test_desk import _client

    _write(job_apply_home)
    desk = _desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                return await c.post("/api/answers/show", headers={"x-desk-token": desk.token},
                                    json={"id": _id(ENTRIES[0])})
        finally:
            await desk.stop()

    got = run(go())
    assert got.status_code == 200 and got.headers["cache-control"] == "no-store"


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


# answers.yaml as a person keeps it by hand: a note at the top, a comment between two entries, an inline "# why", a
# multi-line answer and quoted ones
HAND = r"""# My own note: these are kept by hand too.
answers:
- match: Employee\s+ID
  answer: WW123456   # why: the badge number there
  question: Employee ID
  at: Example Fab
# the weekend one, asked a lot
- match: weekends
  answer: "Yes, every \"other\" weekend"
  question: Are you willing to work weekends?
- match: cover\s+note
  answer: |
    Line one of the note.
    Line two of the note.
  question: Cover note
- match: start\s+date
  answer: 'Two weeks'' notice'
  question: Start date   # asked by staffing firms
"""
COMMENTS = ("# My own note: these are kept by hand too.\n", "# the weekend one, asked a lot\n",
            "  question: Start date   # asked by staffing firms\n")


def test_change_and_forget_keep_the_persons_own_comments_and_layout(srv, job_apply_home):
    """The design: "Everything else in the file stays as written". change replaces that answer's value alone (a
    multi-line one and a quoted one too), where it is; forget cuts that entry's lines alone; the note at the top, the
    comment between two entries and another entry's inline comment stay, and the rest reads as it did."""
    path = job_apply_home / "answers.yaml"
    path.write_text(HAND, encoding="utf-8")
    entries = yaml.safe_load(HAND)["answers"]
    ids = [_id(e) for e in entries]
    said = _calls(_desk(srv), [
        ("POST", "/api/answers/change", {"id": ids[2], "answer": "One line now."}),
        ("POST", "/api/answers/change", {"id": ids[3], "answer": "Three weeks' notice"}),
        ("POST", "/api/answers/change", {"id": ids[1], "answer": 'Sure, "most" weekends'}),
    ])
    assert said == [(200, {"ok": True})] * 3
    text = path.read_text(encoding="utf-8")
    assert all(c in text for c in COMMENTS) and "  answer: WW123456   # why: the badge number there\n" in text, text
    assert not text.startswith(HEADER)  # (edited in place, not written anew)
    changed = [entries[0], {**entries[1], "answer": 'Sure, "most" weekends'},
               {**entries[2], "answer": "One line now."}, {**entries[3], "answer": "Three weeks' notice"}]
    assert yaml.safe_load(text) == {"answers": changed}
    assert "Line one of the note." not in text

    assert _calls(_desk(srv), [("POST", "/api/answers/forget", {"id": ids[0]}),
                               ("POST", "/api/answers/forget", {"id": ids[2]})]) == [(200, {"ok": True})] * 2
    text = path.read_text(encoding="utf-8")
    assert all(c in text for c in COMMENTS), text
    assert "WW123456" not in text and "why: the badge number" not in text and "One line now." not in text
    assert yaml.safe_load(text) == {"answers": [changed[1], changed[3]]}
    assert text.startswith(COMMENTS[0] + "answers:\n" + COMMENTS[1] + "- match: weekends\n"), text


def test_forgetting_the_last_answer_keeps_the_note_and_the_next_one_is_saved_under_it(job_apply_home):
    """The only entry forgotten: "answers:" is left with nothing under it, which reads as the empty list it is, so the
    edit in place stands (the person's note stays); the next answer saved is kept under "answers:"."""
    path = job_apply_home / "answers.yaml"
    path.write_text("# My own note.\nanswers:\n- match: weekends\n  answer: 'Yes'  # why: it pays more\n",
                    encoding="utf-8")
    config.forget_answer("weekends\n")
    assert path.read_text(encoding="utf-8") == "# My own note.\nanswers:\n"
    assert config.remembered_answers() == []
    config.save_answer("Are you willing to work nights?", "No")
    assert [a["answer"] for a in yaml.safe_load(path.read_text(encoding="utf-8"))["answers"]] == ["No"]


@pytest.mark.parametrize("between", ["# the next one's note\n", "\n", "\n# the next one's note\n\n"],
                         ids=["a comment", "a blank line", "both"])
def test_forget_leaves_what_is_between_an_entry_ending_in_a_list_and_the_next(job_apply_home, between):
    """An entry whose last value is a block list ends, by its node, on the next entry's dash: the comment and blank
    lines between are the next entry's, and stay when it's forgotten; so do those after the last entry in the file."""
    path = job_apply_home / "answers.yaml"
    first = "- match: shifts\n  answer:\n  - Days\n  - Nights\n"
    second = "- match: weekends\n  answer: 'Yes'\n  at:\n  - Example Fab\n"
    path.write_text("answers:\n" + first + between + second + between, encoding="utf-8")
    config.forget_answer("shifts\n")
    assert path.read_text(encoding="utf-8") == "answers:\n" + between + second + between
    # (and the last entry in the file, its list's end at the end of the file)
    path.write_text("answers:\n" + second.replace("  at:\n  - Example Fab\n", "") + first + between,
                    encoding="utf-8")
    config.forget_answer("shifts\n")
    assert path.read_text(encoding="utf-8") == "answers:\n- match: weekends\n  answer: 'Yes'\n" + between


def test_an_answer_saved_from_a_jobs_card_keeps_the_persons_comments_and_other_entries(job_apply_home):
    """A job's card's answer, "remember" ticked (save_answer), rewrote the whole of answers.yaml: the person's note,
    comments and hand-written entries' layout went. It's put in the file's own text now, as Change and Forget are: a
    new one first under "answers:", one answered again cut from where it was and put first; every other line stays."""
    path = job_apply_home / "answers.yaml"
    path.write_text(HAND, encoding="utf-8")
    entries = yaml.safe_load(HAND)["answers"]
    new = config.save_answer("Are you willing to work nights?", "No")
    text = path.read_text(encoding="utf-8")
    assert not text.startswith(HEADER) and text == HAND.replace(
        "answers:\n", "answers:\n" + yaml.safe_dump([new], sort_keys=False, allow_unicode=True)), text
    assert yaml.safe_load(text) == {"answers": [new, *entries]}
    # the weekend question answered again: its entry goes from where it was (the comment above it stays) and comes first
    again = config.save_answer("Are you willing to work weekends?", "Most weekends", source="Example Fab")
    text = path.read_text(encoding="utf-8")
    assert all(c in text for c in COMMENTS) and "  answer: WW123456   # why: the badge number there\n" in text, text
    assert "  answer: |\n    Line one of the note.\n    Line two of the note.\n" in text and "every" not in text
    assert yaml.safe_load(text) == {"answers": [again, new, entries[0], *entries[2:]]}
    assert text.startswith(COMMENTS[0] + "answers:\n- match: "), text
    # a list dashed further in keeps its dashes; a file with no answers: yet gains one at its end
    path.write_text("# mine\nanswers:\n  # kept by hand\n  - match: shifts\n    answer: Days\n", encoding="utf-8")
    config.save_answer("Can you lift 50 pounds?", "Yes")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# mine\nanswers:\n  - match: ") and text.endswith(
        "  # kept by hand\n  - match: shifts\n    answer: Days\n"), text
    assert [a["answer"] for a in yaml.safe_load(text)["answers"]] == ["Yes", "Days"]
    path.write_text("# only a note so far\n", encoding="utf-8")
    config.save_answer("Can you lift 50 pounds?", "Yes")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# only a note so far\nanswers:\n- match: ") and len(_kept(job_apply_home)) == 1, text


def test_an_answer_saved_where_the_edit_wouldnt_read_as_wanted_is_written_anew(job_apply_home):
    """The read-back guard: two "answers:" keys (YAML reads the last), the new entry put under the first wouldn't be
    read at all. The edit in place is checked against what was wanted before it's written, so the file is written anew,
    and the answer kept is the one YAML reads, with the new one."""
    path = job_apply_home / "answers.yaml"
    path.write_text("answers:\n- match: first\n  answer: A\nanswers:\n- match: second\n  answer: B\n", encoding="utf-8")
    new = config.save_answer("Are you willing to work nights?", "No")
    text = path.read_text(encoding="utf-8")
    assert text.startswith(HEADER), text
    assert yaml.safe_load(text) == {"answers": [new, {"match": "second", "answer": "B"}]}


def test_a_file_laid_out_so_it_cant_be_edited_in_place_is_written_anew_with_the_change(srv, job_apply_home):
    """A flow list (all on one line): the change can't be made in its text safely, so the file is written as the desk
    writes it, with that change alone."""
    path = job_apply_home / "answers.yaml"
    path.write_text('answers: [{match: weekends, answer: "Yes"}, {match: nights, answer: "No"}]\n', encoding="utf-8")
    assert _calls(_desk(srv), [("POST", "/api/answers/change", {"id": "weekends\n", "answer": "Sometimes"})]) == [
        (200, {"ok": True})]
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "answers": [{"match": "weekends", "answer": "Sometimes"}, {"match": "nights", "answer": "No"}]}


def test_many_answers_saved_at_once_are_all_kept(job_apply_home, monkeypatch):
    """Two saves at once (a job's card and the Your answers page, two jobs' cards): each reads, changes and writes
    answers.yaml under its lock, through a temporary file of its own, so none is lost. A slow read widens the window."""
    read = config._answers_file

    def slow_read():
        out = read()
        time.sleep(0.02)
        return out

    monkeypatch.setattr(config, "_answers_file", slow_read)
    config.save_answer("Question zero", "kept")
    labels = [f"Question number {n}" for n in range(12)]
    threads = [threading.Thread(target=config.save_answer, args=(label, f"answer {n}"))
               for n, label in enumerate(labels)]
    threads.append(threading.Thread(target=config.change_answer,
                                    args=(f"{config.question_pattern('Question zero')}\n", "changed")))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    kept = {a["question"]: a["answer"] for a in yaml.safe_load(
        (job_apply_home / "answers.yaml").read_text(encoding="utf-8"))["answers"]}
    assert kept == {"Question zero": "changed", **{label: f"answer {n}" for n, label in enumerate(labels)}}
    assert not list(job_apply_home.glob("answers.yaml*.tmp"))
