---
name: api_call
type: python
description: Call a REST API named in tools/apis.json. path is relative to the API base URL. Returns status, ok and the body (JSON or text).
path: tools/api_call.py
args:
  type: object
  properties:
    api: {type: string, minLength: 1}
    method: {enum: [GET, POST, PUT, PATCH, DELETE], default: GET}
    path: {type: string, default: ""}
    query: {type: object}
    json: {}
  required: [api]
  additionalProperties: false
timeout: 60
---
