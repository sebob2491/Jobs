{
 "requests": [],
 "watch": false,
 "why": "no job is queued or waiting on the desk",
 "account": "jordan.sample@example.com",
 "watch_command": "uv run --quiet --frozen --project \"/plugins/job-apply/server\" job-apply-wait-mail --desk \"/home/example/.job-apply/desk_watch_8765.json\"",
 "note": "Read only what each request's query finds (that job's own senders, since its wait began), and never send, delete, label or mark anything."
}
