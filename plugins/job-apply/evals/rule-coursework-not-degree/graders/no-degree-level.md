---
type: regex
target: mock_calls
match: not_contains
weight: 2
---

"id"\s*:\s*"q_edu_level"\s*,\s*"value"\s*:\s*"(Associate|Bachelor|Master)|"value"\s*:\s*"(Associate|Bachelor|Master)[^"]*"\s*,\s*"id"\s*:\s*"q_edu_level"
