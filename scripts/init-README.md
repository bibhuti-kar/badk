# badk project

`scripts/init.sh` from the badk clone made this folder. The `badk` command reads `agents/`, `tools/` and `.env` from the current folder.

Run `<badk clone>/scripts/init.sh` here again at any time. It adds the standard tools and files that are missing and keeps all your files. It never adds to an existing `agents/` folder, so a project made before the flow feature does not get `agents/coordinator-flow.md` (see step 4). Only `README.md` (this file) and `TOOLSET.md` are replaced; a changed one is saved first as `README.md.bak` / `TOOLSET.md.bak`. To get a new copy of a standard tool, delete its files in `tools/` and run init again.

## Step 1: start the stack (once, in the clone)

```bash
cd <badk clone> && ./start.sh
```

PostgreSQL, RabbitMQ, the writer and the viewer run in containers. Your agents and tools run on this host.

## Step 2: set the model in `.env`

`.env` came from the clone, so the stack keys (`APP_PORT`, `DATABASE_URL`, `RABBITMQ_URL`) already match. Set the model keys. Substitute your own values:

| Provider | `LLM_PROVIDER` | `LLM_MODEL` (example) | `LLM_BASE_URL` | `LLM_API_KEY` |
|---|---|---|---|---|
| Anthropic API | `anthropic` | `claude-haiku-5-5` | not used | your API key |
| DeepSeek API | `openai` | `deepseek-chat` | `https://api.deepseek.com` | your API key |
| Ollama (local) | `openai` | an Ollama model tag | `http://127.0.0.1:11434` | empty |
| Claude Code login | `claude_cli` | `haiku` | not used | empty (uses `claude -p`) |

- `anthropic` calls the Messages API with your key. There is no Claude Code harness and there are no Claude Code tools. Only your `tools/` run.
- `claude_cli` runs `claude -p` with your Claude Code login (subscription auth).
- `PROVIDER_TIMEOUT_SEC` is the limit for one model call. Keep it less than the `time_budget_sec` of `agents/coordinator.md` (600). The load check tells you when it is too large.
- `LLM_MAX_TOKENS` is the maximum reply length of one `anthropic` call.

## Step 3: check

```bash
badk check
```

Each line is `ok` or `FAIL`:

| Line | What it checks |
|---|---|
| `config` | `.env` loads and every value is valid. A FAIL stops the other checks. |
| `agents` | Every agent and tool file is valid, every `permissions` name has a tool file, and the time budgets fit. It shows the coordinator provider and model. |
| `provider` | `anthropic` has `LLM_API_KEY`; `claude_cli` finds `claude` on PATH. It does not call the model. |
| `broker+viewer` | RabbitMQ accepts a login and the viewer `/health` answers. |
| `database` | The viewer reads the run list from PostgreSQL. |

The exit code is 0 only when every line is `ok`.

## Step 4: run

```bash
badk run "say hello" --headless
```

- stderr shows `run_id: <id>` and the progress. stdout is the answer only.
- Exit code: 0 completed, 3 the run failed, 1 or 2 the run did not start.
- `progress/<run_id>.log` keeps the progress lines of each run.
- No model needed: `badk run "/flow hello" --headless` runs `agents/coordinator-flow.md`, a flow of two `demo_echo` steps (see step 5). It makes no model call and prints the step results. A project made before this version has no such file: copy `examples/project/agents/coordinator-flow.md` from the badk clone into `agents/`.

## Step 5: choose an agent type

An agent file is one of four types. Save a block below as `agents/<name>.md`, then run `badk check`. Each block is the frontmatter; the role text follows it in the file. The full guide is "Agent types" in the badk clone `README.md`.

1. Pure agentic (classical). The model decides: it picks sub-agents, they call tools, and the coordinator writes the answer. This is `agents/coordinator.md` as `init.sh` made it. Run it with `badk run "say hello" --headless`. A second coordinator of this type is `agents/coordinator-agentic.md`; run it with `badk run "/agentic say hello" --headless`.

```yaml
---
name: coordinator-agentic
provider: $LLM_PROVIDER
model: $LLM_MODEL
context_window: 8192
compression_threshold: 0.75
token_budget: 6000
time_budget_sec: 600
max_tries: 2
permissions: []
agents: [sub-demo]
always: [sub-demo]
workspace: ["."]
---
```

2. Deterministic workflow. A `flow` with `final: none`: the file fixes the steps and no model is called. `init.sh` made this file as `agents/coordinator-flow.md`. Run it with `badk run "/flow hello" --headless`. A step is `tool`, `agent` or `select`; `after` sets the order (default: the previous step); `{{ask}}` is the ask and `{{t1.body.stdout}}` is a field of an earlier result. A flow has no `when`, `if` or loops: to branch, use a `select` step, a sub-agent prompt or a tool.

```yaml
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
```

3. Combination. A flow with a tool step, then a `select` step where the model picks sub-agents from a list, then `final: model` so the model writes the answer from the step results. Run it with `badk run "/combo say hello" --headless`.

```yaml
---
name: coordinator-combo
provider: $LLM_PROVIDER
model: $LLM_MODEL
context_window: 8192
token_budget: 6000
time_budget_sec: 2000
max_tries: 1
permissions: [demo_echo]
workspace: ["."]
final: model
flow:
  - {id: t1, tool: demo_echo, args: {text: "{{ask}}"}, after: []}
  - {id: p, select: [sub-echo, sub-note], after: [t1]}
---
```

4. Sub-agent flow and nesting. A sub-agent can have a `flow` too. The `select` step above can pick this one, so flows nest (depth limit 4). It needs no model.

```yaml
---
name: sub-echo
provider: $LLM_PROVIDER
model: $LLM_MODEL
context_window: 4096
token_budget: 3000
time_budget_sec: 300
max_tries: 1
permissions: [demo_echo]
workspace: ["."]
use_when: Echo the task with a fixed flow, no model call.
final: none
flow:
  - {id: t1, tool: demo_echo, args: {text: "{{ask}}"}, after: []}
---
```

5. A plain sub-agent for the `select` step to choose against `sub-echo`. It has no `flow`: the model answers it.

```yaml
---
name: sub-note
provider: $LLM_PROVIDER
model: $LLM_MODEL
context_window: 4096
token_budget: 3000
time_budget_sec: 300
max_tries: 1
permissions: []
workspace: ["."]
use_when: Write a short note about the task with the model.
---
```

The frontmatter of an agent file is limited to 500 tokens, so keep a flow short.

## Step 6: see the trace

Open `http://localhost:$APP_PORT/runs/<run_id>`. Each model call is an `llm` record with `tokens_in`, `tokens_out` and `latency_ms`. Each tool call is a `tool` record with its args and result. The run page shows the provider and model of each agent.

To compare providers, run the same ask with each provider. Change only `LLM_PROVIDER`, `LLM_MODEL` and `LLM_API_KEY` in `.env`, then compare the answer, the tokens and the time on the run pages.

## Step 7: the standard tools

These tools are in `tools/`. An agent can call a tool only when its `permissions` list names it, for example `permissions: [web_search, web_fetch]`.

| Tool | What it does | Config |
|---|---|---|
| `web_search` | Search the web with DuckDuckGo (no key). Returns title, url and snippet. | none |
| `web_fetch` | Get a public web page as markdown text. Private and local addresses are refused. | none |
| `mcp_list` | List the tools of an MCP server (a tool file of `type: mcp`). | `tools/mcp.json` |
| `mcp_call` | Call one tool of an MCP server (a tool file of `type: mcp`). | `tools/mcp.json` |
| `api_call` | Call a REST API directly, with no MCP. | `tools/apis.json` |
| `secret_demo` | Example of a bash script that gets a secret through `tools/with-secrets`. | `tools/secrets.env` |
| `demo_echo` | Example bash tool, used by `sub-demo`. | none |

Secrets are in two places:

| Where the key is | Which tool gets it | How |
|---|---|---|
| The project `.env` | Only a tool whose file lists the key in `env:` | A bash or python tool reads `$KEY`; an http tool writes `{{env.KEY}}` in a header value. badk refuses to start when the key is missing or shorter than 8 characters, and masks the value as `***`. Use this for your own tools. |
| `tools/secrets.env` (KEY=VALUE, mode 600, git-ignored) | `api_call`, a script run through `tools/with-secrets`, and a python tool that imports `toolkit_secrets` | `apis.json` writes `${KEY}`. Each of these tools masks the values as `***` in the output it prints. badk also registers each value of 8 characters or more at start and masks it everywhere, error text too. |
| `${NAME}` in `tools/mcp.json` | The server entry that uses it, when a permitted `mcp` tool reaches that server | badk fills it from the process env, then `.env`, then `tools/secrets.env` (the first value that is not empty wins). A missing, empty or short (< 8 characters) value stops the start. badk masks the value everywhere, error text too. |

`TOOLSET.md` has the full config reference, the MCP fields, modes and limits (for example: `mcp_call` covers every server in `mcp.json`) and the connectors for Microsoft 365, Teams, Slack, Telegram and Google.

## Step 8: make a new tool or update a config

Pick the row for what you need. After each change, run `badk check`, then add the tool name to the `permissions` of the agent that may use it.

| You need | Do this |
|---|---|
| A new MCP server | Add an entry to `tools/mcp.json` (paste the server's `mcpServers` config from its README). Write its keys as `${KEY}` in `mcp.json` and put them in `.env` or `tools/secrets.env`. No new tool file: agents use `mcp_list` and `mcp_call` with the new server name. |
| A new REST API | Add an entry to `tools/apis.json`: `base_url`, `headers` (with `${KEY}`), `methods`. No new tool file: agents use `api_call` with the new API name. |
| A bash command | `sed 's/^name: .*/name: my_tool/' tools/_template_bash.md > tools/my_tool.md`, then edit `command`, `description` and `args`. The command runs without a shell; put pipes and redirects in a script. |
| A tool that needs a key (password, token, API key) | Put the key in `.env` and add `env: [KEY]` to the tool file. A bash or python tool reads `$KEY`; an http tool writes `{{env.KEY}}` in a header value, and its `url` needs a fixed `http(s)://host` (use `https`: with `http` the key goes in clear text). |
| A bash script that uses `tools/secrets.env` | Put the script in `tools/scripts/` (`chmod +x`). Put the keys in `tools/secrets.env`. In the tool file, set `command: tools/with-secrets KEY1,KEY2 my_script.sh {{arg}}`. Copy `tools/secret_demo.md` as the start. |
| Python code | `sed 's/^name: .*/name: my_tool/' tools/_template_python.md > tools/my_tool.md` and `cp tools/_template_python.py tools/my_tool.py`. Set `path: tools/my_tool.py`. The script reads its args as JSON on stdin and prints one JSON object. For a key, add `env: [KEY]` and read `os.environ["KEY"]`. |
| One fixed HTTP endpoint | `sed 's/^name: .*/name: my_tool/' tools/_template_http.md > tools/my_tool.md`, then edit `request` and `args`. |

Rules for every tool file:

- `name` is the file name without `.md`: lowercase letters, digits and `_`.
- `args` is a JSON schema. Use `additionalProperties: false` and `required`, so a bad call is refused before anything runs.
- `description` tells the model what the tool does and what it returns. Write it as one or two plain sentences.
- `timeout` is in seconds. Keep it less than the agent `time_budget_sec`.
- `env` (optional) lists the `.env` keys the tool gets. No other tool gets them.
- `output` (optional) is a JSON schema of the result `body` of a flow tool step. A body that does not match is `TOOL_ERROR`. See `tools/_template_python.md`.
- In a bash or python tool, an `args` property with `type: path` is a file path. It must be inside the agent `workspace`. An http tool does not check it.
- Every tool child gets `BADK_RUN_ID` and `BADK_TASK_ID`, so a row that the tool writes can link back to the run.

## Step 9: add your own agents

- Copy `agents/_template.md` to `agents/sub-<name>.md`. Set `name`, `use_when`, `workspace` and `permissions` (the tool names it may call).
- Add the agent name to `agents` (and to `always` if it must always run) in `agents/coordinator.md`.
- Run `badk check` after each change.

`sub-demo` and `demo_echo` are examples. Remove them when you have your own (and remove `sub-demo` from `agents/coordinator.md`). `agents/coordinator-flow.md` calls `demo_echo`, so remove or edit it too.
