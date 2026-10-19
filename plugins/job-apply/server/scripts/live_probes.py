"""The live check's weekly probes: two desk paths the nightly rarely or never takes (a code review's leftover list,
Oct 18), as the test identity (live_identity.py; TEST_IDENTITY.md), which never submits.

    uv run python scripts/live_probes.py --out live-probes [--probes reset,tailored] [--companies "KLA"]
    uv run python scripts/live_probes.py --out live-probes --report-only   # the report again, from what's there

- reset: with settings.manage_accounts the desk resets a saved password the site refuses, through the site's own
  "Forgot password" and its emailed link (pipeline_accounts.py's _ask_for_reset, _set_new_password). The nightly takes
  that path only when a site happens to refuse. At RESET_TENANTS (two Workday tenants where the test identity has an
  account), two passes a tenant, each a live_smoke.py run of its own (its own home, browser and sign-ins):
    1. Workday's saved password is derived_password(the site password), which the site refuses: the desk asks for
       a reset, reads the email, sets the derived password and signs in;
    2. the real site password, refused in turn: the desk resets it back, which leaves the site as the nightly
       found it. Pass 2 runs whatever pass 1 did, unless pass 1 shows the site's password was never changed.
  Each pass says: refused, reset asked for, email read, new password set, signed in, reached the application. A
  CAPTCHA or a security question on the way stops it there (the desk's rules: the person's, never worked around:
  no --security-answers here) and is said.
- tailored: the desk prefers a tailored resume in the job's folder over the profile's (autofill.tailored_document,
  render_document's PDFs, the desk's tailoring switch). At TAILORED_EMPLOYERS, live_smoke.py --tailored-resume
  drops a clearly marked tailored test resume (TAILORED_NAME, from the run's fake profile only) into the job's
  folder; the review page must show it, not the profile's (live_identity.RESUME_NAME).

Each probe ends "pass", "fail" or "unchecked" (couldn't check this week). Only a desk failure is "fail": the site
refused the saved password and the desk didn't ask for a reset, read the email, set the password or sign in after it,
with nothing of the site's in the way; the review page showing the profile's resume (or none) instead of the tailored
one; and, always, a pass 2 that didn't restore the real password, whatever stopped it, as the tenant may be left on
the derived one. What the site or the person stops a run with (a bot check, a CAPTCHA, a security question, a site's
error page, no posting found: the live check's own outcome words, live_compare.pipeline_outcome's) is "unchecked",
and so is anything else that ends a run short of what a probe looks at.

The runs go one after another (one inbox), in the live-nightly workflow's "probes" check (its concurrency group is
the nightly's). Everything printed and written has the test identity's email and passwords masked, the derived one
too. Writes probes.json and report.md in --out, beside each run's own folder (reset-<employer>-<pass>,
tailored-<employer>), and prints a LIVE_WEEKLY line of JSON a probe (nightly_summary.py --probes reads them).
Exit 1 when a probe failed (the desk's), else 0 (one that couldn't check is said, and isn't a failure); 2 when
nothing could run.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import functools
import json
import os
import re
import signal
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_compare  # (scripts/live_compare.py, beside this one: the live check's outcome words)
from live_compare import slug  # (an employer's folder in a run's)
import live_identity  # (scripts/live_identity.py, beside this one)

SMOKE = Path(__file__).with_name("live_smoke.py")

# Two Workday tenants where the test identity has its account, and the desk signs in to it with the saved password
# at the first try (no Create Account on the way), reaching the review page run after run, with no CAPTCHA or bot
# check anywhere in their history: the nightly's accounts check, runs 124 (Oct 17) and 135 (Oct 18), and the trend
# issue's last runs (RRRRRR for both). Each pass is a full application to the review page (never submitted).
RESET_TENANTS: tuple[dict[str, str], ...] = (
    {"name": "KLA", "why": "Workday (kla.wd1): signs in with the saved password at the first try every night, "
                           "and mails from the tenant's own domain (workday.kla.com), which the desk's sender check "
                           "has a rule for"},
    {"name": "Hitachi High-Tech America", "why": "Workday (hitachi.wd1): a second tenant that signs in at the first "
                                                 "try every night, its mail from Workday's own senders"},
)
# Employers whose review page shows the file the application took: Greenhouse's (the form itself, a fresh upload
# every application), and NXP Semiconductors' Workday (nxp.wd3, the test identity's account: "Resume/CV <file>
# successfully uploaded"), whose account keeps the resume of its last application: the desk replaces it with the one
# tailored for the job (0.3.209), so the review page must show the tailored file there too
TAILORED_EMPLOYERS: tuple[dict[str, str], ...] = (
    {"name": "ASM (ASM America)", "why": "Greenhouse: no account, a fresh upload every application, and its review "
                                         "page is the filled form, the attached file's name on it"},
    {"name": "NXP Semiconductors", "why": "Workday: its account keeps the resume of its last application, which "
                                          "the desk replaces with the tailored one (0.3.209)"},
)
PROBES = ("reset", "tailored")

# --- the derived password ------------------------------------------------------------------------------------------

DERIVED_SUFFIX = "Rp7!"  # a capital, a small letter, a digit and a symbol: what Workday's password rules ask for
OTHER_SUFFIX = "Qz4#"  # (for a site password that already ends in DERIVED_SUFFIX at the longest length)
PASSWORD_MIN, PASSWORD_MAX = 8, 64
_REFUSED_CHARACTERS = re.compile(r"[\s'\"\\]")  # (TEST_IDENTITY.md: some job sites refuse spaces, quotes, backslashes)


def password_problems(password: str) -> list[str]:
    """What a job site's password rules (Workday's: 8 characters or more, a capital, a small letter, a digit and a
    symbol) would refuse in this password, as words; none when it meets them. Never says the password."""
    out = []
    if len(password) < PASSWORD_MIN:
        out.append(f"shorter than {PASSWORD_MIN} characters")
    if len(password) > PASSWORD_MAX:
        out.append(f"longer than {PASSWORD_MAX} characters")
    for what, pattern in (("capital letter", r"[A-Z]"), ("small letter", r"[a-z]"), ("digit", r"\d"),
                          ("symbol", r"[^A-Za-z0-9\s]")):
        if not re.search(pattern, password):
            out.append(f"no {what}")
    if _REFUSED_CHARACTERS.search(password):
        out.append("a space, a quote or a backslash (some job sites refuse those)")
    return out


def derived_password(site: str) -> str:
    """The reset probe's first pass's saved password: the site password with DERIVED_SUFFIX added (cut to
    PASSWORD_MAX, its own suffix kept), never the site password itself, the same every run. ValueError for none."""
    site = site.strip()
    if not site:
        raise ValueError("no site password to derive one from")
    for suffix in (DERIVED_SUFFIX, OTHER_SUFFIX):
        derived = site[:PASSWORD_MAX - len(suffix)] + suffix
        if derived != site:
            return derived
    raise AssertionError("unreachable: the two suffixes differ")


def derived_env(site: str) -> dict[str, str]:
    """The environment that makes derived_password(site) Workday's saved password for a run (config.get_secret's
    JOB_APPLY_SECRET_WORKDAY_PASSWORD, ahead of secrets.yaml): only Workday's, as the probe's tenants are Workday's."""
    return {live_identity._env_key("workday_password"): derived_password(site)}


def secrets_of(env: Mapping[str, str]) -> tuple[str, list[str]]:
    """The test identity's email, and every password a probe's runs use (the derived one too), as the masker takes
    them."""
    email, inbox, site = live_identity.credentials(env)
    passwords = [site, inbox, *(env.get(k, "") for k in live_identity.ENV_VARS[1:])]
    if site:
        passwords.append(derived_password(site))
    return email, [p for p in passwords if p]


# --- one reset pass, from its run's record --------------------------------------------------------------------------

STEPS = ("refused", "reset_asked", "email_read", "password_set", "signed_in", "reached_application")
STEP_WORDS = {"refused": "refused", "reset_asked": "reset asked for", "email_read": "email read",
              "password_set": "new password set", "signed_in": "signed in",
              "reached_application": "reached the application"}
# (the desk's own log lines, pipeline.py's words)
_PRESSED = re.compile(r"^pressed \u201c[^\u201d]+\u201d with your saved password")
_REFUSED = re.compile(r"saved password,? (?:which )?didn't sign in", re.I)
_ASKED = re.compile(r"so I asked it to email a password reset")
_EMAIL_READ = re.compile(r"^opened the password reset link from your email")
_SET = re.compile(r"^set your saved password as .+'s new one")
_ACCOUNT_PRESSED = re.compile(r"^pressed \u201c[^\u201d]+\u201d to (?:create|finish creating) your account")
# a step of the application itself, past the sign-in
_APPLICATION = re.compile(r"^(?:filled \d+ (?:field|answer)|clicked \u201c|reached the review page|ticked \u201c|"
                          r"picked \u201c|chose \u201c|left \d+ |answered )")
# as far as a run goes ("ready", "needs_you your_submit"): the person's own Submit is next
_REVIEW = live_compare.REACHED
_SIGN_IN_NEEDS = {"sign_in", "email_code", "bot_check"}
# What the site or the person stops a run with, never the desk's to get past: the live check's outcomes that are the
# site's alone (no posting found, a site down for maintenance, a bot check), and a stop whose reason (or, for a CAPTCHA
# or a security question met on the way, a log line) says it's the site's or the person's
_THE_SITES = {"no postings", live_compare.SITE_DOWN, "needs_you bot_check"}
_THE_PERSONS = re.compile(r"captcha|picture code|security questions?|bot check|checking that you're a person", re.I)
_SITE_ERROR = re.compile(r"something went wrong|web server's error page|site's own trouble|turned the desk's browser "
                         r"away|down for maintenance|didn't finish loading|\b5\d\d (?:bad gateway|service unavailable|"
                         r"gateway time-?out|internal server error)\b", re.I)
_MET = re.compile(r"captcha|picture code|security questions?", re.I)
# a site's words refusing a password it had before (a password history rule)
_REUSED = re.compile(r"\bprevious(?:ly)?\b|\bre-?use|\bused (?:before|recently)\b|\bhistory\b|\brecent passwords?\b|"
                     r"\bsame as (?:your )?(?:old|current|previous)\b", re.I)


def _last_round(rec: Mapping[str, Any] | None) -> dict[str, Any]:
    rounds = (rec or {}).get("rounds") or []
    return rounds[-1] if rounds and isinstance(rounds[-1], dict) else {}


def ended(rec: Mapping[str, Any] | None) -> str:
    """Where a run ended: its last round's status and need ("ready", "needs_you sign_in"), or why it has none."""
    if not rec:
        return "no record (the run didn't finish)"
    if rec.get("crash"):
        return "crash"
    last = _last_round(rec)
    if not last:
        return str(rec.get("note") or "no rounds")
    return f"{last.get('status') or ''} {last.get('need') or ''}".strip()


def outcome_words(rec: Mapping[str, Any] | None) -> str:
    """Where a run ended, in the live check's own words (live_compare.pipeline_outcome: "needs_you bot_check · <title>
    · <the desk's reason>", "no postings · ...", "crash · ...")."""
    if not rec:
        return "no record (the run didn't finish)"
    result = live_compare.pipeline_outcome(dict(rec))
    return live_compare._join(result.outcome, result.detail)


def blocked_by(rec: Mapping[str, Any] | None) -> str:
    """What of the site's or the person's stopped this run, in the live check's own words (outcome_words), or "":
    no posting found, a site down, a bot check; a stop whose reason is a CAPTCHA, a security question or a site's error
    page ("Something went wrong", a 5xx); or a CAPTCHA or a security question the desk met on the way and turned from
    (a reset's picture code: it went back to make an account instead), short of the review page."""
    if not rec or rec.get("crash"):
        return ""
    outcome = live_compare.pipeline_outcome(dict(rec)).outcome
    if outcome in _REVIEW:
        return ""
    last = _last_round(rec)
    reason = str(last.get("reason") or "")
    if outcome in _THE_SITES or outcome.startswith("needs_you") and (_THE_PERSONS.search(reason)
                                                                     or _SITE_ERROR.search(reason)):
        return outcome_words(rec)
    met = next((str(line) for line in last.get("log") or [] if _MET.search(str(line))), "")
    return f"{outcome_words(rec)} (on the way: {met[:200]})" if met else ""


def reset_steps(rec: Mapping[str, Any] | None) -> dict[str, Any]:
    """One reset pass from its run's record (live_smoke.py's LIVE_PIPELINE record): each of STEPS true or false, from
    the desk's own log lines, and "how_far" ("review page", "application" or ""), "pressed" (a sign-in sent with the
    saved password), "account_pressed" (a Create Account pressed on the way), "blocked" (blocked_by), "outcome"
    (outcome_words), "ended" and "said" (its last words)."""
    last = _last_round(rec)
    log = [str(x) for x in last.get("log") or []]
    reason = str(last.get("reason") or "")
    status = f"{last.get('status') or ''} {last.get('need') or ''}".strip()
    at = next((i for i, line in enumerate(log) if _SET.search(line)), None)
    pressed = next((i for i, line in enumerate(log) if _PRESSED.search(line)), None)
    refused = any(_REFUSED.search(line) for line in log) or bool(_REFUSED.search(reason))
    # past the sign-in: after the new password was set, or (no reset) after the first sign-in the site took
    since = at if at is not None else pressed if pressed is not None and not refused else None
    later = log[since + 1:] if since is not None else []
    stepped = any(_APPLICATION.search(line) for line in later)
    crashed = bool((rec or {}).get("crash")) or not rec
    out_of_sign_in = not crashed and bool(last) and (status in _REVIEW or last.get("status") == "needs_you"
                                                     and last.get("need") not in _SIGN_IN_NEEDS)
    signed_in = since is not None and (stepped or out_of_sign_in)
    how_far = "review page" if status in _REVIEW and signed_in else "application" if signed_in else ""
    return {"refused": refused, "reset_asked": any(_ASKED.search(line) for line in log),
            "email_read": any(_EMAIL_READ.search(line) for line in log), "password_set": at is not None,
            "signed_in": signed_in, "reached_application": bool(how_far), "how_far": how_far,
            "pressed": pressed is not None, "account_pressed": any(_ACCOUNT_PRESSED.search(line) for line in log),
            "blocked": blocked_by(rec), "outcome": outcome_words(rec), "crashed": crashed, "ended": ended(rec),
            "said": str((rec or {}).get("crash") or reason or (rec or {}).get("note") or "")[:300]}


def pass_two_needed(first: Mapping[str, Any]) -> bool:
    """Whether the second pass (the real password, which resets the site back) has to run: always, but where pass 1
    shows the site's password was never changed (refused, and the desk never opened a reset's link, set a password or
    pressed a Create Account, nor did the run break part way)."""
    return not (first["refused"] and not first["email_read"] and not first["password_set"]
                and not first["account_pressed"] and not first["crashed"])


def may_have_changed(first: Mapping[str, Any]) -> bool:
    """Whether the site may be on the derived password after pass 1: it opened a reset's link, set a password, pressed
    a Create Account or broke part way, or a sign-in with the derived password wasn't refused (the site took it, or
    something stopped the run before it said)."""
    return bool(first["email_read"] or first["password_set"] or first["account_pressed"] or first["crashed"]
                or first["pressed"] and not first["refused"])


def left_on(first: Mapping[str, Any], second: Mapping[str, Any] | None) -> str:
    """The password a tenant is left on after the probe: "real" (pass 2 set it back or signed in with it, or pass 1
    proved it never changed), "unknown" (pass 2 said it set the real one back, and never signed in with it after that:
    refused, or the run stopped first; the site may still be on the derived one), "as found" (this run changed nothing:
    pass 1 never got to send the derived password, and pass 2 couldn't say) or "derived?" (maybe the derived one)."""
    if second is not None and second["password_set"] and not second["signed_in"]:
        # (run 136's pass 2 at KLA: the reset email it read was pass 1's, its link spent; the desk said it set the
        # real password, the sign-in after it was refused, and the site was reported left on the real one)
        return "unknown"
    if second is not None and (second["password_set"] or second["signed_in"] and not second["refused"]):
        return "real"
    if not pass_two_needed(first):
        return "real"
    return "derived?" if may_have_changed(first) else "as found"


def pass_status(steps: Mapping[str, Any]) -> tuple[str, str]:
    """One pass: ("pass", "") with every step; ("fail", why) where the site refused the saved password and the desk
    went no further, nothing of the site's or the person's in the way; else ("unchecked", why): what of the site's or
    the person's stopped it (the live check's words), or that it never got to a reset (the site took the password, or
    the run ended before the sign-in)."""
    if all(steps[k] for k in STEPS):
        return "pass", ""
    if steps["blocked"]:
        return "unchecked", steps["blocked"]
    if steps["refused"]:
        missing = next(STEP_WORDS[k] for k in STEPS[1:] if not steps[k])
        return "fail", (f"the site refused the saved password, and the desk didn't get to \u201c{missing}\u201d, with "
                        f"nothing of the site's in the way: {steps['outcome']}")
    if steps["signed_in"]:
        return "unchecked", "the site took the saved password, so there was nothing to reset"
    return "unchecked", f"it ended before the site refused the saved password: {steps['outcome']}"


def reset_result(tenant: Mapping[str, str], first_rec: Mapping[str, Any] | None,
                 second_rec: Mapping[str, Any] | None, second_ran: bool) -> dict[str, Any]:
    """A tenant's reset probe: its passes' steps and statuses, its own status ("fail" if a pass failed, or always when
    pass 2 didn't restore the real password where pass 1 may have changed it; "pass" with both passes whole; else
    "unchecked"), where the site's password is left, and the notes and warning to say."""
    first = reset_steps(first_rec)
    second = reset_steps(second_rec) if second_ran else None
    name = tenant["name"]
    notes: list[str] = []
    first_status = pass_status(first)
    second_status = pass_status(second) if second is not None else ("not run", "")
    if first_status[0] == "unchecked" and not first["refused"] and first["signed_in"]:
        first_status = ("unchecked", "the site took the derived password: it was on it already (an earlier probe's "
                                     "pass 2 didn't finish), so nothing was reset; pass 2 sets the real one back")
    if second is not None and not second["refused"] and second["signed_in"] and first["password_set"]:
        second_status = ("fail", "pass 1 said it set the derived password, yet the site took the real one in pass 2")
    if second is None:
        notes.append("pass 2 not run: pass 1 never opened a reset's link, so the site's password was never changed"
                     if not pass_two_needed(first) else "pass 2 didn't run (the probe was stopped before it)")
    left = left_on(first, second)
    warning = ""
    if left == "unknown" and second is not None:  # (always: left_on's "unknown" is pass 2's)
        after =("the run broke off before any sign-in" if second["crashed"] else
                 f"it ended {second['ended']}")
        warning = (f"Pass 2 at {name} said it set the real password back through the site's reset, and never signed "
                   f"in with it after that ({after}): the site may not have taken it, so the test identity's Workday "
                   "account there may still be on the derived password. The nightly's own reset may repair it: the "
                   "site refuses the real saved password, and the desk resets it to that through the site's "
                   "\u201cForgot password\u201d email.")
    elif left == "derived?":
        warning = (f"Pass 2 failed at {name}: the test identity's Workday account there may be left on the derived "
                   "password. The nightly's own reset will repair it: the site refuses the real saved password, and "
                   "the desk resets it to that through the site's \u201cForgot password\u201d email.")
    elif left == "as found":
        notes.append("this run never sent the derived password, so it changed nothing there")
    if warning and second is not None and _REUSED.search(second["said"]):
        warning += (" But the site's words say it won't take a password it had before, so that reset may be "
                    "refused the same way: look at it before the next nightly.")
    passes = [{"pass": 1, "saved": "derived", "ran": True, **first, "status": first_status[0],
               "status_why": first_status[1]},
              {"pass": 2, "saved": "real", "ran": second is not None, **(second or {}), "status": second_status[0],
               "status_why": second_status[1]}]
    statuses = {first_status[0], second_status[0]}
    status = ("fail" if warning or "fail" in statuses else "pass" if statuses == {"pass"} else "unchecked")
    return {"probe": "reset", "employer": name, "why": tenant.get("why", ""), "status": status, "left_on": left,
            "warning": warning, "notes": notes, "passes": passes}


# --- the tailored resume --------------------------------------------------------------------------------------------

TAILORED_NAME = "Jobdesk_Test_Tailored_Probe_Resume.pdf"  # (its stem ends in "resume": autofill.tailored_document's)


def tailored_name(day: datetime.date | None = None) -> str:
    """The tailored test resume's name for a run: a new one each day
    ("Jobdesk_Test_Tailored_Probe_20261018_Resume.pdf"), so a Workday account that kept last week's (NXP's) is told from
    this week's, and the review page showing the name means the desk put this one in, not that it left last week's (the
    kept-resume review of Oct 18)."""
    return f"{Path(TAILORED_NAME).stem[:-len('_Resume')]}_{(day or datetime.date.today()):%Y%m%d}_Resume.pdf"
TAILORED_NOTE = ("TAILORED TEST RESUME, made by the Job Desk's weekly probe (scripts/live_probes.py) to see that the "
                 "application takes it. Not a real applicant; nothing in it is true.")
_FILE_NAME = re.compile(r"[\w().-]*\w\.(?:pdf|docx?|rtf|txt)\b", re.I)


def tailored_markdown(profile: Mapping[str, Any], title: str = "", company: str = "") -> str:
    """The tailored test resume, in render_document's layout, from the run's fake profile only (its name, its 555 phone,
    its city, its jobs and schools; never an email address), clearly marked as a test."""
    personal = profile.get("personal") or {}
    address = personal.get("address") or {}
    name = " ".join(str(personal.get(k) or "") for k in ("first_name", "last_name")).strip() or "Jobdesk Test"
    contact = " | ".join(str(p) for p in (personal.get("phone"), ", ".join(
        str(x) for x in (address.get("city"), address.get("state")) if x)) if p)
    lines = [f"# {name}", contact, "", f"**{TAILORED_NOTE}**", ""]
    if title or company:
        lines += [f"Tailored for: {', '.join(x for x in (title, company) if x)}", ""]
    lines += ["## Experience", ""]
    for job in profile.get("work_history") or []:
        if isinstance(job, dict):
            lines.append(f"### {job.get('title')} \u2014 {job.get('company')}, {job.get('location')} "
                         f"*{job.get('start')} \u2013 {job.get('end')}*")
            if job.get("description"):
                lines.append(f"- {job['description']}")
            lines.append("")
    lines += ["## Education", ""]
    for school in profile.get("education_history") or []:
        if isinstance(school, dict):
            lines += [f"### {school.get('degree')}, {school.get('major')} \u2014 {school.get('school')} "
                      f"*{school.get('start')} \u2013 {school.get('end')}*", ""]
    return "\n".join(lines).rstrip() + "\n"


def _flat(text: str) -> str:
    """A file name or a page's words as a site may show a name: any letter case, "_", "-" and spaces alike."""
    return re.sub(r"[\s_-]+", "-", text.lower())


def files_shown(text: str) -> list[str]:
    """The file names a page's words show (each once, in order)."""
    return list(dict.fromkeys(_FILE_NAME.findall(text)))[:8]


def tailored_check(text: str, tailored: str = TAILORED_NAME,
                   profiles: str = live_identity.RESUME_NAME) -> dict[str, Any]:
    """Which resume the review page's words show: "pass" when the tailored one and not the profile's, else "fail"
    with what it shows instead (the profile's, both, or neither). By the names' stems, in any letter case, with "_",
    "-" and spaces alike."""
    flat = _flat(text)
    has_tailored = _flat(Path(tailored).stem) in flat
    has_profiles = _flat(Path(profiles).stem) in flat
    shown = files_shown(text)
    if has_tailored and not has_profiles:
        return {"verdict": "pass", "shown": shown, "detail": f"the review page shows {tailored}"}
    if has_tailored:
        detail = f"the review page shows both: {tailored} and the profile's {profiles}"
    elif has_profiles:
        detail = f"the review page shows the profile's {profiles}, not the tailored {tailored}"
    else:
        detail = f"the review page shows neither {tailored} nor the profile's {profiles}" + (
            f" (its files: {', '.join(shown)})" if shown else "")
    return {"verdict": "fail", "shown": shown, "detail": detail}


def tailored_result(employer: Mapping[str, str], rec: Mapping[str, Any] | None, review_text: str) -> dict[str, Any]:
    """An employer's tailored-resume probe, from its run's record (with live_smoke.py's "tailored": the file dropped,
    which file the desk's chooser takes, or why there's none) and the words of the page it ended on. "fail" where the
    desk's chooser takes another file or the review page shows the profile's resume (or none) in place of the
    tailored one; "unchecked" where the run never got the file made or never reached the review page (in the live
    check's own words, blocked_by's where it's the site's or the person's)."""
    made = (rec or {}).get("tailored") or {}
    out: dict[str, Any] = {"probe": "tailored", "employer": employer["name"], "why": employer.get("why", ""),
                           "file": made.get("file") or TAILORED_NAME, "ended": ended(rec)}
    if made.get("error") and "picked" in made:  # (made, and the desk's own chooser would take another file)
        return {**out, "status": "fail", "verdict": "fail", "shown": [], "detail": made["error"]}
    if not made or made.get("error"):
        why = made.get("error") or f"the run never got to make it: {outcome_words(rec)}"
        return {**out, "status": "unchecked", "verdict": "unchecked", "shown": [],
                "detail": f"no tailored resume in the job's folder ({why})"}
    check = tailored_check(review_text, out["file"])
    if ended(rec) not in _REVIEW:
        return {**out, "status": "unchecked", "verdict": "unchecked", "shown": check["shown"],
                "detail": f"didn't reach the review page: {blocked_by(rec) or outcome_words(rec)}"}
    return {**out, "status": check["verdict"], **check}


# --- the runs -----------------------------------------------------------------------------------------------------

PASS_MINUTES = 20  # a run's most (live_smoke.py's own limit is longer: a run past this is stopped)


def folder_of(probe: str, employer: str, n: int | None = None) -> str:
    """A run's own folder in the probes' --out: reset-kla-1, tailored-asm-asm-america."""
    return f"{probe}-{slug(employer)}" + (f"-{n}" if n else "")


def run_args(employer: str, out: Path, *, derived: bool = False, tailored: bool = False) -> list[str]:
    """live_smoke.py's arguments for one run: the pipeline as the test identity at this one employer (--everywhere, so
    any employer on the plugin's lists; --companies matches it alone: tests/test_live_probes.py checks), never with
    --security-answers (a security question stops a probe), and --derived-password or --tailored-resume as asked."""
    return ["--pipeline", "--test-identity", "--everywhere", "--companies", employer, "--out", str(out),
            *(["--derived-password"] if derived else []), *(["--tailored-resume"] if tailored else [])]


def _group() -> dict[str, Any]:
    return {"start_new_session": True} if os.name == "posix" else {}


def _stop(proc: Any) -> None:
    """A run past its time stopped, its browser with it (its own process group, where there is one)."""
    with contextlib.suppress(ProcessLookupError, OSError):
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()


async def run_one(args: list[str], folder: Path, minutes: float = PASS_MINUTES) -> dict[str, Any]:
    """One live_smoke.py run, its lines passed on as they come (each already masked by the run itself), its outcome
    written in its folder (outcome.json: the exit code, or that it was stopped at its time)."""
    folder.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "JOB_APPLY_NEVER_SUBMIT": "1"}
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(sys.executable, str(SMOKE), *args, env=env, limit=2 ** 24,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                **_group())

    async def relay() -> int:
        assert proc.stdout is not None
        while line := await proc.stdout.readline():
            sys.stdout.write(line.decode(errors="replace"))
            sys.stdout.flush()
        return await proc.wait()

    try:
        outcome: dict[str, Any] = {"exit": await asyncio.wait_for(relay(), minutes * 60)}
    except asyncio.TimeoutError:
        _stop(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 30)
        outcome = {"exit": None, "stopped": f"still running after {minutes:g} minutes, so it was stopped"}
    outcome["seconds"] = round(time.monotonic() - started, 1)
    (folder / "outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
    return outcome


def read_record(folder: Path, employer: str) -> dict[str, Any] | None:
    """The employer's record in a run's folder (its pipeline.json), or one saying why there is none."""
    try:
        records = json.loads((folder / "pipeline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        records = None
    if isinstance(records, list):
        mine = [r for r in records if isinstance(r, dict) and r.get("company") == employer]
        if mine:
            return mine[0]
    try:
        outcome = json.loads((folder / "outcome.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    said = outcome.get("stopped") or f"the run ended (exit {outcome.get('exit')}) with no record of {employer}"
    return {"company": employer, "crash": said}


def review_text(folder: Path, employer: str) -> str:
    """The words of the page a run ended on (its saved pipeline/<employer>/ pages, a frame's too), as live_audit.py
    reads a review page."""
    from live_audit import page_text

    pages = sorted((folder / "pipeline" / slug(employer)).glob("*.html"))
    return " ".join(page_text(p.read_text(encoding="utf-8", errors="replace")) for p in pages)


def chosen(employers: Iterable[Mapping[str, str]], companies: str) -> list[Mapping[str, str]]:
    """The probes' employers, narrowed by --companies (comma-separated, any part of a name, as live_smoke.py's)."""
    wanted = [w.strip().lower() for w in companies.split(",") if w.strip()]
    return [e for e in employers if not wanted or any(w in e["name"].lower() for w in wanted)]


async def run_probes(out: Path, probes: Iterable[str], companies: str = "") -> None:
    """The probes' runs, one after another (one inbox): each reset tenant's two passes back to back, then each
    tailored employer's run."""
    probes = set(probes)
    if "reset" in probes:
        for tenant in chosen(RESET_TENANTS, companies):
            name = tenant["name"]
            print(f"\n== reset probe, {name}: pass 1, Workday's saved password derived from the site password",
                  flush=True)
            first_dir = out / folder_of("reset", name, 1)
            await run_one(run_args(name, first_dir, derived=True), first_dir)
            if pass_two_needed(reset_steps(read_record(first_dir, name))):
                print(f"\n== reset probe, {name}: pass 2, the real site password", flush=True)
                second_dir = out / folder_of("reset", name, 2)
                await run_one(run_args(name, second_dir), second_dir)
            else:
                print(f"\n== reset probe, {name}: pass 2 not needed (the site's password was never changed)",
                      flush=True)
    if "tailored" in probes:
        for employer in chosen(TAILORED_EMPLOYERS, companies):
            print(f"\n== tailored-resume probe, {employer['name']}", flush=True)
            folder = out / folder_of("tailored", employer["name"])
            await run_one(run_args(employer["name"], folder, tailored=True), folder)


def collect(out: Path, probes: Iterable[str], companies: str = "") -> dict[str, Any]:
    """The probes' results, from their runs' folders in `out` (what run_probes left, or an earlier run's)."""
    probes = set(probes)
    results: dict[str, Any] = {"at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                               "reset": [], "tailored": []}
    if "reset" in probes:
        for tenant in chosen(RESET_TENANTS, companies):
            name = tenant["name"]
            second_dir = out / folder_of("reset", name, 2)
            results["reset"].append(reset_result(tenant, read_record(out / folder_of("reset", name, 1), name),
                                                 read_record(second_dir, name), second_dir.is_dir()))
    if "tailored" in probes:
        for employer in chosen(TAILORED_EMPLOYERS, companies):
            folder = out / folder_of("tailored", employer["name"])
            results["tailored"].append(tailored_result(employer, read_record(folder, employer["name"]),
                                                       review_text(folder, employer["name"])))
    results["failed"], results["unchecked"] = failed(results), unchecked(results)
    return results


def failed(results: Mapping[str, Any]) -> list[str]:
    """The desk's failures, one line each: what fails the run."""
    out = []
    for r in results.get("reset") or []:
        if r["status"] == "fail":
            out.append(f"{r['employer']}: {r['warning']}" if r["warning"] else "; ".join(
                f"{r['employer']}, pass {p['pass']}: {p['status_why']}" for p in r["passes"] if p["status"] == "fail"))
    out += [f"{r['employer']}, tailored resume: {r['detail']}" for r in results.get("tailored") or []
            if r["status"] == "fail"]
    return out


def unchecked(results: Mapping[str, Any]) -> list[str]:
    """What couldn't be checked this week, one line each (the site's or the person's, in the live check's words):
    said, and no failure."""
    out = [f"{r['employer']}, pass {p['pass']}: {p['status_why']}" for r in results.get("reset") or []
           for p in r["passes"] if p["status"] == "unchecked"]
    out += [f"{r['employer']}, tailored resume: {r['detail']}" for r in results.get("tailored") or []
            if r["status"] == "unchecked"]
    return out


# --- the report -----------------------------------------------------------------------------------------------------

STATUS_WORDS = {"pass": "pass", "fail": "FAIL", "unchecked": "couldn't check this week", "not run": "not run"}
LEFT_WORDS = {"real": "the real password", "as found": "as it was (this run never changed it)",
              "derived?": "maybe the derived password",
              "unknown": "an unknown password: maybe still the derived one, as pass 2 said it set the real one and "
                         "never signed in with it after that"}
FAILED_HEADING = "Failed (the desk's: these fail the run)"
UNCHECKED_HEADING = "Couldn't check this week (the site's or the person's: not a failure)"


def _yes(value: Any) -> str:
    return "yes" if value else "no"


def summary_lines(results: Mapping[str, Any]) -> list[str]:
    """The probes' results in a few plain lines (nightly_summary.py --probes prints them): the failures and what
    couldn't be checked, each under its own heading, then each probe."""
    out = []
    for heading, said in ((FAILED_HEADING, results.get("failed") or []),
                          (UNCHECKED_HEADING, results.get("unchecked") or [])):
        out.append(f"{heading}: {len(said)}")
        out += [f"  - {line}" for line in said]
    for r in results.get("reset") or []:
        out.append(f"reset {r['employer']}: {STATUS_WORDS[r['status']]} (site left on {LEFT_WORDS[r['left_on']]})")
        for p in r["passes"]:
            if not p["ran"]:
                out.append(f"  pass {p['pass']} ({p['saved']} password): not run")
                continue
            steps = ", ".join(f"{STEP_WORDS[k]} {_yes(p[k])}" for k in STEPS)
            out.append(f"  pass {p['pass']} ({p['saved']} password): {STATUS_WORDS[p['status']]}: {steps}"
                       + (f" ({p['how_far']})" if p.get("how_far") else "") + f"; ended {p['ended']}")
        out += [f"  note: {n}" for n in r["notes"]]
        if r["warning"]:
            out.append(f"  WARNING: {r['warning']}")
    for r in results.get("tailored") or []:
        out.append(f"tailored {r['employer']}: {STATUS_WORDS[r['status']]}: {r['detail']}"
                   + (f"; the page's files: {', '.join(r['shown'])}" if r.get("shown") else "")
                   + f"; ended {r['ended']}")
    return out


_cell = functools.partial(live_compare._cell, one_line=True)  # (a detail's line break would end its table row)


def report_markdown(results: Mapping[str, Any]) -> str:
    """The run's step summary: the failures and what couldn't be checked, each under its own heading, then a table a
    probe."""
    lines = [f"## Weekly probes ({results.get('at', '')})", ""]
    for heading, said in ((FAILED_HEADING, results.get("failed") or []),
                          (UNCHECKED_HEADING, results.get("unchecked") or [])):
        lines += [f"### {heading}: {len(said)}", ""]
        lines += [f"- {'**' + _cell(line) + '**' if heading == FAILED_HEADING else _cell(line)}" for line in said]
        lines += [""] if said else []
    if results.get("reset"):
        lines += ["### Password reset (Workday, the test identity's account)", "",
                  "Pass 1 saves a password derived from the real one, which the site refuses: the desk resets the site "
                  "to it. Pass 2 saves the real one: the desk resets it back.", "",
                  "| Tenant | Pass | Saved | " + " | ".join(STEP_WORDS[k].capitalize() for k in STEPS)
                  + " | Ended | Result |", "|---|---|---|" + "---|" * len(STEPS) + "---|---|"]
        for r in results["reset"]:
            for p in r["passes"]:
                if not p["ran"]:
                    lines.append(f"| {_cell(r['employer'])} | {p['pass']} | {p['saved']} | "
                                 + " | ".join("–" for _ in STEPS) + " | not run | – |")
                    continue
                cells = [_yes(p[k]) + (f" ({p['how_far']})" if k == "reached_application" and p.get("how_far") else "")
                         for k in STEPS]
                lines.append(f"| {_cell(r['employer'])} | {p['pass']} | {p['saved']} | " + " | ".join(cells)
                             + f" | {_cell(p['ended'])} | {STATUS_WORDS[p['status']]} |")
        lines.append("")
        for r in results["reset"]:
            lines.append(f"- **{_cell(r['employer'])}: {STATUS_WORDS[r['status']]}**, site left on "
                         f"{LEFT_WORDS[r['left_on']]}" + "".join(f"; {_cell(n)}" for n in r["notes"]))
            if r["warning"]:
                lines.append(f"  - **{_cell(r['warning'])}**")
        lines.append("")
    if results.get("tailored"):
        lines += ["### Tailored resume", "", "The job's folder held a tailored test resume "
                  "(Jobdesk_Test_Tailored_Probe_<day>_Resume.pdf, a new name each day); "
                  f"the profile's is {live_identity.RESUME_NAME}.", "",
                  "| Employer | Files the review page shows | Result | Detail |", "|---|---|---|---|"]
        for r in results["tailored"]:
            lines.append(f"| {_cell(r['employer'])} | {_cell(', '.join(r.get('shown') or []) or 'none')} | "
                         f"{STATUS_WORDS[r['status']]} | {_cell(r['detail'])} |")
        lines.append("")
    if not results.get("reset") and not results.get("tailored"):
        lines.append("No probe ran (none matched --companies).")
    return "\n".join(lines) + "\n"


def write_report(out: Path, results: Mapping[str, Any], mask: Callable[[str], str]) -> None:
    """probes.json and report.md in `out`, masked; a LIVE_WEEKLY line a probe on stdout."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "probes.json").write_text(mask(json.dumps(results, indent=2)), encoding="utf-8")
    (out / "report.md").write_text(mask(report_markdown(results)), encoding="utf-8")
    for r in [*results.get("reset", []), *results.get("tailored", [])]:
        print(mask("LIVE_WEEKLY " + json.dumps(r)), flush=True)


def read_results(where: Path) -> dict[str, Any]:
    """probes.json from a run's live-probes artifact (a zip, or its folder unzipped). ValueError when there's none."""
    import zipfile

    if where.is_file() and where.suffix == ".zip":
        with zipfile.ZipFile(where) as z:
            names = sorted((n for n in z.namelist() if n.rsplit("/", 1)[-1] == "probes.json"), key=len)
            if not names:
                raise ValueError(f"{where.name} holds no probes.json")
            data = json.loads(z.read(names[0]))
    else:
        found = sorted(where.rglob("probes.json"), key=lambda p: len(p.parts)) if where.is_dir() else []
        if not found:
            raise ValueError(f"{where} holds no probes.json")
        data = json.loads(found[0].read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("probes.json isn't the probes' results")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=Path("live-probes"))
    ap.add_argument("--probes", default=",".join(PROBES), help=f"which probes, comma-separated ({', '.join(PROBES)})")
    ap.add_argument("--companies", default="", help="only these employers of the probes' (comma-separated, any part)")
    ap.add_argument("--report-only", action="store_true",
                    help="run nothing: the report again from the runs already in --out (no secrets needed)")
    args = ap.parse_args(argv)
    probes = [p.strip() for p in args.probes.split(",") if p.strip()]
    if not probes or any(p not in PROBES for p in probes):
        print(f"--probes takes {', '.join(PROBES)}. Nothing was run.", file=sys.stderr)
        return 2
    mask: Callable[[str], str] = lambda text: text  # noqa: E731
    if not args.report_only:
        problem = live_identity.env_problem(os.environ)
        if problem:
            print(problem.replace("--test-identity needs", "The weekly probes need"), file=sys.stderr)
            return 2
        site = live_identity.credentials(os.environ)[2]
        derived_problems = password_problems(derived_password(site))
        if derived_problems:  # (never the password: what's wrong with it)
            print(f"The derived password wouldn't meet Workday's rules ({'; '.join(derived_problems)}): change "
                  f"{live_identity.ENV_SITE_PASSWORD} (TEST_IDENTITY.md step 4). Nothing was run.", file=sys.stderr)
            return 2
        mask = live_identity.masker(*secrets_of(os.environ))
        sys.stdout = live_identity.MaskedStream(sys.stdout, mask)
        sys.stderr = live_identity.MaskedStream(sys.stderr, mask)
        print(f"Weekly probes ({', '.join(probes)}) as the test identity ({live_identity.EMAIL_MASK}); nothing is "
              "submitted, and its email and passwords are masked in everything printed and written.", flush=True)
        args.out.mkdir(parents=True, exist_ok=True)
        asyncio.run(run_probes(args.out, probes, args.companies))
        for folder in args.out.iterdir():  # (each run masked its own; once more, the derived password too)
            if folder.is_dir():
                live_identity.mask_files(folder, mask)
    results = collect(args.out, probes, args.companies)
    write_report(args.out, results, mask)
    print("\n" + "\n".join(summary_lines(results)), flush=True)
    # (a desk failure fails the run; what couldn't be checked this week is said above, and doesn't)
    return 1 if results["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
