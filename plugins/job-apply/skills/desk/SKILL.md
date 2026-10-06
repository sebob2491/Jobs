---
name: desk
description: Open the Job Desk, a page on the user's computer that lists recommended openings from every employer in the plugin's list, ranked against their profile, and applies to the ones they pick with one button. Use when the user wants a dashboard or interface for their job search, wants to see recommended jobs, wants to apply to several jobs at once, or asks for one-click applying.
---

# Job Desk

The desk is a local web page served by this plugin. It does what the `find-jobs` and
`apply` skills do, but the user drives it with buttons:

- **Find jobs** searches every employer's career site for the profile's
  `preferences.titles` in its `preferences.locations` (or home state). It ranks each
  opening, with plain reasons and concerns under it: a Bachelor's requirement against
  an Associate's, senior titles, clearances, export control, travel.
- **Apply to selected** works through the picked jobs one at a time in the automation
  browser. It clicks through Apply, autofills every page, adds Workday's work and
  education blocks, and stops at each review page.
- **Needs you** collects whatever it can't do alone:
  - questions the profile doesn't answer, answered right on the page. Answers are
    remembered in `~/.job-apply/answers.yaml` unless the user unticks "remember".
    Consent questions default to not remembered;
  - sign-ins, bot checks and emailed codes. The user deals with these in the browser
    window, and the desk carries on by itself once the page moves past them.
- **Workday password** stores one password for the user's Workday accounts (each
  employer has its own). With it, the desk signs in by itself and fills in Create
  Account forms, leaving the terms box and the button to the user. If the password
  doesn't sign in (usually a first application there), the desk opens that employer's
  Create Account form and fills it in. It's only typed into Workday's own addresses.
  The user types it into the desk page; never ask for it in the conversation.
- **Submit** sends one application. **Submit for me** sends every application that
  needs nothing, and stays off until the user turns it on. LinkedIn and Indeed are
  always submitted by the user.

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

## Bot checks

When a site shows Cloudflare's "Just a moment…", a CAPTCHA or a "verify you are human"
page, the desk pauses that job, brings its tab to the front, and waits for the user to
pass the check. It never tries to get around one: no disguising the browser, no
CAPTCHA-solving services. The automation browser keeps its own profile in
`~/.job-apply/browser`, so a site checked once usually lets it through for a while. If
a site refuses automated browsers altogether (TSMC's careers site has), the user
applies there in their everyday browser, or finds the same posting on Indeed.
