---
name: mcp_list
type: mcp
action: list
description: List the tools of an MCP server named in tools/mcp.json (name, description, inputSchema).
args:
  type: object
  properties:
    server: {type: string, minLength: 1}
  required: [server]
  additionalProperties: false
timeout: 120
---
