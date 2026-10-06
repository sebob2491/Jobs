---
name: apply
description: Apply to jobs by filling the application in the user's browser with the job-apply tools — LinkedIn Easy Apply, Indeed, Workday, Greenhouse, Lever, Eightfold, SuccessFactors, Oracle and any company careers site. Use whenever the user shares one or more job links and wants to apply ("apply to these", "submit my application to…"), or asks to work through jobs saved in their tracker.
---

# Apply to jobs

You drive a real, visible browser through the `job-apply` MCP tools. The user can
watch the window and take over at any point. Your job is to get each application
to its final review page, filled out accurately, and then submit it under the rules
below.

## Rules

- **Only state facts the user gave you.** Answers come from `~/.job-apply/profile.yaml`,
  their resume, or the user directly. Never invent or stretch experience, degrees,
  certifications, years, clearances, citizenship or work authorization. If the
  profile doesn't answer a question, ask.
- **Submitting:** `open_application` returns a `submit_policy`:
  - `after_user_confirms` (default): show the review page (`screenshot`), sum up the
    key answers, and call `submit_application(job_id, user_confirmed=true)` only
    after the user says to submit *this* application.
  - `auto`: the user opted in for this ATS in `settings.auto_submit_ats`. Submit
    without asking only if every required field is filled and you wrote no free-text
    answer the user hasn't approved. Otherwise ask.
  - `user_clicks_submit` (LinkedIn and Indeed): their terms prohibit automated
    submission, and they restrict accounts that break the rule. Fill everything,
    call `submit_application` (it marks the job `ready_to_submit` and does not click),
    then ask the user to click Submit in the browser. When they say it went
    through, call `update_job(job_id, status="applied")`.
- `click` refuses final submit buttons. Don't work around it with other selectors.
- CAPTCHAs, email verification codes, sign-ins and passwords belong to the user. Ask
  them to handle these in the browser window, or use `fill_secret` with a secret
  they stored themselves. Never type a password with `fill_form`.
- Check `list_jobs` before starting so you never apply to the same posting twice.

## Workflow for each job

1. **Preflight (once per session).** Run `setup_status`. If `profile_complete` is
   false, stop and run the `setup` skill (`/job-apply:setup`) first.

2. **Ingest.** Run `ingest_job(url)`. If LinkedIn or Indeed refuses plain HTTP, retry
   with `use_browser=true`. For Indeed you can also use the Indeed connector's
   `get_job_details` if it's available, then `add_job`. Read the description.

3. **Fit check (2–4 lines).** Compare the posting's hard requirements with the
   profile: degree, years, US person/citizenship (common in semiconductor jobs
   because of export control), clearance, travel %, shifts, cleanroom, lifting.
   Flag real blockers and ask before applying to a clear mismatch. If the user
   skips the job, call `update_job(status="skipped")`.

4. **Tailored documents (optional; ask once per batch).** If the user wants them,
   write a cover letter (and/or a tailored resume) grounded in their real resume.
   Save it in the job's `folder` as `cover_letter.pdf`/`.docx` (and `resume.pdf`/`.docx`).
   `autofill` uploads files from the job folder ahead of the profile defaults.

5. **Open the form.** Run `open_application(job_id)`, then click through to the form:
   "Apply", "Easy Apply", "Apply now", "Apply Manually", "Autofill with Resume".
   ATS-specific steps are in [references/ats-notes.md](references/ats-notes.md). If
   the site asks the user to sign in or create an account, see that file.

6. **Fill loop, one page at a time:**
   1. `autofill()` fills what the profile answers and returns `needs_input`
      (required fields first).
   2. For each field in `needs_input`:
      - A factual question the resume or profile clearly answers (e.g. "years of
        experience with vacuum systems") → answer it, conservatively.
      - Free text ("Why do you want to work here?") → draft 2–4 specific sentences
        from the posting and the resume. Show the user the draft before you fill it,
        unless they already told you to draft on your own.
      - Anything else, including consent and attestation checkboxes → ask the user.
        Put all of a page's questions in a single message.
   3. `fill_form([{id, value}, ...])`. For `checkbox_group`, pass a list.
   4. If a widget fails, or you aren't sure what the page shows, run `inspect_form`
      or `screenshot`. Fix anything listed in `errors`.
   5. `click` the "Next", "Continue", "Save and Continue" or "Review" button, and repeat.
   6. When the user answers a question that will come up again (lifting, cleanroom,
      background check, relatives…), offer to save it to the `answers:` list in
      `~/.job-apply/profile.yaml` so `autofill` handles it next time.

7. **Review and submit.** On the final review page, run `screenshot(full_page=true)`,
   list the answers that matter (work authorization, salary, free-text answers,
   uploaded files), then follow the submit rules above. After an automated submit,
   check `confirmed`. If it's false, read `page_text` before you call
   `update_job(status="applied")`.

## Several jobs at once

Ingest all of them first, then show one table: id, company, title, location, ATS,
fit notes. Collect every question you can answer up front (salary, start date,
relocation) in a single message. Then work through the applications one at a time,
and give a short status line after each. End with `list_jobs` so the user sees the
tracker. Don't open several applications in parallel.

## Tracker statuses

saved → in_progress → ready_to_submit → applied → interviewing → offer, or
rejected, withdrawn, skipped. Use `update_job` whenever the user mentions an
outcome ("Lam emailed me for an interview"). `export_jobs_csv` writes a
spreadsheet.
