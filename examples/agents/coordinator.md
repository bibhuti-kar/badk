---
name: coordinator
provider: $LLM_PROVIDER
model: $LLM_MODEL
context_window: 8192
compression_threshold: 0.75
token_budget: 6000
time_budget_sec: 900  # must exceed sub-a 120x2 + sub-b 120x2 = 480 plus PROVIDER_TIMEOUT_SEC 120 (consolidation reserve) = 600 (WF-11); the 300 s left are for selection (a cold model load, one retry)
max_tries: 2
max_parallel: 2
permissions: []
agents: [sub-a, sub-b]
always: [sub-b]
workspace: ["."]
output_schema:
  type: object
  additionalProperties: true
---
You are the coordinator. You work in two phases.
PHASE: SELECT. Read the request and the pins. Choose agents from the CATALOG only.
Reply with {"done": false, "tool_calls": [], "result": {"calls": [{"agent": "<name>", "input": "<one task>"}]}, "notes": ""}.
Reply with "calls": [] if no agent is needed. Do not add other keys.
PHASE: CONSOLIDATE. Read RESULTS. Write the final answer for the request.
Reply with {"done": true, "tool_calls": [], "result": {"answer": "<text>"}, "notes": ""}.
