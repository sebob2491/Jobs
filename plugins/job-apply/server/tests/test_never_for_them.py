"""The never-list (never.py): every path that agrees, ticks or presses for the person reads one list,
_never_for_them, so none of them misses a kind another refuses (a review's structure list, Oct 18: each path
kept a list of its own, and they drifted apart). Each path reads what it read before 0.3.208, and besides only
words that are a sign-up or a consent wherever they stand (the review of 0.3.208)."""

import re

import pytest

from job_apply import config, formjs, pipeline
from job_apply.autofill import _CONSENT_KINDS, _answer_bank
from job_apply.never import (ANYWHERE, BOX_WORDS, EMAILS, FUTURE_ROLES, MAIL, NEVER, PARAGRAPH_WORDS, PRECISE,
                             _never_for_them)

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
        lambda c: pipeline._other_positions_consent({"kind": "checkbox", "label": _and(OTHER_POSITIONS, c)}) is not None,
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
        frozenset({"arbitration", "waiver"})),
    "a privacy notice, by its opening": (
        lambda c: pipeline._terms_notice(POLICY, (_by(c) + " " + POLICY_TEXT).strip())[0], AGREEMENT),
    # (its long text names its affiliates, marketing and cookies as its practices: APS's, live)
    "a privacy notice, past its opening": (lambda c: pipeline._terms_notice(POLICY, POLICY_TEXT * 5 + _by(c))[0], ANYWHERE),
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
    # sign-up in so many words, nor cookies by its opening; a notice's words are read for a waiver (the review of Oct 15)
    "the test identity's notice": (
        lambda c: pipeline._agreeable_notice(STATEMENT, f"{_by(c).strip()} {STATEMENT_TEXT}".strip(), True)[0],
        frozenset({"signs up", "cookies", "arbitration", "waiver"})),
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
                      "its cookies notice": "I agree to the Terms of Use and acknowledge the Privacy and Cookies Notice",
                      "how it uses cookies": "I agree to the candidate privacy policy, including how Acme uses cookies",
                      "how long it keeps the data": "I agree to the Terms of Use and Privacy Policy and understand Acme may "
                                                    "keep my data for 12 months"}.items():
    STILL_AGREED[f"a Create Account's terms box naming {_what}"] = lambda b=_label: bool(pipeline._terms_boxes(_box(b)))
    STILL_AGREED[f"an email-first site's agreement box naming {_what}"] = \
        lambda b=_label: bool(pipeline._consent_boxes(_box(b)))
for _what, _label in {"the cookie policy read": "I certify that the answers given are true and complete, and that I have "
                                                "read the Candidate Privacy Notice and Cookie Policy",
                      "the application retained": f"{CERTIFY}. I understand Acme will retain my application in line with "
                                                  "its records policy.",
                      "the information kept on file": f"{CERTIFY}, and I understand Acme may keep my information on file "
                                                      "for one year."}.items():
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
# other roles, which CLAUDE.md keeps narrow), a subscription in a Create Account's terms box, a cookie banner's agreement
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
