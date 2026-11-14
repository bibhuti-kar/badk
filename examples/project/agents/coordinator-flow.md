---
name: coordinator-flow
provider: $LLM_PROVIDER
model: $LLM_MODEL
context_window: 8192
token_budget: 6000
time_budget_sec: 300
max_tries: 1
permissions: [demo_echo]
workspace: ["."]
final: none
flow:
  - {id: t1, tool: demo_echo, args: {text: "{{ask}}"}, after: []}
  - {id: t2, tool: demo_echo, args: {text: "t1 said {{t1.body.stdout}}"}, after: [t1]}
---
Two demo_echo steps, the second reads the first. No model call is made.
Budget: 300 > C + F = (30x2 + 30x2) + 0 = 120.
