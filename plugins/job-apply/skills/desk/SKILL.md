---
name: desk
description: Open the Job Desk, a page on the user's computer that lists recommended openings from every employer in the plugin's list, ranked against their profile, and applies to the ones they pick with one button. Use when the user wants a dashboard or interface for their job search, wants to see recommended jobs, wants to apply to several jobs at once, or asks for one-click applying.
---

# Job Desk

The desk is a local web page served by this plugin. It does what the `find-jobs` and
`apply` skills do, but the user drives it with buttons:

- **Find jobs** searches every employer's career site for the profile's
  `preferences.titles` in its `preferences.locations` (or home state). It ranks each
  opening, with plain reasons and concerns under it: a degree requirement the user's
  education doesn't meet, senior titles, clearances, export control, travel.
- **Apply to selected** works through the picked jobs one at a time in the automation
  browser. It clicks through Apply, autofills every page, adds Workday's work and
  education blocks, and stops at each review page.
- **Needs you** collects whatever it can't do alone:
  - questions the profile doesn't answer, answered right on the page. Answers are
    remembered in `~/.job-apply/answers.yaml` unless the user unticks "remember".
    A remembered answer fills only the same question, worded the same, and one about
    an employer ("Why do you want to work here?") isn't reused for another company.
    Consent questions default to not remembered. A box the page wouldn't let the desk fill
    (it timed out) isn't a question: the card says "I couldn't fill …";
  - a dialog open over a form, which the user answers in the browser before pressing
    Resume (the boxes behind it are filled after that). With `settings.accept_notices` (on
    unless the user turns it off; never in practice mode) the desk agrees for the user to an
    employer's notice about AI screening of the application (Eightfold's opens as the resume
    goes up), and picks an application's attestation that its information is true and
    complete (or consent to the background check that comes with applying), noting each in
    the job's log. Any other dialog over a form still stops for the user, and cookie banners
    keep their own rule;
  - sign-ins, bot checks, CAPTCHAs and emailed codes. The user deals with these in the
    browser window, and the desk carries on by itself once the page moves past them. A
    link a site emails to confirm the address opens in the user's usual browser, which
    leaves the desk's tab where it was: the user reloads that tab after opening the link.
    The queue waits while the user works in that tab. After 5 minutes with nothing
    happening there, the other jobs go ahead, and the desk picks the job up again once
    its tab is past the pause. With an email app password saved in **Site passwords**
    (`email_password`, for the profile's email address), the desk reads the code or link
    from the inbox itself. It reads only mail from that job's site or its job system,
    sent after the wait began (or in the two minutes before), read-only, and never
    presses a button labelled Submit. Never ask for that
    password in the chat either: it goes into the desk page.
  - **Alert me** (in the header) turns on desktop notifications for jobs that come to
    need the user or are ready to submit, so the desk can be left in the background.
- **Notes (N)** (in the header, once there are any) collects the notes the desk takes by
  itself whenever a job stops on something it most likely got wrong: stuck, a sign-in, a
  Submit that didn't go through or showed no confirmation, or an answer that didn't go in
  (not bot checks, CAPTCHAs, emailed codes or questions the profile doesn't answer). The
  user files them all at once: **File on GitHub** opens one issue with them, **Copy all**
  copies the whole text, and **Clear** removes them once filed. A note holds no answers and
  says the employer by its job system rather than its name. **Report a problem** on a job's
  card stays for one job: a fuller report, with the employer and the pages the desk saved
  (the page the job stopped on among them). Its box **Don't say which job it was** (for
  Claude, `report_problem(job_id, anonymous=True)`) makes it again with only the job system
  and where it stopped: no employer, job title, address or requisition number in the issue
  or the saved pages. Offer it when the user would rather not show which job they applied for.
- **Site passwords** stores one password per job system: Workday, SuccessFactors, iCIMS,
  ApplicantStack, UKG Pro or Infor. The page names, by each system, the employers on the
  user's own lists that use it (on the semiconductor list, Workday has 12 of them; on the
  Phoenix list, 24). Each employer has its own account. With a password saved, the desk signs in by itself
  (pressing the sign-in form's own button, whatever it reads: SuccessFactors' says "Submit")
  and creates the account from Create Account forms (email, password, name and country from
  the profile; it ticks the site's terms, never a newsletter, and presses the button; a
  picture code stays the user's). If the password doesn't sign in, then with the email app
  password saved the desk first resets that employer's password to the saved one through
  the site's "Forgot password" email; when the site says it has no account for the email,
  or no reset email comes in 3 minutes (the other jobs carry on meanwhile), it opens the
  site's Create Account form ("Create an account", "Register", "Don't have an account
  yet?") and creates the account. Without the inbox it tries Create Account first, and
  asks for the reset (the user opens the emailed link) where the site says the email already
  has an account, or where the password still doesn't sign in after Create Account. It presses
  Create Account once per job, and a sign-in with a refused password twice at most (once more
  after a reset or an account it made), so the account isn't locked; then the desk card says what
  the site said. A Resume the user presses on that sign-in card tries the saved password once more
  (after they reset it to that by hand, say); a Resume on any other card doesn't.
  `settings.manage_accounts: false` leaves all of this to the user (the desk fills
  in Create Account for them to finish); practice mode never creates an account. A password is only typed into its
  own system's addresses. The user types it into the desk page; never ask for it in the
  conversation.
- **Submit** sends one application. **Submit for me** sends every application that
  needs nothing, and stays off until the user turns it on. LinkedIn and Indeed are
  always submitted by the user.
- **A newer version**: the desk looks at the plugin's published version when it starts and
  every few hours, and says when a newer one is out, with the two update commands (then a
  restart of the Claude app). Best done between runs. `JOB_APPLY_NO_UPDATE_CHECK=1` turns the look off.
- **Tailor my resume for each job** holds each picked job, before its browser tab
  opens, until Claude has written a resume for it (see Tailoring resumes). The page
  says how many are waiting. **Use my usual resume** sends one on without.

## Steps

1. Run `setup_status`. If the profile is missing required fields, run the `setup`
   skill first: the desk fills forms from the profile alone.
2. Call `open_job_desk()`. It opens the page in the user's browser and returns its
   address. The address carries a private key, so don't post it anywhere.
3. In two or three sentences, tell the user what to press: **Find jobs**, tick the
   openings they want (or **Select recommended**), then **Apply to selected**. Say
   that it stops at each review page unless they turn on **Submit for me**.
4. Jobs from Indeed or LinkedIn (via the `find-jobs` skill) that are saved with
   `add_job` or `ingest_job` appear in the desk's list too, ranked the same way. The
   user can also paste job links into the box above the list. The desk reads each
   posting (LinkedIn and Indeed in a background browser tab) and ticks it for Apply.
5. The desk keeps running while Claude Code is open. Without Claude, the user can run
   `uv run --project <plugin>/server job-apply-desk` (the path is `plugin_root` in
   `setup_status`).

## Tailoring resumes

When the user says "tailor my resumes", or the desk shows jobs waiting for a tailored
resume:

1. Call `tailoring_queue()`. It lists the waiting jobs with their posting text, the
   user's real resume (`base_resume`, or `resume_file` to read) with the profile's work
   and education history, and the `rules`.
2. For each job, write the resume in Markdown. Choose, order and reword what the real
   resume says so the parts the posting asks for come first, in its own words where
   they're true. Never add a tool, certification, number, duty, employer, date or
   degree; coursework is not a degree.
3. The first time, show the user one tailored resume and ask if the style is right.
   After that, carry on without asking.
4. Call `render_document("resume", markdown, job_id)`. The desk carries on with that
   job as soon as its PDF is saved. A `warning` about length means tighten it and
   render again; the job waits until it fits on 2 pages.
5. A job whose description says it hasn't been read yet: `ingest_job(url)` first.

## Bot checks

When a site shows Cloudflare's "Just a moment…", a CAPTCHA or a "verify you are human"
page, the desk pauses that job, brings its tab to the front, and waits for the user to
pass the check. It never tries to get around one: no disguising the browser, no
CAPTCHA-solving services. The automation browser keeps its own profile in
`~/.job-apply/browser` (`browser-msedge` when Edge stands in for Chrome), so a site
checked once usually lets it through for a while. If
a site refuses automated browsers altogether (TSMC's careers site has), the user
applies there in their everyday browser, or finds the same posting on Indeed.
