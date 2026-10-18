"""What the Job Desk never agrees to, ticks or presses for the person, whichever path asks (CLAUDE.md's "Rules the
product keeps"): a newsletter, job alerts, a talent community, marketing, texts or calls, being contacted or kept on
file for other roles, their data shared or kept, cookies (their own rule), a legal waiver or arbitration, the terms of
employment.

One list, by kind (NEVER), read by every path that agrees, ticks or presses for them through _never_for_them: a
notice, a terms gate and an attestation (settings.accept_notices), a Create Account's terms boxes, its privacy
statement and its consent to other positions (settings.manage_accounts), the live check's test identity, and a
remembered answer given at another employer. Each path kept a list of its own before, and they drifted apart (a
review's structure list, Oct 18): a word added here reaches every path that reads its kind.

Most kinds say what they are wherever they're read (PRECISE). A few hold words that are a sign-up only where one list
read them before 0.3.208 ("text", "partners", "notified" in the paragraph above a bare "I agree"; "receive" or "future"
in a Create Account box's own words), and are read only there (the review of 0.3.208: read everywhere, they refused
"the text box below", "our background screening partners"). Which kinds a path reads, and its exceptions, are said
where it reads."""

from __future__ import annotations

import re
from collections.abc import Collection

# Texts and automated calls to the person (BrassRing's "consenting to Texting or automated calling", Edward Jones' run
# 83; the review of 0.3.165)
CALLS_OR_TEXTS = (r"\btexting\b|\btext (?:messag\w*|me|you)\b|\bcall(?:s|ing)? (?:or|and|&) text\w*\b|autodial\w*|"
                  r"\bautomated (?:calls?|calling|dial\w*|messag\w*|texts?)\b|pre-?recorded|\bwhatsapp\b")

# The kinds read only where their words were read before 0.3.208 (each path says which it reads)
# - a Create Account box's own words that take in more than the site's terms ("... and receive updates", "... with a
#   third party", "for future vacancies": the review of 0.3.117's wider account terms): its terms boxes and an
#   email-first site's agreement boxes, which must say no more than the terms
BOX_WORDS = "more than the terms"
# - the words of a paragraph that make the bare "I agree" under it a sign-up (the review of 0.3.126's and 0.3.133's):
#   alone, elsewhere, they're ordinary words ("the text box below", "call my references", "notify Acme of any change",
#   "our background screening partners", "how we share your personal information", "Example Staffing", "keeping your
#   personal data secure", "the future vacancies we fill")
PARAGRAPH_WORDS = "a paragraph's sign-up words"
# - the future roles a person is kept on file for, which a certification names as what false statements cost
#   ("... may disqualify me from future opportunities"): read in an agreement's or a notice's words, as before
FUTURE_ROLES = "future roles"
# - what a sign-up sends, beyond the application itself: communications, updates, news, offers ("... and consent to
#   receive email communications from Acme", "... and would like to receive updates": the safety audit of Oct 12), as an
#   attestation and an account's box read them, and the words above a bare "I agree"; a notice's or an agreement's
#   words say so in passing ("you consent to receive communications electronically")
EMAILS = "emails or updates"
# - an email, a message or a text received, beyond the application itself ("... and agree to receive emails"), as an
#   attestation and an account's box read them. Not above a bare "I agree", whose paragraph names the account's own
#   ("You will receive an email to verify your account", "... a one-time passcode by email", "... an email with your
#   username": the reviews of 0.3.133's and 0.3.208)
MAIL = "an email or a message"
# (both read with the words after them: "emails about my application" is no sign-up)

# By kind, in the order a match is named. Every word each path's own list held before is here (the tests keep those
# lists, word for word, and check each path still refuses them)
NEVER: dict[str, re.Pattern[str]] = {kind: re.compile(words, re.I | re.S) for kind, words in {
    # a legal waiver, however deep in a notice's words it sits: an arbitration agreement, a jury trial waived, a
    # non-compete (the review of 0.3.165). Whole words: the agreements' and notices' own list read "waive" and "jury"
    # inside any word before 0.3.208, which found only "injury" and "perjury" ("waiver", "waived" begin with it), and
    # that reach was dropped on purpose (Oct 18): a notice of a work-related injury, or a certification under penalty
    # of perjury, is no waiver
    "arbitration": r"arbitrat",
    "waiver": r"\bwaive|\bjury\b|non-?compete|non-?solicit",
    # the terms of employment, never a site's terms of use: an at-will statement, "the terms and conditions of my
    # employment", an employment agreement (BrassRing's "Employment At Will" step, Edward Jones' run 89)
    "at-will": r"\bat[- ]will\b|\b(?:terms|conditions)\b[^.]{0,30}\bof (?:my |your |this |the )?employment\b|"
               r"\bemployment (?:agreement|contract)\b",
    # a sign-up in so many words, however deep in a notice ("By clicking Accept you also join our Talent Community")
    "signs up": r"\b(?:join\w*|subscrib\w*|sign(?:s|ed|ing)? (?:you )?up|receiv\w*|enrol\w*|added to|opt(?:s|ed)? "
                r"(?:you )?in)\b.{0,60}?\b(?:talent (?:community|network|pool)|newsletters?|job alerts?|marketing|"
                r"promotion\w*)",
    "newsletter": r"newsletter|mailing list|subscri",
    "job alerts": r"job alerts?|alerts? by e-?mail|e-?mail alerts?|notify me|similar (?:jobs|openings|roles)",
    "talent community": r"talent (?:community|network|pool)|stay informed|keep me",
    "marketing": r"marketing|promotion|campaigns?",
    "offers": r"offers",
    EMAILS: r"\breceiv\w*\b.{0,40}\b(?:communications?|updates?|news\w*|offers)\b|"
            r"\b(?:e-?mail|marketing|promotional) communications?\b|\bupdates? (?:about|on|from)\b",
    MAIL: r"\breceiv\w*\b.{0,40}\b(?:e-?mails?|messages?|information about|texts?)\b",
    "texts or calls": CALLS_OR_TEXTS + r"|text messages?|\bsms\b",
    "being contacted": r"contact(?:ed)? me|be contacted|send me",
    "other roles": r"other (?:\w+ )?(?:roles|positions|jobs|openings|opportunities)",
    FUTURE_ROLES: r"future (?:job |career )?(?:opportunities|openings|roles|positions)",
    "their data shared": r"share my|affiliat",
    "their data kept": r"keep (?:my|your) (?:profile|information|data|resume)|retain (?:my|your)",
    "cookies": r"cookie",
    PARAGRAPH_WORDS: r"\btexts?\b|\bcalls?\b|\balerts?\b|\bnotif(?:y|ied|ications?)\b|partners|staffing|share your|"
                     r"recruit\w* agenc|\be-?mail (?:me|you)\b|\bopt(?:ing)?[- ]in\b|stay (?:connected|in touch)|"
                     r"\breceiv\w*\b.{0,40}\b(?:news\w*|offers|updates?|alerts?|promotions?|(?:job|career) "
                     r"opportunities|marketing)\b|talent database|careers? (?:community|network)|"
                     r"future (?:\w+ )?vacanc|keep\w* (?:my|your) (?:personal )?data|"
                     # a sign-up's mail in plain words ("You will receive emails about new jobs", "... a monthly email
                     # digest of new openings": the last check of 0.3.208), never the account's or the application's
                     r"\b(?:e-?mails?|digest|messages?)\b[^.]{0,40}\b(?:new (?:jobs|openings|positions|roles)|"
                     r"openings|opportunities|events)\b",
    BOX_WORDS: r"receiv\w*|e-?mails?|updates?|notifications?|messages?|partners?|third[- ]part|future|talent pool|"
               r"retain|retention",
}.items()}

# The kinds whose words say what they are wherever they're read: every path that agrees for the person reads these
PRECISE = frozenset(NEVER) - {BOX_WORDS, PARAGRAPH_WORDS, FUTURE_ROLES, EMAILS, MAIL}
# What a long notice's or page's words are read for wherever in them they sit: a legal waiver, and a sign-up in so many
# words. The rest of such words name the employer's practices (its affiliates, its marketing, its cookies: APS's privacy
# statement, live) and the other positions it may suggest (Micron's AI notice, live), so they're read where the notice
# says what it is (its heading, its opening)
ANYWHERE = frozenset({"arbitration", "waiver", "signs up"})


def _about_the_application(tail: str) -> bool:
    """Do the words after "receive emails" say they're about the application itself, and nothing more ("... about my
    application status", not "... about my application and other opportunities": the review of 0.3.126's)? Mail of
    another kind may come first ("... emails and updates about my application": the last check of 0.3.208)"""
    own = re.match(r"(?:\W*(?:and|or|&) (?:e-?mails?|updates?|messages?|communications?|notifications?)\b)*"
                   r"\W*(?:about|regarding|on|concerning) (?:the status of )?(?:my|this|the|your) application"
                   r"(?: status| process)?(?P<rest>.*)", tail, re.I | re.S)
    if own is None:
        return False
    rest = own["rest"]
    return not re.match(r"\s*(?:and|or|&|as well)\b", rest, re.I) \
        and not re.search(r"other|opportunit|jobs|offers|roles|positions|newsletter|events", rest[:120], re.I)


def _never_for_them(text: str, *, only: Collection[str] | None = None, but: Collection[str] = ()) -> str | None:
    """The first kind (NEVER's) these words hold of what the desk never agrees to, ticks or presses for the person, or
    None. The paths ask only whether there's one; the kind is what the tests hold each path to, kind by kind. `only`
    reads just those kinds, `but` all the others: each path says which it reads, and its own exceptions."""
    for kind, words in NEVER.items():
        if kind in but or only is not None and kind not in only:
            continue
        if kind in (EMAILS, MAIL):
            if any(not _about_the_application(text[m.end():]) for m in words.finditer(text)):
                return kind
        elif words.search(text):
            return kind
    return None
