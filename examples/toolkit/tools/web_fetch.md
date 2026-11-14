---
name: web_fetch
type: python
description: Get a public web page (http or https) as markdown text. Private and local addresses are refused.
path: tools/web_fetch.py
args:
  type: object
  properties:
    url: {type: string, pattern: "^https?://"}
    max_chars: {type: integer, minimum: 500, maximum: 60000, default: 20000}
  required: [url]
  additionalProperties: false
timeout: 40
---
