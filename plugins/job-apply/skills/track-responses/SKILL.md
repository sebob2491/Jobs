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
   `ready_to_submit`, `interviewing` or `offer`. Then run `logged_emails` to get the
   thread ids you've already recorded.

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
   For a thread with several messages, classify and record its newest one. The tool
   never moves a status backwards and ignores a message it has already seen.

5. **Report** in a short table: company, role, what arrived, new status. Then list
   the action items: interviews to schedule, assessments with deadlines, emails
   that need a reply. Offer to:
   - draft replies, saved as Gmail **drafts** only. Never send email for the user;
   - add confirmed interview times to Google Calendar, if that connector is available,
     after the user says yes.

## Rules

- Read only threads that match the searches above. Don't browse the inbox.
- Don't send, archive, label, delete or forward anything.
- Quote at most a sentence of an email back to the user. Summarize instead.

## Running it regularly

To run this check on a schedule, the user can use Claude Code's `/loop` command with
`/job-apply:track-responses`, or set up a scheduled Routine with the same prompt.
