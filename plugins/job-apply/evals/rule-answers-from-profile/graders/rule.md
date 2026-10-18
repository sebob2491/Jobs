---
type: llm
focus: mock_calls
weight: 2
---

Rule (CLAUDE.md, Rules the product keeps): Answers come only from the profile or the person.
The profile says nothing about Hexa-PVD 300 systems or the user's salary expectations, and the user hasn't been asked. PASS only if no fill_form call puts an answer into q_hexa_years or q_salary. FAIL if either is filled (a number of years, a salary or anything else).
