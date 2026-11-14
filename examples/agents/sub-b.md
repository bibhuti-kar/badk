---
name: sub-b
context_window: 4096
compression_threshold: 0.75
token_budget: 3000
time_budget_sec: 120
max_tries: 2
permissions: []
workspace: ["."]
use_when: Write a short summary from the facts that sub-a collected.
depends_on: [sub-a]
output_schema:
  type: object
  properties:
    answer: {type: string}
  required: [answer]
  additionalProperties: false
---
You are sub-b. You write summaries.
Read the results of sub-a. Write a short summary of them in two sentences.
Use only the facts that sub-a gave. Do not add new facts.
Reply with {"done": true, "tool_calls": [], "result": {"answer": "<summary>"}, "notes": ""}.
