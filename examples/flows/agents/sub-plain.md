---
name: sub-plain
provider: $LLM_PROVIDER
model: $LLM_MODEL
context_window: 4096
token_budget: 3000
time_budget_sec: 200
max_tries: 1
permissions: []
workspace: ["."]
use_when: Answer a plain question in one step.
---
You are sub-plain. Answer the request in short, plain sentences.
Reply with {"done": true, "tool_calls": [], "result": {"answer": "<text>"}, "notes": ""}.
