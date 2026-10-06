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

## Workday (`*.myworkdayjobs.com`): Applied Materials, KLA, Intel, Microchip, NXP, TEL, Analog Devices, Hitachi High-Tech, ASML (some postings)

- Each company has its own Workday account. Use the same email each time.
- Path: **Apply** → choose **Autofill with Resume** (Workday parses the resume
  into My Experience, so check what it parsed) or **Apply Manually** → sign in
  or create an account → steps.
- Steps: My Information → My Experience → Application Questions → Voluntary
  Disclosures → Self Identify → Review. The Next button reads **Save and Continue**.
  The last button reads **Submit**, which goes through `submit_application`.
- Dropdowns are `listbox` fields, and `inspect_form` opens them to read their options.
  "How Did You Hear About Us?", School and Field of Study are search pickers
  (`combobox`): the value is typed and Enter pressed. If the result is a category
  rather than a final option, `click` the option text.
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

## SAP SuccessFactors (`successfactors.com`, `/job/City-Title-ST-Zip/<id>/` URLs): TSMC Arizona, Amkor, Qorvo, Entegris

- Click **Apply now**, then sign in or create an account (email and password).
- The application is usually one long page with sections and attachments, so a single
  `autofill` covers most of it. Scroll and run `inspect_form` again if sections expand.

## Oracle Recruiting Cloud (`*.oraclecloud.com/hcmUI/CandidateExperience`): onsemi, Texas Instruments

- The flow asks for the email first, then sends a one-time code to that address.
  If `settings.email_codes` is on, fetch the code as the apply skill describes.
  Otherwise the user enters it, or reads it out to you.

## Greenhouse (`greenhouse.io`) and Lever (`lever.co`): ASM

- Both use a single page. Greenhouse forms are often embedded in an iframe on the
  company's site, which is why field ids start with `f1-`.
- Custom questions and a voluntary EEO section are at the bottom. The final button,
  **Submit Application** or **Submit application**, goes through `submit_application`.

## ASML (`asml.com/en/careers`)

- Postings are on asml.com. **Apply** may lead to Workday (`asml.wd3.myworkdayjobs.com`)
  or another system, so read `ats` from the page that opens and follow that ATS's
  notes. Many US field roles ask export-control (US person) questions.
