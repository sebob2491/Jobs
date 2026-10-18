---
name: track-responses
description: Check the user's email for replies to their job applications (confirmations, assessments, interview requests, offers, rejections) and update the job-apply tracker. Use when the user asks "did I hear back?", "update my applications", "check my email for interviews", or wants a regular status check. Needs the Gmail connector.
---

# Track responses to applications

You use the Gmail connector's tools (`search_threads`, `get_thread`/`get_message`)
to find employer replies, and the job-apply tools (`list_jobs`, `logged_emails`,
`log_email`) to record them. If no Gmail tools are available, say that the
Gmail connector needs to be connected, and stop.

## Consent

Run `setup_status`. If `settings.email_tracking` is false, ask the user once
whether you may search their email for replies to applications. Explain that you'll
only read messages that mention companies in their tracker. If they agree, set
`settings.email_tracking: true` in `~/.job-apply/profile.yaml`. If they decline,
stop.

## Steps

1. `list_jobs` with no filter. Keep jobs whose status is `applied`,
   `ready_to_submit`, `interviewing` or `offer`. Each has `last_heard` (the newest
   email logged for it) and `follow_up_due` (the day a follow-up became due: applied
   `settings.follow_up_days` days ago, 10 unless the profile says, with no email logged,
   not even a confirmation). Then run `logged_emails` to get the thread ids you've
   already recorded.

2. **Search.** Use one query per company, over the period since the earliest applied
   date (at least the last 30 days):
   `"<Company>" (application OR candidate OR interview OR position OR assessment OR offer) newer_than:60d`
   Also search for mail from the ATS platforms, then match the results to a company by
   the body text:
   `from:(myworkday.com OR greenhouse-mail.io OR greenhouse.io OR hire.lever.co OR eightfold.ai OR successfactors.com OR icims.com OR taleo.net OR oraclecloud.com OR paycomonline.net OR paycom.com OR ultipro.com OR ukg.com OR applicantstack.com OR inforcloudsuite.com) newer_than:60d`
   Skip a thread that `logged_emails` already lists, unless it has a message newer
   than the `received_at` listed for it: a rejection or interview invite can arrive
   as a reply in the same thread as the confirmation.

3. **Read and classify** each new thread. Read only what you need to classify it.
   - `confirmation`: "we received your application", "thank you for applying".
   - `assessment`: an online test, questionnaire, video interview (HireVue) or skills check.
   - `interview`: a request to schedule, or a confirmed phone, video or onsite interview.
   - `offer`: an offer letter or verbal offer.
   - `rejection`: "unfortunately", "not moving forward", "other candidates", "position has been filled".
   - `other`: a newsletter, job alert or recruiter outreach for a different role.

   Match the thread to a job by company, plus title or requisition number when the
   company has more than one tracked job. If you can't tell which job it is, ask
   rather than guess.

4. **Record** each one with `log_email(job_id, thread_id, category, summary, received_at)`.
   `summary` is one line, e.g. "Phone screen request from recruiter, reply with availability".
   `received_at` is the message's date (`YYYY-MM-DD`, or its ISO time). The Job Desk's
   My applications shows it as "Heard <date>: <category>", with the summary as its tooltip.
   For a thread with several messages, classify and record its newest one. The tool
   never moves a status backwards and ignores a message it has already seen.

5. **Report** in a short table: company, role, what arrived, new status. Then list
   the action items: interviews to schedule, assessments with deadlines, emails
   that need a reply. Then list the applications still due a follow-up (`list_jobs`
   again: `follow_up_due` set, with a **Follow up** button on the desk, which copies
   "Draft a follow-up email for job <id>, ..." for the user to paste to you): applied that
   long ago with no reply at all. Offer to:
   - draft replies, saved as Gmail **drafts** only. Never send email for the user;
   - draft a short, polite follow-up email for each one due (asking where the
     application stands and restating interest in the role), as a Gmail **draft** only,
     to the address the employer wrote from or the recruiter's (ask the user when there's
     none). The user sends it;
   - add confirmed interview times to Google Calendar, if that connector is available,
     after the user says yes.

6. **Offer a schedule, once.** Unless the user already has one, offer once to repeat this
   check on a schedule (daily, for example), using the scheduled tasks or Routines of
   their Claude app if it has them (within a Claude Code session, `/loop` with
   `/job-apply:track-responses` works too). Say plainly that:
   - it runs on the Gmail connector, with `settings.email_tracking` on: a scheduled run
     with it off stops and says so, without asking;
   - it never uses the Job Desk's email app password, which only reads sign-up codes
     and a site's reset email;
   - each run only logs what arrived and reports it. It drafts replies only when the
     user asks, as Gmail drafts, and never sends anything.

   If they say yes, set it up with a prompt like "Run /job-apply:track-responses: log
   the replies to my applications and report them. Don't draft anything unless I ask."
   If they say no, don't offer again in this conversation.

## Rules

- Read only threads that match the searches above. Don't browse the inbox.
- Don't send, archive, label, delete or forward anything.
- Quote at most a sentence of an email back to the user. Summarize instead.
- Read replies only through the Gmail connector. The Job Desk's email app password
  (`email_password`) is never used for them: it only reads sign-up codes and a site's
  reset email.
