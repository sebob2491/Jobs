# Applicant tracking system (ATS) notes

`open_application`, `click` and `inspect_form` report an `ats` value detected from
the page URL. Use this file for the parts of each flow that `autofill` can't do.

## Sign-ins and accounts (all ATSs)

- First check whether the persistent browser is already signed in. Sign-ins are
  saved in `~/.job-apply/browser` between sessions.
- **Signing in:** ask the user to sign in in the browser window, then continue.
  Alternatively, if they stored a password in `~/.job-apply/secrets.yaml` (for
  example `workday_password: ...`), fill the email with `fill_form` and the
  password with `fill_secret(field_id, "workday_password")`.
- **New accounts:** creating one is the user's decision. Ask first. Then use the
  profile email and `fill_secret` for both password fields. The user must accept
  any terms checkbox. Workday then emails a "verify your account" link. Open it
  yourself if `settings.email_codes` is on, otherwise the user clicks it.
- If you see a CAPTCHA or a "verify you are human" check, ask the user to solve it.
- **Cookie banners** can cover the form, and then clicks fail or no fields show up.
  Which cookies to allow is the user's choice. Ask once per session. If they've said
  it's fine, take the privacy-preserving option: "Reject all", "Decline" or
  "Necessary only". Never accept marketing cookies on their behalf.

## Workday (`*.myworkdayjobs.com`, `*.myworkdaysite.com`): Applied Materials, KLA, Intel, Microchip, NXP, TEL, Analog Devices, Hitachi High-Tech, Entegris, Axcelis, Onto Innovation, Thermo Fisher, ASML (some postings)

- Each company has its own Workday account. Use the same email each time.
- Path: **Apply** → choose **Autofill with Resume** (Workday parses the resume
  into My Experience, so check what it parsed) or **Apply Manually** → sign in
  or create an account → steps.
- Some sites (KLA, NXP, Hitachi) open on a sign-in page that shows only **Sign in
  with Apple / Google / LinkedIn / email** buttons and no fields. Click **Sign in
  with email**, then run `inspect_form` again for the email and password fields.
  Never use the Apple, Google or LinkedIn buttons: they sign in with the user's
  other accounts, so the user does that themselves if they want to.
- Steps: My Information → My Experience → Application Questions → Voluntary
  Disclosures → Self Identify → Review. The Next button reads **Save and Continue**.
  The last button reads **Submit**, which goes through `submit_application`.
- Dropdowns are `listbox` fields, and `inspect_form` opens them to read their options.
  "How Did You Hear About Us?", Country Phone Code, School and Field of Study are search
  pickers (`combobox`): the value is typed and Enter pressed. If the result is a category
  rather than a final option, `click` the option text. Text typed into one without
  picking from its list disappears when it loses focus, so only a picked entry counts.
  In "How Did You Hear About Us?" some entries are groups ("Job Board" holds Indeed,
  LinkedIn, ...): picking one opens its own list, and `fill_form` reports the group's
  entries as `options` so the user can choose one.
- On **My Experience**, call `add_entries("work")` and `add_entries("education")`.
  They click **Add** / **Add Another** until there is one block per entry in the
  profile's `work_history` and `education_history`. Then call `autofill`, which
  fills each "Work Experience N" / "Education N" block from entry N: title,
  company, location, "I currently work here", From/To month and year, description,
  school, degree, field of study, GPA. If "Autofill with Resume" already created
  blocks, check them against the profile and fix them with `fill_form`, rather than
  adding duplicates. Dates are separate Month and Year inputs (`sublabel` in
  `inspect_form`). To fill one by hand, pass `"03"` and `"2022"`.
- Voluntary Disclosures includes a terms-and-conditions consent checkbox. The
  user has to agree to it, so ask.
- Self Identify is the disability form (CC-305). Fill it from `eeo.disability`,
  or ask. The name and date fields there take the full name and today's date.

## LinkedIn (`linkedin.com`)

- **Easy Apply** opens a modal. Click **Next** or **Review** to move through it. Its
  final button, **Submit application**, is for the user to click.
- The contact info is already filled in from the LinkedIn profile, so check that it matches.
  To pick a resume, choose one that's already uploaded or upload the profile resume.
- "How many years of work experience do you have with X?" expects a whole number.
  Take it from the resume, and if the resume shows none, say 0 rather than
  stretching. Ask the user if you aren't sure.
- At the end, a "Follow <company>" checkbox is checked by default. Leave it alone
  unless the user has said otherwise.
- **Apply** (without "Easy") opens the company site in a new tab. The tools switch
  to the new tab automatically, and `tabs()` lists the open tabs. That application
  then follows the rules for its own ATS.
- Never page through search results or profiles automatically. If the user wants
  LinkedIn jobs, they search themselves and paste the links.

## Indeed (`indeed.com`, `smartapply.indeed.com`)

- **Apply now** starts Indeed's own flow, which uses the resume stored with Indeed
  (`get_resume` from the Indeed connector shows it). Click **Continue** to move
  between steps. The final button, **Submit your application**, is for the user
  to click.
- **Apply on company site** goes to the employer's ATS in a new tab.

## Eightfold (`*.eightfold.ai`, `careers.lamresearch.com`): Lam Research, Micron, Infineon

- The posting page has an **Apply** button. These sites often ask for the resume
  first, then fill in the form from it, so check what they parsed.
- Some flows ask for an email and a one-time code before the form. Fetch the code
  if `settings.email_codes` is on, otherwise ask the user for it.

## SAP SuccessFactors (`successfactors.com`, `/job/City-Title-ST-Zip/<id>/` URLs): TSMC Arizona, Amkor, Qorvo, Edwards Vacuum

- Click **Apply now**, then sign in or create an account (email and password).
- The application is usually one long page with sections and attachments, so a single
  `autofill` covers most of it. Scroll and run `inspect_form` again if sections expand.

## Oracle Recruiting Cloud (`*.oraclecloud.com/hcmUI/CandidateExperience`): onsemi, Texas Instruments

- The flow asks for the email first, then sends a one-time code to that address.
  If `settings.email_codes` is on, fetch the code as the apply skill describes.
  Otherwise the user enters it, or reads it out to you.
- If **Next** leads to a page whose **Continue** goes back to the job posting, don't go
  round again. Read what that page says. In live checks (Oct 2026) both onsemi and TI
  started doing this for a test address that had asked for many codes in one day,
  probably a limit on codes per address. The Job Desk stops after one lap.
- Texas Instruments shows a privacy banner whose only button is **AGREE AND PROCEED**.
  It covers **Apply Now** and the email step, so no fields appear until it's gone.
  Agreeing is the user's call, like any cookie choice: ask once per session.

## Greenhouse (`greenhouse.io`) and Lever (`lever.co`): ASM

- Both use a single page. Greenhouse forms are often embedded in an iframe on the
  company's site, which is why field ids start with `f1-`.
- `open_application` opens a Greenhouse job-board posting at Greenhouse's own form
  (`job-boards.greenhouse.io/embed/job_app?for=<board>&token=<id>`). A board set to send
  visitors to the company's page (asm.com) would otherwise land behind that site's cookie
  banner, which can keep the embedded form from loading.
- Custom questions and a voluntary EEO section are at the bottom. The final button,
  **Submit Application** or **Submit application**, goes through `submit_application`.

## ApplicantStack (`*.applicantstack.com`): SCREEN SPE USA

- Every opening is listed on one page (`/x/openings`), with its location. The board also
  has a **My Account** page for returning to a saved or submitted application.

## CAPTCHAs

- Some sites put up a CAPTCHA mid-application: Daifuku's iCIMS shows hCaptcha's picture
  puzzle after its email step. The user solves it in the browser; never try to solve one.
  The Job Desk notices the challenge, waits, and carries on once the page moves on.

## iCIMS (`*.icims.com`): Daifuku America

- The portal draws its pages inside a frame, so field ids start with `f1-`. Its job
  search lists each opening with its location (`US-AZ-Chandler`) and posting date. It
  turns away plain requests (HTTP 405), so the search runs in a background browser tab.
- A posting's button reads **Apply for this job online**.

## Paycom (`paycomonline.net`): Ebara Technologies

- The career page loads every opening from Paycom's own API, which only answers that
  page, so the search runs in a background browser tab. Titles end in the requisition
  number, as in "Field Service Technician II (33195)"; the search drops it from the title.
- The career page has **Sign In** and **Create Account** links: Paycom keeps an applicant
  account per employer. A posting's button is **Quick Apply**.
- Its cookie banner offers only **Accept Cookies**, so leave it be unless the user says
  otherwise.

## UKG Pro / UltiPro (`recruiting*.ultipro.com`): Nikon Precision

- The job board lists every opening with its locations (Nikon's field service roles are at
  Intel's Chandler fabs) and loads them from its own API, so the search runs in a
  background browser tab. A posting's address ends in `OpportunityDetail?opportunityId=…`.
- A posting can open under an "Accessibility Note" that hides the page's buttons until
  **Dismiss Note** is pressed. The Job Desk dismisses it; it's information, not a choice.
- **Apply now** and **Sign In** are web components (`<ukg-button>`). Apply now leads to
  UKG's sign-in page (`signin-us.ultipro.com`), where the user signs in or creates an
  account with that employer.

## ASML (`asml.com/en/careers`)

- Postings are on asml.com. **Apply** may lead to Workday (`asml.wd3.myworkdayjobs.com`)
  or another system, so read `ats` from the page that opens and follow that ATS's
  notes. Many US field roles ask export-control (US person) questions.
