{
 "page": {
  "url": "https://jobs.examplecontrols.example/electronics-technician/apply",
  "headings": [
   "Submit your application"
  ]
 },
 "filled": [
  {
   "id": "f1",
   "label": "First Name",
   "value": "Jordan",
   "from": "personal.first_name"
  },
  {
   "id": "f2",
   "label": "Last Name",
   "value": "Sample",
   "from": "personal.last_name"
  },
  {
   "id": "f3",
   "label": "Email",
   "value": "jordan.sample@example.com",
   "from": "personal.email"
  },
  {
   "id": "f4",
   "label": "Phone",
   "value": "480-555-0142",
   "from": "personal.phone"
  },
  {
   "id": "f5",
   "label": "Resume/CV",
   "value": "~/.job-apply/resume.pdf",
   "from": "documents.resume",
   "uploaded": true
  },
  {
   "id": "f6",
   "label": "Are you legally authorized to work in the United States?",
   "value": "Yes",
   "from": "work_authorization.authorized_to_work"
  },
  {
   "id": "f7",
   "label": "Will you now or in the future require sponsorship?",
   "value": "No",
   "from": "work_authorization.requires_sponsorship"
  }
 ],
 "failed": [],
 "needs_input": [
  {
   "id": "q_assoc",
   "label": "Do you have an Associate's degree in Electronics Technology or a related field?",
   "kind": "radio_group",
   "options": [
    "Yes",
    "No"
   ],
   "required": true
  },
  {
   "id": "q_edu_level",
   "label": "Highest level of education completed",
   "kind": "select",
   "options": [
    "High school diploma or GED",
    "Some college, no degree",
    "Associate degree",
    "Bachelor's degree",
    "Master's degree"
   ],
   "required": true
  }
 ],
 "already_filled": 0,
 "errors": [],
 "actions": [
  {
   "id": "b2",
   "text": "Submit application",
   "is_submit": true
  }
 ]
}
