"""The never-list (never.py): every path that agrees, ticks or presses for the person reads one list,
_never_for_them, so none of them misses a kind another refuses (a review's structure list, Oct 18: each path
kept a list of its own, and they drifted apart)."""

import re

import pytest

from job_apply import config, formjs, pipeline
from job_apply.autofill import _answer_bank
from job_apply.never import ANYWHERE, IN_PASSING, NEVER, _never_for_them

# One sentence for each kind never agreed to for the person, worded as the tests of each path word theirs, and
# without words that agree (each path's own words do that)
SAID = {
    "arbitration": "settle any dispute through binding arbitration",
    "waiver": "waive the right to a trial by jury",
    "at-will": "understand that employment with Example Corp is at-will",
    "signs up": "join our Talent Community",
    "newsletter": "get the monthly newsletter",
    "job alerts": "get job alerts for roles like this one",
    "talent community": "have a place in the Talent Network",
    "marketing": "get marketing and promotional offers",
    "emails or updates": "receive email communications from Example Corp",
    "texts or calls": "let Example Corp call or text me",
    "being contacted": "be contacted by recruiters",
    "other roles": "be considered for other open roles",
    "their data shared": "let Example Corp share my profile with staffing agencies",
    "their data kept": "let Example Corp keep my resume on file",
    "cookies": "allow the use of cookies",
    "more than the terms": "let a third party process my application",
}
# (a sign-up in so many words names what it signs up for; receiving emails is a box's word too)
ALSO = {"signs up": {"talent community"}, "emails or updates": {"more than the terms"}}
EVERY = frozenset(NEVER)


def test_each_sentence_is_its_own_kind_and_every_kind_has_one():
    """A kind added to the list gets a sentence here, and every path below is then held to refuse it."""
    assert set(SAID) == set(NEVER)
    for kind, said in SAID.items():
        assert {k for k in NEVER if _never_for_them(said, only={k})} == {kind} | ALSO.get(kind, set()), kind
        assert _never_for_them(said) is not None


def _and(words, clause):
    return words if clause is None else f"{words}, and {clause}"


def _by(clause):
    return "" if clause is None else f" By accepting you {clause}."


CERTIFY = "I certify that the information in this application is true and complete"
PRIVACY = "I have read and agree to the Privacy Notice and Terms of Use"
# BrassRing's consent (Edward Jones, run 83): a lone "I agree"
CONSENT = ("By clicking the 'I agree' button below, you are acknowledging you have received and had the opportunity to "
           "review the above Privacy Notice")
TERMS_BOX = "I agree to the Terms of Use and Privacy Policy"
ABOVE = "Please review and accept our Terms of Use and Candidate Privacy Policy"
OTHER_POSITIONS = "I understand that I may be considered for other open positions in my country"  # Northrop's
STATEMENT_LINK = "Read and accept the data privacy statement"  # SuccessFactors'
POLICY = "Privacy Policy of Example Corp"
POLICY_TEXT = "Example Corp protects the personal data you give it, as this policy describes. "
AI_HEADING = "Notice Related to Example Corp's Use of the Eightfold AI Recruiting Software"
AI_TEXT = ('Example Corp uses an artificial intelligence ("AI") recruiting software provided by Eightfold AI to assist '
           "with and support its recruiting efforts. Before submitting your application, please review this notice.")
STATEMENT = "Example Corp Consent Statement"  # (the live check's test identity's: Freeport-McMoRan's, run 56)
STATEMENT_TEXT = "Example Corp Data Privacy Statement: how Example Corp uses the details you give it."
REMEMBERED = ("Relocation: {} hires for its sites across the country, and many of its openings ask for a move{}. Are you "
              "willing to relocate?")


def _remembered(clause):
    """A Yes remembered at Acme for a question in a notice, given at Example Fab for the same question in its own."""
    config.save_answer(REMEMBERED.format("Acme", ""), "Yes", "Acme")
    label = REMEMBERED.format("Example Fab", "" if clause is None else f", and applicants may {clause}")
    answer = _answer_bank(config.Profile.load(), label, {"company": "Example Fab"})
    return answer is not None and answer.value == "Yes"


def _box(label, **more):
    return {"fields": [{"id": "t", "kind": "checkbox", "label": label, "value": False, "required": True, **more}]}


# Each path's own entry point, given its own words with a sentence of one kind in them (None: its own words alone),
# says whether the desk would agree, tick or press for the person; and the kinds it refuses. A kind left out is
# that path's own exception, said where it reads (pipeline.py, autofill.py)
PATHS = {
    # an attestation (settings.accept_notices); an at-will sentence may stand beside the application's own
    # information certified true (the second review of 0.3.170)
    "attestation: a certification's box": (
        lambda c: pipeline._attestation({"kind": "checkbox", "label": _and(CERTIFY, c), "required": True}) is not None,
        EVERY - IN_PASSING - {"at-will"}),
    "attestation: a privacy agreement's box": (
        lambda c: pipeline._attestation({"kind": "checkbox", "label": _and(PRIVACY, c), "required": True}) is not None,
        EVERY - IN_PASSING),
    "attestation: a lone choice": (
        lambda c: pipeline._attestation({"kind": "radio_group", "label": CONSENT, "required": True,
                                         "options": [_and("I agree", c)]}) is not None,
        EVERY),
    "attestation: a group's tick": (
        lambda c: pipeline._attestation({"kind": "checkbox_group", "label": "Consents", "required": True,
                                         "options": [_and("I accept the privacy notice", c)]}) is not None,
        EVERY - IN_PASSING),
    # a Create Account's own boxes (settings.manage_accounts): every kind
    "a Create Account's terms box": (lambda c: bool(pipeline._terms_boxes(_box(_and(TERMS_BOX, c)))), EVERY),
    "an email-first site's agreement box": (lambda c: bool(pipeline._consent_boxes(_box(_and(TERMS_BOX, c)))), EVERY),
    "the words above a bare I agree": (lambda c: pipeline._terms_above(_and(ABOVE, c) + "."), EVERY - IN_PASSING),
    # (its own words, other roles, are the owner's call, Oct 10)
    "a consent to other positions": (
        lambda c: pipeline._other_positions_consent({"kind": "checkbox", "label": _and(OTHER_POSITIONS, c)}) is not None,
        EVERY - {"other roles"}),
    "a Create Account's privacy statement link": (
        lambda c: pipeline._privacy_statement({"actions": [{"id": "a", "text": _and(STATEMENT_LINK, c) + ".",
                                                            "in_account_form": True}]}) is not None,
        EVERY - IN_PASSING),
    "a terms gate, by its headings": (
        lambda c: pipeline._terms_gate({"text": "I Accept"},
                                       {"headings": ["Privacy Agreement" + ("" if c is None else f": you {c}")]}),
        EVERY - IN_PASSING),
    # (a framed gate's page has a sign-up of its own beside it: Schwab's, live, Oct 10)
    "a terms gate, by its page's words": (
        lambda c: pipeline._terms_gate({"text": "I Accept"}, {"headings": ["Privacy Agreement"]},
                                       "Please read our privacy agreement." + _by(c)),
        frozenset({"arbitration", "waiver"})),
    "a privacy notice, by its opening": (
        lambda c: pipeline._terms_notice(POLICY, (_by(c) + " " + POLICY_TEXT).strip())[0], EVERY - IN_PASSING),
    # (its long text names its affiliates, marketing and cookies as its practices: APS's, live)
    "a privacy notice, past its opening": (
        lambda c: pipeline._terms_notice(POLICY, POLICY_TEXT * 5 + _by(c))[0], ANYWHERE),
    "a notice not named for its terms": (
        lambda c: pipeline._terms_notice("Before you apply", "Please read and accept our privacy notice." + _by(c))[0],
        EVERY - IN_PASSING),
    "an AI notice, by its heading": (
        lambda c: pipeline._agreeable_notice(AI_HEADING + ("" if c is None else f": you {c}"), AI_TEXT, False)[0],
        EVERY - IN_PASSING),
    # (its words name the other positions it may suggest: Micron's, live)
    "an AI notice, by its words": (lambda c: pipeline._agreeable_notice(AI_HEADING, AI_TEXT + _by(c), False)[0],
                                   ANYWHERE),
    # the live check's test identity: a legal notice, the data it's given and other roles are its own to agree to (the
    # owner's call, Oct 13), but the notice's words are read for a waiver too (the review of Oct 15)
    "the test identity's notice": (
        lambda c: pipeline._agreeable_notice(STATEMENT, f"{_by(c).strip()} {STATEMENT_TEXT}".strip(), True)[0],
        EVERY - IN_PASSING - {"at-will", "other roles", "their data shared", "their data kept"}),
    "a remembered answer at another employer": (_remembered, EVERY - IN_PASSING),
    # (a cookie banner's is its own rule's; what else a page's agreement button says is _terms_gate's)
    "a page's agreement button": (
        lambda c: pipeline._agreement_button({"actions": [{"id": "a", "text": _and("I accept", c)}]}) is not None,
        frozenset({"cookies"})),
}


@pytest.mark.parametrize("path", list(PATHS))
def test_each_path_agrees_to_its_own_words(path):
    agreed, _ = PATHS[path]
    assert agreed(None), path


@pytest.mark.parametrize("kind", list(SAID))
@pytest.mark.parametrize("path", list(PATHS))
def test_each_path_refuses_every_kind_never_agreed_to_but_its_own_exceptions(path, kind):
    """For each kind never agreed to for the person, a sentence of it refused by each path's own entry point (and
    one a path leaves out by its own exception, said where it reads, still agreed: those are the only ones)."""
    agreed, refuses = PATHS[path]
    assert agreed(SAID[kind]) is (kind not in refuses), (path, kind, SAID[kind])


def _alternatives(pattern):
    """A regex's top-level alternatives."""
    out, depth, start, i, in_class = [], 0, 0, 0, False
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            out.append(pattern[start:i])
            start = i + 1
        i += 1
    return [*out, pattern[start:]]


# Each path's own list before the one list (origin/main at 0.3.207), word for word: what they refused, each word
# still refused
CALLS = (r"\btexting\b|\btext (?:messag\w*|me|you)\b|\bcall(?:s|ing)? (?:or|and|&) text\w*\b|autodial\w*|"
         r"\bautomated (?:calls?|calling|dial\w*|messag\w*|texts?)\b|pre-?recorded|\bwhatsapp\b")
NOT_TERMS = (r"newsletter|mailing list|marketing|job alerts?|text messages?|\bsms\b|promotion|offers|subscribe|similar "
             r"jobs|talent (?:community|network)|keep me|stay informed|send me|contact(?:ed)? me|be contacted|share my|"
             r"other (?:\w+ )?(?:roles|positions|jobs|openings|opportunities)|affiliat|" + CALLS)
OLD_LISTS = {
    "pipeline._CALLS_OR_TEXTS": CALLS,
    "pipeline._NOT_TERMS": NOT_TERMS,
    "pipeline._MORE_THAN_TERMS": r"receiv\w*|e-?mails?|updates?|notifications?|messages?|partners?|third[- ]part|future|"
                                 r"talent pool|retain|retention",
    "pipeline._SIGNS_UP_TOO": r"\breceiv\w*\b.{0,40}\b(?:e-?mails?|communications?|updates?|messages?|news\w*|offers|"
                              r"information about|texts?)\b|\b(?:e-?mail|marketing|promotional) communications?\b|"
                              r"\bupdates? (?:about|on|from)\b|\bnewsletter",
    "pipeline._SIGN_UP_ABOVE": r"talent (?:community|network|pool)|job alerts?|newsletter|subscribe|marketing|"
                               r"mailing list|\bnotif(?:y|ied|ications?)\b|\bopt(?:ing)?[- ]in\b|\be-?mail (?:me|you)\b|"
                               r"text messages?|\bsms\b|keep me|stay informed|similar jobs|promotion\w*|offers|send me|"
                               r"contact(?:ed)? me|be contacted|share my|other (?:\w+ )?(?:roles|positions|jobs|openings|"
                               r"opportunities)|\breceiv\w*\b.{0,40}\b(?:news\w*|offers|updates?|alerts?|promotions?|"
                               r"(?:job|career) opportunities|marketing)\b|talent database|future (?:\w+ )?vacanc|"
                               r"keep\w* (?:my|your) (?:personal )?data|share (?:my|your)|partners|staffing|"
                               r"recruit\w* agenc|careers? (?:community|network)|\balerts?\b|\btexts?\b|\bcalls?\b",
    "pipeline._NOT_ATTESTED": r"newsletter|marketing|job alerts?|text messages?|\bsms\b|promotion|subscribe|"
                              r"talent (?:community|network)|" + CALLS,
    "pipeline._WAIVES": r"arbitrat|\bwaive|\bjury\b|non-?compete|non-?solicit",
    "pipeline._SIGNS_UP": r"\b(?:join\w*|subscrib\w*|sign(?:s|ed|ing)? (?:you )?up|receiv\w*|enrol\w*|added to|"
                          r"opt(?:s|ed)? (?:you )?in)\b.{0,60}?\b(?:talent (?:community|network|pool)|newsletters?|"
                          r"job alerts?|marketing|promotion\w*)",
    "pipeline._NEVER_AGREED": NOT_TERMS + r"|talent pool|future (?:job |career )?(?:opportunities|openings|roles|"
                              r"positions)|keep (?:my|your) (?:profile|information|data|resume)|retain (?:my|your)|"
                              r"campaigns?|cookie|arbitration|waive|jury|non-?compete|non-?solicit",
    "pipeline._EMPLOYMENT_TERMS": r"\bat[- ]will\b|\b(?:terms|conditions)\b[^.]{0,30}\bof (?:my |your |this |the )?"
                                  r"employment\b|\bemployment (?:agreement|contract)\b",
    "pipeline._agreement_button's": r"cookie",
    # (its words of a notice of consent, terms or an attestation are still its own)
    "autofill._NOTICE_OF_CONSENT's never-words": r"talent (?:community|network|pool)|job alerts?|newsletter|subscribe|"
                                                 r"marketing|mailing list|text messages?|\bsms\b|promotion|arbitration",
}
# and the lists that tell a sign-up's page or box from an account's or the application's, still their own: each of
# their words is the one list's too
LIVE_LISTS = {
    "pipeline._NOT_TERMS": pipeline._NOT_TERMS.pattern,
    "pipeline._SIDE_BOX": pipeline._SIDE_BOX.pattern,
    "formjs SIDE_BOX": re.search(r"const SIDE_BOX = /(.+?)/i;", formjs.EXTRACT_JS)[1],
    "formjs SIGN_UP": re.search(r"const SIGN_UP = /(.+?)/i;", formjs.EXTRACT_JS)[1],
}
# The words they hold, each on its own
WORDS = [
    "texting", "text messages", "text me", "text you", "calls or texts", "calling and texting", "call & text",
    "autodialed", "automated calls", "automated calling", "automated dialing", "automated messages", "automated texts",
    "pre-recorded", "prerecorded", "WhatsApp",
    "newsletter", "mailing list", "marketing", "job alert", "job alerts", "text message", "SMS", "promotion", "offers",
    "subscribe", "similar jobs", "talent community", "talent network", "keep me", "stay informed", "send me",
    "contact me", "contacted me", "be contacted", "share my", "other roles", "other open positions", "other jobs",
    "other openings", "other opportunities", "affiliates",
    "receive", "received", "email", "e-mails", "update", "updates", "notification", "notifications", "message",
    "messages", "partner", "partners", "third party", "third-party", "future", "talent pool", "retain", "retention",
    "receive emails", "receive communications", "receive updates", "receive messages", "receive news",
    "receive offers", "receive information about", "receive texts", "email communications",
    "marketing communications", "promotional communications", "updates about", "updates on", "updates from",
    "notify", "notified", "opt in", "opt-in", "opting in", "email me", "e-mail you", "promotions", "receive alerts",
    "receive promotions", "receive job opportunities", "receive career opportunities", "receive marketing",
    "talent database", "future vacancies", "future job vacancies", "keep my data", "keeping your personal data",
    "share your", "staffing", "recruiting agency", "recruitment agencies", "career community", "careers network",
    "alert", "alerts", "text", "texts", "call", "calls",
    "alerts by email", "email alerts", "notify me", "similar openings", "similar roles", "stay connected",
    "stay in touch",
    "arbitration", "waive", "jury", "non-compete", "noncompete", "non-solicit",
    "join our talent community", "subscribe to the newsletter", "sign up for job alerts", "signs you up for marketing",
    "receive promotional", "enroll in the talent pool", "added to our talent network", "opt in to marketing",
    "opts you in to newsletters",
    "future opportunities", "future job openings", "future career roles", "future positions", "keep my profile",
    "keep your information", "keep your resume", "retain my", "retain your", "campaign", "campaigns", "cookie",
    "at-will", "at will", "terms and conditions of my employment", "conditions of employment", "employment agreement",
    "employment contract",
    "Sign up for Job Alerts", "Get email alerts", "Newsletter",
]


@pytest.mark.parametrize("name, pattern", [*OLD_LISTS.items(), *LIVE_LISTS.items()])
def test_the_one_list_holds_every_word_each_paths_own_list_held(name, pattern):
    """No path loses a word: every word of each path's list before the one list (and of the lists that still tell
    a sign-up's page from an account's) is refused by _never_for_them. A word added to one of those lists needs one
    here, and in never.py."""
    for alternative in _alternatives(pattern):
        words = [w for w in WORDS if re.search(alternative, w, re.I | re.S)]
        assert words, f"{name}: no word here for {alternative!r}"
        for word in words:
            assert _never_for_them(word), (name, alternative, word)
