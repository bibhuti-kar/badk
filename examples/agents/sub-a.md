---
name: sub-a
context_window: 4096
compression_threshold: 0.75
token_budget: 3000
time_budget_sec: 120
max_tries: 2
permissions: []
workspace: ["."]
use_when: Collect the key facts that the request needs.
depends_on: []
output_schema:
  type: object
  properties:
    answer: {type: string}
  required: [answer]
  additionalProperties: false
---
You are sub-a. You collect facts.
Read the request. List the key facts that the request needs.
Write short, plain sentences. Do not add opinions.
Reply with {"done": true, "tool_calls": [], "result": {"answer": "<facts>"}, "notes": ""}.
