{
 "requests": [
  {
   "job_id": 12,
   "employer": "Example Fab Co",
   "want": "code",
   "senders": [
    "examplefab.example"
   ],
   "after": 1792508400,
   "after_time": "2026-10-20T08:00:00-07:00",
   "before": null,
   "query": "from:(examplefab.example) after:1792508400"
  }
 ],
 "watch": true,
 "why": null,
 "account": "jordan.sample@example.com",
 "watch_command": "uv run --quiet --frozen --project \"/plugins/job-apply/server\" job-apply-wait-mail --desk \"/home/example/.job-apply/desk_watch_8765.json\"",
 "note": "Read only what each request's query finds (that job's own senders, since its wait began), and never send, delete, label or mark anything."
}
