# badk toolkit

Ready tools for a badk project: web search, web fetch, MCP servers, REST APIs, and scripts that need secrets. Each tool is a normal badk tool file in `tools/`. An agent can use a tool only when its `permissions` list names it.

| Tool | What it does | Config | Library |
|---|---|---|---|
| `web_search` | Search the web, no key. Returns title, url and snippet of each result. | none | [ddgs](https://pypi.org/project/ddgs/) (DuckDuckGo first, then its other keyless engines) |
| `web_fetch` | Get a public page as markdown text. | none | badk's own http executor (private addresses refused) + [html2text](https://pypi.org/project/html2text/) |
| `mcp_list` | List the tools of an MCP server. A tool file of `type: mcp`. | `tools/mcp.json` | badk's `mcp` tool type (official [mcp](https://pypi.org/project/mcp/) SDK v2, a core dependency of badk) |
| `mcp_call` | Call one tool of an MCP server. A tool file of `type: mcp`. | `tools/mcp.json` | badk's `mcp` tool type |
| `api_call` | Call a REST API directly (no MCP). | `tools/apis.json` | httpx |
| `secret_demo` | Example: a bash script that gets a secret in its env. | `tools/secrets.env` | `tools/with-secrets` (stdlib) |

Keys have two places. `api_call`, the `secret_demo` script and your own scripts that run through `tools/with-secrets` take their keys from `tools/secrets.env` (KEY=VALUE, mode 600, ignored by version control). `apis.json` refers to a key as `${KEY}`, so it holds no key and you can commit it. These tools mask each value as `***` in the output they print. For a tool of your own, list the key from the project `.env` in the tool file as `env: [KEY]` (see the project README). Only that tool gets it, and badk masks it everywhere, error text too.

The MCP tools are different. badk fills `${NAME}` in `tools/mcp.json` itself (see Step 4), and it masks each filled value everywhere.

## Step 1: get the toolset

`<badk clone>/scripts/init.sh` copies these tools into the project `tools/` and installs the libraries (the `toolkit` extra). In the project, this file is `TOOLSET.md`. Run init again in an existing project to add the tools that are missing; it keeps your files, your configs and `tools/secrets.env`.

If your program calls `badk.run()` from its own venv, the tools run with that venv's Python. Add the extra there too: `uv add --editable "<badk clone>[toolkit]"`.

## Step 2: give the tools to an agent

In `agents/sub-<name>.md`, list the tools the agent may call:

```yaml
permissions: [web_search, web_fetch, mcp_list, mcp_call, api_call]
```

Tell the agent in its body which server or API to use. For example: "Call mcp_list with server slack one time, then mcp_call with the tool you need."

## Step 3: web search and fetch

No setup. Examples of the tool call the model makes:

```json
{"name": "web_search", "args": {"query": "telegram bot api sendMessage", "max_results": 5}}
{"name": "web_fetch", "args": {"url": "https://core.telegram.org/bots/api", "max_chars": 20000}}
```

- `web_search` returns `engine` too. DuckDuckGo sometimes blocks a machine; then the result comes from ddgs's other keyless engines (`engine: "auto"`).
- `web_fetch` reads at most 100 kB of the page, then converts it to markdown. Loopback, private and link-local addresses are refused, also after a redirect.

## Step 4: MCP servers (`tools/mcp.json`)

`mcp_list` and `mcp_call` are tool files of `type: mcp`. badk is the MCP client (official mcp SDK). The config file has the same `mcpServers` shape that Claude Desktop and Cursor use, so you can paste the config from a server's README:

```json
{
  "mcpServers": {
    "slack": {
      "command": "npx",
      "args": ["-y", "slack-mcp-server@latest", "--transport", "stdio"],
      "env": {"SLACK_MCP_XOXP_TOKEN": "${SLACK_MCP_XOXP_TOKEN}"}
    },
    "remote": {
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer ${REMOTE_TOKEN}"}
    }
  }
}
```

- `command` + `args` + `env`: a local (stdio) server. It runs as a child of the badk process, with your user's rights. It gets only the `env` of its entry (plus the MCP SDK default keys), not your `.env`. It starts in the project folder; a relative `cwd` or script path is taken from there.
- `url` + `headers`: a remote server (streamable HTTP).
- `mcp_list {"server": "slack"}` returns each tool with its `inputSchema`. `mcp_call {"server": "slack", "tool": "<name>", "arguments": {...}}` calls one. The result is the MCP result (`content`, and `structuredContent` when the server sends it).
- `npx -y` downloads the server on first use. Pin a version (`pkg@1.2.3`) when you need the same code each run.

Fields of a `type: mcp` tool file:

| Field | Meaning |
|---|---|
| `server` | Pin the server (a name in `mcp.json`). Not set: the model gives `server` in the args. |
| `tool` | Pin the MCP tool. Needs `server`. Not set: the model gives `tool` in the args. |
| `action` | `call` (default) or `list`. `list` takes no `tool`. |
| `args` | The JSON Schema for the args the model sees. |
| `timeout` | Seconds for one call. |

An `mcp` tool has no `command`, `path`, `request` or `env`. A `type: path` arg is refused.

The four modes:

| Tool file | The model gives | Result |
|---|---|---|
| Generic (`mcp_call`): no pin | `server`, `tool`, `arguments` | One call of that tool. |
| `server` pinned | `tool`, `arguments` | One call on that server. |
| `server` and `tool` pinned | the arguments of the MCP tool | One call of that one tool. |
| `action: list` (`mcp_list`) | `server` (none when pinned) | Every tool of the server, with its `inputSchema`. |

How the session works:

- badk keeps one session per server in its process. The first call starts the server (stdio) or opens the session (HTTP). Later calls use it, from any agent or thread. A server stays up between calls, so its state stays too.
- A tool timeout ends only the wait of the caller. It does not cancel a start. A start that never answers is dropped after 120 s. A call after the start ends uses that start.
- A change to an entry in `mcp.json` starts a new session for that server. The old one closes when it is idle. When the process ends, the servers end.
- badk never sends a call again by itself after a dead session. The one retry that every tool type has (classical agent loop and flow tool step) applies to `mcp` too.
- A result with `isError: true` is a `TOOL_ERROR`. The step status is `error`, and the steps that depend on it are skipped unless they set `on_error: run`. The text is masked and has at most 500 characters.
- A result over the output size limit is a `TOOL_ERROR`, as for every tool type.

Keys: `${NAME}` in `mcp.json` is filled from the process environment, then the project `.env`, then `tools/secrets.env`. The first value that is not empty wins. badk fills only the servers that a permitted `mcp` tool reaches. NAME follows the `env:` rules: `A-Z`, `0-9` and `_`, starting with a letter; not `BADK_...`; not a variable that every tool gets already (`PATH`, `HOME`, ...). Write `$${X}` to keep a literal `${X}`. A key that is missing, empty or shorter than 8 characters, or a bad name, is a `VALIDATION_ERROR` at every start (`badk.run`, `badk run`, `badk serve`, `badk check`, `badk mcp`). The error names `mcp.json`, the server and the key. At start, badk registers each filled value, and each value of 8 characters or more in `tools/secrets.env`, for masking. It masks them everywhere: results, the trace, and error text (a traceback). A literal value in `mcp.json` is not masked: write each key as `${NAME}`. A local server's own stderr log goes to your terminal as is (for `badk mcp`: the client's log).

A tool with no `server` pin (`mcp_list`, `mcp_call`) reaches every server in `mcp.json`, so the keys of every server must be set at each start. Keep only the servers you use in `mcp.json`, or give the agent tool files with `server` pinned.

Limits:

| Limit | Effect |
|---|---|
| Two generic tools | The model sees `mcp_list` and `mcp_call`, not one tool for each MCP tool. It calls `mcp_list` first to learn the names and schemas. For one MCP tool with its own name and schema, make a tool file with `server` and `tool` pinned. |
| Argument check | badk checks only the args in the tool file. The MCP server checks the content of `arguments`. |
| Permission | `mcp_call` in `permissions` lets the agent call every tool of every server in `mcp.json`. Name the server in the agent body; badk does not enforce it. To limit an agent, give it a tool file with `server` (and `tool`) pinned. |

## Step 5: REST APIs (`tools/apis.json`)

```json
{
  "slack": {
    "base_url": "https://slack.com/api",
    "headers": {"Authorization": "Bearer ${SLACK_BOT_TOKEN}"},
    "methods": ["GET", "POST"]
  },
  "msgraph": {
    "base_url": "https://graph.microsoft.com/v1.0",
    "bearer_cmd": ["az", "account", "get-access-token", "--resource-type", "ms-graph", "--query", "accessToken", "-o", "tsv"],
    "methods": ["GET"]
  }
}
```

| Key | Meaning |
|---|---|
| `base_url` | Every call goes to this URL plus `path`. A `path` with `://`, `//` or `..` is refused. |
| `headers`, `params` | Sent with every call. `${KEY}` comes from `secrets.env`. |
| `methods` | Allowed methods. Default `["GET"]`, so an API is read-only unless you add more. |
| `bearer_cmd` | A command whose stdout is a fresh token (for OAuth APIs). Runs before each call. |
| `timeout` | Seconds, default 30. |

Call: `{"api": "slack", "method": "POST", "path": "/chat.postMessage", "json": {"channel": "C123", "text": "hi"}}`. The reply is `{"status", "ok", "body"}`.

## Step 6: bash scripts that need secrets

badk passes only `PATH`, `HOME`, a few locale and proxy variables, `BADK_RUN_ID`, `BADK_TASK_ID` and the `.env` keys that the tool file lists in `env:` to a tool. For a key in the project `.env`, `env: [KEY]` in the tool file is the simplest way (see "Give a tool a secret" in the badk README). For a key in `tools/secrets.env`, use `tools/with-secrets`:

1. Put the script in `tools/scripts/` and make it executable: `chmod +x tools/scripts/backup.sh`.
2. Put the secrets in `tools/secrets.env`: `DB_PASSWORD=...`.
3. Make a bash tool file `tools/backup.md`:

```yaml
---
name: backup
type: bash
description: Back up the named database.
command: tools/with-secrets DB_PASSWORD,DB_USER backup.sh {{database}}
args:
  type: object
  properties:
    database: {type: string}
  required: [database]
  additionalProperties: false
timeout: 300
---
```

`with-secrets` puts only the named keys in the script's env, runs `tools/scripts/<script>` (a file name, not a path), and masks every secret value in stdout and stderr. `secret_demo.md` is a working example.

## Connectors (Microsoft 365, Teams, Slack, Telegram, Google)

These are open-source projects. Each one runs on your laptop with your own login. The examples in `mcp.json`, `apis.json` and `secrets.env.example` are ready to fill in.

| Service | MCP server (full features) | Laptop login, one time | Direct API in `apis.json` (simple calls) |
|---|---|---|---|
| Microsoft 365: Outlook mail, calendar, OneDrive, Teams, SharePoint | [softeria/ms-365-mcp-server](https://github.com/softeria/ms-365-mcp-server), server `ms365`. `--org-mode` enables Teams and SharePoint (work account). | `npx -y @softeria/ms-365-mcp-server --login` (device code). The token is kept in the OS keychain. | `msgraph`: token from `az login` through `bearer_cmd`, read only. |
| Teams: post to a channel | (as above) | Teams channel → Workflows → "Post to a channel when a webhook request is received". Put the URL in `TEAMS_WEBHOOK_URL`. | `teams_webhook`: POST, path `""`, json is the message card. |
| Slack | [korotovsky/slack-mcp-server](https://github.com/korotovsky/slack-mcp-server), server `slack` | A user token (`xoxp-`) in `SLACK_MCP_XOXP_TOKEN` | `slack`: bot token (`xoxb-`) in `SLACK_BOT_TOKEN`; POST `/chat.postMessage`. |
| Telegram | [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp), server `telegram` (your user account). Install from GitHub, not PyPI. | `git clone`, `uv sync`, run its `session_string_generator.py`. Set `TELEGRAM_MCP_DIR`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_STRING`. | `telegram`: bot token from @BotFather; POST `/sendMessage` with `{"chat_id", "text"}`, GET `/getUpdates`. |
| Google: Gmail, Calendar, Drive | [taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp), server `google` (`uvx workspace-mcp`) | A Google Cloud OAuth "Desktop app" client with the Gmail, Calendar and Drive APIs enabled. The first call opens the browser. | `google`: token from `gcloud auth application-default login --scopes=...` through `bearer_cmd`. |

- The `google` example sets `WORKSPACE_MCP_PORT=8765`, so its OAuth callback does not take port 8000 (badk's viewer). Check the server's README if the variable name has changed.
- A session string, a user token and an OAuth token give full access to that account. Keep them only in `secrets.env` or the OS keychain. Use each server's read-only option when you only read.
- These servers are community projects, not products of Microsoft, Slack, Telegram or Google. Check them before you use them on a company account.

## Traceability

Each tool call is a `tool` record in the run trace (viewer: `/runs/<run_id>`), with its args and its result. Keys that badk knows (`env:`, `${NAME}` of `mcp.json`) are masked everywhere. Tool errors (bad server name, refused path, HTTP failure) come back to the model as a tool error with the reason.
