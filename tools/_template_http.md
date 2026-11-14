---
name: _template_http
type: http
description: Send an HTTP request. Templates in header and body leaves.
request:
  method: POST
  url: https://example.com/api/{{id}}
  headers: {Content-Type: application/json}
  body: {text: "{{text}}"}
args:
  type: object
  properties:
    id: {type: string}
    text: {type: string}
  required: [id, text]
  additionalProperties: false
timeout: 30
---
