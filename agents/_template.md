---
name: sub-example
context_window: 4096
compression_threshold: 0.75
token_budget: 3000
time_budget_sec: 120  # the coordinator's time_budget_sec must be greater than the heaviest depends_on chain (time_budget_sec x max_tries summed) + PROVIDER_TIMEOUT_SEC (consolidation reserve); checked at load. At run start the remaining time must also exceed (W - L) / max_parallel + L + PROVIDER_TIMEOUT_SEC over the selected graph (W = all node weights, L = heaviest chain), else TIME_BUDGET_INSUFFICIENT
max_tries: 2
permissions: []
workspace: ["."]
use_when: One line that says when to pick this agent.
depends_on: []
output_schema:
  type: object
  properties:
    answer: {type: string}
  required: [answer]
  additionalProperties: false
---
Role instruction in ASD-STE100. Body is passed verbatim as role_instruction.
