---
name: setup
description: Set up the job-apply plugin, which means building the applicant profile from the user's resume, answering the work-authorization, preference and EEO questions, and opening the browser so the user can sign in to LinkedIn and Indeed. Use on first use, when setup_status reports a missing profile field, or when the user wants to change their saved answers or submit settings.
---

# Set up job-apply

All of the user's data lives in `~/.job-apply/`, outside the plugin:

| Path | What |
|---|---|
| `profile.yaml` | Answers used to fill forms (created from the plugin's `templates/profile.example.yaml`) |
| `resume.pdf` | Default resume to upload |
| `secrets.yaml` | Optional career-site passwords, which the user writes. Never read it or print it |
| `tracker.db` | Application tracker (SQLite) |
| `browser/` | Persistent browser profile that keeps sign-ins |
| `applications/<id>-<company>-<title>/` | Per-job tailored documents, screenshots and the submission record |

## Steps

1. Run `setup_status`. On first run it creates `~/.job-apply/profile.yaml` from the
   template, and it lists the missing fields.

2. **Resume.** Ask for the resume file path, or look for a resume in the working
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

3. **Fill in what the resume can't answer.** Ask with AskUserQuestion where the
   choices are fixed, and keep it to one or two rounds:
   - Work authorization: authorized to work in the US, sponsorship needed now or
     later, US citizen, US person for export control (ITAR/EAR, common for
     semiconductor equipment jobs), security clearance.
   - Preferences: willing to relocate, willing to travel (Field Service roles often
     travel 25–75%), shifts, nights, weekends and on-call, desired salary (or leave
     it empty to be asked each time), earliest start date, target titles and
     locations.
   - Voluntary self-identification (gender, Hispanic/Latino, race, veteran,
     disability): explain that these are optional, and record their answer or a
     decline option such as "Decline to self-identify". Don't push for an answer.
   - Recurring screening questions in the `answers:` list: cleanroom work, lifting
     50 lbs, background check or drug screen, relatives at the company, non-compete.
   - Submit mode: `review` (default, they approve each submit) or `auto` for chosen
     ATSs such as `[workday, greenhouse, lever]`. LinkedIn and Indeed always need
     them to click Submit themselves.
   - Email, only if the Gmail connector is connected: may Claude read the
     verification codes and links career sites email them (`settings.email_codes`)?
     May it check their email for replies to applications (`settings.email_tracking`)?
     Both default to off. Explain that Claude only searches for those specific
     messages and never sends or deletes mail.

4. Write `~/.job-apply/profile.yaml` and keep its comments. Run `setup_status` again
   until `profile_complete` is true. Then show the user a short summary of their
   answers to confirm, with sensitive values (EEO choices) summarized rather than
   echoed.

5. **Browser.** Run `open_application(url="https://www.linkedin.com/login")`. A
   Chrome window opens with its own profile. Ask the user to sign in to LinkedIn,
   and to Indeed (`https://secure.indeed.com/auth`), in that window. The sign-ins are
   kept for future sessions. If the browser doesn't start, have them install
   Google Chrome, or run the `playwright install chromium` command shown in
   `setup_status`'s `browser_note` and set `settings.browser_channel: chromium` in
   the profile.

6. **Passwords (optional).** Company sites on Workday and SuccessFactors need an
   account per company. If the user wants Claude to type the password, they create
   `~/.job-apply/secrets.yaml` themselves, for example
   `workday_password: "..."`, and run `chmod 600` on it. Never ask them to paste a
   password into the chat.
