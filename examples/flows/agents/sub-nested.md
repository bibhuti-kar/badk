---
name: sub-nested
provider: $LLM_PROVIDER
model: $LLM_MODEL
context_window: 4096
token_budget: 3000
time_budget_sec: 600
max_tries: 1
permissions: [echo_text]
workspace: ["."]
use_when: Echo the task, then answer it with sub-plain.
final: none
flow:
  - {id: t1, tool: echo_text, args: {text: "{{ask}}"}, after: []}
  - {id: a1, agent: sub-plain, after: [t1]}
---
Nested flow: one tool step, then the sub-plain agent.
Budget: 600 > C + F = (30x2 + 200) + 0 = 260.
