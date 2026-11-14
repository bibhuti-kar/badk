---
name: coordinator-pipeline
provider: $LLM_PROVIDER
model: $LLM_MODEL
context_window: 8192
token_budget: 6000
time_budget_sec: 300
max_tries: 1
permissions: [echo_text]
workspace: ["."]
final: none
flow:
  - {id: t1, tool: echo_text, args: {text: "{{ask}}"}, after: []}
  - {id: t2, tool: echo_text, args: {text: "t1 said {{t1.body.stdout}}"}, after: [t1]}
---
Pipeline of tool steps. No model call is made.
Budget: 300 > C + F = (30x2 + 30x2) + 0 = 120.
