"""The never-list (never.py): every path that agrees, ticks or presses for the person reads one list,
_never_for_them, so none of them misses a kind another refuses (a review's structure list, Oct 18: each path
kept a list of its own, and they drifted apart). Each path reads what it read before 0.3.208, and besides only
words that are a sign-up or a consent wherever they stand (the review of 0.3.208)."""

import re

import pytest

from job_apply import config, formjs, pipeline
from job_apply.autofill import _CONSENT_KINDS, _answer_bank
from job_apply.never import (ANYWHERE, BEYOND_CHECK, BOX_WORDS, EMAILS, FUTURE_ROLES, GATE_OWN, GATE_PAGE, JOB_NEWS,
                             MAIL, NEVER, PARAGRAPH_WORDS, PRECISE, _never_for_them)

# One sentence for each kind never agreed to for the person, worded as the tests of each path word theirs, and
# without words that agree (each path's own words do that)
SAID = {
    "arbitration": "settle any dispute through binding arbitration",
    "waiver": "waive the right to a trial by jury",
    "at-will": "understand that employment with Example Corp is at-will",
    "signs up": "join our Talent Community",
    "newsletter": "get the monthly newsletter",
    "job alerts": "get job alerts for roles like this one",
    JOB_NEWS: "let Example Corp forward you details of new job openings",
    "talent community": "have a place in the Talent Network",
    "marketing": "get marketing and promotional material",
    "offers": "get special offers",
    EMAILS: "receive communications from Example Corp",
    MAIL: "receive a message from Example Corp",
    "texts or calls": "let Example Corp call or text me",
    "being contacted": "be contacted by recruiters",
    "other roles": "be considered for other open roles",
    FUTURE_ROLES: "be kept on file for future job openings",
    "their data shared": "let Example Corp share my profile",
    "their data kept": "let Example Corp keep my resume on file",
    "cookies": "allow the use of cookies",
    BEYOND_CHECK: "let Example Corp pull a consumer credit report",
    PARAGRAPH_WORDS: "hear from staffing firms",
    BOX_WORDS: "let a third party process my application",
}
# (a sign-up in so many words names what it signs up for; a box's and a paragraph's own words hold some precise ones')
ALSO = {"signs up": {"talent community"}, EMAILS: {BOX_WORDS}, MAIL: {BOX_WORDS}, FUTURE_ROLES: {BOX_WORDS},
        "job alerts": {PARAGRAPH_WORDS}, "texts or calls": {PARAGRAPH_WORDS}}
EVERY = frozenset(NEVER)
# What each kind of path reads (pipeline.py says why): an agreement's own words or a notice's heading or opening, an
# attestation's (which reads emails beyond the application too), a Create Account box's own words, the words above a
# bare "I agree", an attestation's label
AGREEMENT = PRECISE | {FUTURE_ROLES}
ATTESTATION = AGREEMENT | {EMAILS, MAIL}
BOX = EVERY - {PARAGRAPH_WORDS, "cookies", "their data kept"}
ABOVE = EVERY - {BOX_WORDS, MAIL}
ATTESTED = (PRECISE - {"cookies", "their data kept"}) | {EMAILS, MAIL}


def test_each_sentence_is_its_own_kind_and_every_kind_has_one():
    """A kind added to the list gets a sentence here, and every path below is then held to read it or not."""
    assert set(SAID) == set(NEVER)
    for kind, said in SAID.items():
        assert {k for k in NEVER if _never_for_them(said, only={k})} == {kind} | ALSO.get(kind, set()), kind
        assert _never_for_them(said) is not None


def test_the_kinds_each_path_reads_are_the_ones_pipeline_says():
    assert pipeline._AGREEMENT_KINDS == AGREEMENT
    assert pipeline._BOX_KINDS == BOX and pipeline._ATTESTED_KINDS == ATTESTED
    assert pipeline._ABOVE_KINDS == ABOVE


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
ABOVE_WORDS = "Please review and accept our Terms of Use and Candidate Privacy Policy"
OTHER_POSITIONS = "I understand that I may be considered for other open positions in my country"  # Northrop's
STATEMENT_LINK = "Read and accept the data privacy statement"  # SuccessFactors'
POLICY = "Privacy Policy of Example Corp"
POLICY_TEXT = "Example Corp protects the personal data you give it, as this policy describes. "
AI_HEADING = "Notice Related to Example Corp's Use of the Eightfold AI Recruiting Software"
AI_TEXT = ('Example Corp uses an artificial intelligence ("AI") recruiting software provided by Eightfold AI to assist '
           "with and support its recruiting efforts. Before submitting your application, please review this notice.")
STATEMENT = "Example Corp Consent Statement"  # (the live check's test identity's: Freeport-McMoRan's, run 56)
STATEMENT_TEXT = "Example Corp Data Privacy Statement: how Example Corp uses the details you give it."
REMEMBERED = ("Relocation: {} hires for its sites across the country, and many of its openings ask for a move{}. Are "
              "you willing to relocate?")


def _remembered(clause):
    """A Yes remembered at Acme for a question in a notice, given at Example Fab for the same question in its own."""
    config.save_answer(REMEMBERED.format("Acme", ""), "Yes", "Acme")
    label = REMEMBERED.format("Example Fab", "" if clause is None else f", and applicants may {clause}")
    answer = _answer_bank(config.Profile.load(), label, {"company": "Example Fab"})
    return answer is not None and answer.value == "Yes"


def _box(label, **more):
    return {"fields": [{"id": "t", "kind": "checkbox", "label": label, "value": False, "required": True, **more}]}


def _attests(kind, label, options=None):
    return pipeline._attestation({"kind": kind, "label": label, "required": True, "options": options or []}) is not None


# Each path's own entry point, given its own words with a sentence of one kind in them (None: its own words alone),
# says whether the desk would agree, tick or press for the person; and the kinds it refuses. A kind left out is
# that path's own exception, or one its words read only elsewhere, said where it reads (pipeline.py, autofill.py)
PATHS = {
    # an attestation (settings.accept_notices); an at-will sentence may stand beside the application's own
    # information certified true (the second review of 0.3.170)
    "attestation: a certification's box": (lambda c: _attests("checkbox", _and(CERTIFY, c)), ATTESTED - {"at-will"}),
    "attestation: a privacy agreement's box": (lambda c: _attests("checkbox", _and(PRIVACY, c)), ATTESTATION),
    "attestation: a lone choice": (lambda c: _attests("radio_group", CONSENT, [_and("I agree", c)]), ATTESTATION),
    "attestation: a group's tick": (
        lambda c: _attests("checkbox_group", "Consents", [_and("I accept the privacy notice", c)]), ATTESTATION),
    "attestation: a group's own label": (
        lambda c: _attests("checkbox_group", _and("Consents", c), ["I accept the privacy notice"]),
        frozenset({"arbitration", "waiver", "at-will"})),
    # a Create Account's own boxes (settings.manage_accounts)
    "a Create Account's terms box": (lambda c: bool(pipeline._terms_boxes(_box(_and(TERMS_BOX, c)))), BOX),
    "an email-first site's agreement box": (lambda c: bool(pipeline._consent_boxes(_box(_and(TERMS_BOX, c)))), BOX),
    "the words above a bare I agree": (lambda c: pipeline._terms_above(_and(ABOVE_WORDS, c) + "."), ABOVE),
    # (its own words, other roles, are the owner's call, Oct 10)
    "a consent to other positions": (
        lambda c: pipeline._other_positions_consent({"kind": "checkbox",
                                                     "label": _and(OTHER_POSITIONS, c)}) is not None,
        AGREEMENT - {"other roles"}),
    "a Create Account's privacy statement link": (
        lambda c: pipeline._privacy_statement({"actions": [{"id": "a", "text": _and(STATEMENT_LINK, c) + ".",
                                                            "in_account_form": True}]}) is not None,
        AGREEMENT),
    "a terms gate, by its headings": (
        lambda c: pipeline._terms_gate({"text": "I Accept"},
                                       {"headings": ["Privacy Agreement" + ("" if c is None else f": you {c}")]}),
        AGREEMENT),
    # (a framed gate's page has a sign-up of its own beside it: Schwab's, live, Oct 10)
    "a terms gate, by its page's words": (
        lambda c: pipeline._terms_gate({"text": "I Accept"}, {"headings": ["Privacy Agreement"]},
                                       "Please read our privacy agreement." + _by(c)),
        GATE_PAGE | GATE_OWN),
    # (and the careers page around a framed gate, its own words read apart: the review of run 147's fixes)
    "a framed terms gate, by the careers page around it": (
        lambda c: pipeline._terms_gate({"text": "I Accept"}, {"headings": ["Privacy Agreement"]},
                                       "Please read our privacy agreement." + _by(c),
                                       "Please read our privacy agreement."),
        GATE_PAGE),
    "a privacy notice, by its opening": (
        lambda c: pipeline._terms_notice(POLICY, (_by(c) + " " + POLICY_TEXT).strip())[0], AGREEMENT),
    # (its long text names its affiliates, marketing and cookies as its practices: APS's, live)
    "a privacy notice, past its opening": (lambda c: pipeline._terms_notice(POLICY,
                                                                            POLICY_TEXT * 5 + _by(c))[0], ANYWHERE),
    "a notice not named for its terms": (
        lambda c: pipeline._terms_notice("Before you apply", "Please read and accept our privacy notice." + _by(c))[0],
        AGREEMENT),
    "an AI notice, by its heading": (
        lambda c: pipeline._agreeable_notice(AI_HEADING + ("" if c is None else f": you {c}"), AI_TEXT, False)[0],
        AGREEMENT),
    # (its words name the other positions it may suggest: Micron's, live)
    "an AI notice, by its words": (lambda c: pipeline._agreeable_notice(AI_HEADING, AI_TEXT + _by(c), False)[0],
                                   ANYWHERE),
    # the live check's test identity agrees to what a person's desk leaves to them (the owner's call, Oct 13), never a
    # sign-up in so many words, nor cookies by its opening; a notice's words are read for a waiver
    # (the review of Oct 15)
    "the test identity's notice": (
        lambda c: pipeline._agreeable_notice(STATEMENT, f"{_by(c).strip()} {STATEMENT_TEXT}".strip(), True)[0],
        frozenset({"signs up", JOB_NEWS, "cookies", "arbitration", "waiver"})),
    "a remembered answer at another employer": (_remembered, _CONSENT_KINDS),
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
def test_each_path_refuses_every_kind_it_reads_and_only_those(path, kind):
    """For each kind never agreed to for the person, a sentence of it refused by each path's own entry point that reads
    that kind (and one a path leaves out, by its own exception or as a kind read only elsewhere, still agreed)."""
    agreed, refuses = PATHS[path]
    assert agreed(SAID[kind]) is (kind not in refuses), (path, kind, SAID[kind])


def _gate(headings, text=""):
    return pipeline._terms_gate({"text": "I Accept"}, {"headings": headings}, text)


GROUP = ["I accept the privacy notice", "Send me job alerts"]
# Wordings each path agreed to before 0.3.208, which a word read on every path refused for a while (the review of
# 0.3.208): agreed again
STILL_AGREED = {
    "a certification under penalty of perjury": lambda: _attests(
        "checkbox", "I certify under penalty of perjury that the information in this application is true and complete"),
    "a certification signed in the text box": lambda: _attests(
        "checkbox", "Electronic signature: by typing my name in the text box below, I certify that the information in "
                    "this application is true and complete"),
    "a certification to notify of a change": lambda: _attests(
        "checkbox", f"{CERTIFY}, and I agree to notify Acme promptly of any change to it"),
    "a certification that references may be called": lambda: _attests(
        "checkbox", f"{CERTIFY}, and I understand the company may call my references"),
    "a background check by screening partners": lambda: _attests(
        "checkbox", "I authorize Acme and its background screening partners to conduct a background check"),
    "a privacy agreement with updates about the application": lambda: _attests(
        "checkbox", "I agree to the privacy policy and to receive updates about my application"),
    "a lone choice that received the privacy notice": lambda: _attests(
        "radio_group", "By clicking the 'I agree' button below, you are acknowledging the above Privacy Notice",
        ["I have received and agree to the Privacy Notice"]),
    "an injury past a privacy policy's opening": lambda: pipeline._terms_notice(
        POLICY, POLICY_TEXT * 5 + "We may keep records of a work-related injury.")[0],
    "an injury in a gate's page": lambda: _gate(["Privacy Agreement"], "Please read our privacy agreement. We may keep "
                                                                       "records of a work-related injury."),
    "an injury past a privacy dialog's opening": lambda: pipeline._agreeable_notice(
        POLICY, POLICY_TEXT * 5 + "We may keep records of a work-related injury.", False)[0],
    "a privacy policy that says how it shares the information": lambda: pipeline._terms_notice(
        POLICY, "This notice explains how we collect, use and share your personal information. " + POLICY_TEXT)[0],
    "a staffing firm's privacy agreement": lambda: pipeline._terms_notice(  # (site/privacy-agreement.html)
        "Privacy Agreement", "Example Staffing collects the personal information you give in your application to "
                             "consider you for employment.")[0],
    "a gate headed Data Privacy Notification": lambda: _gate(["Data Privacy Notification"]),
    "a gate beside a Stay Connected heading": lambda: _gate(["Privacy Agreement", "Stay Connected"]),
    "terms above a verification email": lambda: pipeline._terms_above(
        "Accept our Terms of Use. You will receive a verification email."),
    "terms above an email to activate the account": lambda: pipeline._terms_above(
        "Accept our Terms of Use. You will receive an email with a link to activate your account."),
    "terms above a confirmation email": lambda: pipeline._terms_above(
        "Accept our Terms of Use. You will receive a confirmation email shortly."),
    "terms above word of the application's status": lambda: pipeline._terms_above(
        "Accept our Terms of Use. You will receive information about the status of your application by email."),
    "terms above an email to verify the account": lambda: pipeline._terms_above(
        "Accept our Terms of Use. You will receive an email to verify your account."),
    "a group under Privacy and Marketing Preferences": lambda: pipeline._attestation(
        {"kind": "checkbox_group", "label": "Privacy and Marketing Preferences", "required": True, "options": GROUP})
        == [GROUP[0]],
    "a group under Consents and Notifications": lambda: pipeline._attestation(
        {"kind": "checkbox_group", "label": "Consents and Notifications", "required": True, "options": GROUP})
        == [GROUP[0]],
    "the test identity's at-will statement": lambda: pipeline._agreeable_notice(
        "Employment At-Will Statement", STATEMENT_TEXT, True) == (True, False),
    "a remembered answer by a notice of on-call work": lambda: _remembered("be on-call some weekends"),
    "a remembered answer by a notice of a text": lambda: _remembered("get a text when it's reviewed"),
    "a remembered answer by a staffing firm's notice": lambda: _remembered("apply through Example Staffing"),
    "a remembered answer by a notice of partners": lambda: _remembered("work at its partners' sites"),
    "a remembered answer by a notice of the outcome": lambda: _remembered("be notified of the outcome"),
}
# (the re-review of 0.3.208) a Create Account's box or an attestation that names the site's cookie policy beside its
# terms, or how long it keeps the application, as before; and the account's own mail named above a bare "I agree"
TERMS_AND_COOKIES = "I have read and agree to the Terms and Conditions, Privacy Policy and Cookie Policy"
for _what, _label in {"its cookie policy": TERMS_AND_COOKIES,
                      "its cookies notice": "I agree to the Terms of Use and acknowledge "
                                            "the Privacy and Cookies Notice",
                      "how it uses cookies": "I agree to the candidate privacy policy, including how Acme uses cookies",
                      "how long it keeps the data": "I agree to the Terms of Use and Privacy Policy and understand "
                                                    "Acme may keep my data for 12 months"}.items():
    STILL_AGREED[f"a Create Account's terms box naming {_what}"] = lambda b=_label: bool(pipeline._terms_boxes(_box(b)))
    STILL_AGREED[f"an email-first site's agreement box naming {_what}"] = \
        lambda b=_label: bool(pipeline._consent_boxes(_box(b)))
for _what, _label in {"the cookie policy read": "I certify that the answers given are true and complete, and that I "
                                                "have read the Candidate Privacy Notice and Cookie Policy",
                      "the application retained": f"{CERTIFY}. I understand Acme will retain my application in line "
                                                  "with its records policy.",
                      "the information kept on file": f"{CERTIFY}, and I understand Acme may keep my information on "
                                                      "file for one year."}.items():
    STILL_AGREED[f"a certification naming {_what}"] = lambda b=_label: _attests("checkbox", b)
TERMS_PARAGRAPH = "By creating an account you accept our Terms of Use and Privacy Policy."
for _mail in ("You will receive emails about your account.", "You will receive a one-time passcode by email.",
              "You will receive an email confirming your registration.",
              "You will receive a verification code at the email address you entered.",
              "You will receive emails related to your application.", "You will receive an email with your username."):
    STILL_AGREED[f"terms above: {_mail}"] = lambda m=_mail: pipeline._terms_above(f"{TERMS_PARAGRAPH} {m}")
# (the last check of 0.3.208) mail of two kinds about the application alone, in either order
for _both in ("emails and updates", "updates and emails"):
    STILL_AGREED[f"a certification to receive {_both} about the application"] = lambda b=_both: _attests(
        "checkbox", f"{CERTIFY}, and I agree to receive {b} about my application")
    STILL_AGREED[f"a lone choice to receive {_both} about the application"] = lambda b=_both: _attests(
        "radio_group", f"{CERTIFY}, and I agree to receive {b} about my application", ["I agree"])


# A waiver's words inside other words ("injury", "perjury") are no waiver: every path that reads a waiver agrees to
# them wherever it agrees otherwise (the agreements' and notices' own list read them so before 0.3.208; that reach was
# dropped on purpose, Oct 18)
INJURY, PERJURY = "report any work-related injury", "swear to it under penalty of perjury"
for _path, (_agreed, _refuses) in PATHS.items():
    if "waiver" in _refuses:
        STILL_AGREED[f"{_path}: an injury"] = lambda a=_agreed: a(INJURY)
        STILL_AGREED[f"{_path}: under penalty of perjury"] = lambda a=_agreed: a(PERJURY)
SWORN = f"{CERTIFY} under penalty of perjury"
STILL_AGREED.update({
    "a Yes/No question certified under penalty of perjury": lambda: pipeline._attestation(
        {"kind": "select", "label": SWORN, "required": True, "options": ["Yes", "No"]}) == "Yes",
    "a one-choice question certified under penalty of perjury": lambda: pipeline._attestation(
        {"kind": "combobox", "label": SWORN, "required": True, "options": ["I Agree"]}) == "I Agree",
    "a lone radio sworn under penalty of perjury": lambda: pipeline._attestation(
        {"kind": "radio_group", "label": SWORN, "required": True, "options": ["I agree"]}) == "I agree",
    "a group under a perjury heading": lambda: pipeline._attestation(
        {"kind": "checkbox_group", "label": "Certified under penalty of perjury", "required": True, "options": GROUP})
        == [GROUP[0]],
    "an injury in a privacy notice's opening": lambda: pipeline._terms_notice(
        POLICY, "This notice covers the records we keep of a work-related injury. " + POLICY_TEXT)[0],
})


@pytest.mark.parametrize("wording", list(STILL_AGREED))
def test_what_each_path_agreed_to_before_it_still_agrees_to(wording):
    assert STILL_AGREED[wording](), wording


# What stays refused beside those: a sign-up's mail above a bare "I agree" (and the account's own mail takes nothing
# else with it: the review of 0.3.208), a consent to be considered for similar roles in an attestation (a consent to
# other roles, which CLAUDE.md keeps narrow), a subscription in a Create Account's terms box, a cookie
# banner's agreement
STILL_REFUSED = {
    "terms above communications received electronically": lambda: pipeline._terms_above(
        f"{TERMS_PARAGRAPH} You consent to receive communications electronically."),
    "terms above email communications": lambda: pipeline._terms_above(
        f"{TERMS_PARAGRAPH} You will receive email communications from Acme."),
    "terms above an email to verify the account and news": lambda: pipeline._terms_above(
        "Accept our Terms of Use. You will receive an email to verify your account and news from Acme."),
    "terms above an email to verify the account and career opportunities": lambda: pipeline._terms_above(
        "Accept our Terms of Use. You will receive an email to verify your account and career opportunities."),
    "a certification with similar roles": lambda: _attests(
        "checkbox", "I certify the information is true and complete and understand I may be considered for similar "
                    "roles at Acme."),
    "a terms box with a subscription": lambda: bool(pipeline._terms_boxes(_box(
        "I accept the Terms of Service and the Subscription Agreement"))),
    "an agreement button for cookies": lambda: pipeline._agreement_button(
        {"actions": [{"id": "a", "text": "I accept the Cookie Policy"}]}) is not None,
    "a certification to receive emails and updates about the application and other roles": lambda: _attests(
        "checkbox", f"{CERTIFY}, and I agree to receive emails and updates about my application and other roles"),
    # a sign-up's mail in plain words above a bare "I agree" (agreed before 0.3.208: the last check of it)
    "terms above emails about new jobs": lambda: pipeline._terms_above(
        f"{TERMS_PARAGRAPH} You will receive emails about new jobs."),
    "terms above a digest of new openings": lambda: pipeline._terms_above(
        f"{TERMS_PARAGRAPH} You will receive a monthly email digest of new openings."),
    "terms above emails about events and opportunities": lambda: pipeline._terms_above(
        f"{TERMS_PARAGRAPH} You will receive emails from Acme about events and opportunities."),
}


@pytest.mark.parametrize("wording", list(STILL_REFUSED))
def test_what_each_path_refused_before_it_still_refuses(wording):
    assert not STILL_REFUSED[wording](), wording


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
    "_CALLS_OR_TEXTS": CALLS,
    "_NOT_TERMS": NOT_TERMS,
    "_MORE_THAN_TERMS": r"receiv\w*|e-?mails?|updates?|notifications?|messages?|partners?|third[- ]part|future|"
                        r"talent pool|retain|retention",
    "_SIGNS_UP_TOO": r"\breceiv\w*\b.{0,40}\b(?:e-?mails?|communications?|updates?|messages?|news\w*|offers|"
                     r"information about|texts?)\b|\b(?:e-?mail|marketing|promotional) communications?\b|"
                     r"\bupdates? (?:about|on|from)\b|\bnewsletter",
    "_SIGN_UP_ABOVE": r"talent (?:community|network|pool)|job alerts?|newsletter|subscribe|marketing|"
                      r"mailing list|\bnotif(?:y|ied|ications?)\b|\bopt(?:ing)?[- ]in\b|\be-?mail (?:me|you)\b|"
                      r"text messages?|\bsms\b|keep me|stay informed|similar jobs|promotion\w*|offers|send me|"
                      r"contact(?:ed)? me|be contacted|share my|other (?:\w+ )?(?:roles|positions|jobs|openings|"
                      r"opportunities)|\breceiv\w*\b.{0,40}\b(?:news\w*|offers|updates?|alerts?|promotions?|"
                      r"(?:job|career) opportunities|marketing)\b|talent database|future (?:\w+ )?vacanc|"
                      r"keep\w* (?:my|your) (?:personal )?data|share (?:my|your)|partners|staffing|"
                      r"recruit\w* agenc|careers? (?:community|network)|\balerts?\b|\btexts?\b|\bcalls?\b",
    "_NOT_ATTESTED": r"newsletter|marketing|job alerts?|text messages?|\bsms\b|promotion|subscribe|"
                     r"talent (?:community|network)|" + CALLS,
    "_WAIVES": r"arbitrat|\bwaive|\bjury\b|non-?compete|non-?solicit",
    "_SIGNS_UP": r"\b(?:join\w*|subscrib\w*|sign(?:s|ed|ing)? (?:you )?up|receiv\w*|enrol\w*|added to|"
                 r"opt(?:s|ed)? (?:you )?in)\b.{0,60}?\b(?:talent (?:community|network|pool)|newsletters?|"
                 r"job alerts?|marketing|promotion\w*)",
    # (its "waive" and "jury" were read inside any word too, which found only "injury" and "perjury": that reach was
    # dropped on purpose, Oct 18, and the words themselves, "waive", "waiver", "jury", "jury trial", are still refused)
    "_NEVER_AGREED": NOT_TERMS + r"|talent pool|future (?:job |career )?(?:opportunities|openings|roles|"
                     r"positions)|keep (?:my|your) (?:profile|information|data|resume)|retain (?:my|your)|"
                     r"campaigns?|cookie|arbitration|waive|jury|non-?compete|non-?solicit",
    "_EMPLOYMENT_TERMS": r"\bat[- ]will\b|\b(?:terms|conditions)\b[^.]{0,30}\bof (?:my |your |this |the )?"
                         r"employment\b|\bemployment (?:agreement|contract)\b",
    "_agreement_button's": r"cookie",
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
# The words they hold, each on its own (and two as a paragraph says them, beside an email to verify the account)
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
    "receive an email to verify your account and news from Acme",
    "receive an email to verify your account and career opportunities",
    "talent database", "future vacancies", "future job vacancies", "keep my data", "keeping your personal data",
    "share your", "staffing", "recruiting agency", "recruitment agencies", "career community", "careers network",
    "alert", "alerts", "text", "texts", "call", "calls",
    "alerts by email", "email alerts", "notify me", "similar openings", "similar roles", "stay connected",
    "stay in touch",
    "arbitration", "waive", "waiver", "jury", "jury trial", "non-compete", "noncompete", "non-solicit",
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
    """No word is lost from the one list: every word of each path's list before it (and of the lists that still tell
    a sign-up's page from an account's) is one of _never_for_them's kinds. A word added to one of those lists needs one
    here, and in never.py."""
    for alternative in _alternatives(pattern):
        words = [w for w in WORDS if re.search(alternative, w, re.I | re.S)]
        assert words, f"{name}: no word here for {alternative!r}"
        for word in words:
            assert _never_for_them(word), (name, alternative, word)


# The lists each path read before the one list (origin/main at 0.3.207). An attestation's label read _NOT_TERMS without
# its affiliates and offers of employment (and an at-will sentence beside the application's information certified true)
MAIN_READ = {
    "attestation: a certification's box": ("_NOT_ATTESTED", "_WAIVES", "_SIGNS_UP_TOO", "_NOT_TERMS"),
    "attestation: a privacy agreement's box": ("_NEVER_AGREED", "_EMPLOYMENT_TERMS", "_WAIVES", "_SIGNS_UP_TOO"),
    "attestation: a lone choice": ("_NEVER_AGREED", "_WAIVES", "_NOT_ATTESTED", "_SIGNS_UP_TOO"),
    "attestation: a group's tick": ("_NEVER_AGREED", "_EMPLOYMENT_TERMS", "_SIGNS_UP_TOO"),
    "attestation: a group's own label": ("_WAIVES", "_EMPLOYMENT_TERMS"),
    "a Create Account's terms box": ("_NOT_TERMS", "_MORE_THAN_TERMS", "_WAIVES", "_EMPLOYMENT_TERMS"),
    "an email-first site's agreement box": ("_NOT_TERMS", "_MORE_THAN_TERMS"),
    "the words above a bare I agree": ("_SIGN_UP_ABOVE", "_NEVER_AGREED", "_WAIVES"),
    "a consent to other positions": ("_NEVER_AGREED",),
    "a Create Account's privacy statement link": ("_NEVER_AGREED", "_EMPLOYMENT_TERMS"),
    "a terms gate, by its headings": ("_NEVER_AGREED",),
    "a terms gate, by its page's words": ("_WAIVES",),
    "a privacy notice, by its opening": ("_NEVER_AGREED", "_EMPLOYMENT_TERMS", "_WAIVES", "_SIGNS_UP"),
    "a privacy notice, past its opening": ("_WAIVES", "_SIGNS_UP"),
    "a notice not named for its terms": ("_NEVER_AGREED", "_EMPLOYMENT_TERMS", "_WAIVES", "_SIGNS_UP"),
    "an AI notice, by its heading": ("_NEVER_AGREED",),
    "an AI notice, by its words": ("_WAIVES", "_SIGNS_UP"),
    "the test identity's notice": ("_SIGNS_UP", "_WAIVES"),
    "a remembered answer at another employer": ("autofill._NOTICE_OF_CONSENT's never-words",),
    "a page's agreement button": ("_agreement_button's",),
}


@pytest.mark.parametrize("path", list(MAIN_READ))
def test_each_path_still_refuses_every_word_of_the_lists_it_read_before(path):
    """A word lost on one path, which the one list still holds for others, is caught here: each path's entry point
    refuses every word of each list it read before, in its own words."""
    agreed, _ = PATHS[path]
    for name in MAIN_READ[path]:
        for alternative in _alternatives(OLD_LISTS[name]):
            for word in WORDS:
                read = word
                if path.startswith("attestation: a certification") and name == "_NOT_TERMS":
                    read = re.sub(r"\baffiliat\w*|\boffers? of employment", "", word, flags=re.I)
                if re.search(alternative, read, re.I | re.S):
                    assert not agreed(word), (path, name, alternative, word)


# The weekly mutation pass's first sample (scripts/mutation_pass.py, Oct 18): words of a kind's list no test read as
# that kind

@pytest.mark.parametrize("said", ["Get email alerts", "Send me e-mail alerts", "an email alert"])
def test_email_alerts_are_job_alerts_wherever_they_are_read(said):
    """"email alerts" is a job alert in so many words: a path that reads only the precise kinds refuses it too, not
    only the paragraph above a bare "I agree", whose own words read any "alerts"."""
    assert _never_for_them(said, only=PRECISE) == "job alerts"


@pytest.mark.parametrize("more", ["new jobs", "special offers", "open roles", "open positions", "our newsletter",
                                  "hiring events", "other news", "career opportunities"])
def test_mail_about_the_application_that_brings_more_with_it_is_refused(more):
    """Emails about the application are the application's own, but not when they bring more ("..., including special
    offers"): a choice read for mail and messages alone (pipeline's agreed choices) refuses each."""
    assert _never_for_them("I agree to receive emails about my application", only={EMAILS, MAIL}) is None
    assert _never_for_them(f"I agree to receive emails about my application, including {more}",
                           only={EMAILS, MAIL}) == MAIL


@pytest.mark.parametrize("named", ["privacy statements", "privacy policies", "privacy notices",
                                   "data protection and privacy statements"])
def test_the_sites_privacy_statements_in_the_plural_are_its_terms(named):
    """Resorts World's Workday Create Account (live run 144): a bare "I consent" under "By creating a candidate account,
    you consent to Resorts World privacy statements." wasn't ticked, the plural not read as the site's privacy
    statement, and the account was never made. Its terms in the plural are its terms; a sign-up beside them is still
    refused."""
    assert pipeline._terms_above(f"By creating a candidate account, you consent to Example Resort {named}.")
    assert not pipeline._terms_above(f"By creating a candidate account, you consent to Example Resort {named} and to "
                                     "receive our newsletter.")
    assert not pipeline._terms_above(f"By creating a candidate account, you join our talent community under Example "
                                     f"Resort {named}.")


@pytest.mark.parametrize("words", ["Sign up for career updates. See our privacy notice.",
                                   "By creating an account, you agree to receive career news. See our privacy policy.",
                                   "You consent to Example Resort's privacy statement and to our candidate community.",
                                   "By creating an account you join our Candidate Network under our privacy policy."])
def test_career_updates_and_a_candidate_community_are_refused(words):
    """The review of the plural terms (run 144): "career updates" (a newsletter by another name) and a "candidate
    community" (a talent community by another name) beside the site's privacy notice read as its terms alone, so a
    bare "I agree" under them would be ticked. They're the person's, wherever they're read."""
    assert not pipeline._terms_above(words)
    assert _never_for_them(words, only=PRECISE) in {"newsletter", "talent community", "signs up"}


INSIGHT_TERMS = ("I agree to the Terms and understand that information collected will be used as described here, the "
                 "Notice at Collection, and Privacy Policy. ")


@pytest.mark.parametrize("words", ["You will also receive job recommendations by email.",
                                   "We'll email you job recommendations.", "Get career recommendations by e-mail.",
                                   "Job recommendations will be emailed to you.",
                                   "You'll get job recommendations by email.", "We will send you recommended jobs.",
                                   "You will receive emails with jobs that match your profile.",
                                   "We'll email you job matches.",
                                   "You will receive personalized job suggestions by email."])
def test_job_picks_by_email_are_a_sign_up_wherever_they_are_read(words):
    """Insight Enterprises' "Data Privacy Agreement" (live run 145) opens "When you upload your resume, we provide job
    recommendations to you. You will also receive job recommendations by email.": job alerts by another name, which the
    desk never agrees to for the person; and, sent by email or received in either order, a sign-up in so many words,
    read however deep in a notice (ANYWHERE: the review of fix-accept-gates)."""
    assert _never_for_them(words, only=PRECISE) in {"job alerts", "signs up"}
    assert _never_for_them(words, only=ANYWHERE) == "signs up"
    for text in (f"{words} {INSIGHT_TERMS}", INSIGHT_TERMS * 3 + words):  # (in its opening, and past it)
        assert pipeline._agreeable_notice("Data Privacy Agreement", text, False) == (False, False), text


@pytest.mark.parametrize("words", ["You will receive job recommendations.", "See your job matches.",
                                   "Browse recommended roles.", "Personalized job suggestions.",
                                   "Jobs that match your profile.", "We will send you recommended positions."])
def test_job_picks_in_other_words_are_job_alerts(words):
    """The review of fix-accept-gates: job matches, recommended jobs, roles or positions, personalized job suggestions,
    jobs that match the person's profile, and job recommendations received, are job alerts. Recommendations on the site
    alone are how it works, and no sign-up."""
    assert _never_for_them(words, only=PRECISE) in {"job alerts", "signs up"}
    assert _never_for_them("When you upload your resume, we provide job recommendations to you.", only=PRECISE) is None


@pytest.mark.parametrize("words, kind", [
    ("I understand that my employment may be terminated at any time, with or without cause or notice.", "at-will"),
    ("Employment is terminable at the will of either party.", "at-will"),
    ("Either party may end the relationship at any time, with or without cause.", "at-will"),
    ("The District may end my employment at any time for any reason.", "at-will"),
    ("Nothing in this application creates a contract of employment for any definite period.", "at-will"),
    ("New employees may be dismissed without cause.", "at-will"),
    ("I release the District and all persons from any liability for furnishing such information.", "waiver"),
    ("I hereby release and hold harmless Example School District from all claims.", "waiver"),
    ("I agree not to participate in any class action against the District.", "waiver"),
    ("Any claim will be brought individually and not as a class or collective action.", "waiver"),
    ("I agree not to compete with the District for one year after employment ends.", "waiver"),
    ("I agree that any lawsuit must be filed within six months.", "waiver"),
    ("I authorize the District to obtain a consumer credit report about me.", BEYOND_CHECK),
    ("I authorize the release of my medical records.", BEYOND_CHECK),
    ("I authorize the District to contact my former employers.", BEYOND_CHECK),
    ("I authorize any former employer to release information about me to the District.", BEYOND_CHECK),
    ("I consent to a review of my social media accounts.", BEYOND_CHECK),
    ("I consent to the sharing of my information with third parties for their own purposes.", "their data shared"),
])
def test_waivers_the_terms_of_employment_and_more_than_a_background_check_in_other_words(words, kind):
    """The review of fix-accept-gates: a Statement of Truth's gate (and an attestation's box, which reads the same list)
    took in a release from liability, a class action given up, an agreement not to compete, a shorter time to sue, an
    at-will statement in other words, a credit report, medical records, a former employer's records, a social media
    review, the person's information shared. Each is its kind, and an attestation's box with it is the person's."""
    assert _never_for_them(words) == kind
    # (an at-will sentence may stand beside a box's certification of the application's own information: the second
    # review of 0.3.170)
    assert _attests("checkbox", f"{CERTIFY}. {words}") is (kind == "at-will"), words
    if kind in {"waiver", "arbitration"}:  # (a legal waiver, read however deep in a notice)
        assert not pipeline._agreeable_notice(POLICY, POLICY_TEXT * 5 + words, False)[0]


@pytest.mark.parametrize("words", ["We may update this Privacy Policy at any time with or without notice.",
                                   "Your account may be terminated for any reason or no reason under these Terms of "
                                   "Use.",
                                   "Records are stored for a definite period, as the law requires."])
def test_an_at_will_statements_words_are_one_only_about_employment(words):
    """The second review of fix-accept-gates: "with or without notice", "for any reason or no reason", "a definite
    period" are an at-will statement's words about employment, and only there: a privacy policy or the site's terms
    that use them are still agreed to."""
    assert _never_for_them(words, only=PRECISE) is None, words
    assert pipeline._agreeable_notice("Privacy Policy", words, False) == (True, True)
    assert pipeline._agrees_to_terms("I agree to the Terms of Use. " + words)
    said = words.replace("this Privacy Policy", "your employment").replace("Your account", "Your employment")
    said = said.replace("Records are stored", "No one is hired")
    assert _never_for_them(said, only=PRECISE) == "at-will", said


@pytest.mark.parametrize("words", ["I certify that my information is true, and I authorize Acme to verify it with my "
                                   "previous employers.",
                                   "I understand the company may call my references.",
                                   "This notice covers how we store medical information you choose to give us."])
def test_a_check_of_the_application_itself_is_still_its_own(words):
    """What the background check that comes with applying does is no more than it (the review of 0.3.208: "the company
    may call my references"); only a credit report, medical records released, a former employer's records, a social
    media review are."""
    assert _never_for_them(words, only=PRECISE) is None, words


# Kforce's required privacy agreement (live run 147), its words made up but for the clause that sends job opportunities
KFORCE_LIKE = ("By clicking “I Accept” below, you agree to the preceding terms and authorize Example Staffing to "
               "collect, transmit, use and store your PI in a database where it will be used for the application "
               "process and as further outlined in the Privacy Policy below including, but not limited to, {clause}, "
               "communicating with you about your application, or for administration purposes.")


@pytest.mark.parametrize("clause", ["sending you further information about Example Staffing job opportunities",
                                    "emailing you new career opportunities",
                                    "to contact you about other job opportunities",
                                    "reaching out to you about future opportunities"])
def test_job_opportunities_sent_are_job_alerts_even_inside_a_required_gate(clause):
    """Kforce's required privacy agreement (live run 147) let it use the person's data for "sending you further
    information about Kforce job opportunities", and the desk pressed its "I Accept" (a terms gate) for them: job alerts
    in other words, which the desk never agrees to, required or not. Read in a terms gate's whole page, a notice's
    words however deep, an attestation's and an account box's own words; the gate is the person's."""
    words = KFORCE_LIKE.format(clause=clause)
    assert _never_for_them(words, only=GATE_OWN) == JOB_NEWS, words
    assert not pipeline._terms_gate({"text": "I Accept"}, {"headings": ["Privacy Agreement"]}, words)
    assert not pipeline._terms_gate({"text": "I Accept"}, {"headings": ["Privacy Agreement"]}, words, words)
    assert not pipeline._agreeable_notice(POLICY, POLICY_TEXT * 5 + words, False)[0]
    assert not _attests("checkbox", f"{PRIVACY}. {words}")
    assert not pipeline._terms_boxes(_box(f"{TERMS_BOX}. {words}"))


@pytest.mark.parametrize("words", [KFORCE_LIKE.format(clause="processing your application"),
                                   "We may contact you about your application or this position.",
                                   "We will send you an email about your application for this job opportunity.",
                                   "Recruiters may contact you about the job you applied for.",
                                   # (a noun, never a verb that sends; the person's own applying: the review of run
                                   # 147's fixes)
                                   "We collect the text you enter when you apply for job opportunities with us.",
                                   "We store the email you use to apply for employment opportunities.",
                                   "We will send you a confirmation email each time you apply for job openings.",
                                   "We will email you when the job openings you applied for are filled or closed.",
                                   "We may text you about interview times for the job openings you applied to.",
                                   "We may contact you regarding the job openings you applied for."])
def test_the_applications_own_mail_is_still_no_job_opportunities_sent(words):
    """What the application itself brings (an email about it, a recruiter's call about the job applied to) is no job
    opportunities sent: a gate that says no more is still agreed to (settings.accept_notices)."""
    assert _never_for_them(words, only={JOB_NEWS}) is None, words
    assert pipeline._terms_gate({"text": "I Accept"}, {"headings": ["Privacy Agreement"]}, words)


def test_a_consent_to_other_positions_is_given_only_when_it_sends_no_job_opportunities():
    """Northrop's "Contact Consent" (the owner's call, Oct 10) is still given on its own words; with job opportunities
    sent in them too it's the person's (CLAUDE.md: never when its words also sign the person up for job alerts)."""
    assert pipeline._other_positions_consent({"kind": "checkbox", "label": OTHER_POSITIONS}) is True
    assert pipeline._other_positions_consent({"kind": "checkbox", "label": OTHER_POSITIONS + ", in addition to the "
                                              "role(s) to which I apply"}) is True
    for more in ("and Example Corp may send me information about job opportunities",
                 "and recruiters may contact me about other opportunities"):
        assert pipeline._other_positions_consent({"kind": "checkbox", "label": f"{OTHER_POSITIONS}, {more}"}) is None


@pytest.mark.parametrize("words", ["We may contact you regarding other job opportunities.",
                                   "to contact you concerning future employment opportunities",
                                   "We may contact you for future employment opportunities.",
                                   "We may use your information to notify you of other job openings.",
                                   "We will inform you of new career opportunities.",
                                   "We'd like to let you know about future opportunities with us.",
                                   # USAA's and Caesars' (captured live, runs 145 and 147)
                                   "USAA Talent Acquisition will only send you information regarding employment "
                                   "opportunities.",
                                   "We may also use your information to contact you about other job opportunities "
                                   "that may be of interest to you."])
def test_job_opportunities_sent_in_other_words(words):
    """The review of run 147's fixes: job opportunities sent in other words than Kforce's are job alerts too."""
    assert _never_for_them(words, only={JOB_NEWS}) == JOB_NEWS, words
    assert not pipeline._terms_gate({"text": "I Accept"}, {"headings": ["Privacy Agreement"]}, words)


def test_a_framed_gates_own_words_are_read_for_job_opportunities_never_the_careers_page_around_it():
    """Charles Schwab's iCIMS gate (live, Oct 10) sits in a frame on a careers page whose own talent network widget
    sends job opportunities: the gate's own words are read for those, never the page around it, which is still read
    whole for a legal waiver."""
    gate, page = {"text": "I Accept"}, {"headings": ["Privacy Agreement"]}
    own = "Please read and accept our privacy agreement before you apply."
    around = own + " Join our talent network and we will email you new job opportunities."
    assert pipeline._terms_gate(gate, page, around, own)
    assert not pipeline._terms_gate(gate, page, around)  # (its own words not read apart: the page's, whole)
    assert not pipeline._terms_gate(gate, page, around + " You waive the right to a trial by jury.", own)
    assert not pipeline._terms_gate(gate, page, around, KFORCE_LIKE.format(
        clause="sending you further information about Example Staffing job opportunities"))
