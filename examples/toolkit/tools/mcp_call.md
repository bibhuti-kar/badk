---
name: mcp_call
type: mcp
description: Call one tool of an MCP server named in tools/mcp.json. arguments must match the tool's inputSchema (see mcp_list).
args:
  type: object
  properties:
    server: {type: string, minLength: 1}
    tool: {type: string, minLength: 1}
    arguments: {type: object, default: {}}
  required: [server, tool]
  additionalProperties: false
timeout: 120
---
