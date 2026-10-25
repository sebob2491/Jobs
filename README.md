# Jobs: a Claude Code plugin that applies to jobs for you

`job-apply` is a Claude Code plugin. Paste job links (from LinkedIn, Indeed or a
company careers site) and Claude does the rest:

1. **Reads the posting.** It pulls the title, company, location, description,
   and the job system (Workday, say) that runs the application.
2. **Checks fit** against your profile and flags hard blockers, such as an
   export-control or citizenship requirement, a degree, or the amount of travel.
3. **Opens the application in a real browser window** on your machine (the desk's
   browser window), which you can watch, and fills it in from your profile. That
   covers contact details, address, work authorization, sponsorship, voluntary EEO
   answers, the resume upload, and answers you've saved before.
4. **Asks you** about anything your profile doesn't answer. It drafts free-text
   answers from your resume for you to approve.
5. **Stops at the review page** and submits when you say so.
6. **Tracks everything** in a local tracker: saved, in progress, applied,
   interviewing and so on. With your permission it can also check your Gmail for
   replies and update the tracker.

It also fills Workday's "My Experience" section, with one block per job and
degree in your profile, and it can write a tailored resume or cover letter for
each job as a PDF that it uploads in place of your default. In the Job Desk, turn
on "Tailor my resume for each job" and say "tailor my resumes" to Claude: each
picked job waits until Claude has written its resume, then carries on. Turn on
"Write a cover letter for each job" and the desk has Claude write each job's cover
letter by itself (see [Cover letters](#cover-letters)).

It works with any careers site, plus specific handling for the systems common
at semiconductor companies: Workday (Applied Materials, KLA, Intel, Microchip,
NXP, TEL, ADI, Onto Innovation, Axcelis, Thermo Fisher, Air Liquide, Air Products, Brooks),
Eightfold (Lam Research, Micron, Infineon), SuccessFactors (Edwards, TSMC Arizona, Amkor, Qorvo,
Rogers, Pfeiffer Vacuum), Oracle (onsemi, TI), Greenhouse (ASM, Marketech), ApplicantStack
(SCREEN), iCIMS (Daifuku, Fujifilm), Paycom (Ebara), UKG Pro (Nikon Precision, UCT), Infor
CloudSuite (Benchmark), Cornerstone (Linde, Matheson), Phenom (EMD Electronics) and Lever.
`plugins/job-apply/data/companies.yaml` lists 39 semiconductor employers with
Arizona sites, including equipment makers whose field service engineers work at
Arizona fabs, and the suppliers whose technicians run the fabs' gases and chemicals.

Looking for other work, or somewhere else? The Job Desk's **What and where** button (or
asking Claude) sets the job titles to look for, the places to look (a city and state
each, such as "Las Vegas, NV") and which of the plugin's employer lists to search:
- `semiconductor-az`, the list above;
- `phoenix-metro`, large Phoenix-area employers in health care, education, finance,
  utilities, aerospace, retail and staffing;
- `las-vegas`, Las Vegas-area employers that hire IT people: casinos' corporate IT, gaming
  technology, data centers, public agencies and IT staffing firms.

The places go in `profile.yaml` (`preferences.locations`) and the lists in
`~/.job-apply/companies.yaml`, where you can add employers of your own under
`companies:`, in the shape of
[`templates/companies.example.yaml`](plugins/job-apply/templates/companies.example.yaml).
Set your target titles (`preferences.titles`, such as "HR Generalist" or "Recruiter") to
match.

## Install in one step

On Windows, open PowerShell (Start menu, type PowerShell) and paste:

```
irm https://raw.githubusercontent.com/sebob2491/Jobs/main/plugins/job-apply/setup/install.ps1 | iex
```

On a Mac or Linux, open Terminal and paste:

```
curl -fsSL https://raw.githubusercontent.com/sebob2491/Jobs/main/plugins/job-apply/setup/install.sh | bash
```

It installs what's missing (Git, Claude Code, uv), adds this plugin to Claude Code (or updates
it), gets a browser ready, makes your `~/.job-apply` folder, offers to copy your resume into
it, and runs the plugin's doctor to check it all. It asks before installing Claude Code or Git, never
asks for a password, and is safe to run again; running it again is also how to update. Then
open the Claude app (or run `claude`) and say **"set up job-apply"**: Claude reads your resume
and asks the few questions it can't answer.

To hand it your resume, or to see what it would do without changing anything (`-DryRun`,
`--dry-run`):

```
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/sebob2491/Jobs/main/plugins/job-apply/setup/install.ps1))) -Resume "$HOME\Documents\resume.pdf"
curl -fsSL https://raw.githubusercontent.com/sebob2491/Jobs/main/plugins/job-apply/setup/install.sh | bash -s -- --resume ~/Documents/resume.pdf
```

Something not working? Ask Claude to "run the job-apply doctor", or run
`uv run --project <plugin>/server job-apply-doctor` in a terminal. It checks Python, uv, the
plugin's version (and whether a newer one is out), the browser, your profile and resume, and
says what to do about anything that isn't right. It changes nothing.

## How submitting works

| Where the application is | What Claude does |
|---|---|
| Company sites (Workday, Greenhouse, Lever, Eightfold, SuccessFactors, …) | Fills everything and shows you the review page. You say "submit" and Claude submits. You can opt in to **auto** mode for chosen job systems, and then Claude submits complete applications without asking. |
| **LinkedIn Easy Apply** and **Indeed Apply** | Fills everything, then **you click Submit** in the desk's browser window. Both sites' terms prohibit automated submission, and they restrict accounts that do it, so the plugin never presses that button. |

Claude never invents experience, credentials or work-authorization answers.
Anything that isn't in your profile or resume is a question for you. CAPTCHAs and
bot checks stay with you; so do sign-ins and emailed codes, unless the Job Desk has
your site passwords and email app password. Passwords can be typed
from a local secrets file without passing through the chat.

## Install by hand

The [one-step installer](#install-in-one-step) does all of this. To do it yourself, you need:

- [Claude Code](https://claude.com/claude-code)
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/). It runs the
  plugin's Python server and installs its dependencies on first launch. On Windows, set
  the user environment variable `UV_PYTHON_INSTALL_DIR` to a folder outside AppData (such
  as `%USERPROFILE%\.uv-python`): the Claude app can't always find a Python uv keeps in AppData.
- Google Chrome, or Microsoft Edge without it (every Windows computer has Edge). If
  neither starts, run the `playwright install chromium` command that `setup_status`
  shows.

Then, in Claude Code:

```
/plugin marketplace add sebob2491/Jobs
/plugin install job-apply@sebob-jobs
```

Restart Claude Code and run:

```
/job-apply:setup
```

Setup comes in two stages. First it asks for your resume, fills in
`~/.job-apply/profile.yaml`, and asks only what your first application needs: contact
details, work authorization, sponsorship and US-person status, whether the desk may accept
cookie banners, make your job-site accounts and agree to employers' notices for you, and
what and where to search. Then it opens the Job Desk. After that it offers, once, the rest
(relocation, travel, shifts, salary, background, screening and EEO questions): each
answered is a stop the desk won't make. It asks you to sign in to LinkedIn and Indeed in the
desk's browser window only if you'll paste their job links. The sign-ins persist.

## Use

```
Apply to these:
https://www.linkedin.com/jobs/view/<job id>/
https://<company>.wd1.myworkdayjobs.com/External/job/<location>/<title>_<req id>
https://job-boards.greenhouse.io/<company>/jobs/<id>
```

```
/job-apply:find-jobs field service engineer roles in Chandler and Phoenix
```

```
/job-apply:track-responses        (did anyone reply? updates the tracker from Gmail)
```

```
What have I applied to this month?   ·   Mark the Lam job as interviewing   ·   Export my tracker to CSV
```

### The Job Desk: recommended jobs and one Apply button

```
/job-apply:desk
```

opens a page on your computer (served by the plugin on `127.0.0.1`):

> **Your first application**
> 1. Press **Find jobs**.
> 2. Tick the openings you want (or press **Select recommended**).
> 3. Press **Apply to selected**: the desk fills each one in the desk's browser window.
> 4. Answer what it asks under **Needs you**.
> 5. Check each review page and press **Submit**: nothing is sent before you do (unless you turn on **Submit for me**).
>
> Once: save a password for each job system under **Site passwords**, and the desk makes your accounts.

1. **Find jobs** searches every employer's career site for your target titles in
   your area. Each opening gets a fit score, with the reasons and concerns under it:
   "title matches", "posted this week", "requires a Bachelor's (you have a high
   school diploma)", "senior-level role", "requires an active clearance". Press a score
   (or point at it) to see how it adds up: "start 50 · title matches +25 · posted this
   week +10 · in Phoenix +5 = 90". Openings first saved since your last visit (the desk
   keeps when you last looked, in `desk.json`) are tagged **New**, and stay so while you're
   on the page, and the bar over the list has a **New** group. Ones listed only as "US -
   Multiple Locations", "Remote - US (Field Based)" and the like are kept and marked
   "check the posting": you may be able to do them from your area.
   **Not interested** on an opening hides it, with a reason if you give one (Title,
   Company, Location, Pay or Other). It's never applied to, not even by **Apply to
   selected**; **Undo** (at the foot of the window, or by its row under the bar's **Not
   interested** group) brings it back. The mark is the job's, so the same posting found on
   Indeed and on the employer's site stays hidden on both, and Claude asks you before
   applying to it. For Company, that employer's other openings score 10 lower, said in their
   score's breakdown.
   It searches only when you press it, unless you turn on **Search by itself every 12
   hours** (under **More**, at the end of the Openings head; off by default; it waits while
   the queue is paused). Each switch that's on is said in a line under the head. A search running is
   shown in the header, with **Stop search** beside it.
2. Tick the ones you want, or press **Select recommended**, then **Apply to
   selected**. **Select recommended** leaves out openings whose posting asks for a
   degree you don't have (unless it says "or equivalent experience"), and ones the
   desk couldn't open to check, or whose posting puts the job in another state.
   They're under **All**, with the reason, so you can still tick them yourself.
   The desk works through them in the desk's browser window, two at a time, each in its own
   tab (**Jobs at once**, beside Submit for me, sets 1, 2 or 3), the ones most likely to
   go all the way first: employers where the plugin's live check reached the review page,
   then job systems it usually gets through, then the rest, and last LinkedIn's and Indeed's
   (their Submit is always yours) and employers where the desk met bot checks more often
   than not. Each group keeps your order, and a job you start alone, or **Carry on**, isn't
   moved. It
   clicks into each application, fills every page, adds Workday's work and
   education blocks, and stops at each review page. It never works on two jobs at
   one employer's site at once, and only one job at a time signs in, makes an account or
   waits on an emailed code. **Working on** shows each job under way.
3. **Needs you** lists what it can't do alone:
   - Questions your profile doesn't answer. You answer them on the page, and they're
     remembered for later applications unless you untick that. A remembered answer
     fills the same question, worded the same way. An answer about one employer
     ("Why do you want to work here?", "Have you worked for us before?") isn't reused
     for another.
   - A dialog open over a form. You answer it in the desk's browser window and press **Carry on**, and the
     desk fills the form in behind it. It agrees for you to an employer's notice about AI
     screening of your application (Eightfold's employers show one as your resume goes up)
     and to its privacy notice and terms of use (a privacy agreement's "I Accept", a terms
     box; never a newsletter, job alerts or a talent community, texts or automated calls, a
     legal waiver, or the terms of your employment), and picks an application's attestation
     that its information is true and complete, or
     your consent to the background check that comes with applying, noting each in the job's
     log. To answer those yourself, set `accept_notices: false` under `settings:` in your
     profile. Practice mode never agrees to them.
   - Sign-ins, bot checks, CAPTCHAs and emailed codes. You deal with those in the
     desk's browser window, and the desk carries on by itself (with a site's password and your
     email app password saved in **Site passwords**, below, it does the sign-ins and the
     codes itself). A link a site emails you to
     confirm your address opens in your usual browser, so reload that job's tab in the
     desk's browser window once you've opened it. The other jobs wait while you work in that
     tab: none starts (those already under way go on in their own tabs), and no other tab
     comes to the front. If nothing happens in it for 5 minutes, they go ahead without it, so you can
     leave a batch running and come back. Finish each waiting job in its tab later, and
     the desk picks it up again by itself.
   - Press **Alert me** in the desk's header to get a desktop notification whenever a
     job needs you or is ready to submit, so you can leave the desk working in the
     background.
4. Press **Submit** on each finished application, or turn on **Submit for me**, so
   that every application that needs nothing from you is sent. LinkedIn and Indeed
   are always yours to submit. Each finished application's card (and a stopped one's)
   has a **What went in** panel: the answers the desk gave on each page, the files it
   put up, what it agreed to for you, any account it made and the optional questions it
   left empty, so you can check them without reading every review page again. It never
   shows a password, an emailed code or a security answer.
5. Pressed **Skip job** by mistake? **Undo** on its card (or by its row in the list) puts it back
   where it was, back in the queue or in the list, and sends nothing: it stops at its review page for you.
6. A posting the site says has closed or been filled is skipped by itself, with the site's
   words on its card (**Undo** puts it back). A saved draft from an earlier visit (Workday's
   **Continue Application**) is picked up where it was, and its answers are checked against
   your profile. If the desk changes any, the card's **What went in** lists each one
   ("old → new") and the job waits on its review page for you, even with **Submit for me**
   on. A page that loads blank is loaded again once or twice before it asks you. A job the
   employer's site stops with its own passing error (its error page, "Something went wrong"
   again after a reload, "try again later", a page that never loads) is tried again by itself
   about 20 minutes later, once, opened afresh in its tab: its card says when, with **Try now**,
   and **Pause queue** holds it. If that tab changed since, or you opened another on the site,
   the desk leaves it to you; and if the site fails again, the card stays for you. A job the
   desk tried again by itself is never sent by itself, even with **Submit for me** on: it waits
   on its review page for your Submit, in case it went in meanwhile elsewhere. A site that turns
   the desk's browser away, can't be reached or limits how often it's asked, bot checks, and
   sign-in or account steps are never tried again by themselves.

#### Cover letters

Turn on **Write a cover letter for each job** (beside Submit for me) and each job you
apply to gets a cover letter of its own, written while the job waits in the queue and
uploaded wherever its application has a cover letter box:

- **Who writes it:** Claude, through your own Claude Code sign-in: the desk runs the
  `claude` command in the background, so it counts against your Claude plan like any other
  use of Claude Code, and there's no API key to set up (though if `ANTHROPIC_API_KEY` is set
  where the desk's server runs, Claude Code uses that key, billed to it, instead of your
  plan). If `claude` isn't on this
  computer or isn't signed in, the desk says so once at the top of the page (sign in by
  running `claude` in a terminal and typing `/login`), and your applications go on with your
  usual cover letter (`documents.cover_letter` in your profile), or none.
- **What it says:** one page from your resume, your profile and the job's posting, using
  only what they say: never a degree, certification, clearance, years of experience or skill
  they don't state (coursework isn't a degree), no made-up numbers, names or contact details,
  and no blanks like [Hiring Manager] left in. Your name and contact line come from your
  profile. The posting is only Claude's material: anything in it that tries to give Claude
  instructions is ignored, and Claude runs with no tools, none of your MCP servers or
  plugins, and none of your own settings or hooks (an organization's managed settings, and
  any hooks they set, still apply). The
  desk then checks each letter before it's used: one that claims more years of experience than
  your profile states, a number that isn't in your resume, profile or the posting, a degree,
  diploma, clearance (or a higher level of one), certification or license your resume and
  profile don't show (a profile answer of "None" or "Eligible" shows nothing), a greeting to
  someone the posting doesn't name, or your street address is set aside, and that job gets your
  usual cover letter. Then Claude reads it once more, the same way (a second call, given the
  letter, your resume and profile but never the posting), and one with any claim they don't
  support is set aside too. If 4 letters in a row fail these checks, the desk says so once and
  uses your usual cover letter until you turn the switch off and on (or restart the desk).
- **The first one waits for you:** the first letter the desk writes shows on its job's
  card. Press **Use it** and every letter after it goes in without asking; to change how
  they're written, tell Claude in the chat what to change; or press **Turn cover letters
  off**. Each job's log says when its letter was written, and its card's **Cover letter**
  opens it. The letter is saved in the job's folder, and a job tried again keeps it.
- `settings.cover_letter_model` in your profile picks the model (the usual one is
  `claude-opus-5-5`, at medium effort; `claude-sonnet-5-5` is lighter on your plan's usage).

Each employer has its own account on its job site. In the desk's **Site passwords**
card, save one password for a job system: Workday (15 of the semiconductor employers and 27 of the
Phoenix list's), SuccessFactors
(Edwards, Qorvo, Amkor), iCIMS (Daifuku), ApplicantStack (SCREEN), UKG Pro (Nikon
Precision), Infor (Benchmark) or Taleo (Kforce). The desk then creates your account at each of that system's
employers (your email, the password, and your name and country from the profile; it ticks
their terms, never a newsletter, and a consent to be considered for other open positions only
where the site won't make the account without it, then presses the button; the form's security
questions come up on the job's card for you to answer, go into that form alone and are never
remembered; a picture code stays yours) and
signs you in after that (pressing the sign-in form's own button, even one that reads
"Submit"). When the password doesn't sign in at an employer, the desk tries it once, then,
with the email app password below saved, resets that employer's password to the saved one
through the site's own "Forgot password" email, which it reads itself. When the site says it
has no account for your email, or no reset email comes in 3 minutes (most likely your first
application there; the other jobs carry on meanwhile), or its reset isn't one the desk can do
(a security question, say: those stay yours), it opens that employer's Create
Account form ("Create an account", "Register", "Don't have an account yet?") and creates the
account. Without the email app password it tries Create Account first, and asks you to
open the reset email where the site says the email already has an account. An account you made
there before that was never verified (the site refuses the sign-in with "Verify your account
before you sign in") isn't reset: with the email app password the desk presses the site's
"Resend Account Verification" once and opens the new email's link; without it the card names
that button. To do these steps yourself, set
`manage_accounts: false` under `settings:` in your profile: the desk then fills in Create
Account for you to finish. Practice mode never creates an account. On Qorvo's one-page application
it puts the password in both boxes. The password goes from the page straight into
`~/.job-apply/secrets.yaml` on your computer, and is only ever typed into that system's
own addresses (for Workday, `*.myworkdayjobs.com` and `*.myworkday.com`). Claude never sees
it, and the desk only reports whether one is saved.

The same card takes an **email app password** for the address in your profile (for
Gmail, make one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords);
it needs 2-Step Verification; Yahoo, iCloud and AOL addresses work the same way, but
Outlook.com and Hotmail no longer accept app passwords). With it saved, a job waiting on
an emailed code or confirmation link gets it from your inbox:
- the desk types the code in and presses Verify, Confirm, Continue or Next (never
  Submit), or opens the link in its own browser and reloads the job's tab;
- it reads only mail from that job's site or its job system, sent after the job began
  waiting, for up to 15 minutes, in your inbox and, when the inbox has none, in your
  Spam (Junk) folder, where a site's first mail sometimes lands;
- it opens them read-only, so nothing is marked read, moved or deleted.

If the app password is turned down, the desk page says so and stops trying until you
save it again (a new one, or the same one if the mail service was only having trouble).
A link is only opened when its address is the job's own site. The app password is only
ever used to read those emails, and the password-reset email a site was just asked for:
Claude can't have it typed into any page.

The desk works in each job's own browser tab and follows only the tabs an application
opens itself, so a tab you open (to check your email, say) is left alone. Once a job has
gone in, its tab stays open for you to see the confirmation while it's among the three newest
that did; older ones are closed, so a long queue doesn't leave dozens of tabs open. A job
waiting on you, or that failed, keeps its tab. Jobs already marked applied are never queued
again. If Submit doesn't bring up a confirmation, the job
waits in **Needs you** with **I submitted it myself** rather than being counted as sent, and
**Submit for me** never presses that job's Submit again (even after a restart): picked
again, it stops at the review page for you. A job-alerts or newsletter box with its own
Submit (in a site's footer, say) is never taken for the application's.

A restart (of the Claude app, a crash, a plugin update) doesn't lose the queue. Once the
desk is open again, the jobs it hadn't begun are back in the queue in their order, paused:
a notice at the top says how many, and how many of them Submit for me will send once
they're filled (it applies only if it's still on), and nothing starts until you press
**Carry on with the queue**. A job it had begun isn't queued again by itself, as you may have sent it
on the site: its card says the desk restarted before the application was finished, with
**I submitted it myself**, **Carry on** (filled again up to its review page, never sent) and
**Skip job**. A Submit you pressed before the restart wasn't sent, and that
card says so. A job whose Submit was being pressed, or showed no confirmation, may have
gone in: its card asks you to check. A desk page you left open has to be opened again (ask
Claude to open the job desk): its key is new each time the desk starts.

**Clear the queue** (beside Carry on, and on that notice) takes out every job the desk hasn't
begun, after asking: they go back to the openings, not skipped, to apply to again. Jobs it had
begun keep their cards, and a Submit you pressed still goes. **Find jobs** searches again
whether or not a queue is waiting. To leave the desk going through a long list, turn on
**Don't wait for me** (beside Jobs at once, now up to 5): a sign-in, or an emailed code only you
can read, then waits on its card while the other jobs go on (never another job at the same
site). A code the desk reads itself (from your inbox, or Claude from your Gmail) still holds
them while it's looked for, usually a minute: one code at a time, so it never goes to another job. The **Company** box above
the openings shows only the companies you choose (type any part of a name, or pick one it
suggests).

Each line of a job's log on the desk shows the time it happened. Each time the desk is done
with a job for now (at its review page, sent, stopped for you, failed, skipped, cut short by
the desk stopping, or marked as applied), it writes a line in `~/.job-apply/runs.jsonl`: the
job, when it began and ended, the minutes it took, how it ended and why, and how many steps
it went through. Your answers, codes and passwords are kept out of it, as far as the desk can
tell. **This week** in the header sums the last 7 days up: jobs filled to their review page,
sent by the desk and marked as applied, its stops for you by kind, failed and skipped, the
time with the desk (waits included, and jobs filled at once overlap), and the employers it
stopped at most, with the latest lines. Or ask Claude how the week went.

**Safety** in the header lists what the desk does in your name, and what it never does, as
your settings stand now: practice mode, Submit for me, LinkedIn and Indeed (always yours),
agreeing to notices, making accounts, cookie banners, emailed codes and the email app
password's limits, reading replies, Jobs at once, Troubleshooting, bot checks (always yours)
and passwords. Each line says what it means for you ("Submit for me: off. The desk stops at
each review page; you press Submit.") and where to change it. It only shows them: nothing
there changes a setting.

**A morning check.** Ask Claude "what's new?" and it calls `whats_new`: the openings first
saved since its last call (or since a day or time you give), best score first. It doesn't
search by itself, so for fresh ones, have it search first (or turn on Search by itself every
12 hours). Claude can offer to do both each morning.

**My applications** (beside Recommended and All) lists each application you sent in,
newest first, with where it stands and when the employer last wrote: "Heard 2026-10-03:
confirmation" (point at it for the email's one-line summary), or "No reply yet". The bar
over it groups them: **Waiting**, **Heard back**, **Interviews** (interviewing or an offer)
and **Closed** (rejected or withdrawn). One you applied to 10 days ago or more
(`follow_up_days`) with no email at all, not even a "we received your application", is
marked with the day its follow-up became due, and **Follow up** copies a request for you
to paste to Claude, which drafts a polite follow-up as a Gmail draft, for you to send. An interview's row has **Prep me**, which copies a request
("Prep me for my interview: job 12, ...") for you to paste to Claude, which then prepares
you for it. The replies come from `/job-apply:track-responses`, which reads them through
the Gmail connector with `email_tracking` on, never with the desk's email app password, and
can offer to run that check every day as a scheduled task.

To add jobs from anywhere else, paste their links (LinkedIn, Indeed or any company
site) into the box above the list. The desk reads each posting, scores it and ticks it
for Apply. Jobs Claude saves from Indeed or LinkedIn show up in the same list. The same
job from Indeed and the employer's own site is one row (and one application), applied for
on the employer's site, with an **Also on Indeed** link. Where the desk can't be sure it's
the same job (another opening alike it, a repost weeks later, or one you already started
or applied to on Indeed), it keeps both and asks first: **Apply anyway** or **Skip job**.
Without Claude Code, run `uv run --project <plugin>/server job-apply-desk`.

**Bot checks.** The desk never tries to get around Cloudflare, CAPTCHAs or other bot
checks: no disguised browser, no CAPTCHA-solving services. When a site shows one, it
pauses that job and brings its tab to the front for you. The desk's browser window keeps
its own profile, so once you've passed a site's check, that site usually lets it
through for a while. TSMC's careers site turns automated browsers away entirely, so
apply there in your everyday browser, or through the same posting on Indeed.

## Where your data lives

Everything personal stays on your machine in `~/.job-apply/`. Set
`JOB_APPLY_HOME` to put it somewhere else.

| Path | Contents |
|---|---|
| `profile.yaml` | Your answers. Edit it any time ([template](plugins/job-apply/templates/profile.example.yaml)). |
| `resume.pdf` | Default resume |
| `secrets.yaml` | Optional career-site passwords, such as `workday_password`. Save one from the Job Desk, or write the file yourself and run `chmod 600` on it. A password named for a job system goes only onto that system's sites; any other only onto that site's own domain (`acme_password` on acme.com) or its tenant on a shared job system (acme.wd5.myworkdayjobs.com), never an address that merely contains the name. |
| `tracker.db` | Application tracker (SQLite). `export_jobs_csv` writes a spreadsheet. |
| `answers.yaml` | Answers you gave in the Job Desk, reused on later applications. Edit or delete entries freely. |
| `companies.yaml` | Optional: which of the plugin's employer lists to search (`lists:`) and employers of your own, in place of the default list. |
| `recommendations.json`, `desk.json` | The Job Desk's last search, and its switches ("Submit for me", "Jobs at once") and when you last looked at it |
| `openings_found.json`, `whats_new.json` | When each opening was first found (for **New** and `whats_new`), and when `whats_new` was last called |
| `desk-queue.json` | The Job Desk's queue, kept for a restart: each job's number and where it was, never your answers |
| `runs.jsonl` | The Job Desk's history: a line each time it's done with a job for now (when, how long, how it ended and why), with your answers, codes and passwords kept out as far as the desk can tell. The newest 4000 lines or so are kept. |
| `browser/` | The desk's browser's profile, which keeps your sign-ins |
| `applications/<id>-<company>-<title>/` | Tailored resume and cover letter, a reference list made for the job, screenshots, submission record, `debug/` snapshots |

Documents in `profile.yaml`: `documents.resume` (and, optionally, `documents.cover_letter`) go up where a form
asks for a resume or a cover letter. Three more are optional, each one file or a list of files, for the forms that
ask for them (schools, cities, counties and other public employers often do): `documents.transcripts` (unofficial
transcripts), `documents.references` (a reference list) and `documents.certificates` (certificates, licenses or
diplomas). Only a box asking for one of them gets it: never a box for something else (a letter of reference, a form
to sign, a proof of citizenship, eligibility or identity, a driver's license). Name each certificate's file for what
it is (`nursing-license.pdf`, `diploma.pdf`): a box asking for a diploma, a license or a particular certificate gets
only a file whose name says so. A box that takes several files gets every one you have of what it names. With no
reference list file, the Job Desk lays out the people under `references:` (each one's name, and as you like their
relationship, company, phone and email: only what you write) on one page in the job's folder, and uploads that,
only while it holds them as they are. A required box for one you haven't added waits for you, its card naming the
line to add. Your references' names, phones and emails are kept out of problem reports like your own details.

Settings in `profile.yaml`:

| Setting | Default | Effect |
|---|---|---|
| `submit_mode` | `review` | Set to `auto` to let Claude submit complete applications without asking, for the job systems in `auto_submit_ats`. `dry_run` fills everything and never submits, which is good for a first practice run. |
| `email_codes` | `false` | Claude may read the sign-in codes and verification links that career sites email you (needs the Gmail connector): when you apply with Claude in the chat, and for the Job Desk while its chat is open and no email app password is saved on it (Claude reads only that job's own mail since it began waiting, and the desk puts the code in) |
| `email_tracking` | `false` | Claude may search your email for replies to your applications (needs the Gmail connector) |
| `accept_notices` | `true` | The Job Desk agrees for you to an employer's notice about AI screening of your application and to its privacy notice and terms of use (never a newsletter, job alerts or a talent community, texts or automated calls, a legal waiver, or the terms of your employment), and picks an application's attestation that its information is true (or your consent to its background check), noting each in the job's log. Never in practice mode. `false` leaves them to you. |
| `accept_cookies` | `false` | The Job Desk declines cookie banners. One with no way to decline (TI's "Agree and Proceed") waits for you, unless this is `true`: then the desk accepts it for you. |
| `manage_accounts` | `true` | The Job Desk creates your account at a job system with the password you saved on the desk for it (ticking the site's terms, never a newsletter), resets a password the site refuses through its "Forgot password" email, and presses "Resend Account Verification" for an account never verified. A form's security questions are asked on the desk's card and never remembered; a picture code stays yours. Never in practice mode. `false`: the desk fills in Create Account for you to finish. |
| `jobs_at_once` | `2` | How many applications the Job Desk fills at the same time (1 to 3), each in its own tab of the desk's browser window. Never two at one employer's site or job-system tenant; one at a time signs in, makes an account, resets a password or waits on an emailed code; while a sign-in or code needs you, no other job starts and no other tab comes to the front. `1` fills them one after another. The desk's **Jobs at once** buttons change it there (kept in `desk.json` until this setting changes). |
| `follow_up_days` | `10` | The days an application you sent in waits with no reply (not even a "we received it" email) before the Job Desk's My applications marks it for a follow-up (1 to 90). |
| `auto_submit_ats` | `[]` | With `submit_mode: auto`, the job systems Claude may submit on without asking (`workday`, `greenhouse`, `lever`, `eightfold`, `successfactors`). The Job Desk has its own switch, **Submit for me**. |
| `browser_channel` | `chrome` | The browser the desk drives: `chrome`, `msedge`, or `chromium` (Playwright's bundled browser). `headless: true` hides its window. |

## What's inside

```
.claude-plugin/marketplace.json      marketplace entry (sebob-jobs)
plugins/job-apply/
  .claude-plugin/plugin.json
  .mcp.json                          starts the MCP server with uv
  skills/
    setup/        build the profile from your resume, sign in to sites
    apply/        the application workflow + per-ATS notes (references/ats-notes.md)
    find-jobs/    search Indeed (via the Indeed connector) and company career sites
    track-responses/  read replies from Gmail and update the tracker
    interview-prep/   research, likely questions and STAR stories for an interview
    desk/         open the Job Desk page (recommended jobs, one-button applying)
  data/companies.yaml                semiconductor employers in Arizona, careers URL + ATS
  templates/profile.example.yaml
  setup/install.ps1, install.sh      the one-step installers (Windows; macOS and Linux)
  server/                            Python MCP server (Playwright browser automation)
```

### MCP tools

| Tool | Purpose |
|---|---|
| `setup_status`, `get_profile` | Check what's missing and read the profile |
| `search_area` | Read or change what and where Find jobs looks: the job titles, the places (a city and state each) and the plugin's employer lists, as the desk's What and where window does |
| `common_questions` | List the questions most applications ask that the profile doesn't answer (desired pay, start date, shifts...) and save the person's answers, as the desk's Common questions card does |
| `doctor` | Check this computer's setup (Python, uv, the plugin's version, the browser, the profile and resume) and say what to fix; the same as the `job-apply-doctor` command |
| `search_company_jobs` | Search employers' own job boards by keyword and location: Workday, Greenhouse, Lever, Eightfold, SmartRecruiters, Oracle, ApplicantStack, Taleo, Talemetry, iCIMS Jibe, Jobvite, Cornerstone, Phenom, amazon.jobs and Phoenix Children's own site. ASML's site, iCIMS portals, Paycom and UKG Pro boards, SuccessFactors' newer search (Edwards) and older career sites (Amkor) and Infor CloudSuite boards (Benchmark) are read through a background browser tab |
| `ingest_job`, `add_job` | Save a posting. Parses Workday, Greenhouse, Lever and SmartRecruiters APIs, schema.org JobPosting data, or page text. |
| `list_jobs`, `get_job`, `update_job`, `export_jobs_csv` | Application tracker |
| `open_application`, `click`, `tabs`, `close_browser` | Navigate the browser, and close it. `click` refuses final submit buttons, and a cookie banner unless `accept_cookies` is on. |
| `inspect_form` | Every field on the page, including iframes and custom dropdowns: label, type, options, required |
| `autofill` | Fill everything the profile answers and return the fields that still need a decision |
| `add_entries` | Click "Add" until there's one Work Experience / Education block per profile entry |
| `fill_form`, `fill_secret` | Fill specific fields; type a stored password without exposing it |
| `screenshot`, `page_text` | See the page |
| `debug_snapshot` | Save the page (HTML of every frame, a screenshot, the extracted fields). This also happens automatically when a field fails; a job's folder keeps the newest 20. |
| `forget_saved_pages` | Delete the saved pages, screenshots and problem reports' pages (a job's, or every job's): they show the page as it was, your details on it among them. The pictures taken before and after a Submit stay, as the record of what was sent. |
| `render_document` | Markdown resume, cover letter or reference list → PDF in the job folder |
| `tailoring_queue` | The Job Desk's jobs waiting for a tailored resume, with each posting, the resume and the rules |
| `report_problem` | A problem report for a job, scrubbed, to file as an issue (`anonymous=True` leaves out which job it was) |
| `submit_application` | Final submit, enforcing the rules above, and record the result |
| `log_email`, `logged_emails` | Record employer replies; each email counts once and statuses only move forward |
| `open_job_desk` | Open the Job Desk page: recommended openings ranked against the profile, one-button applying |
| `desk_history` | How the Job Desk's last few days went, from its history (`runs.jsonl`), as its This week window shows it |
| `whats_new` | The openings first saved since its last call (or a given day or time), best score first, with a short list to say them in; it never searches by itself |

## Development

```
cd plugins/job-apply/server
uv sync --extra dev
uv run playwright install chromium
uv run pytest -n auto
```

`-n auto` runs the tests across every CPU core (pytest-xdist); each test has its own
temporary `JOB_APPLY_HOME` and browser, so they don't get in each other's way. Plain
`uv run pytest` runs them one at a time.

`uv run ruff check .` (lint for bugs: unused or undefined names, bugbear's patterns; it never
reformats) and `uv run mypy src/job_apply` (type check) run in CI ahead of the tests; run them
before opening a PR.

The tests cover ATS detection, posting parsing, the profile-to-field matching
rules, the tracker and email log, PDF rendering, fixture redaction, and
end-to-end browser runs (headless Chromium) against mock forms (a generic form,
Workday-style dropdowns, search pickers and My Experience blocks, an embedded
iframe form, the submit guard, and the LinkedIn/Indeed submit rule) and against
pages captured from real career sites and scrubbed (`tests/fixtures/live`). GitHub
Actions runs them on Python 3.10 and 3.13, and on Windows, and validates the plugin manifests.

### Testing against real career sites

The `live-smoke` workflow (Actions tab → live-smoke → Run workflow) runs
`scripts/live_smoke.py` on GitHub's runners, which can reach the job sites. For
every company in `companies.yaml` it:
- searches for Arizona field-service and equipment roles;
- reads one real posting;
- opens its application in headless Chromium;
- clicks through "Apply" and "Apply Manually";
- lists the form's fields and autofills them with a fake test profile.

Submitting is off (`JOB_APPLY_NEVER_SUBMIT=1`), the profile is a fake applicant's with a
test resume made for the run, and it never contacts LinkedIn or Indeed. Without
`--test-identity` it never signs in or creates accounts; with it (the nightly's accounts
check) the test identity, a clearly fake applicant with an inbox of its own, makes
accounts at the listed employers and reads their emailed codes, and still never submits
(`scripts/TEST_IDENTITY.md`). The job summary shows
a table, and the log has one `LIVE_RESULT` line of JSON per company. Tick
"Commit captured pages" to save the pages as regression fixtures.

To run it yourself, add `--pipeline --fake-passwords` to drive the Job Desk's
one-button pipeline instead. It checks the default semiconductor list; add
`--lists phoenix-metro` (or `--lists phoenix-metro,semiconductor-az`) to check other lists. Add `--parallel 4` to check four employers at a time,
each in a browser of its own; every site is still visited once. A full run then
takes about 6 minutes instead of 22.

**Nightly live check.** The `live-nightly` workflow runs four checks, each on a machine
of its own, four employers at a time: the pipeline check on the semiconductor list
(technician jobs), the pipeline check on the phoenix-metro list for HR jobs (`--role hr`,
an applicant with an HR background), the search check on both lists, and the accounts
check (the test identity at every employer on both lists). It's scheduled for 3:17 AM
Arizona time, but GitHub starts schedules hours late, so a routine dispatches it each
morning (or run it from the Actions tab). Each check has its own table in the issue; the
finance check is paused.
`scripts/live_compare.py` boils each employer down to one result that holds steady
from night to night: where the pipeline ended (`ready`, `needs_you sign_in`,
`needs_you stuck`, ...), or whether the search works (`works`, `error`, or `no search`).
How many openings a search finds changes daily and is never compared. The issue titled
"Nightly live check" (label `live-check`) keeps the last results in its body, and gets
a comment, which notifies you, only when an employer's result changes or an employer
joins or leaves a list. The body also names the employers an earlier run took to the
review page that the last run didn't, with the date each last got there. It's reopened if
it was closed. A second issue, "Nightly live trends" (label `live-trends`), keeps the
history: the last 60 runs' counts per check and median times, and the employers whose
result keeps flipping; its body is rewritten each run, without a comment. A check that doesn't finish
fails the run, and the employers it didn't reach keep their last result. Each run's
reports and logs are kept as an artifact for 7 days. GitHub pauses scheduled workflows
in a repository with no activity for 60 days.

### Reporting a problem

When the Job Desk gets a job wrong, press **Report a problem** on that job. It writes a
report to `~/.job-apply/reports/<job>-<time>/`:
- what the desk did, step by step;
- each page it saved (the last pages it stopped on for you, and any page a fill failed on),
  with every field's label and whether it was filled (never what was in it);
- `report.zip`, which also holds the saved pages.

The profile's personal details, plus your city, schools and employers, are replaced by
`REDACTED`. Web addresses lose their session codes, and screenshots are never included.
The desk then shows you the whole report, with a link that opens a new issue on this
repository with the report's text filled in. Nothing is sent unless you follow the link and
file the issue. GitHub issues are public: anyone can read one, and see which job you
reported, so read it over first. Tick **Don't say which job it was** in the report to make it
again with only the job system (Workday, iCIMS, ...) and where it stopped: the employer, the
job's title, its addresses and its requisition number are left out of the issue and the saved
pages. The saved pages in `report.zip` aren't in the issue, because a filled-in form can
still show your answers; they stay on your computer. Claude can make the same report with
`report_problem` (`anonymous=True` for one that doesn't say which job it was).

The desk also takes a note by itself, with nothing pressed, each time a job stops on
something it most likely got wrong: it's stuck, at a sign-in, its Submit didn't go through
or showed no confirmation, or an answer didn't go in. Bot checks, CAPTCHAs, emailed codes
and questions your profile doesn't answer are yours, so they aren't noted. **Notes (N)** in
the desk's header shows them all as one issue: **File on GitHub** opens it, **Copy all**
copies the whole text (for when it's too long for the link), and **Clear** removes them once
they're filed. A note holds less than a report: none of your answers, and each employer said
by its job system ("a Workday employer") rather than named, since one issue holds many jobs
(the addresses of the pages it stopped on still show the site). The newest 50 are kept in
`~/.job-apply/notes/`.

### Turning a failure into a test

When a field won't fill on a real site, the plugin saves a snapshot to
`~/.job-apply/applications/<job>/debug/<time>/` (and the Job Desk keeps the last three pages
a job stopped on for you, in `debug/<time>-stop/`). To turn it into a regression
test, run:

```
cd plugins/job-apply/server
uv run python -m job_apply.fixtures ~/.job-apply/applications/<job>/debug/<time> workday-my-information
```

This writes `tests/fixtures/live/<name>.html`, with scripts removed and your
profile details replaced by `REDACTED`, plus `<name>.expect.json`, which lists the
fields the extractor must keep finding. Fix the extractor or the matching rules,
trim the expectations to fields you've checked, look the HTML over for any other
personal data, and commit. `tests/test_live_fixtures.py` picks it up
automatically.

## dev-kit: development practices for any project

This marketplace also carries **dev-kit**, a small plugin for working on code with
Claude Code in any project. It isn't specific to job applications. It holds:
- a practices skill Claude follows while it works: tests that run in parallel, CI with
  required checks and auto-merge, a review pass on every PR, a test for every fix that
  fails on the old code, fewer themed PRs, version bumps, parallel agents, handoff notes;
- `/dev-kit:check`, which audits a repo against those practices and sets up what's
  missing with your OK, including a `CLAUDE.md`, so cloud sessions on that repo follow them too;
- a one-line reminder at the start of each session to run the check on projects that
  haven't had one in 30 days.

Install it like job-apply: `/plugin install dev-kit@sebob-jobs` in Claude Code, or
`claude plugin install dev-kit@sebob-jobs` in a terminal.

## Limitations

- Real career sites change often. The tests run against local forms and pages captured
  from real sites, and the nightly check runs the desk at every listed employer, but a
  site can still change between runs: expect Claude to sometimes fall back to
  `inspect_form` and `screenshot` and work field by field. The debug snapshots and the
  desk's problem reports exist to turn those cases into fixes quickly.
- Many company job systems (Workday, SuccessFactors) need an account per employer. The Job Desk
  creates them with a password you save on it (`manage_accounts`, on unless you turn it
  off) and reads the emailed codes and links with an email app password saved there (or,
  with Claude, `email_codes`); without those, the account and the code are yours. Bot
  checks and CAPTCHAs are always yours.
- The first launch of the MCP server installs its Python dependencies, which takes
  a minute. If `/mcp` shows `job-apply` as failed right after install, reconnect it.
