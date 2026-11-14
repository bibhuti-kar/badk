---
name: _template_python
type: python
description: Run a Python script. Args arrive as JSON on stdin, JSON goes to stdout.
path: tools/_template_python.py
args:
  type: object
  properties:
    text: {type: string}
  required: [text]
  additionalProperties: false
timeout: 30
# output (optional): a JSON schema for the result `body` of a flow tool step. A body that
# does not match is a TOOL_ERROR naming the field (never retried), and a flow reference
# `{{id.body.path}}` to a path the schema lacks is refused at load. The model tool loop
# ignores it. For a python tool the body is the printed object.
# output:
#   type: object
#   properties:
#     echo: {type: string}
#   required: [echo]
---
