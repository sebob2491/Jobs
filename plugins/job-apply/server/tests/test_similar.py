"""Suggested answers (similar.py), the server's half: a model is asked whether a new question means the same as one the
person answered, with the questions' words alone; the offer is shown on the card and used only after the person's yes.
No network and no real model: the stand-in records the exact prompt and answers what the test hands it. Made-up people
and employers only: the repo is public."""

import asyncio
import json
import logging

import pytest
import yaml
from conftest import run
from test_desk import _client
from test_desk_settings import _calls, _desk
from test_letters import KEY, _status, fake_api  # noqa: F401  (fake_api: a fixture)

from job_apply import autofill, config, letters, report, similar
from job_apply.desk import Desk
from job_apply.pipeline import Applier, Run, question_key

COMPANY = "Example Litho"
TITLE = "Equipment Technician"
RELOCATE = "Would you be willing to relocate?"
RELOCATE_EARLIER = "Are you willing to move to another city for work?"
MILES = ["Within 50 miles", "Within 100 miles", "Anywhere in the US"]


def box(label, options=("Yes", "No"), **more):
    return {"id": label[:6], "kind": "radio_group", "label": label, "required": True, "options": list(options), **more}


class Model:
    """letters._ask as the tests' own: it writes down each call (the system prompt, the message, the model, the time
    allowed) and answers as `reply` says (a text; or a function of the call, which may raise), after `slow` seconds."""

    def __init__(self):
        self.calls = []
        self.reply = "{}"
        self.slow = 0.0

    async def ask(self, exe, system, content, model, timeout):
        self.calls.append({"exe": exe, "system": system, "content": content, "model": model, "timeout": timeout})
        if self.slow:
            await asyncio.sleep(self.slow)
        said = self.reply(self.calls[-1]) if callable(self.reply) else self.reply
        return said

    @property
    def prompts(self):
        return [c["content"][0]["text"] for c in self.calls]

    def message(self):
        """The one call's message as JSON."""
        [text] = self.prompts
        return json.loads(text)


@pytest.fixture
def model(job_apply_home, loop, monkeypatch):
    m = Model()
    monkeypatch.setattr(letters, "_ask", m.ask)
    monkeypatch.setattr(letters, "find_claude", lambda: "claude-for-the-tests")
    monkeypatch.setattr(letters, "claude_on_hand", lambda: "claude-for-the-tests")
    monkeypatch.setattr(letters, "api_key", lambda: None)
    monkeypatch.delenv("JOB_APPLY_NEVER_SUBMIT", raising=False)
    return m


def remember(question, answer, source="", at=""):
    config.save_answer(question, answer, source, at=at)


def answers_text(home):
    path = home / "answers.yaml"
    return path.read_text(encoding="utf-8") if path.exists() else None


def pause(applier, run_, questions):
    """The applier's stop with questions, as the pipeline makes it (the hook after it starts the one task)."""
    applier._pause(run_, "questions", "your profile doesn't answer", questions, code="questions")


def new_run(srv, applier, questions, company=COMPANY):
    job = srv.add_job(url="https://example.com/jobs/1", title=TITLE, company=company)["job"]
    r = Run(job["id"], TITLE, company)
    applier.runs[job["id"]] = r
    return job["id"], r, questions


async def stopped(applier, r, questions):
    """A stop with questions, and the hook's task awaited (None where there is none)."""
    pause(applier, r, questions)
    task = applier._similar_tasks.get(r.job_id)
    if task is not None:
        await task
    return task


# ------------------------------------------------------------------ 1: what goes to the model
# (the new question's side: a box that is any of these is never asked about)
NEW_EXCLUDED = {
    "no choices": {"id": "t", "kind": "text", "label": "Describe your relocation plans"},
    "an account form's": box("Choose a security level", account=True),
    "about a notice": box("Do you want updates?", about="notice text"),
    "own id": box("Where did you work before?", own_id=True),
    "id unsure": box("Anything else we should call you?", id_unsure=True),
    "employer id": box("Provide it if you have one", employer_id=True),
    "left here": box("Would you drive a forklift?", left_here=True),
    "asks an id": box("Employee ID number on file"),
    "security question": box("Choose a security question to answer"),
    "agree": box("Do you agree to the arbitration of disputes?"),
    "consent": box("Do you consent to a background check?"),
    "notice": box("Have you read the privacy notice?"),
    "terms": box("Do you accept the terms of use?"),
    "acknowledge": box("Do you acknowledge the AI screening notice?"),
    "certify": box("Do you certify your answers are true?"),
    "attest": box("I attest that I told the truth"),
    "employer specific": box("Do you have family members who work here?"),
    "names the employer": box("Have you used Example Litho tools?"),
    "points away": box("Do you meet the requirements above?"),
    "a joined label": box("Questions | Shift | Pick one"),
    "a date's part": box("Date", options=["01", "02"], role="spinbutton", sublabel="Day"),
}
# (the earlier side: remembered answers that are never offered, each with an answer among the choices)
EARLIER_EXCLUDED = [
    ("kept for an employer", "Would you do shift work on weekends?", "Yes", "Other Fab", "Other Fab"),
    ("an id", "Employee ID", "Yes", "", ""),
    ("agree", "Do you agree to binding arbitration?", "Yes", "", ""),
    ("consent", "Do you consent to a background check?", "Yes", "", ""),
    ("privacy notice", "Have you read the privacy notice?", "Yes", "", ""),
    ("terms", "Do you accept the terms of use?", "Yes", "", ""),
    ("acknowledge", "I acknowledge the notice about AI screening", "Yes", "", ""),
    ("security", "Your security question: pick one", "Yes", "", ""),
    ("employer specific", "Do any of your relatives work here?", "Yes", "", ""),
    ("names the employer", "Have you used Example Litho tools?", "Yes", "", ""),
    ("points away", "Do you meet the requirements above?", "Yes", "", ""),
]


@pytest.mark.parametrize("kind", NEW_EXCLUDED)
def test_a_new_question_of_an_excluded_kind_is_never_asked_about(job_apply_home, kind):
    q = NEW_EXCLUDED[kind]
    assert similar.may_ask(q, COMPANY) is False


def test_a_plain_question_with_choices_and_a_work_authorization_question_may_be_asked_about(job_apply_home):
    assert similar.may_ask(box(RELOCATE), COMPANY) is True
    # (it asks about the person: it agrees to nothing)
    assert similar.may_ask(box("Are you legally authorized to work in the United States?"), COMPANY) is True
    assert similar.may_ask(box("Are you legally authorized to work here and do you agree?"), COMPANY) is False


def test_the_prompt_holds_the_words_of_the_questions_and_nothing_else(model, job_apply_home):
    """Nothing but the questions' words and the choices: no answer of the person's (the one remembered for a question
    that isn't offered, an ID, the profile's), no name, email, employer or job title, and none of the kinds of
    question that are never asked about, on the new side or the earlier."""
    remember(RELOCATE_EARLIER, "yes")
    remember("Would you work a rotating shift?", "Maybe someday")  # (not among the choices)
    for _, question, answer, source, at in EARLIER_EXCLUDED:
        remember(question, answer, source, at=at)
    remember("Do you hold a forklift license?", "No")
    config.save_answer("Employee ID", "WW123456", COMPANY, at=COMPANY)
    asked = [box(RELOCATE), box("Do you hold a valid forklift license?"), *NEW_EXCLUDED.values()]
    found = run(similar.suggest(asked, COMPANY))
    assert found == {} and len(model.calls) == 1
    call = model.calls[0]
    text = model.prompts[0]
    sent = json.loads(text)
    assert set(sent) == {"new_questions"}
    assert [(n["question"], n["choices"]) for n in sent["new_questions"]] == [
        (RELOCATE, ["Yes", "No"]), ("Do you hold a valid forklift license?", ["Yes", "No"])]
    assert all(set(n) == {"number", "question", "choices", "earlier_questions"} for n in sent["new_questions"])
    for n in sent["new_questions"]:
        assert sorted(e["question"] for e in n["earlier_questions"]) == sorted(
            [RELOCATE_EARLIER, "Do you hold a forklift license?"])
    assert all(set(e) == {"number", "question"} for n in sent["new_questions"] for e in n["earlier_questions"])
    everything = text + call["system"]
    for private in ("yes", "Maybe someday", "WW123456", "Sam", "Rivera", "sam.rivera@example.com", "480-555-0123",
                    "Chandler", "Intel", COMPANY, TITLE, "Equipment"):
        assert private not in everything.replace("Yes", ""), private
    for words in [q["label"] for q in NEW_EXCLUDED.values()] + [e[1] for e in EARLIER_EXCLUDED]:
        assert words not in everything, words
    assert call["model"] == similar.SIMILAR_MODEL == "claude-haiku-5-5" and call["timeout"] == similar.SIMILAR_TIMEOUT


# ------------------------------------------------------------------ 2: an answer among the new choices
def test_an_earlier_answer_is_offered_only_when_it_is_one_of_the_new_questions_choices(model, job_apply_home):
    remember(RELOCATE_EARLIER, "Yes")
    q = box(RELOCATE)
    [c] = similar.candidates(q, COMPANY)
    assert (c["question"], c["answer"]) == (RELOCATE_EARLIER, "Yes")
    # (case and spacing apart: the offer is spelled as the new question's own choice)
    remember(RELOCATE_EARLIER, "  yes ")
    assert [c["answer"] for c in similar.candidates(q, COMPANY)] == ["Yes"]
    # not among the choices: no candidate, nothing is sent, and the card is as it is
    remember(RELOCATE_EARLIER, "Yes, if the pay is right")
    assert similar.candidates(q, COMPANY) == []
    assert run(similar.suggest([q], COMPANY)) == {} and model.calls == []
    remember(RELOCATE_EARLIER, "Within 100 miles")
    assert similar.candidates(q, COMPANY) == []
    assert [c["answer"] for c in similar.candidates(box("How far would you relocate?", MILES), COMPANY)] == [
        "Within 100 miles"]
    # (an answer left empty, or a list's, is none)
    remember(RELOCATE_EARLIER, None)
    assert similar.candidates(q, COMPANY) == []


def test_a_stop_with_no_candidate_sends_nothing_and_its_card_is_as_it_was(model, srv, job_apply_home):
    applier = Applier(srv)
    asked = [box(RELOCATE)]
    _, r, questions = new_run(srv, applier, asked)

    async def go():
        return await stopped(applier, r, questions)

    assert run(go()) is not None  # (a task started: one is, wherever a model is)
    assert model.calls == [] and r.similar == {} and questions == [box(RELOCATE)]
    assert "similar" not in json.dumps(r.public())


# ------------------------------------------------------------------ 3: nothing is filled before "use it"
def test_an_offer_changes_nothing_until_the_person_says_yes_and_is_in_no_state_or_report(model, srv, job_apply_home):
    remember(RELOCATE_EARLIER, "within 100 miles")
    before = answers_text(job_apply_home)
    applier = Applier(srv)
    question = box("How far could you relocate?", MILES)
    job_id, r, questions = new_run(srv, applier, [question, box("Shift you prefer", ["Days", "Nights"])])
    model.reply = '{"1": 1}'

    async def go():
        await stopped(applier, r, questions)

    run(go())
    assert r.status == "needs_you" and r.need == "questions" and r.once == {}
    assert r.similar == {question_key("How far could you relocate?"): {
        "earlier": RELOCATE_EARLIER, "answer": "Within 100 miles", "match": config.question_pattern(RELOCATE_EARLIER)}}
    assert [q.get("similar") for q in r.questions] == [True, None]  # (the flag alone, on the card's question)
    assert answers_text(job_apply_home) == before
    # (the state, a problem report and a note: no earlier question, no `similar`, and the earlier answer as it was
    # spelled by the person, whose choice's own spelling the card has anyway)
    state = json.dumps(r.public())
    job = {"id": job_id, "title": TITLE, "company": COMPANY, "url": "https://example.com/jobs/1"}
    shown = state + report.build(job, r, config.Profile.load())["preview"] + report.note(job, r)
    for held in (RELOCATE_EARLIER, "within 100 miles", "earlier", "match"):
        assert held not in shown, held
    assert "similar" not in r.public() and '"similar": true' in state
    # a model's one question: the stop still waits, nothing was typed for it
    assert r.status == "needs_you" and applier.runs[job_id].once == {}


def test_the_offers_of_a_stop_are_dropped_when_it_ends_and_never_land_on_the_next(model, srv, job_apply_home):
    remember(RELOCATE_EARLIER, "Yes")
    applier = Applier(srv)
    _, r, questions = new_run(srv, applier, [box(RELOCATE)])
    model.reply = '{"1": 1}'
    model.slow = 5

    async def go():
        pause(applier, r, questions)
        task = applier._similar_tasks[r.job_id]
        await asyncio.sleep(0.05)
        applier._cancel(r.job_id)  # (what the person's answer, Skip and Carry on do)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert r.similar == {} and "similar" not in questions[0]
        # a later stop's own: the first stop's slow answer never comes to it; this one's offer is for its own question
        model.slow = 0
        pause(applier, r, [box("Another one", ["Yes", "No"])])
        await applier._similar_tasks[r.job_id]

    run(go())
    assert list(r.similar) == ["another one"] and [q.get("similar") for q in r.questions] == [True]
    assert not any(q.get("similar") for q in questions)


# ------------------------------------------------------------------ 4: use it, remember it, and the next run is silent
def test_use_it_with_remember_saves_the_new_wording_with_matched_and_the_next_run_fills_it_without_a_model(
        model, srv, job_apply_home):
    remember(RELOCATE_EARLIER, "Yes")
    applier_desk = Desk(srv)
    applier = applier_desk.applier
    job_id, r, questions = new_run(srv, applier, [box(RELOCATE)])
    model.reply = '{"1": 1}'

    async def go():
        await stopped(applier, r, questions)

    run(go())
    assert r.similar
    assert applier_desk.answer(job_id, [{"label": RELOCATE, "use_similar": True, "remember": True}]) is None
    # (filled as any answer is, the offer's value and not the page's: remembered, so it is in answers.yaml, not in once)
    assert r.once == {}
    entries = yaml.safe_load(answers_text(job_apply_home))["answers"]
    assert entries[0] == {"match": config.question_pattern(RELOCATE), "answer": "Yes", "question": RELOCATE,
                          "from": COMPANY, "matched": RELOCATE_EARLIER}
    assert {a["question"]: a.get("matched") for a in config.remembered_answers()} == {
        RELOCATE: RELOCATE_EARLIER, RELOCATE_EARLIER: None}
    assert all("matched" not in a for a in config.remembered_answers() if a["question"] == RELOCATE_EARLIER)
    # the next run: the silent path answers it, and no model is asked (a call would fail this test)
    count = len(model.calls)
    model.reply = lambda call: pytest.fail("the model was asked")
    got = autofill._answer_bank(config.Profile.load(), RELOCATE, {"company": COMPANY})
    assert got is not None and got.value == "Yes" and got.rule.startswith("answers[")
    assert len(model.calls) == count


def test_use_it_without_remember_fills_this_application_alone(model, srv, job_apply_home):
    remember(RELOCATE_EARLIER, "No")
    desk = Desk(srv)
    job_id, r, questions = new_run(srv, desk.applier, [box(RELOCATE)])
    model.reply = '{"1": 1}'
    run(stopped(desk.applier, r, questions))
    before = answers_text(job_apply_home)
    desk.answer(job_id, [{"label": RELOCATE, "use_similar": True, "remember": False}])
    assert r.once == {question_key(RELOCATE): "No"} and answers_text(job_apply_home) == before


# ------------------------------------------------------------------ 5: declined pairs
def test_a_declined_pair_is_kept_and_never_sent_again(model, srv, job_apply_home):
    remember(RELOCATE_EARLIER, "Yes")
    desk = _desk(srv)
    job_id, r, questions = new_run(srv, desk.applier, [box(RELOCATE)])
    model.reply = '{"1": 1}'
    run(stopped(desk.applier, r, questions))
    assert r.similar and questions[0]["similar"] is True
    [(status, said)] = _calls(desk, [("POST", "/api/similar/decline", {"job_id": job_id, "label": RELOCATE})])
    assert (status, said) == (200, {"ok": True})
    kept = json.loads((job_apply_home / "similar.json").read_text(encoding="utf-8"))
    assert kept == [{"new": "would you be willing to relocate", "earlier": config.question_pattern(RELOCATE_EARLIER)}]
    assert r.similar == {} and "similar" not in questions[0] and r.status == "needs_you"
    # (never a candidate again, for these words; another new question still may be)
    assert similar.candidates(box(RELOCATE), COMPANY) == []
    assert similar.candidates(box("Are you ready to relocate?"), COMPANY) != []
    model.calls.clear()
    assert run(similar.suggest([box(RELOCATE)], COMPANY)) == {} and model.calls == []
    # nothing to decline twice
    [(status, said)] = _calls(desk, [("POST", "/api/similar/decline", {"job_id": job_id, "label": RELOCATE})])
    assert status == 400


# ------------------------------------------------------------------ 6: no model, a slow one, a reply that is wrong
@pytest.mark.parametrize("reply", ["not json at all", "[1]", '{"1": 99}', '{"1": [1, 2]}', '{"1": "1"}',
                                   '{"1": true}', '{"1": 1, "7": 1}', '{"2": 1}', "", '{"1": null}'])
def test_a_reply_that_is_not_what_was_asked_for_is_no_offer(model, job_apply_home, reply):
    remember(RELOCATE_EARLIER, "Yes")
    remember("Do you want to move to another town for a job?", "No")
    model.reply = reply
    assert run(similar.suggest([box(RELOCATE)], COMPANY)) == {}
    assert len(model.calls) == 1


def test_a_reply_is_read_per_question_and_a_fenced_one_is_still_json(model, job_apply_home):
    remember(RELOCATE_EARLIER, "Yes")
    remember("Do you hold a forklift license?", "No")
    asked = [box(RELOCATE), box("Do you hold a valid forklift license?")]
    # (the first question's candidates are numbered 1 and 2, the second's 3 and 4: a number offered for the other
    # question is no match for this one)
    model.reply = lambda call: ('```json\n{"1": %d, "2": %d}\n```' % (
        next(e["number"] for e in json.loads(call["content"][0]["text"])["new_questions"][0]["earlier_questions"]
             if e["question"] == RELOCATE_EARLIER),
        next(e["number"] for e in json.loads(call["content"][0]["text"])["new_questions"][0]["earlier_questions"]
             if e["question"] == "Do you hold a forklift license?")))
    found = run(similar.suggest(asked, COMPANY))
    assert found == {question_key(RELOCATE): similar.Offer(
        RELOCATE_EARLIER, "Yes", config.question_pattern(RELOCATE_EARLIER))}


def test_a_slow_model_is_given_up_on_after_the_time_allowed(model, job_apply_home, monkeypatch):
    remember(RELOCATE_EARLIER, "Yes")
    assert similar.SIMILAR_TIMEOUT == 20.0
    monkeypatch.setattr(similar, "SIMILAR_TIMEOUT", 0.1)  # (the 20 seconds, faked)
    model.slow, model.reply = 5, '{"1": 1}'
    start = asyncio.get_event_loop().time()
    assert run(similar.suggest([box(RELOCATE)], COMPANY)) == {}
    assert asyncio.get_event_loop().time() - start < 3
    assert model.calls[0]["timeout"] == 0.1


@pytest.mark.parametrize("fail", [RuntimeError("boom sk-ant-secret"), letters.LetterError("no"),
                                  letters.Unavailable("nothing to ask with"), ValueError("x")])
def test_a_model_that_fails_is_no_offer_nothing_raised_and_one_debug_line_naming_no_question(
        model, job_apply_home, caplog, fail):
    remember(RELOCATE_EARLIER, "Yes")

    def boom(call):
        raise fail

    model.reply = boom
    with caplog.at_level(logging.DEBUG):
        assert run(similar.suggest([box(RELOCATE)], COMPANY)) == {}
    ours = [x for x in caplog.records if x.name == similar.__name__]
    assert len(ours) == 1 and ours[0].levelno == logging.DEBUG
    said = caplog.text
    for held in (RELOCATE, RELOCATE_EARLIER, "sk-ant-secret", "Would you"):
        assert held not in said


def test_no_model_is_no_offer_and_no_call(model, job_apply_home, monkeypatch):
    remember(RELOCATE_EARLIER, "Yes")
    monkeypatch.setattr(letters, "find_claude", lambda: None)
    assert run(similar.suggest([box(RELOCATE)], COMPANY)) == {} and model.calls == []


def test_a_failed_call_leaves_the_card_as_it_is(model, srv, job_apply_home):
    remember(RELOCATE_EARLIER, "Yes")
    applier = Applier(srv)
    _, r, questions = new_run(srv, applier, [box(RELOCATE)])
    model.reply = "nonsense"
    run(stopped(applier, r, questions))
    assert r.similar == {} and questions == [box(RELOCATE)] and r.status == "needs_you"


def test_the_profiles_similar_model_overrides_the_usual_one(model, job_apply_home):
    path = job_apply_home / "profile.yaml"
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    profile["settings"]["similar_model"] = "claude-sonnet-5-5"
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    remember(RELOCATE_EARLIER, "Yes")
    run(similar.suggest([box(RELOCATE)], COMPANY))
    assert model.calls[0]["model"] == "claude-sonnet-5-5"
    assert "similar_model" in config.KNOWN_SETTINGS and not config.Profile.load().settings.warnings


# ------------------------------------------------------------------ 7: a consent's Yes never travels
def test_a_remembered_yes_to_a_notice_is_no_candidate_for_another_notice_or_for_a_plain_question(model, job_apply_home):
    remember("I agree to the terms and the privacy notice", "Yes")
    remember("I acknowledge the notice about AI screening", "Yes")
    remember("Are you legally authorized to work in the United States?", "Yes")
    notices = ["I acknowledge that my data will be kept for a year", "Do you accept the notice about AI screening?"]
    assert not any(similar.may_ask(box(n), COMPANY) for n in notices)  # (a notice is never asked about)
    # (and the earlier notices are no candidate for a plain Yes/No question: only the work authorization one is)
    assert [c["question"] for c in similar.candidates(box("Do you hold a valid forklift license?"), COMPANY)] == [
        "Are you legally authorized to work in the United States?"]
    # (the work authorization question is not a consent: it is offered for another's wording of it)
    [c] = similar.candidates(box("Are you eligible to work in the US?"), COMPANY)
    assert c["question"] == "Are you legally authorized to work in the United States?"
    run(similar.suggest([box(n) for n in notices], COMPANY))
    assert model.calls == []


# ------------------------------------------------------------------ 8: use_similar with no offer, and the routes' key
def test_use_it_for_a_question_with_no_offer_is_refused_and_nothing_is_filled(model, srv, job_apply_home):
    remember(RELOCATE_EARLIER, "Yes")
    desk = _desk(srv)
    job_id, r, _ = new_run(srv, desk.applier, [box(RELOCATE)])
    r.status, r.need = "needs_you", "questions"
    r.questions = [box(RELOCATE)]
    before = answers_text(job_apply_home)
    sent = {"job_id": job_id, "answers": [{"label": "Another question", "value": "Days", "remember": True},
                                         {"label": RELOCATE, "use_similar": True, "remember": True}]}
    [(status, said)] = _calls(desk, [("POST", "/api/answer", sent)])
    assert status == 400 and "no suggested answer" in said["error"]
    assert r.once == {} and r.status == "needs_you" and answers_text(job_apply_home) == before


def test_the_routes_refuse_without_the_pages_key(model, srv, job_apply_home):
    desk = _desk(srv)
    asks = [("GET", "/api/similar?job_id=1", None), ("POST", "/api/similar/decline", {"job_id": 1, "label": "x"}),
            ("POST", "/api/answer", {"job_id": 1, "answers": [{"label": "x", "use_similar": True}]})]
    assert [s for s, _ in _calls(desk, asks, token=False)] == [403, 403, 403]


def test_the_offers_route_gives_the_answer_to_the_page_alone_and_never_for_another_stop(model, srv, job_apply_home):
    desk = _desk(srv)
    job_id, r, _ = new_run(srv, desk.applier, [])
    shown = box(RELOCATE, similar=True)
    r.status, r.need, r.questions = "needs_you", "questions", [shown, box("Shift", ["Days", "Nights"])]
    r.similar = {question_key(RELOCATE): {"earlier": RELOCATE_EARLIER, "answer": "Yes", "match": "m"}}

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                token = {"x-desk-token": desk.token}
                got = await c.get(f"/api/similar?job_id={job_id}", headers=token)
                none = await c.get("/api/similar?job_id=424242", headers=token)
                bad = await c.get("/api/similar?job_id=x", headers=token)
                state = await c.get("/api/state", headers=token)
                return got, none, bad, state
        finally:
            await desk.stop()

    got, none, bad, state = run(go())
    assert got.status_code == 200 and got.headers["cache-control"] == "no-store"
    assert got.json() == {"offers": [{"label": RELOCATE, "earlier": RELOCATE_EARLIER, "answer": "Yes"}]}
    assert none.json() == {"offers": []} and bad.status_code == 400
    assert RELOCATE_EARLIER not in state.text and '"similar":true' in state.text
    r.status = "failed"  # (a job no longer at its stop offers nothing)
    assert _calls(desk, [("GET", f"/api/similar?job_id={job_id}", None)]) == [(200, {"offers": []})]


# ------------------------------------------------------------------ 9: off
def _stop_and_count(model, srv, applier):
    remember(RELOCATE_EARLIER, "Yes")
    _, r, questions = new_run(srv, applier, [box(RELOCATE)])
    model.reply = '{"1": 1}'
    task = run(stopped(applier, r, questions))
    return task, r, questions


def test_with_the_switch_off_no_task_starts_and_the_card_is_as_it_was(model, srv, job_apply_home):
    applier = Applier(srv)
    applier.set_suggest_similar(False)
    task, r, questions = _stop_and_count(model, srv, applier)
    assert task is None and model.calls == [] and applier._similar_tasks == {}
    assert r.similar == {} and questions == [box(RELOCATE)]
    assert set(r.public()) == {k for k in Run(1).public()}  # (nothing new in the state)
    assert "similar" not in r.public()


def test_in_practice_mode_the_hard_switch_never_asks(model, srv, job_apply_home, monkeypatch):
    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    task, r, questions = _stop_and_count(model, srv, Applier(srv))
    assert task is None and model.calls == [] and r.similar == {}


def test_the_live_checks_test_identity_never_asks(model, srv, job_apply_home, monkeypatch):
    real = config.Settings.from_dict.__func__

    def as_test_identity(cls, *a, **kw):
        s = real(cls, *a, **kw)
        s.test_identity = True
        return s

    monkeypatch.setattr(config.Settings, "from_dict", classmethod(as_test_identity))
    task, r, questions = _stop_and_count(model, srv, Applier(srv))
    assert task is None and model.calls == [] and r.similar == {}
    monkeypatch.setenv("JOB_APPLY_LIVE_TEST_IDENTITY", "1")  # (and the environment's own marks of it)
    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    assert Applier(srv)._similar_on() is False


def test_with_no_model_the_stop_asks_nothing_and_its_card_is_as_it_was(model, srv, job_apply_home, monkeypatch):
    monkeypatch.setattr(letters, "find_claude", lambda: None)
    task, r, questions = _stop_and_count(model, srv, Applier(srv))
    assert model.calls == [] and r.similar == {} and questions == [box(RELOCATE)]


def test_the_switch_is_on_unless_turned_off_and_in_the_state_and_safety(model, srv, job_apply_home, monkeypatch):
    desk = _desk(srv)
    settings = desk.state()["settings"]
    assert settings["suggest_similar"] is True and settings["similar_by"] == letters.BY
    line = {x["key"]: x for x in desk.safety()["lines"]}["suggest_similar"]
    assert line["name"] == "Suggests answers you gave to similar questions" and line["value"] == "on"
    assert line["said"] == ("Only the questions' words go to the model, never an answer or anything about you; used "
                            "only after your yes.")
    [(status, saved)] = _calls(desk, [("POST", "/api/settings", {"suggest_similar": False})])
    assert status == 200 and saved["suggest_similar"] is False
    assert json.loads((job_apply_home / "desk.json").read_text(encoding="utf-8"))["suggest_similar"] is False
    fresh = _desk(srv)
    assert fresh.applier.suggest_similar is False and fresh.state()["settings"]["suggest_similar"] is False
    assert {x["key"]: x for x in fresh.safety()["lines"]}["suggest_similar"]["value"] == "off"
    monkeypatch.setattr(letters, "claude_on_hand", lambda: None)
    assert Desk(srv).state()["settings"]["similar_by"] is None
    nobody = Desk(srv)
    nobody.applier.suggest_similar = True
    assert {x["key"]: x for x in nobody.safety()["lines"]}["suggest_similar"]["value"] == "on, with no model to ask"
    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    on = _desk(srv)
    on.applier.suggest_similar = True
    assert {x["key"]: x for x in on.safety()["lines"]}["suggest_similar"]["value"] == "off in practice mode"


# ------------------------------------------------------------------ 10: the key and the prompt are in no log
def test_the_key_and_the_prompt_appear_in_no_log_line(fake_api, srv,  # noqa: F811
                                                       job_apply_home, caplog, monkeypatch):
    monkeypatch.setattr(letters, "find_claude", lambda: None)
    config.save_site_password(config.API_KEY_NAME, KEY)
    remember(RELOCATE_EARLIER, "Yes")
    asked = [box(RELOCATE)]
    fake_api.answers[:] = ['{"1": 1}', _status(__import__("anthropic").AuthenticationError, 401, f"bad {KEY}")]
    with caplog.at_level(logging.DEBUG):
        found = run(similar.suggest(asked, COMPANY))  # (an offer, through the API with the saved key)
        gone = run(similar.suggest(asked, COMPANY))  # (and a key turned down: no offer)
    assert list(found) == [question_key(RELOCATE)] and gone == {}
    sent = fake_api.asked[0]
    assert fake_api.keys == [KEY, KEY] and sent["model"] == "claude-haiku-5-5"
    assert sent["system"] == similar.SYSTEM
    [prompt] = [b["text"] for b in sent["messages"][0]["content"]]
    assert json.loads(prompt)["new_questions"][0]["question"] == RELOCATE
    text = "\n".join(x.getMessage() for x in caplog.records)
    for held in (KEY, "sk-ant", prompt, RELOCATE, RELOCATE_EARLIER, similar.SYSTEM[:40]):
        assert held not in text, held
    assert not [x for x in caplog.records if x.levelno >= logging.INFO and x.name == similar.__name__]
    assert KEY not in repr(letters.api_key()) and KEY not in json.dumps(Desk(srv).state()["settings"])
