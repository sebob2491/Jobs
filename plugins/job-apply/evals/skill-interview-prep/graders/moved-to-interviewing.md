---
type: regex
target: mock_calls
match: contains
weight: 1
---

__update_job"(?=[^\n]*"job_id"\s*:\s*"?45\b)(?=[^\n]*"status"\s*:\s*"interviewing")
