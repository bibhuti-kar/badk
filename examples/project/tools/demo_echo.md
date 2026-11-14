---
name: demo_echo
type: bash
description: Print the text back with no change. A text that starts with "-" is refused.
command: printf %s {{text}}
args:
  type: object
  properties:
    text: {type: string}
  required: [text]
  additionalProperties: false
timeout: 30
---
