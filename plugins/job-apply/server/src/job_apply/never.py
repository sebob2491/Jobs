"""What the Job Desk never agrees to, ticks or presses for the person, whichever path asks (CLAUDE.md's "Rules the
product keeps"): a newsletter, job alerts, a talent community, marketing, texts or calls, being contacted or kept on
file for other roles, their data shared or kept, cookies (their own rule), a legal waiver or arbitration, the terms of
employment.

One list, by kind (NEVER), read by every path that agrees, ticks or presses for them through _never_for_them: a
notice, a terms gate and an attestation (settings.accept_notices), a Create Account's terms boxes, its privacy
statement and its consent to other positions (settings.manage_accounts), the live check's test identity, and a
remembered answer given at another employer. Each path kept a list of its own before, and they drifted apart (a
review's structure list, Oct 18): a word added here keeps every one of them from it. A path's exceptions are its own,
said where it reads (a long statement's text read only for what's ANYWHERE, say), never a word taken out here."""

from __future__ import annotations

import re
from collections.abc import Collection

# Texts and automated calls to the person (BrassRing's "consenting to Texting or automated calling", Edward Jones' run
# 83; the review of 0.3.165)
CALLS_OR_TEXTS = (r"\btexting\b|\btext (?:messag\w*|me|you)\b|\bcall(?:s|ing)? (?:or|and|&) text\w*\b|autodial\w*|"
                  r"\bautomated (?:calls?|calling|dial\w*|messag\w*|texts?)\b|pre-?recorded|\bwhatsapp\b")

# By kind, in the order a match is named for the log. Every word each path's own list held before is here (the tests
# keep those lists, word for word, and check it).
NEVER: dict[str, re.Pattern[str]] = {kind: re.compile(words, re.I | re.S) for kind, words in {
    # a legal waiver, however deep in a notice's words it sits: an arbitration agreement, a jury trial waived, a
    # non-compete (the review of 0.3.165)
    "arbitration": r"arbitrat",
    "waiver": r"waive|jury|non-?compete|non-?solicit",
    # the terms of employment, never a site's terms of use: an at-will statement, "the terms and conditions of my
    # employment", an employment agreement (BrassRing's "Employment At Will" step, Edward Jones' run 89)
    "at-will": r"\bat[- ]will\b|\b(?:terms|conditions)\b[^.]{0,30}\bof (?:my |your |this |the )?employment\b|"
               r"\bemployment (?:agreement|contract)\b",
    # a sign-up in so many words, however deep in a notice ("By clicking Accept you also join our Talent Community")
    "signs up": r"\b(?:join\w*|subscrib\w*|sign(?:s|ed|ing)? (?:you )?up|receiv\w*|enrol\w*|added to|opt(?:s|ed)? "
                r"(?:you )?in)\b.{0,60}?\b(?:talent (?:community|network|pool)|newsletters?|job alerts?|marketing|"
                r"promotion\w*)",
    "newsletter": r"newsletter|mailing list|subscri",
    "job alerts": r"job alerts?|alerts? by e-?mail|e-?mail alerts?|\balerts?\b|notify me|\bnotif|"
                  r"similar (?:jobs|openings|roles)",
    "talent community": r"talent (?:community|network|pool|database)|careers? (?:community|network)|"
                        r"stay (?:connected|in touch|informed)|keep me",
    "marketing": r"marketing|promotion|offers|campaigns?|"
                 r"\breceiv\w*\b.{0,40}\b(?:news\w*|offers|updates?|alerts?|promotions?|(?:job|career) opportunities|"
                 r"marketing)\b",
    # emails or updates beyond the application itself: "... and consent to receive email communications from Acme"
    # (the safety audit of Oct 12). Read with the words after it: "emails about my application" is no sign-up
    "emails or updates": r"\breceiv\w*\b.{0,40}\b(?:e-?mails?|communications?|updates?|messages?|news\w*|offers|"
                         r"information about|texts?)\b|\b(?:e-?mail|marketing|promotional) communications?\b|"
                         r"\bupdates? (?:about|on|from)\b",
    "texts or calls": CALLS_OR_TEXTS + r"|text messages?|\bsms\b|\btexts?\b|\bcalls?\b",
    "being contacted": r"contact(?:ed)? me|be contacted|send me|\be-?mail (?:me|you)\b|\bopt(?:ing)?[- ]in\b",
    "other roles": r"other (?:\w+ )?(?:roles|positions|jobs|openings|opportunities)|"
                   r"future (?:job |career )?(?:opportunities|openings|roles|positions)|future (?:\w+ )?vacanc",
    "their data shared": r"share (?:my|your)|affiliat|partners|staffing|recruit\w* agenc",
    "their data kept": r"keep\w* (?:my|your) (?:profile|information|data|resume|personal data)|retain (?:my|your)",
    "cookies": r"cookie",
    # a box's own words that take in more than the site's terms ("... and receive updates", "... with a third party",
    # "for future vacancies": the review of 0.3.117's wider account terms): read in full only where a box must say no
    # more than its terms (IN_PASSING)
    "more than the terms": r"receiv\w*|e-?mails?|updates?|notifications?|messages?|partners?|third[- ]part|future|"
                           r"talent pool|retain|retention",
}.items()}

# The kind every path but a Create Account's own terms boxes leaves out: words a statement, a certification or a
# paragraph names in passing ("you have received the Privacy Notice", "you'll receive an email to verify your account",
# "false statements may disqualify me from future opportunities"). Each kind they hold that signs the person up is
# read in its own kind too ("receive job alerts": job alerts)
IN_PASSING = frozenset({"more than the terms"})
# What a long notice's or page's words are read for wherever in them they sit: a legal waiver, and a sign-up in so many
# words. The rest of such words name the employer's practices (its affiliates, its marketing, its cookies: APS's privacy
# statement, live) and the other positions it may suggest (Micron's AI notice, live), so they're read where the notice
# says what it is (its heading, its opening)
ANYWHERE = frozenset({"arbitration", "waiver", "signs up"})


def _about_the_application(tail: str) -> bool:
    """Do the words after "receive emails" say they're about the application itself, and nothing more ("... about my
    application status", not "... about my application and other opportunities": the review of 0.3.126's)?"""
    own = re.match(r"\W*(?:about|regarding|on|concerning) (?:the status of )?(?:my|this|the|your) application"
                   r"(?: status| process)?(?P<rest>.*)", tail, re.I | re.S)
    if own is None:
        return False
    rest = own["rest"]
    return not re.match(r"\s*(?:and|or|&|as well)\b", rest, re.I) \
        and not re.search(r"other|opportunit|jobs|offers|roles|positions|newsletter|events", rest[:120], re.I)


def _never_for_them(text: str, *, only: Collection[str] | None = None, but: Collection[str] = ()) -> str | None:
    """The kind of thing (NEVER's) these words would have the person agree to that the desk never agrees to, ticks
    or presses for them, for the log; None when they ask for none of it. `only` reads just those kinds, `but` all the
    others: a path's own exceptions, said where it reads."""
    for kind, words in NEVER.items():
        if kind in but or only is not None and kind not in only:
            continue
        if kind == "emails or updates":
            if any(not _about_the_application(text[m.end():]) for m in words.finditer(text)):
                return kind
        elif words.search(text):
            return kind
    return None
