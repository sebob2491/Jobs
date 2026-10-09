---
name: interview-prep
description: Prepare the user for an interview for a job in the job-apply tracker. Covers research on the company and role, the technical and behavioral questions likely for the role (semiconductor equipment, field service and technician jobs; HR and recruiting; finance and accounting; or any other), STAR stories built from the user's real experience, questions to ask, and logistics. Use when the user mentions an interview, phone screen, assessment or onsite, or when track-responses records an interview request.
---

# Interview prep

The goal is a short prep sheet the user can review in 15 minutes before the
interview, grounded in their real experience and in this specific posting.

## Gather

1. Find the job with `list_jobs` / `get_job(job_id)`. That gives you the description,
   company and status. Run `update_job(job_id, status="interviewing")` if it isn't
   already set.
2. Read the user's resume (`documents.resume` in `get_profile`) and the profile.
3. If a Gmail connector is available, search the user's email for the recruiter's or
   company's thread and the booking or calendar emails: the time, who will call and
   from what number, and what they said about the role, pay, shifts and locations.
   The email may be in any of the user's linked accounts.
4. Ask only what you still can't find: the date and time, the format (phone, video
   or onsite), the interviewers' names and roles, and whether there's a technical
   assessment.
5. Research the company:
   - A recruiting agency often won't name its client. Work out the likely employer
     from what the recruiter wrote (its fabs, locations, customer count), say it's a
     guess, and add "Who is the employer?" to the questions to ask.
   - If the Indeed connector is available, use `get_company_data` for ratings,
     salary data for this title, and interview-process reviews.
   - Use WebSearch for recent news relevant to the role: Arizona fab expansions or the
     products a field service team supports; a hospital system's growth or a new HR
     system; a bank's results, an acquisition or an audit firm's new practice.
   - Cite the sources.

## Prepare

Build the sheet in this order:

1. **The role in one paragraph.** What the job really involves: for a field role the
   tools, the customer sites, the travel and the shifts; for an office role the team,
   who it reports to, the systems it uses and its busy seasons (month-end close, open
   enrollment, campus hiring).
2. **Requirement → evidence table.** Each requirement from the posting next to the
   resume line that shows it. Mark the gaps plainly and add a truthful way to
   address each one: related experience, training already started, willingness to
   learn. Never invent experience.
3. **Likely technical questions**, with an answer outline for each, drawn from the
   user's own work. Pick the set that fits the role, and only the topics the posting
   asks for:
   - **Equipment, field service and technician roles:**
     - *Troubleshooting:* walk through a recent equipment failure. Define the problem,
       isolate it with schematics and logs, test, fix, verify, then document.
     - *Core skills:* reading electrical and mechanical schematics; vacuum (pumps,
       gauges, leak checking); RF generators and matching; PLC and interlock logic;
       pneumatics; PMs versus corrective maintenance; uptime, MTBF and MTTR.
     - *Safety:* lockout/tagout and hazardous-energy control, cleanroom protocol,
       chemical handling, when to stop work.
     - *Company-specific:* what the company's tools do, for example EUV lithography at
       ASML, etch and deposition at Lam, or the EUV drive laser at TRUMPF.
   - **HR and recruiting roles:**
     - *Employment law basics:* FMLA, ADA accommodations, FLSA exempt and non-exempt,
       I-9 and E-Verify, EEO and harassment complaints; when to bring in legal.
     - *Employee relations:* running an investigation, documenting it, and a
       termination or a hard conversation handled well.
     - *Recruiting:* full-cycle steps, sourcing, working with hiring managers, and
       the numbers (time to fill, offer acceptance, hires per recruiter).
     - *Systems and programs:* the HRIS and ATS the posting names (Workday, UKG, ADP,
       iCIMS), onboarding, benefits and open enrollment, confidentiality.
   - **Finance and accounting roles:**
     - *Accounting:* month-end close, journal entries, account reconciliations,
       accruals, and how the three financial statements connect; GAAP topics the
       posting names (revenue recognition, leases).
     - *Analysis:* budget versus actual and variance analysis, forecasting, and
       explaining a number to someone outside finance.
     - *Tools:* Excel (pivot tables, XLOOKUP or INDEX/MATCH), and the ERP or reporting
       systems the posting names (SAP, Oracle, Workday Financials, NetSuite, Power BI).
     - *Controls:* SOX controls, audits and audit requests. Expect a short Excel or
       case exercise for analyst roles.
   - **Any other role:** the posting's core skills, each with a real example from the
     user's work.
4. **Behavioral STAR stories (4–6).** Situation, task, action, result, built from
   the user's real experience. Good topics are a hard problem solved, someone
   under pressure (a customer, a manager, an employee), a judgement call (a safety
   stop, a policy exception, a number that didn't add up), a mistake and what
   changed afterwards, teaching or helping a teammate, and a deadline met (a
   shutdown, a close, a hiring push). If the resume is too thin for a story, ask the
   user for the details.
5. **Questions to ask them**, as fits the role: what success looks like in the first
   90 days, the team and who the role reports to, and how people move up from this
   level; for a field role also the training program, the certification path, the
   real travel percentage, the shift pattern and on-call, and which customer site it
   supports; for an office role the systems used, the busiest times of year, and how
   the team works with the rest of the business.
6. **Logistics:** the time in the user's own time zone, the dial-in or address, what
   to bring or wear, and the names to remember. Booking emails and invites often give
   the sender's zone without saying so. Check which zone it is (the thread usually
   says), and convert it. Arizona keeps Mountain Standard Time all year, so from
   March to November it is two hours behind Central, not one: 10:00 AM Central is
   8:00 AM in Phoenix. Put both times on the sheet. If the Google Calendar connector
   is available and the user agrees, add the interview to their calendar.

Ask the user whether they want the sheet in chat or as a document they can keep.
Keep it scannable: short bullets, with the answers as outlines rather than scripts.

## Afterwards

- Ask how it went and record it with
  `update_job(job_id, notes="...", event_note="interview: ...")`.
- Offer a short thank-you email to each interviewer, saved as a Gmail **draft**
  (never sent) if the connector is available, otherwise as text to copy.
