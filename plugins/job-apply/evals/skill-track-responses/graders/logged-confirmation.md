---
type: regex
target: mock_calls
match: contains
weight: 1
---

__log_email"(?=[^\n]*"job_id"\s*:\s*"?50\b)(?=[^\n]*"category"\s*:\s*"confirmation")
