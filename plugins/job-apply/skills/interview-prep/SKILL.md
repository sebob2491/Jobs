---
name: interview-prep
description: Prepare the user for an interview for a job in the job-apply tracker. Covers research on the company and role, the technical and behavioral questions likely in semiconductor equipment, field service and technician interviews, STAR stories built from the user's real experience, questions to ask, and logistics. Use when the user mentions an interview, phone screen, assessment or onsite, or when track-responses records an interview request.
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
   - Use WebSearch for recent news relevant to the role, such as Arizona fab
     expansions, new tools, or the products this team services.
   - Cite the sources.

## Prepare

Build the sheet in this order:

1. **The role in one paragraph.** What the job really involves: the tools, the
   customer sites, the travel, the shifts.
2. **Requirement → evidence table.** Each requirement from the posting next to the
   resume line that shows it. Mark the gaps plainly and add a truthful way to
   address each one: related experience, training already started, willingness to
   learn. Never invent experience.
3. **Likely technical questions**, with an answer outline for each, drawn from the
   user's own work:
   - **Troubleshooting:** walk through a recent equipment failure. Define the
     problem, isolate it with schematics and logs, test, fix, verify, then
     document.
   - **Core skills, as the posting needs them:** reading electrical and mechanical
     schematics; vacuum (pumps, gauges, leak checking); RF generators and matching;
     PLC and interlock logic; pneumatics; PMs versus corrective maintenance; uptime,
     MTBF and MTTR.
   - **Safety:** lockout/tagout and hazardous-energy control, cleanroom protocol,
     chemical handling, when to stop work.
   - **Company-specific:** what the company's tools do, for example EUV lithography
     at ASML, etch and deposition at Lam, or the EUV drive laser at TRUMPF.
4. **Behavioral STAR stories (4–6).** Situation, task, action, result, built from
   the user's real experience. Good topics are a hard fix, a customer under
   pressure, a safety call, a mistake and what changed afterwards, teaching or
   helping a teammate, and working nights or travelling. If the resume is too thin
   for a story, ask the user for the details.
5. **Questions to ask them:** training program length, the certification path, the
   real travel percentage, the shift pattern and on-call, which customer site the
   role supports, and how people move up from this level.
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
