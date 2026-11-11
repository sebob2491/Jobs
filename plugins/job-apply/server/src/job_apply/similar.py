"""Suggested answers: when a job stops with questions for the person, a model is asked one thing, whether a new
question means the same as one the person answered before. It is sent the questions' words and nothing else: never an
answer, the employer, the job, the person's name or anything from the profile. It never writes an answer: it says which
earlier question (if any) a new one repeats, and the answer offered is the person's own, from answers.yaml, shown on the
card and used only after the person's yes (Desk.answer's `use_similar`).

Only a question with choices is asked about, and an earlier answer is offered only when it is word for word one of
those choices (apart from case and spacing). Never a notice or consent, a security question, an ID, an answer about one
employer, or a question that points at the page's other words: the guards the silent path has (autofill._answer_bank),
and more. This module never loosens that path."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, letters
from .autofill import (
    _EMPLOYER_SPECIFIC,
    _POINTS_AWAY,
    _asks_an_id,
    _bare_question,
    _notice_of_consent,
    _this_employers_names,
    clean_label,
    norm,
)

log = logging.getLogger(__name__)

SIMILAR_TIMEOUT = 20.0  # seconds the one call may take (the card is there meanwhile, and as it is without it)
SIMILAR_MODEL = "claude-haiku-5-5"  # the profile's settings.similar_model overrides it
MOST_QUESTIONS = 12  # new questions in one call
MOST_CANDIDATES = 8  # earlier questions offered the model for one new question
MOST_CHOICES = 25  # a list longer than this isn't one to suggest for
MOST_DECLINED = 500  # declined pairs kept

SYSTEM = (
    "You compare the wording of form questions. You are given JSON: a list of new questions, each with its choices and "
    "a list of earlier questions. For each new question say whether one earlier question asks exactly the same thing "
    "in other words, so that anyone would give the same answer to both. Be strict: a different topic, time span, "
    "amount, place, person or condition is not the same question, and neither is the opposite (a question that asks "
    "whether you do and one that asks whether you do not). When unsure, say null. Reply with only a JSON object that "
    "maps each new question's number, as a string, to the number of one earlier question or to null. No other text.")

# a consent or notice, by the words the page's own test uses (desk.html's question()), besides autofill's
_CONSENT_WORDS = re.compile(r"agree|consent|certif|acknowledg|terms|attest|authori[sz]|privacy|signature|waiver|"
                            r"\bnotices?\b|disclosures?|\bpolic(?:y|ies)\b", re.I)
# "legally authorized to work" asks about the person, it agrees to nothing
_WORK_AUTHORIZATION = re.compile(r"\b(?:legally |lawfully )?(?:authori[sz]ed|permitted|eligible|allowed) to work\b|"
                                 r"\bwork authori[sz]ation\b", re.I)
_PLACEHOLDER = re.compile(r"^(?:select|choose|--|no selection$)", re.I)
_DATE_PART = re.compile(r"^(?:month|day|year)$", re.I)


def _security(text: str) -> bool:
    from .pipeline import _SECURITY_QUESTION  # (pipeline reads this module)

    return bool(_SECURITY_QUESTION.search(text))


def _consent(text: str) -> bool:
    """A consent, a notice, terms or an attestation: autofill's own test (_notice_of_consent) and the words the page's
    card uses, with a work authorization question (which only asks) left out of both."""
    plain = _WORK_AUTHORIZATION.sub(" ", text)
    return _notice_of_consent(plain) or bool(_CONSENT_WORDS.search(plain))


def _names(text: str, names: set[str]) -> bool:
    """Does `text` name any of these (norm()'s forms), as whole words?"""
    said = f" {norm(text)} "
    return any(f" {n} " in said for n in names)


def _fit(text: str) -> bool:
    """A question's words that may go to the model at all, on either side."""
    return not (_asks_an_id(text) or _security(text) or _consent(text) or _EMPLOYER_SPECIFIC.search(text)
                or _POINTS_AWAY.search(text))


def choices_of(q: dict[str, Any]) -> list[str]:
    """The choices the card shows for a question (desk.html's question() leaves out a list's placeholder)."""
    options = q.get("options")
    if not isinstance(options, list):
        return []
    out: list[str] = []
    for o in options:
        text = " ".join(str(o).split()) if isinstance(o, (str, int)) and not isinstance(o, bool) else ""
        if text and not _PLACEHOLDER.match(text) and text not in out:
            out.append(text)
    return out


def question_key(label: str) -> str:
    """A question's key, as the desk keeps its answers (pipeline.question_key)."""
    return norm(clean_label(label))


def _own(text: str) -> str:
    """A choice or an answer as compared: case and spacing apart."""
    return " ".join(str(text).split()).casefold()


def may_ask(q: dict[str, Any], company: str) -> bool:
    """May the model be asked whether this new question means the same as an earlier one? Only a question with
    choices; never one of the person's own details (an ID, a reference), an account form's, a consent, a notice, a
    security question, one about this employer, one that points at the page's other words, or one the card answers by
    its box (a label shared with another box, a date's part): its answer is never remembered by its words."""
    label = str(q.get("label") or "")
    if not label.strip() or " | " in label or not choices_of(q) or len(choices_of(q)) > MOST_CHOICES:
        return False
    if q.get("kind") == "checkbox_group":  # (several may be picked: one of them is not its answer)
        return False
    if any(q.get(k) for k in ("account", "about", "own_id", "id_unsure", "employer_id", "left_here")):
        return False
    if q.get("role") == "spinbutton" and _DATE_PART.match(str(q.get("sublabel") or "")):
        return False
    if not _fit(label):
        return False
    names = _this_employers_names({"company": company})
    return not (_names(label, names) or any(_names(c, names) for c in choices_of(q)))


@dataclass(frozen=True)
class Offer:
    """An earlier answer offered for a new question: the earlier question's words, the answer as the new question's own
    choice spells it, and the entry's pattern (answers.yaml's `match`)."""
    earlier: str
    answer: str
    match: str


# ------------------------------------------------------------------ declined pairs
DECLINED_LOCK = threading.RLock()


def _declined_path() -> Path:
    return config.home() / "similar.json"


def _declined() -> set[tuple[str, str]]:
    try:
        data = json.loads(_declined_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {(str(d["new"]), str(d["earlier"])) for d in data if isinstance(d, dict) and "new" in d
            and "earlier" in d} if isinstance(data, list) else set()


def decline(label: str, match: str) -> None:
    """The person said no to offering this earlier question (its pattern, `match`) for these words: never offered for
    them again. Raises OSError where it can't be kept."""
    pair = {"new": _bare_question(label), "earlier": match}
    with DECLINED_LOCK:
        try:
            data = json.loads(_declined_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = []
        kept = [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
        if pair not in kept:
            kept.append(pair)
        config.ensure_home()
        config.write_text_atomic(_declined_path(), json.dumps(kept[-MOST_DECLINED:], indent=1))


# ------------------------------------------------------------------ the earlier questions that may be offered
def candidates(q: dict[str, Any], company: str) -> list[dict[str, Any]]:
    """The person's remembered answers (answers.yaml) that may be offered for this new question: each with its
    question's words and a non-empty answer, never one that's an ID, kept for an employer (`at`), a notice or consent,
    a security question, about one employer, or already declined for these words; and only one whose answer is one
    of the new question's own choices, word for word apart from case and spacing. Each is {"question", "answer" (the
    choice as the new question spells it), "match"}, the likeliest first."""
    label = str(q.get("label") or "")
    choices = {_own(c): c for c in choices_of(q)}
    try:
        entries, at = config._remembered(config._answers_file())
    except (ValueError, OSError):
        return []
    names = _this_employers_names({"company": company})
    declined = _declined()
    here = _bare_question(label)
    words = set(norm(label).split())
    found: list[tuple[int, int, dict[str, Any]]] = []
    seen: set[str] = set()
    for rank, i in enumerate(at):
        e = entries[i]
        question, answer = str(e.get("question") or "").strip(), e.get("answer")
        if not question or not isinstance(answer, str) or not answer.strip() or _own(answer) not in choices:
            continue
        if str(e.get("at") or "").strip() or config._private(e):
            continue
        bare = _bare_question(question)
        if bare == here or bare in seen or (here, str(e["match"])) in declined:
            continue
        if not _fit(question) or _names(question, names):
            continue
        source = norm(str(e.get("from") or ""))
        if source and (source in norm(question) or source in norm(label)
                       or source.split()[0] in norm(answer).split()):
            continue  # (given for one employer, or naming it)
        seen.add(bare)
        found.append((-len(words & set(norm(question).split())), rank,
                      {"question": question, "answer": choices[_own(answer)], "match": str(e["match"])}))
    found.sort(key=lambda t: t[:2])
    return [c for _, _, c in found[:MOST_CANDIDATES]]


# ------------------------------------------------------------------ the one call
def _prompt(asked: list[tuple[str, dict[str, Any], list[dict[str, Any]]]]) -> tuple[str, dict[int, dict[int, dict]]]:
    """The message: JSON of the new questions (a number, the words, the choices) and each one's earlier questions (a
    number, the words), and nothing else. Also, for each new question's number, the earlier questions offered it, by
    their numbers."""
    listed: list[dict[str, Any]] = []
    offered: dict[int, dict[int, dict[str, Any]]] = {}
    n = 0
    for number, (_, q, found) in enumerate(asked, 1):
        earlier = []
        offered[number] = {}
        for c in found:
            n += 1
            earlier.append({"number": n, "question": c["question"]})
            offered[number][n] = c
        listed.append({"number": number, "question": " ".join(str(q.get("label") or "").split()),
                       "choices": choices_of(q), "earlier_questions": earlier})
    return json.dumps({"new_questions": listed}, ensure_ascii=False), offered


def _read(said: str, offered: dict[int, dict[int, dict]]) -> dict[int, int]:
    """The reply's matches, {new number: earlier number}: only a JSON object with just the new numbers as keys, each
    mapped to null or to one earlier question's number that was offered for it. Anything else for a question is no
    match; a reply that isn't such an object, or has another key, is none at all."""
    text = said.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S | re.I)
    try:
        data = json.loads(fenced.group(1) if fenced else text)
    except ValueError:
        return {}
    if not isinstance(data, dict) or {str(k) for k in data} - {str(n) for n in offered}:
        return {}
    out: dict[int, int] = {}
    for key, value in data.items():
        number = int(key) if str(key).isdigit() else 0
        if type(value) is int and number in offered and value in offered[number]:
            out[number] = value
    return out


async def suggest(questions: list[dict[str, Any]], company: str) -> dict[str, Offer]:
    """The earlier answers to offer for a stop's new questions, by each question's key (question_key): one call to a
    model, with the questions' words alone. {} where there's nothing to ask, no model, or the call fails, takes too long
    (SIMILAR_TIMEOUT) or answers anything but what it was asked for: the card is then as it was."""
    try:
        keys = [question_key(str(q.get("label") or "")) for q in questions]
        asked = [(k, q, found) for k, q in zip(keys, questions)
                 if keys.count(k) == 1 and may_ask(q, company) and (found := candidates(q, company))]
        asked = asked[:MOST_QUESTIONS]
        if not asked:
            return {}
        exe = letters.api_key() or letters.find_claude()
        if exe is None:
            return {}
        try:
            model = config.Profile.load().settings.similar_model or SIMILAR_MODEL
        except Exception:
            model = SIMILAR_MODEL
        message, offered = _prompt(asked)
        said = await asyncio.wait_for(
            letters._ask(exe, SYSTEM, [{"type": "text", "text": message}], model, SIMILAR_TIMEOUT), SIMILAR_TIMEOUT)
        matches = _read(said, offered)
        return {asked[number - 1][0]: Offer(c["question"], c["answer"], c["match"])
                for number, which in matches.items() for c in [offered[number][which]]}
    except asyncio.CancelledError:
        raise
    except Exception as e:  # (a timeout, no connection, a refusal, a reply that can't be read: no offer; no word of it)
        log.debug("suggested answers: none this time (%s)", type(e).__name__)
        return {}
