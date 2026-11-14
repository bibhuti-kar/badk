---
name: web_search
type: python
description: Search the web with DuckDuckGo. Returns a list of results, each with title, url and snippet.
path: tools/web_search.py
args:
  type: object
  properties:
    query: {type: string, minLength: 1}
    max_results: {type: integer, minimum: 1, maximum: 20, default: 5}
  required: [query]
  additionalProperties: false
timeout: 40
---
