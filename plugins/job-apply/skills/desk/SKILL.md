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
  education doesn't meet, senior titles, clearances, export control, travel. Pressing a
  score (or pointing at it) shows how it adds up, from the ranking's own parts ("start 50 ·
  title matches +25 · posted this week +10 · in Phoenix +5 = 90").
  Openings first saved since the user's last visit (the desk keeps when they last looked
  at the page, `last_seen` in `desk.json`, and the look before this visit; a look 30 minutes
  after the last begins a new visit) carry a **New** mark all visit long, and make the bar's
  **New** group. **Not interested** on an opening hides it, with an optional reason (Title,
  Company, Location, Pay, Other), kept in the tracker: it's never queued, by Apply to
  selected or otherwise, until its **Undo** (in the word at the foot of the window, or by its
  row in the bar's **Not interested** group). For Company, that employer's other openings
  score 10 lower ("you said not interested in Acme −10"). It isn't offered on a job the desk
  has a card for (Skip job is for those) or one sent in. The mark is the job's, whichever
  site its posting is read from (Indeed's, or the employer's once they're one job).
  `list_jobs` and `get_job` say `not_interested: true` with the reason, `open_application`
  warns, and `submit_application` presses nothing for it without `user_confirmed`: ask the
  user before applying to one.
  It searches only when pressed. **Search by itself every 12 hours** (under the Openings
  head's **More**, off unless the user turns it on) also searches when the openings are
  over 12 hours old, but not while the queue is paused. While a search runs, the header
  says so (by itself, or pressed) with **Stop search** beside it: the search ends and the
  openings found before it stay.
- **Apply to selected** works through the picked jobs in the desk's browser window, two at a
  time by default, each in its own tab. Two or more picked together go the likeliest to go
  all the way first (the page says so once): employers in `data/desk_handled.yaml`, then job
  systems most of whose listed employers are there (Workday, say), then the rest, and last
  LinkedIn's and Indeed's (their Submit is the user's) and employers where the desk's own
  history met bot checks more often than it reached the review page. Each group keeps the
  user's order; one started alone, or a Carry on, isn't moved. It clicks through Apply, autofills every page, adds
  Workday's work and education blocks, and stops at each review page. Before they go, it says what will stop
  them: a job system with no saved password, no email app password for sites that email a
  code, common questions not answered yet, in a window whose default button is **See to these
  first** (it takes the user to the card for the first one: Site passwords with that job system
  picked, or Common questions; Enter never applies); **Apply anyway** goes ahead.
  A posting the site says has closed or been filled is skipped by itself (its card quotes
  the site; Undo puts it back). A saved draft's **Continue Application** is the way in
  when it's the page's only one and the draft is this job's. Its answers are checked
  against the profile (never the person's own written words, self-identification or
  how-heard answers), and a draft the desk changed waits on its review page, even with
  Submit for me on, its card listing each change. A page that loads blank is loaded
  again up to twice before the person is asked.
- **Needs you** collects whatever it can't do alone:
  - questions the profile doesn't answer, answered right on the page. Answers are
    remembered in `~/.job-apply/answers.yaml` unless the user unticks "remember".
    A remembered answer fills only the same question, worded the same, and one about
    an employer ("Why do you want to work here?") isn't reused for another company.
    Consent questions default to not remembered. An optional box for the user's ID at the employer
    (a WWID, an employee, worker, badge or personnel number) is asked too, once a job, unless the
    profile says they never worked there; left empty, it isn't asked again for that job, and an
    answer is remembered for that employer alone. A box the page wouldn't let the desk fill
    (it timed out) isn't a question: the card says "I couldn't fill …";
  - a dialog open over a form, which the user answers in the desk's browser window before pressing
    Carry on (the boxes behind it are filled after that). With `settings.accept_notices` (on
    unless the user turns it off; never in practice mode) the desk agrees for the user to an
    employer's notice about AI screening of the application (Eightfold's opens as the resume
    goes up) and to its privacy notice and terms of use (a privacy agreement's "I Accept"
    before the application, a privacy dialog's Ok, a required "I agree to the Privacy notice
    and Terms of use" box), and picks an application's attestation that its information is
    true and complete (or consent to the background check that comes with applying), noting
    each in the job's log. Never a newsletter, job alerts or a talent community. A dialog that
    offers how to start the application's profile (Eightfold's "Create your profile with
    existing information": buttons to upload a resume and to enter the information) isn't a
    notice: its "Enter information" is pressed and the form filled from the profile (never the
    site's reading of the resume, whose guesses could overstate it). Any other dialog over a form still
    stops for the user, and cookie banners keep their own rule;
  - sign-ins, bot checks, CAPTCHAs and emailed codes. The user deals with these in the
    desk's browser window, and the desk carries on by itself once the page moves past them. A
    link a site emails to confirm the address opens in the user's usual browser, which
    leaves the desk's tab where it was: the user reloads that tab after opening the link.
    The queue waits while the user works in that tab. After 5 minutes with nothing
    happening there, the other jobs go ahead, and the desk picks the job up again once
    its tab is past the pause. With an email app password saved in **Site passwords**
    (`email_password`, for the profile's email address), the desk reads the code or link
    from the inbox itself. It reads only mail from that job's site or its job system,
    sent after the wait began (or in the two minutes before), read-only (the inbox, and its
    Spam folder when the inbox has none), and never presses a button labelled Submit. Never
    ask for that password in the chat either: it goes into the desk page. Without one, with
    `settings.email_codes` on and the Gmail connector here, you read it for the desk through
    Gmail while this chat is open (see **Emailed codes through Gmail** below), and the job's
    card says "Claude is looking in your Gmail for the code".
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
  and where it stopped: no employer (by any name a page gives it, its parent group's too), job
  title, place, address or requisition number in the issue or the saved pages. Offer it when the user would rather not show which job they applied for.
- **Site passwords** stores one password per job system: Workday, SuccessFactors, iCIMS,
  ApplicantStack, UKG Pro, Infor or Taleo (Kforce's; where Taleo asks for a user name, the desk
  uses the profile email as it). The page names, by each system, the employers on the
  user's own lists that use it (on the semiconductor list, Workday has 15 of them; on the
  Phoenix list, 27). A pill shows each one saved, and the card's count says how many of
  them are. Each employer has its own account. With a password saved, the desk signs in by itself
  (pressing the sign-in form's own button, whatever it reads: SuccessFactors' says "Submit")
  and creates the account from Create Account forms (email, password, name and country from
  the profile; it ticks the site's terms, never a newsletter, and a consent to be considered for
  other open positions only when the site won't make the account without it; it presses the button; a
  picture code stays the user's; the form's security questions come up on the job's card, go into that
  form alone and are never remembered, logged or sent to Claude, and the desk presses the button once
  they're in). If the password doesn't sign in, then with the email app
  password saved the desk first resets that employer's password to the saved one through
  the site's "Forgot password" email; when the site says it has no account for the email,
  or no reset email comes in 3 minutes (the other jobs carry on meanwhile), or the reset isn't
  one the desk can do (a security question stays the user's), it opens the
  site's Create Account form ("Create an account", "Register", "Don't have an account
  yet?") and creates the account. Without the inbox it tries Create Account first, and
  asks for the reset (the user opens the emailed link) where the site says the email already
  has an account, or where the password still doesn't sign in after Create Account. It presses
  Create Account once per job, and a sign-in with a refused password twice at most (once more
  after a reset or an account it made), so the account isn't locked; then the desk card says what
  the site said. It prefers a reset email that came after it asked for the reset (one from just
  before only after a short wait, the two clocks may be apart; one from well before, an earlier
  reset's, never), and opens each reset link once. Where the reset
  page says its link expired or the password was used before, or the only email's link was opened
  already, or the saved password still doesn't sign in after the reset, the job stops on a sign-in
  card that says so, and no new account is made there. A Carry on the user presses on that sign-in card tries the saved password once more
  (after they reset it to that by hand, say; from a spent link's page the desk goes back to the
  sign-in first); a Carry on pressed on any other card doesn't. An account
  there from before that was never verified (the site refuses the sign-in with "Verify your account
  before you sign in") isn't reset: with the email app password saved the desk presses the site's
  "Resend Account Verification" once and opens the new email's link, then signs in; without it the
  card names that button for the user.
  `settings.manage_accounts: false` leaves all of this to the user (the desk fills
  in Create Account for them to finish); practice mode never creates an account. A password is only typed into its
  own system's addresses. The user types it into the desk page; never ask for it in the
  conversation.
- **Submit** sends one application. **Submit for me** sends every application that
  needs nothing, and stays off until the user turns it on. LinkedIn and Indeed are
  always submitted by the user.
- **What went in** (a panel on each Ready card, and on a stopped job's card, closed until
  opened) lists what the desk put into that application: each question's answer by page
  (a long one cut short), the files put up by name, the notices, terms and attestations it
  agreed to, an account it made, and the optional questions it left empty. The user checks
  the answers there instead of reading every review page again. It never shows a password,
  an emailed code or a security answer, and it's on the desk page only (never in the job's
  log, a note or a problem report).
- **Jobs at once** (1, 2 or 3, beside Submit for me; `settings.jobs_at_once` in profile.yaml,
  default 2) is how many applications the desk fills at the same time, each in its own tab.
  Never two at one employer's site or job-system tenant; one at a time signs in, makes an
  account, resets a password or waits on an emailed code (the inbox is one); while a sign-in
  or code needs the user, or they're in one of the desk's rounds, no new job starts (those under
  way go on in their own tabs) and no other tab comes to the front; a Submit they press goes
  beside the jobs under way. **Working on** shows each job under way. 1 fills them one after another, as before. The desk's choice is kept in
  `desk.json` until profile.yaml's setting is changed.
- **Skip job** takes a job out of the queue and closes its tabs (its windows too, never another job's); it's never applied to until
  **Undo** (on its card under Ready and done, or by its row in the list) puts it back where it
  was, with the tracker's status from before: back in the queue (opened again from the start), or
  in the list unticked if the desk hadn't begun it. Undo sends nothing: the job stops at its review page.
- **Pause queue** (in the header, while jobs are queued or running) lets the jobs under way finish their pass and
  starts no other until **Carry on with the queue**. A Submit the user presses still goes, and jobs waiting on them are still
  watched. A paused queue stays paused after a restart. To stop the queue, never close the desk's browser window: that
  breaks the job in progress (it says so, and its **Carry on** opens the application again).
- **A restart** (of the Claude app, a crash, a plugin update) doesn't lose the queue. The desk keeps it in
  `~/.job-apply/desk-queue.json` (each job's number and where it was: nothing the user typed or a page said). Once
  it's open again, the jobs it hadn't begun are back in the queue in their order, **paused** (one waiting for a
  tailored resume waits for it again): a notice at the top says how many, how many of them Submit for me will send once
  they're filled (it applies only if it's still on), and how old the queue is past a week; nothing starts until the user
  presses **Carry on with the queue**. A job it had begun (under way, or waiting on the user) isn't queued again by itself, since
  the user may have sent it on the site: its card says the desk restarted before the application was finished, with
  **I submitted it myself**, **Carry on** (filled again from the start up to its review page, never sent) and **Skip
  job**. A Submit the user pressed before the restart was never sent: that card says so. One whose Submit was being
  pressed, or was pressed with no confirmation, may have gone in: its card asks the user to check (**I submitted it myself**,
  or **Carry on**, which sends nothing). One applied to or skipped meanwhile is left out. A desk page left open doesn't
  reconnect (its key is new at each start): open the desk again (`open_job_desk`). With two Claude sessions, one desk
  keeps the queue and the other says so, keeping none; about a minute after the first ends, the other takes it up, its
  jobs held for **Carry on with the queue** while its own go on. A folder that allows no lock (some network drives) keeps no
  queue, and the desk says so.
- **History.** Each line of a job's log on the desk shows the time it was logged (the computer's own clock). Each time
  the desk is done with a job for now (at its review page, sent by the desk, stopped for the user with the kind of stop,
  failed, skipped, cut short by the desk stopping, or marked as applied: the user's I submitted it myself, or found applied
  already), it writes a line in `~/.job-apply/runs.jsonl`: the job, its site, when the pass began and ended and its
  minutes, how it ended and why (the desk's own words, with answers, codes and passwords kept out as far as it can
  tell), the steps it went through, how many times the desk's rounds took the user to it and they pressed Carry on, and
  Jobs at once. **This week** (in the header) sums up the last 7 days: jobs filled to their review page, sent by the
  desk and marked as applied, the stops for the user by kind, failed and skipped, the time with the desk in all and a
  job's median (waits for sites and the inbox included, and jobs filled at once overlap), the employers it stopped at
  most, and the latest lines. For "how did this week go?", call `desk_history()` (`days=` for another span); the desk
  needn't be open.
- **Safety** (in the header) opens a read-only window listing each setting that decides what
  the desk does in the user's name, its value now and what that means for them, with where to
  change it: practice mode (`submit_mode`), Submit for me, LinkedIn and Indeed (always the
  user's Submit), `accept_notices`, `manage_accounts`, `accept_cookies`, `email_codes` and
  the email app password's limits, `email_tracking`, Jobs at once, Troubleshooting, bot
  checks (always the user's) and passwords. It's worked out from the same settings the desk
  acts on, so when the user asks "what will the desk do for me?", point them there.
- **A newer version**: the desk looks at the plugin's published version when it starts and
  every few hours, and says when a newer one is out, with the two update commands (then a
  restart of the Claude app). Best done between runs. `JOB_APPLY_NO_UPDATE_CHECK=1` turns the look off.
- **Only where the desk goes all the way** shows only openings at employers where the
  plugin's live check took a test applicant to the review page (a chip marks them in
  the list either way). Bot checks, CAPTCHAs and steps it can't do yet are elsewhere.
  Each of the Openings head's switches that's on is said in a muted line under the head.
- **Troubleshooting** (under the head's **More**, with Search by itself) is for getting the desk going at more employers: Find jobs
  searches every list's employers (the user's own lists, and the HR list too), only the
  openings where the desk doesn't go all the way yet show, whatever their fit, every one is
  tried, and nothing is submitted while it's on (the header says so; the desk still makes
  accounts and fills each step, which the sites keep). Turned off, the desk applies for real.
  In it, **Select recommended** ticks one opening per employer (the best fit): the others
  there most likely stop at the same place.
- **The bar over the openings** (once any job has been tried) shows them by where they
  stand, each with its count: Not started, Working, Bot checks (with **Solve them**, the
  round that brings each up for the user), Questions and sign-ins, Stuck (the desk couldn't get past
  something: its Notes, or each card's Report a problem, are for Claude to fix), Ready and
  Done. A group picked hides the others, and nothing hidden is ticked or applied to.
- **What and where** (the header's button, which reads "What and where:" and the titles and places searched now)
  opens a window with the job titles Find jobs looks for (typed, or picked from its ideas
  for IT, equipment and HR work; the first four are searched), the places it looks in (a
  city and state each) and the plugin's employer lists it searches. Saving it finds jobs
  again; Claude's `search_area` tool changes the same.
- **Common questions** (a card, while any are unanswered) asks the questions most
  applications ask that the profile doesn't answer (`data/question_bank.yaml`): the four
  most asked (each entry's `seen`), with **Show all** for the rest. Answered once, each goes to `answers.yaml` and fills that question at every employer from then on;
  **Your answers** under it shows the ones given. Claude can ask them in chat too
  (`common_questions`, as setup does).
- **My applications** (a tab beside Recommended and All) lists each job sent in, or filled
  and waiting on its review page, newest first, with where it stands in the tracker and
  when the employer last wrote ("Heard 2026-10-03: confirmation", with the email's
  one-line summary as its tooltip; "No reply yet" before any). A bar over it groups them:
  **Waiting** (nothing heard yet), **Heard back** (a confirmation, an assessment or another
  email, and nothing further), **Interviews** (interviewing or an offer) and **Closed**
  (rejected or withdrawn); the group picked is kept in that browser.
  - One applied to `settings.follow_up_days` days ago (10 unless the profile says) with
    no email logged, not even a confirmation, says the day its follow-up became due, with
    **Follow up**: like Prep me, it copies a request for the user to paste to you ("Draft a
    follow-up email for job 12, Technician at Example Fab (use track-responses)"). When it's
    pasted, draft a polite follow-up email for it, as a Gmail draft only (see the
    track-responses skill).
  - An interview's row (or one whose newest email is an assessment) has **Prep me**. The
    page can't reach Claude, so it copies a request for the user to paste to you ("Prep
    me for my interview: job 12, Technician at Example Fab (use interview-prep)"). When
    it's pasted, run the interview-prep skill for that job.
  - The emails are the ones the track-responses skill logged, read through the Gmail
    connector with `email_tracking` on: never with the desk's email app password.
- **Tailor my resume for each job** holds each picked job, before its browser tab
  opens, until Claude has written a resume for it (see Tailoring resumes). The page
  says how many are waiting. **Use my usual resume** sends one on without.
- **Write a cover letter for each job** has the desk write each picked job's cover letter by
  itself, in the background, through the user's own Claude Code sign-in (see Cover letters).

## The desk's wording

Use the desk's own words when you talk about it, and keep to them when you change its page or its messages:

- Voice: headings, pills and messages are the desk speaking to the user ("Needs you", "Waiting for you in the
  desk's browser window"); buttons and switches are the user's own words ("Submit for me", "Tailor my resume for
  each job", "I submitted it myself").
- States: one word per state, the same in an opening's row and its card: Queued, Working…, Questions, Sign in,
  Emailed code, Bot check, CAPTCHA, Stuck, Your Submit (the site's own Submit is the user's), Check if it went in
  (Submit was pressed and no confirmation showed: never "press Submit"), Waiting for a resume, Cover letter to
  check, Restarted, Already begun?, Waiting for you (any other stop), Ready to submit, Submitted, Didn't finish,
  Skipped. The bar over the
  openings groups them: Not started, Working, Bot checks, Questions and sign-ins, Stuck, Ready, Done (and Any, New,
  Not interested).
- Color: yellow means something waits for the user now; blue is information (a tip, an update); red is a problem
  (its text starts "Problem: "); green is done or safe; grey is neutral.
- A message says what happened, then what to press, in two sentences. The browser the desk fills applications in
  is "the desk's browser window", and a site's applicant system is a "job system (Workday, say)", never an "ATS".
- A count beside a word means the same set wherever that word appears. "Needs you" (the side card, and the page's
  title) counts every job stopped for the user; among the openings the bar shows, those are its Bot checks and
  Questions and sign-ins, the stopped ones among its Stuck, and the ones among its Ready that wait on a Submit or a
  check of the user's. "N to submit" in Ready and done's head counts its Ready to submit cards.

## Steps

1. Run `setup_status`. If the profile is missing required fields, run the `setup`
   skill first: the desk fills forms from the profile alone.
2. Call `open_job_desk()`. It opens the page in the user's browser and returns its
   address. The address carries a private key, so don't post it anywhere.
3. In two or three sentences, tell the user what to press: **Find jobs**, tick the
   openings they want (or **Select recommended**), then **Apply to selected**. Say
   that it stops at each review page unless they turn on **Submit for me**. Then see
   **Emailed codes through Gmail** below: with the Gmail connector here, start its watch
   (or ask its one question) before you finish your reply.
4. Jobs from Indeed or LinkedIn (via the `find-jobs` skill) that are saved with
   `add_job` or `ingest_job` appear in the desk's list too, ranked the same way. The
   user can also paste job links into the box above the list. The desk reads each
   posting (LinkedIn and Indeed in a background browser tab) and ticks it for Apply.
5. The desk keeps running while Claude Code is open. Without Claude, the user can run
   `uv run --project <plugin>/server job-apply-desk` (the path is `plugin_root` in
   `setup_status`).
6. **A morning check.** Offer it once: "each morning: search, then whats_new". Search
   first (the desk's Find jobs, its Search by itself every 12 hours, or the find-jobs
   skill), then call `whats_new()`: the openings first saved since its last call (or
   `since="2026-10-17"`, `"12h"`), best score first, with a `summary` to show as it is. It
   never searches by itself. With the user's go-ahead, this can be a scheduled task.

## Emailed codes through Gmail

Sites email a sign-up code or a link to confirm the address while the desk applies. The
desk reads them itself with an email app password saved on its page, and needs no Claude
for that. Without one, you can read them for it through the Gmail connector, while this
chat is open. Password reset emails are never read this way: they stay with the app
password. `open_job_desk()` returns `emailed_codes`: `email_codes` (the setting),
`app_password` (`saved`, `saved, but the desk can't use it`, or `not saved`), `watch`
(true when the setting is on and no app password works) and `watch_command`.

- **Gmail tools here** (`search_threads` and `get_thread` among your tools) **and `watch`
  is true**: start the watch. Run `watch_command` exactly as given with your Bash tool in
  the background (`run_in_background`): it waits on the desk (about 19 minutes at most)
  and ends when a job needs its code, which wakes you. Say in one sentence that you'll
  read the codes career sites email for the desk's jobs while this chat is open, and that
  if a permission prompt shows for the command, they can choose not to be asked again for
  that one command.
- **Gmail tools here, `email_codes` off and `app_password` not `saved`**: ask once whether
  you may read, through Gmail, the sign-up codes and links career sites email them for the
  desk's jobs (only those messages, never other mail). On a yes, write
  `settings.email_codes: true` in `~/.job-apply/profile.yaml` (keeping its comments), and
  start the watch. On a no, leave it: they type the codes themselves.
- **No Gmail tools**, or an app password that works: nothing to do here.

Each time the watch ends (and whatever it printed):

1. Call `mail_requests()`. Each request has the job, the employer, what it waits for
   (`want`: `code` or `link`), `after_time` and a Gmail `query`; `account` is the
   person's address.
2. For each request, call `search_threads` with its `query` exactly as given. Never widen
   it, add words, pass `includeTrash`, or search spam, trash or `in:anywhere`, and search
   for nothing else. When the connector has several accounts, pass `account` as given.
3. Read the newest one or two matching threads with `get_thread` and
   `messageFormat: "PLAIN_TEXT"` (never the full content: its HTML is long, and its links
   come through in the plain text). Keep only the newest one to three messages dated after
   the request's `after_time`.
4. Call `give_mail(job_id, messages)` with each kept message's `sender`, `date`, `subject`
   and `plaintext_body` as `get_thread` gave them. Call it with `messages=[]` when nothing
   came. The desk checks them as it does its own inbox's (only that job's senders, since
   its wait began; a link only back to that job's site), types the code into the job's own
   tab itself or opens the link there, and says what happened in `said`. Never type a code
   or open a link yourself, and never repeat a code in the chat. The desk asks again less
   often each time nothing came, and stops asking after a few looks.
5. If `watch` is still true, run `watch_command` again in the background. When it's false
   (no job queued or waiting, the setting off, or an app password that works now), stop,
   and say so in one line. Stop too when the user asks.

Stop, and tell the user once in a sentence, when the watch command fails to run (it can't
find `uv`, or exits with an error other than "nothing to watch"): the desk then asks you
for nothing, and the codes are theirs to type. Do the same when Gmail refuses you (its
sign-in has lapsed, or it turns the search down): say they can reconnect the Gmail
connector and ask you to watch again. And when `account` isn't one of the Gmail accounts
the connector has linked (its `list_accounts`, or a search that says so): say once that
the connector doesn't reach the address the desk applies with, so the codes are theirs
to type, and stop. Never search another account instead.

When a request's job can't be told apart from another job's (one sender emails both, and
the other began its own email step after this one paused), the desk stops asking for it
after one look at the time before that step, and its card says the code is the user's to
enter: nothing for you to do.

Never send, delete, archive, label or mark any message, and never read mail no request
names. Whichever comes first goes in: the user can still type the code in the desk's
browser window themselves.

## Tailoring resumes

When the user says "tailor my resumes", or the desk shows jobs waiting for a tailored
resume:

1. Call `tailoring_queue()`. It lists the waiting jobs with their posting text, the
   user's real resume (`base_resume`, or `resume_file` to read) with the profile's work
   and education history, and the `rules`.
2. For each job, write the resume in Markdown. Choose, order and reword what the real
   resume says so the parts the posting asks for come first, in its own words only
   where the resume says the same thing. Never add a tool, certification, number, duty,
   employer, date or degree; coursework is not a degree. Never widen how much, how many
   or how often, and keep the jobs newest first.
3. The first time, show the user one tailored resume and ask if the style is right.
   After that, carry on without asking.
4. Call `render_document("resume", markdown, job_id)`. The desk carries on with that
   job as soon as its PDF is saved. A `warning` about length means tighten it and
   render again; the job waits until it fits on 2 pages.
5. A job whose description says it hasn't been read yet: `ingest_job(url)` first.

## Cover letters

With **Write a cover letter for each job** on, the desk writes each picked job's letter by
itself, in the background, as soon as the job is queued: it runs the user's own `claude`
command in print mode, with no tools and none of their MCP servers, so it uses their Claude
plan and needs no API key. The letter (one page, from their resume, profile and the posting,
using only what those say) is checked twice for what it claims (by the desk, then by a second
`claude` call given the letter, resume and profile but never the posting): anything either
finds and the job gets the usual cover letter, its log saying why (4 such in a row and the desk
says so once and writes no more until the switch is turned off and on). A letter that passes goes into
the job's folder, and a cover letter box uploads it; a box reached while it's still being
written or checked waits up to 2 minutes for it. A job tried again keeps the letter it has. Where `claude` is missing, not signed in, too old, at its
plan's limit or can't reach Claude, the desk says so once at the top of the page, and jobs go on with
`documents.cover_letter` (or none): tell the user to install Claude Code and sign in (run
`claude` in a terminal, then `/login`), or to update it.

- **The first letter** ever written waits on its job's card ("Cover letter to check") for
  the user's **Use it** (after that, letters go in without asking) or **Turn cover letters
  off**; no other letter is written until then (a restart of the desk keeps it so). It's in
  the job's folder as `cover_letter_draft.md`. When the user asks to change
  it, read that file, write the new letter (same shape: `# Full Name`, the contact line, the
  greeting, the paragraphs, `Sincerely,` and the name), using only what their resume and
  profile say, and call `render_document("cover_letter", markdown, job_id)`: the card shows
  the new one, and **Use it** sends it. One longer than a page comes back with an `error`
  saying how many pages: tighten it and call again (the card keeps the one it had).
- **One letter for a job**, outside the switch: write it the same way and call
  `render_document("cover_letter", markdown, job_id)`; the desk uses it and never writes
  over it.
- `settings.cover_letter_model` picks the model (empty: `claude-sonnet-5-5`).

## Bot checks

When a site shows Cloudflare's "Just a moment…", a CAPTCHA or a "verify you are human"
page, the desk pauses that job, brings its tab to the front, and waits for the user to
pass the check. It never tries to get around one: no disguising the browser, no
CAPTCHA-solving services. The desk's browser window keeps its own profile in
`~/.job-apply/browser` (`browser-msedge` when Edge stands in for Chrome), so a site
checked once usually lets it through for a while. If
a site refuses automated browsers altogether (TSMC's careers site has), the user
applies there in their everyday browser, or finds the same posting on Indeed.
