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
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, letters
from .report import report_strings
from .autofill import (
    _EEO_RULES,
    _EMPLOYER_SPECIFIC,
    _POINTS_AWAY,
    RULES,
    _asks_an_id,
    _bare_question,
    _employer_key,
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
MOST_KEPT = 200  # a run's kept results (Applier's, by card question: a re-pause of the same card asks nothing again)

SYSTEM = (
    "You compare the wording of form questions. You are given JSON: a list of new questions, each with its choices and "
    "a list of earlier questions. For each new question say whether one earlier question asks exactly the same thing "
    "in other words, so that anyone would give the same answer to both. Be strict: a different topic, time span, "
    "amount, place, person or condition is not the same question, and neither is the opposite (a question that asks "
    "whether you do and one that asks whether you do not). When unsure, say null. Reply with only a JSON object that "
    "maps each new question's number, as a string, to the number of one earlier question or to null. No other text.")

# a consent or notice, by the words the page's own test uses (desk.html's question()), besides autofill's; and what
# reads as one: an understanding, an e-signature, a release or a waiver
_CONSENT_WORDS = re.compile(r"agree|consent|certif|acknowledg|terms|attest|authori[sz]|privacy|signature|waiver|"
                            r"\bnotices?\b|disclosures?|\bpolic(?:y|ies)\b|understand|\be-?sign|\breleas\w*|\bwaiv\w*",
                            re.I)
# "legally authorized to work" asks about the person, it agrees to nothing
_WORK_AUTHORIZATION = re.compile(r"\b(?:legally |lawfully )?(?:authori[sz]ed|permitted|eligible|allowed) to work\b|"
                                 r"\bwork authori[sz]ation\b", re.I)
_PLACEHOLDER = re.compile(r"^(?:select|choose|--|no selection$)", re.I)
_DATE_PART = re.compile(r"^(?:month|day|year)$", re.I)
# The kinds no answer of which ever travels, on either side:
# - self-identification: autofill's own EEO rules (gender, hispanic, race, veteran, disability, sexual orientation)
#   and its pronouns rule, and besides them ethnicity, military status, age and date of birth;
_SELF_ID = re.compile("|".join(f"(?:{pattern})" for name, pattern, *_ in RULES if name in _EEO_RULES | {"pronouns"})
                      + r"|\bethnic|\bdisabilit|\bmilitary status\b|\bage\b|\bdate of birth\b|\bbirth ?date\b|"
                        r"\bdob\b|\bhow old\b|\bpronoun|\bbirthday|\b(?:over|under) (?:the age of )?\d+\b|"
                        r"\btransgender|\breligio|\bfaith\b|\bmarital|\bmarried\b|\bspouse|\bnational origin\b|"
                        r"\bnationalit|\bmedical|\bpregnan|\bhealth\b|\bphysical or mental\b|\bchurch|\bworship|"
                        r"\bsabbath|\bare you single\b|\bsingle or married\b|\bdomestic partner|\bchildren\b|"
                        r"\bexpecting a\b|\bborn\b|\b(?:native|first) language\b|\bmaiden name\b", re.I)
# - pay;
_PAY = re.compile(r"\bsalar|\bcompensation\b|\bpay ?(rate|range|expectations?|requirements?)\b|\bwages?\b|"
                  r"\b(expected|desired|minimum|target|hourly) (pay|rate|salary|wage)|\bhourly rate\b", re.I)
# - a criminal record and the checks of one, a background check or a drug test
_RECORD = re.compile(r"\bfelon|\bconvict|\bcrimes?\b|\bcriminal\b|\bmisdemeanou?r|\barrest|\boffen[cs]es?\b|"
                     r"\bcharged\b|\bprobation\b|\bparole\b|\bpending (?:case|charge)|\bguilty\b|\bplea\b|"
                     r"\bincarcerat|\bjail|\bprison|\bno contest\b|\bnolo\b|\bDUI\b|\bDWI\b|\bserved time\b|"
                     r"\bexpunge|\badjudicat|\bcited\b|\bcitations?\b|"
                     r"\bbackground (check|screen|investigation)|\bdrug (test|screen)", re.I)
# (the profile's values that are no one's own: a choice every form has)
_EVERYDAY = {"yes", "no", "true", "false", "none", "n a", "na", "other", "mobile", "home", "work", "cell"}
# Names that are everyday words too: one of these as the person's refuses a question only where it stands as a name
# (capitalized, and not as a sentence's first word: "Will you relocate?" names nobody, "Thanks, Will" does). The cost
# of the list being short is a question not suggested for; of it being long, a name at a sentence's start sent
_WORD_NAMES = frozenset(
    "will long may mark grant bill rose art young white brown green black king price best day case love hope joy "
    "grace april june august summer dawn guy frank rich miles page penny chase drew gene herb jack lance norm pat "
    "rob sue wade bob don ray dean earl faith ivy lily iris amber crystal ruby pearl olive holly heather daisy "
    "autumn winter angel christian major sterling cash colt ford reed stone woods hill field banks rivers brooks "
    "lane park west north south east church bishop baker cook carpenter mason miller smith taylor carter fisher "
    "hunter porter shepherd walker weaver wright knight noble wise sharp strong small little rich power".split())


def _apart(text: str) -> str:
    """Words run together shown apart: "Will_Long-Resume.pdf" and "WillLong" as "Will Long Resume pdf" and
    "Will Long" (a file's name in a list of resumes holds the person's)."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=\w)[_\-.]+(?=\w)", " ", str(text or ""))


def sensitive(text: str) -> bool:
    """A self-identification, pay, or a criminal record or background check question: never sent, never offered."""
    return bool(_SELF_ID.search(text) or _PAY.search(text) or _RECORD.search(text))


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
                or _POINTS_AWAY.search(text) or sensitive(text))


class Personal:
    """The person's details and the job's, as a question's words may hold them: every value of the profile a report
    takes out (report.report_strings: names, contact, address, references, IDs, schools, employers), the rest of its
    personal: section, the titles the person held, and the job's title. Each of 3 characters or more, found as whole
    words in any case; one of figures alone (a phone, a postal code, an ID) found with its separators taken out, so
    "(555) 010-1234" holds "5550101234"."""

    def __init__(self, values: Iterable[str]):
        words: set[str] = set()
        names: set[str] = set()
        self.figures: set[str] = set()
        for value in values:
            said = " ".join(str(value).split())
            if len(said) < 3 or norm(said) in _EVERYDAY:
                continue
            if re.fullmatch(r"[\d\s().+\-/]+", said):
                digits = re.sub(r"\D", "", said)
                if len(digits) >= 3:
                    self.figures.add(digits)
                continue
            (names if said.casefold() in _WORD_NAMES else words).add(said.casefold())
        listed = sorted(words, key=len, reverse=True)
        self.words = re.compile("(?<!\\w)(?:" + "|".join(r"\s+".join(re.escape(w) for w in v.split()) for v in listed)
                                + ")(?!\\w)", re.I) if listed else None
        # (as a name: its first letter a capital, the rest as written or all capitals)
        self.names = re.compile("(?<!\\w)(?:" + "|".join(
            f"{re.escape(n.capitalize())}|{re.escape(n.upper())}" for n in sorted(names))
            + ")(?!\\w)") if names else None

    @classmethod
    def of(cls, prof: config.Profile | None, title: str = "") -> Personal:
        values: list[str] = []
        titles: list[str] = [title]
        if prof is not None:
            values += report_strings(prof)
            values += [v for v in _strings(prof.get("personal"), ("country", "phone_country_code", "phone_type"))]
            titles += [str(prof.get("experience.current_title") or "")]
            titles += [str(e.get("title") or "") for e in prof.get("work_history") or [] if isinstance(e, dict)]
            for ref in prof.references():
                values += [str(v) for v in ref.values() if v]
        # (a title of one word, "Technician", is the trade's word, in half the questions a technician is asked: only
        # a title of two words or more is the person's or the job's own)
        values += [t for t in titles if len(t.split()) > 1]
        return cls(v for v in values if v)

    def holds(self, text: str) -> bool:
        raw = str(text or "")
        apart = _apart(raw)
        # (as written and with its run-together words apart: "McKenzie" and "PayPal" are whole only as written,
        # "Will_Long.pdf" only apart)
        spaced = re.sub(r"(?<=\w)[_\-.]+(?=\w)", " ", raw)  # (its joins alone taken out: "McKenzie_Resume.pdf")
        if self.words is not None and any(self.words.search(t) for t in (raw, spaced, apart)):
            return True
        if self.names is not None:
            # (a file's name, or words run together, "will_resume.pdf", "WillResume": a name there in any letters;
            # not an everyday compound, "long-term")
            for token in re.findall(r"\w+(?:[_\-.]+\w+)*", raw):
                if ("_" in token or re.search(r"\.\w{2,4}$", token) or re.search(r"[a-z][A-Z]", token)) and re.search(
                        self.names.pattern, _apart(token), re.I):
                    return True
            for m in self.names.finditer(raw):
                before, after = raw[:m.start()].rstrip(), raw[m.end():].lstrip()
                if (before and before[-1] not in ".?!:;") or not after or after[0] in ",(:" or re.match(
                        "['\u2019]s\\b", after):
                    return True  # (not a sentence's first word; or all there is; or spoken to, or its owner)
        if self.figures:
            for run in re.findall(r"\d[\d\s().+\-/]*\d|\d", raw):
                digits = re.sub(r"\D", "", run)
                if any(f == digits or len(f) >= 7 and f in digits for f in self.figures):
                    return True
        return False

def _strings(value: Any, leave: tuple[str, ...] = ()) -> list[str]:
    """Every text in a profile section, however deep (but the keys in `leave`)."""
    if isinstance(value, dict):
        return [s for k, v in value.items() if str(k) not in leave for s in _strings(v, leave)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v, leave)]
    return [value.strip()] if isinstance(value, str) and value.strip() else []


def _profile() -> config.Profile | None:
    try:
        return config.Profile.load()
    except Exception:  # (a profile that doesn't read: its details unknown, so nothing is sent: see _prepare)
        return None


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


def may_ask(q: dict[str, Any], company: str, personal: Personal | None = None) -> bool:
    """May the model be asked whether this new question means the same as an earlier one? Only a question with
    choices; never one of the person's own details (an ID, a reference), an account form's, a consent, a notice, a
    security question, a self-identification, pay or a criminal record, one about this employer, one whose words or
    choices hold the person's details or the job's (`personal`: the profile's, read here when not given), one that
    points at the page's other words, or one the card answers by its box (a label shared with another box, a date's
    part): its answer is never remembered by its words."""
    label = str(q.get("label") or "")
    choices = choices_of(q)
    if not label.strip() or " | " in label or not choices or len(choices) > MOST_CHOICES:
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
    if _names(label, names) or any(_names(c, names) for c in choices):
        return False
    personal = personal if personal is not None else Personal.of(_profile())
    return not (personal.holds(label) or any(personal.holds(c) for c in choices))


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
@dataclass(frozen=True)
class _Earlier:
    """A remembered answer that may be offered for some new question (_earlier's checks passed), its rank its place in
    answers.yaml (newest first)."""
    rank: int
    question: str
    answer: str
    match: str
    bare: str
    words: frozenset[str]
    source: frozenset[str]  # (the names of the employer it was given at: its `from`)


def _employers_named(employers: Iterable[str]) -> set[str]:
    """The tracker's employers' names, whole (norm()'s and _employer_key's forms: never a first word or initials, which
    a question's everyday words share), for an answer kept with no employer of its own."""
    out: set[str] = set()
    for name in employers:
        out |= {norm(str(name)), _employer_key(str(name))}
    return {n for n in out if len(n) >= 3}


def _earlier(entries: list[Any], at: list[int], personal: Personal, employers: set[str]) -> list[_Earlier]:
    """The remembered answers that may be offered for any new question: each with its question's words and a
    non-empty answer, never one that's an ID, kept for an employer (`at`), a notice or consent, a security question, a
    self-identification, pay or a criminal record, about one employer (the one it was given at, `from`, or any the
    tracker names), or whose question or answer holds the person's details or the job's."""
    out: list[_Earlier] = []
    for rank, i in enumerate(at):
        e = entries[i]
        question, answer = str(e.get("question") or "").strip(), e.get("answer")
        if not question or not isinstance(answer, str) or not answer.strip():
            continue
        if str(e.get("at") or "").strip() or config._private(e):
            continue
        if not _fit(question) or personal.holds(question) or personal.holds(answer):
            continue
        source = str(e.get("from") or "").strip()
        named = _this_employers_names({"company": source}) if source else set()
        if named and (_names(question, named) or _names(answer, named)):
            continue  # (given at one employer and naming it: "relocate to the Example Fab Chandler site?")
        if employers and _names(question, employers):
            continue
        out.append(_Earlier(rank, question, answer, str(e["match"]), _bare_question(question),
                            frozenset(norm(question).split()), frozenset(named)))
    return out


def _candidates(q: dict[str, Any], company: str, earlier: list[_Earlier],
                declined: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """Of `earlier`, those for this new question: only one whose answer is one of its own choices, word for word apart
    from case and spacing, not its own words again, not naming this employer, and not declined for these words."""
    label = str(q.get("label") or "")
    choices = {_own(c): c for c in choices_of(q)}
    names = _this_employers_names({"company": company})
    here = _bare_question(label)
    words = set(norm(label).split())
    found: list[tuple[int, int, dict[str, Any]]] = []
    seen: set[str] = set()
    for e in earlier:
        if _own(e.answer) not in choices or e.bare == here or e.bare in seen or (here, e.match) in declined:
            continue
        if _names(e.question, names) or e.source and _names(label, set(e.source)):
            continue
        seen.add(e.bare)
        found.append((-len(words & e.words), e.rank,
                      {"question": e.question, "answer": choices[_own(e.answer)], "match": e.match}))
    found.sort(key=lambda t: t[:2])
    return [c for _, _, c in found[:MOST_CANDIDATES]]


def candidates(q: dict[str, Any], company: str, employers: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The person's remembered answers (answers.yaml) that may be offered for this new question (_earlier's and
    _candidates' checks), each {"question", "answer" (the choice as the new question spells it), "match"}, the
    likeliest first. (One question's: a stop's are prepared together, each file read once: _prepare.)"""
    try:
        entries, at = config._remembered(config._answers_file())
    except (ValueError, OSError):
        return []
    prof = _profile()
    if prof is None:
        return []
    earlier = _earlier(entries, at, Personal.of(prof), _employers_named(employers))
    return _candidates(q, company, earlier, _declined())


Asked = list[tuple[str, dict[str, Any], list[dict[str, Any]]]]


def _prepare(questions: list[dict[str, Any]], company: str, title: str,
             employers: Callable[[], Iterable[str]] | None) -> tuple[Asked, Any, str]:
    """A stop's questions to ask about, each with its candidates; and what to ask with and which model. Off the event
    loop (suggest runs it in a thread): the profile, answers.yaml and similar.json each read once, the questions
    may_ask refuses dropped first and the rest cut to MOST_QUESTIONS before any is matched."""
    prof = _profile()
    if prof is None:  # (the person's details unknown: nothing could be checked against them, so nothing goes)
        return [], None, SIMILAR_MODEL
    model = prof.settings.similar_model or SIMILAR_MODEL
    personal = Personal.of(prof, title)
    keys = [question_key(str(q.get("label") or "")) for q in questions]
    twice = Counter(keys)
    askable = [(k, q) for k, q in zip(keys, questions) if twice[k] == 1 and may_ask(q, company, personal)]
    askable = askable[:MOST_QUESTIONS]
    if not askable:
        return [], None, model
    try:
        entries, at = config._remembered(config._answers_file())
    except (ValueError, OSError):
        return [], None, model
    try:
        known = list(employers()) if employers is not None else []
    except Exception:
        known = []
    earlier = _earlier(entries, at, personal, _employers_named(known))
    declined = _declined()
    asked = [(k, q, found) for k, q in askable if (found := _candidates(q, company, earlier, declined))]
    if not asked:
        return [], None, model
    return asked, letters.api_key() or letters.find_claude(), model


# ------------------------------------------------------------------ the one call
def _prompt(asked: Asked) -> tuple[str, dict[int, dict[int, dict]]]:
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


def _read(said: str, offered: dict[int, dict[int, dict]]) -> dict[int, int] | None:
    """The reply's matches, {new number: earlier number}: only a JSON object with just the new numbers as keys, each
    mapped to null or to one earlier question's number that was offered for it. Anything else for a question is no
    match; a reply that isn't such an object, or has another key, is none at all (None: nothing of it is kept)."""
    text = said.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S | re.I)
    try:
        data = json.loads(fenced.group(1) if fenced else text)
    except ValueError:
        return None
    if not isinstance(data, dict) or {str(k) for k in data} - {str(n) for n in offered}:
        return None
    out: dict[int, int] = {}
    for key, value in data.items():
        number = int(key) if str(key).isdigit() else 0
        if type(value) is int and number in offered and value in offered[number]:
            out[number] = value
    return out


def _kept_key(q: dict[str, Any], found: list[dict[str, Any]]) -> tuple:
    """A card question as asked: its words, its choices and its candidates (a decline or a new answer changes it)."""
    return (" ".join(str(q.get("label") or "").split()), tuple(choices_of(q)),
            tuple((c["question"], c["answer"], c["match"]) for c in found))


async def suggest(questions: list[dict[str, Any]], company: str, title: str = "",
                  employers: Callable[[], Iterable[str]] | None = None,
                  kept: dict[tuple, Offer | None] | None = None) -> dict[str, Offer]:
    """The earlier answers to offer for a stop's new questions, by each question's key (question_key): one call to a
    model, with the questions' words alone. {} where there's nothing to ask, no model, or the call fails, takes too long
    (SIMILAR_TIMEOUT) or answers anything but what it was asked for: the card is then as it was. `title`: the job's,
    never sent (a question holding it isn't asked about); `employers`: the tracker's, read with the rest off the loop;
    `kept`: the run's results so far, by _kept_key (a question asked before just as it is now isn't asked again)."""
    try:
        asked, exe, model = await asyncio.to_thread(_prepare, questions, company, title, employers)
        if not asked:
            return {}
        out: dict[str, Offer] = {}
        fresh = []
        for item in asked:
            key = _kept_key(item[1], item[2])
            if kept is not None and key in kept:
                if (offer := kept[key]) is not None:
                    out[item[0]] = offer
            else:
                fresh.append(item)
        if not fresh:
            return out
        if exe is None:
            return {}
        message, offered = _prompt(fresh)
        said = await asyncio.wait_for(letters._ask(exe, SYSTEM, [{"type": "text", "text": message}], model,
                                                   SIMILAR_TIMEOUT, effort=False), SIMILAR_TIMEOUT)
        matches = _read(said, offered)
        if matches is None:  # (not an answer to what was asked: nothing offered for these, nothing kept)
            return out
        for number, (qkey, q, found) in enumerate(fresh, 1):
            which = matches.get(number)
            offer = Offer(offered[number][which]["question"], offered[number][which]["answer"],
                          offered[number][which]["match"]) if which is not None else None
            if offer is not None:
                out[qkey] = offer
            if kept is not None:
                if len(kept) >= MOST_KEPT:
                    kept.clear()
                kept[_kept_key(q, found)] = offer
        return out
    except asyncio.CancelledError:
        raise
    except Exception as e:  # (a timeout, no connection, a refusal, a reply that can't be read: no offer; no word of it)
        log.debug("suggested answers: none this time (%s)", type(e).__name__)
        return {}
