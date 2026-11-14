---
name: sub-demo
context_window: 4096
compression_threshold: 0.75
token_budget: 3000
time_budget_sec: 120
max_tries: 2
permissions: [demo_echo]
workspace: ["."]
use_when: Do not select. It always runs, with the request text, to test the setup.
depends_on: []
output_schema:
  type: object
  properties:
    answer: {type: string}
  required: [answer]
  additionalProperties: false
---
Your input is a text. Call the tool demo_echo one time. Set its text field to your input, with no change.
The tool result has a stdout field. Reply with done true and result {"answer": A}. A is the stdout value, with no change.
Do not call other tools. Do not add words.
