---
description: "The find-jobs skill fires on a search request, passes the pay floor and the days, and reports openings without dropping one whose pay isn't listed."
tags: [skill, find-jobs]
max_turns: 20
timeout_seconds: 450
allowed_tools: [Skill, Read, Glob, Grep]
---

Find me equipment technician jobs around Phoenix posted in the last two weeks that pay at least $25 an hour.
