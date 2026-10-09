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
| `browser/` | Persistent browser profile that keeps sign-ins (`browser-msedge/` when Edge stands in for Chrome) |
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

   **Confirm every degree before writing it.** A resume, an Indeed one especially,
   can list a degree that was never finished. Ask "Did you complete the <degree>?"
   for each one. If it wasn't completed, record that school in `education_history`
   with `degree: ""` and the classes as the major or a note, and set
   `education.highest_degree` to the highest one completed (a GED or high school
   diploma, say, or "Some college" when there is none). Add each degree confirmed
   finished, a high school diploma or GED included, to `education.degrees_earned`
   as `{level: bachelor, field: Finance}` (level: high_school for a diploma, ged,
   associate, bachelor, master or doctorate for a PhD or EdD; leave out a JD, MD,
   PharmD or certificate; field as the diploma names it; nothing else in the
   entry): only these answer "Do you have a Bachelor's degree?" with Yes. Applications ask about degrees and employers check, so a wrong
   one costs more than a missing one. If the source resume lists it wrongly, tell
   the user where to fix it (for Indeed, profile.indeed.com).

3. **Fill in what the resume can't answer.** Ask with AskUserQuestion where the
   choices are fixed, and keep it to one or two rounds:
   - Work authorization: authorized to work in the US, sponsorship needed now or
     later, US citizen, US person for export control (ITAR/EAR, common for
     semiconductor equipment jobs), security clearance.
   - Preferences: willing to relocate, willing to travel (Field Service roles often
     travel 25–75%), a valid driver's license (`personal.drivers_license`), shifts,
     nights, weekends and on-call, desired salary (or leave it empty to be asked each
     time), earliest start date, target titles and locations.
   - Voluntary self-identification (gender, Hispanic/Latino, race, veteran,
     disability): explain that these are optional, and record their answer or a
     decline option such as "Decline to self-identify". Don't push for an answer.
   - Recurring screening questions in the `answers:` list: cleanroom work, lifting
     50 lbs, background check or drug screen, relatives at the company, non-compete.
     For finance work, also a credit check, licenses held (CPA, FINRA registrations) and
     government employment in the last few years (banks and accounting firms ask).
     For hospitals and health systems (Banner Health, HonorHealth, Mayo Clinic…), also
     whether a government agency has ever excluded them from Medicare, Medicaid or other
     government programs.
     A pattern is searched for anywhere in a question, so keep each one to the question
     it answers. Use the template's patterns, and when you write a new one, test it on
     the question and its opposite: a "Yes" for "Are you willing to take a drug test?"
     must not answer "Have you ever failed one?".
   - Submit mode: `review` (default, they approve each submit) or `auto` for chosen
     ATSs such as `[workday, greenhouse, lever]`. LinkedIn and Indeed always need
     them to click Submit themselves.
   - Cookie banners: the desk declines them, and one with no way to decline (TI's "Agree and
     Proceed") waits for them. Ask whether the desk may accept those for them
     (`settings.accept_cookies: true`); leave it off unless they say yes.
   - Email, only if the Gmail connector is connected: may Claude read the
     verification codes and links career sites email them (`settings.email_codes`)?
     May it check their email for replies to applications (`settings.email_tracking`)?
     Both default to off. Explain that Claude only searches for those specific
     messages and never sends or deletes mail.

   - The kind of work and where: the plugin's employer list is semiconductor employers in
     Arizona. For other work (HR, finance, health care…) or somewhere else, write
     `~/.job-apply/companies.yaml` from the plugin's `templates/companies.example.yaml`:
     `lists:` names the plugin's lists to search (`setup_status` shows them under
     `employer_lists`; `phoenix-metro` is large Phoenix-area employers in many fields, and
     `semiconductor-az` the semiconductor ones, which hire HR, finance and IT people too),
     and `companies:` adds employers of their own. Set `preferences.titles` to their
     target titles ("HR Generalist", "Recruiter"), since ranking follows them.

4. Write `~/.job-apply/profile.yaml` and keep its comments. Run `setup_status` again
   until `profile_complete` is true. Then show the user a short summary of their
   answers to confirm, with sensitive values (EEO choices) summarized rather than
   echoed.

5. **Browser.** Run `open_application(url="https://www.linkedin.com/login")`. A
   Chrome window opens with its own profile. Ask the user to sign in to LinkedIn,
   and to Indeed (`https://secure.indeed.com/auth`), in that window. The sign-ins are
   kept for future sessions. Without Chrome it uses Microsoft Edge (on every Windows
   computer). If no browser starts, have them install Google Chrome, or run the
   `playwright install chromium` command shown in `setup_status`'s `browser_note` and
   set `settings.browser_channel: chromium` in the profile.

6. **Passwords (optional).** Company sites on Workday and SuccessFactors need an
   account per company, as do SCREEN's, Nikon Precision's, Benchmark's and Daifuku's.
   The easiest way to store a password for a job system is the Job Desk's **Site
   passwords** card (`open_job_desk`). Otherwise the user creates
   `~/.job-apply/secrets.yaml` themselves, for example
   `workday_password: "..."`, and runs `chmod 600` on it. Never ask them to paste a
   password into the chat.
