---
name: setup
description: Set up the job-apply plugin in two stages. First what the first application needs (the applicant profile from the user's resume, contact details, work authorization and sponsorship, the consents for cookie banners, accounts and notices, and what and where to search), then the Job Desk; after that, offered once, the preference, background, screening and EEO questions that mean fewer stops. Use on first use, when setup_status reports a missing profile field, when something about the plugin seems broken (its doctor tool checks the computer's setup), or when the user wants to change their saved answers or submit settings.
---

# Set up job-apply

All of the user's data lives in `~/.job-apply/`, outside the plugin:

| Path | What |
|---|---|
| `profile.yaml` | Answers used to fill forms (created from the plugin's `templates/profile.example.yaml`) |
| `resume.pdf` | Default resume to upload |
| `secrets.yaml` | Optional career-site passwords, which the user writes. Never read it or print it |
| `tracker.db` | Application tracker (SQLite) |
| `browser/` | Persistent browser profile that keeps sign-ins (`browser-msedge/` when Edge stands in for Chrome) |
| `applications/<id>-<company>-<title>/` | Per-job tailored documents, screenshots and the submission record |

## Before setup: the installer and the doctor

Most people arrive from the one-step installer (`setup/install.ps1` on Windows, `setup/install.sh`
on macOS and Linux; the README has the commands). It installs Git, Claude Code and uv where
they're missing, adds or updates the plugin, gets a browser ready, sets `UV_PYTHON_INSTALL_DIR`
outside AppData on Windows, makes `~/.job-apply`, and copies the resume the person gave it to
`~/.job-apply/resume.pdf` (or `resume.docx`, `.doc`, `.rtf`, `.odt`, `.txt`). Someone who hasn't
installed the plugin yet, or whose computer is missing a piece, can run it (again) safely: each
step looks first and only acts when needed.

When something seems broken (the plugin's tools fail, no browser starts, the desk misbehaves
after an update), run the `doctor` tool (`launch_browser=true` also starts the browser, out of
sight). It reports Python, uv, the plugin's version and whether a newer one is out, the browser,
`~/.job-apply`, the profile, the resume and, on Windows, `UV_PYTHON_INSTALL_DIR`, each with what
to do. Walk the person through each problem in plain words. When the plugin's tools aren't there
at all (its server didn't start), have them run the installer again, or
`uv run --project <plugin>/server job-apply-doctor` in a terminal, and read you its report.

## Steps

Setup comes in two stages, so that a new user reaches their first filled application
soon, with the least answered that it needs:

- **Stage 1, before the first application** (steps 1 to 7): the resume, contact details,
  work authorization and sponsorship, the three consents (cookie banners, accounts,
  notices), and what and where to search. Then the Job Desk opens.
- **Stage 2, the rest** (step 8): what gives fewer stops later: voluntary
  self-identification (EEO), the background questions, screening answers, the common
  questions, and the other preferences. Offer it once, after the desk is open, saying what
  it's for ("Each answer here is a stop the desk won't make: about 10 minutes. Now, or
  later?"), and never push. Run it whenever the user asks for it later ("ask me everything
  the desk needs", "the desk's background questions"), or when the desk's "Fewer stops"
  notice lists something: run `setup_status` and go through its `missing` and
  `profile_gaps` together. Whatever stays unanswered, the desk asks on a job's card when an
  application needs it, and the person answers once there.

### Stage 1: before the first application

1. Run `setup_status`. On first run it creates `~/.job-apply/profile.yaml` from the
   template, and it lists the missing fields.

2. **Resume.** If the installer copied one to `~/.job-apply` (`resume.pdf`, `resume.docx`...;
   `doctor` and `setup_status`'s `missing_profile_fields` say when the profile doesn't point at
   it yet), confirm with the user that it's the one to use, and set `documents.resume` to it.
   Otherwise ask for the resume file path, or look for a resume in the working
   directory. Copy it to `~/.job-apply/resume.pdf` (or `.docx`) and set
   `documents.resume`. If the user has no file handy and the Indeed connector is
   available, call its `get_resume` and use that as the source instead. Indeed
   resumes often lack end dates and bullet points, so ask for those. Then write
   a clean resume in Markdown, show it to the user, render it with
   `render_document("resume", markdown, default=true)`, and set
   `documents.resume` to the path it returns. Read it, then fill `personal`, `experience`, `education`,
   `history.previous_employers`, `work_history` (every job, with start/end month)
   and `education_history` from it. Use only what the resume says; if a date or
   location is missing, ask rather than guess.

   **Confirm every degree before writing it.** A resume, an Indeed one especially,
   can list a degree that was never finished. Ask "Did you complete the <degree>?"
   for each one. If it wasn't completed, record that school in `education_history`
   with `degree: Some college (no degree)` (Workday's Degree box asks; a list's "Some
   College, No Degree" is picked for it) and the classes as the major or a note, and set
   `education.highest_degree` to the highest one completed (a GED or high school
   diploma, say, or "Some college" when there is none). Add each degree confirmed
   finished, a high school diploma or GED included, to `education.degrees_earned`
   as `{level: bachelor, field: Finance}` (level: high_school for a diploma, ged,
   associate, bachelor, master or doctorate for a PhD or EdD; leave out a JD, MD,
   PharmD or certificate; field as the diploma names it; nothing else in the
   entry): only these answer "Do you have a Bachelor's degree?" with Yes. Applications ask about degrees and employers check, so a wrong
   one costs more than a missing one. If the source resume lists it wrongly, tell
   the user where to fix it (for Indeed, profile.indeed.com).

3. **Contact details and work authorization.** Take the name, email, phone and address
   (city, state, country) from the resume, and ask for any `setup_status` still lists as
   missing. Then ask, with AskUserQuestion: authorized to work in the US, sponsorship needed
   now or later, US citizen, US person for export control (ITAR/EAR, common for
   semiconductor equipment jobs), security clearance.

4. **The three consents, in one AskUserQuestion.** First tell the user what each one means,
   then ask all three in a single AskUserQuestion (one question each, its default shown as
   the first choice and marked "(default)"), and write each as they say:
   - Cookie banners (default: off): the desk declines them, and one with no way to decline
     (TI's "Agree and Proceed") waits for them. Ask whether the desk may accept those for
     them (`settings.accept_cookies: true`); leave it off unless they say yes.
   - Accounts on job sites (default: on): tell them the desk creates their accounts itself
     (agreeing to each site's terms in their name, and to being considered for other open
     positions where a site won't make the account without that, never job alerts or a
     newsletter; a form's security questions come up on the desk's card for them, never
     remembered; a picture code stays theirs) and, when a site refuses their saved password,
     asks that site for a password reset and sets the saved password as the new one (reading
     the reset email needs the email app password on the desk; without it, they finish the
     reset from the email). Ask whether that's all right, and write
     `settings.manage_accounts: true` or `false` as they say (`false`: the desk fills in
     Create Account for them to finish).
   - Notices and attestations (default: on): tell them the desk agrees, in their name, to an
     employer's notice about AI screening of their application (Eightfold's employers show
     one as the resume goes up) and to its privacy notice and terms of use (Schwab's "I
     Acknowledge the Privacy Notice", a terms box), and picks an application's attestation
     that its information is true and complete (a Taleo "Statement of Truth", say), or their consent
     to the background check that comes with applying, noting each in the job's log. Never
     a newsletter, job alerts or a talent community, texts or automated calls, a legal
     waiver, or the terms of their employment (an at-will acknowledgement is asked of them).
     It's true because every answer comes from their profile or from them. Any other dialog
     over a form waits for them, and practice mode never agrees. Ask whether that's all
     right, and write `settings.accept_notices: true` or `false` as they say (`false`: the
     desk stops at the notice, and asks the attestation as a question).

5. **The kind of work and where.** Ask the job titles they want ("Help Desk Technician",
   "HR Generalist"; the first four are searched, all count toward each opening's fit),
   where they want to work (a city and state each: "Phoenix, AZ", "Las Vegas, NV") and
   which of the plugin's employer lists fit, then save them with
   `search_area(titles=[...], places=[...], lists=[...])`: its `choices` name the lists and
   what each covers (`semiconductor-az`, Arizona's semiconductor employers, the default,
   who hire HR, finance and IT people too; `phoenix-metro`, large Phoenix-area employers
   in many fields; `las-vegas`, Las Vegas-area employers that hire IT people). It keeps the places in
   `preferences.locations`, the titles in `preferences.titles` (ranking follows them) and the
   lists in `~/.job-apply/companies.yaml`, whose `companies:` takes employers of their own
   (the shape of the plugin's `templates/companies.example.yaml`). They can change all three
   later with the desk's What and where button.

6. Write `~/.job-apply/profile.yaml` and keep its comments. Run `setup_status` again
   until `profile_complete` is true. Then show the user a short summary of their answers to
   confirm.

7. **Open the Job Desk** (`open_job_desk`) and tell the user, briefly:
   - It stops at each application's review page for their own Submit ("Submit for me" on
     the desk changes that); LinkedIn and Indeed applications are always theirs to submit.
   - **Passwords (optional).** Company sites on Workday and SuccessFactors need an account
     per company, as do SCREEN's, Nikon Precision's, Benchmark's and Daifuku's. The desk's
     **Site passwords** card stores one password per job system, on their computer; with
     one saved, the desk makes their accounts and signs them in. Otherwise the user creates
     `~/.job-apply/secrets.yaml` themselves, for example `workday_password: "..."`, and runs
     `chmod 600` on it. Never ask them to paste a password into the chat.
   - The desk's browser window opens with its own profile when the desk applies. Without
     Chrome it uses Microsoft Edge (on every Windows computer). If no browser starts, have
     them install Google Chrome, or run the `playwright install chromium` command shown in
     `setup_status`'s `browser_note` and set `settings.browser_channel: chromium` in the
     profile.
   - **LinkedIn and Indeed**, only when the user says they'll paste LinkedIn or Indeed job
     links: run `open_application(url="https://www.linkedin.com/login")` and ask them to sign
     in to LinkedIn, and to Indeed (`https://secure.indeed.com/auth`), in that window. The
     sign-ins are kept for future sessions. Otherwise skip it: the desk's employer searches
     need neither.

   Then offer stage 2, once.

### Stage 2: fewer stops (offered once, and whenever asked)

8. Run `setup_status` and go through its `profile_gaps`, then ask the rest. Ask with
   AskUserQuestion where the choices are fixed, a few at a time:
   - `work_history dates: <jobs>`: those jobs lack a start or end month (Workday asks both
     for every job): take them from the resume as `start: 2021-03` and `end: 2023-06` (or
     `end: present`), and ask the user for any the resume gives only as years or not at
     all. They needn't remember an old job's months; leave those as they are rather than
     guess. `education_history degree: <schools>`: those schools have no degree written;
     confirm with the user what they finished there (`Some college (no degree)` for classes
     without one).
   - Voluntary self-identification (gender, Hispanic/Latino, race, veteran,
     disability): explain that these are optional, and record their answer or a
     decline option such as "Decline to self-identify". Don't push for an answer.
   - Background (`background:`), the yes/no questions many employers ask: a relative or
     close friend working at the company applied to, a government employee (federal, state
     or local) now or in the last 5 years, military service, the US Department of Defense, a
     non-compete or similar agreement, owning patents, trademarks or copyrights, keeping
     another job or a business, a seat on a board. Write `true` or `false` for each. A
     `false` answers any wording of them, whatever time it covers; a `true` leaves each one
     to the person on the desk, since its dates or details differ. `setup_status`'s
     `background: <keys>` lists the ones not answered yet: ask those all at once with
     AskUserQuestion.
   - U.S. service, in the same AskUserQuestion as the background questions
     (`setup_status`'s `history: <keys>` lists the ones not answered yet): ever worked for the
     U.S. federal government as a civilian (`history.us_government_employee`: any agency or
     branch, any capacity, an internship or a Special Government Employee or IPA assignment
     too; not a state or local government, not a federal contractor), and ever served in the
     U.S. military (`history.us_military`: any branch, active duty, the Reserves or the
     National Guard). Write `true` or `false`. Unlike the background's, each answers both
     ways: "Have you ever worked for the U.S. Government ... or served in the U.S. Military?"
     (federal contractors such as NNSS ask it) is Yes where either is true and No where both
     are false. A `true` for the military is `background.military: true` too; after a `false`,
     `background.military` still asks about any other country's armed forces.
   - An employer the user has worked for and may apply to again: their ID there, for
     `history.employee_ids` (`{Employer: "ID"}`; only that employer's forms get it).
   - Recurring screening questions in the `answers:` list: cleanroom work, lifting
     50 lbs, background check or drug screen. For finance work, also a credit check and
     licenses held (CPA, FINRA registrations).
     For hospitals and health systems (Banner Health, HonorHealth, Mayo Clinic…), also
     whether a government agency has ever excluded them from Medicare, Medicaid or other
     government programs.
     A pattern is searched for anywhere in a question, so keep each one to the question
     it answers. Use the template's patterns, and when you write a new one, test it on
     the question and its opposite: a "Yes" for "Are you willing to take a drug test?"
     must not answer "Have you ever failed one?".
   - Preferences: willing to relocate, willing to travel (Field Service roles often
     travel 25–75%), a valid driver's license (`personal.drivers_license`), shifts,
     nights, weekends and on-call, desired salary (or leave it empty to be asked each
     time), earliest start date.
   - `preferences.how_did_you_hear`: ask, or leave it empty and the desk asks once on its
     common-questions card. Profiles made from the template before 0.3.175 hold its old preset
     `LinkedIn`, which went into every application: ask whether that's true, and clear it if not.
   - **Common questions.** Run `common_questions`: the questions most applications ask that the
     profile doesn't answer (desired pay, start date, shifts...), the ones the desk's Common
     questions card would stop to ask. Ask the unanswered ones together, each choice question with
     AskUserQuestion and its `choices` (a text one, such as desired pay, in their own words), and
     save what they say with `common_questions(answers=[{"id": ..., "answer": ...}])`. Only their
     answers, never a guess; one they'd rather leave stays on the desk's card. `setup_status`'s
     `common_questions_unanswered` counts the ones left.
   - Submit mode: `review` (default, they approve each submit) or `auto` for chosen
     job systems such as `[workday, greenhouse, lever]`. LinkedIn and Indeed always need
     them to click Submit themselves.
   - Jobs at once: tell them the Job Desk fills two applications at the same time, each in its
     own tab of its browser window (`settings.jobs_at_once`, 1 to 3, default 2), which takes a
     long queue in about half the time. Never two at one employer's site; one at a time signs
     in, makes an account or waits on an emailed code; and while a sign-in or code needs them,
     or they're in one of the desk's rounds, no new job starts and none comes to the front. Ask
     whether they'd like 1 (one after another), 2 or 3, and write it; the desk's **Jobs at
     once** buttons change it later.
   - Email, only if the Gmail connector is connected: may Claude read the
     verification codes and links career sites email them (`settings.email_codes`)?
     May it check their email for replies to applications (`settings.email_tracking`)?
     Both default to off. Explain that Claude only searches for those specific
     messages and never sends or deletes mail.

   Write `~/.job-apply/profile.yaml` (keeping its comments), and show the user a short
   summary of their answers to confirm, with sensitive values (EEO choices) summarized
   rather than echoed.
