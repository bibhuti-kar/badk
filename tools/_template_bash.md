---
name: _template_bash
type: bash
description: Print text. Copy this file to a new name and edit.
# RULE: argv command, no shell. `command` is split once at load into fixed words; each {{x}} is plain text inside ONE word, so a value (spaces, quotes, ;, $(..)) stays one argument. Refused at load: unquoted | ; & > < ( ). No expansion: $HOME, globs, ~ and backticks reach the program literally; put pipelines, redirects and shell logic in a script the tool calls (sh scripts/x.sh {{a}}, or a script with a shebang and the exec bit) or use a python tool. A word that STARTS with {{x}} refuses a value starting with "-" (write --opt={{x}} instead); the program name cannot be a placeholder. User decision 2026-11-10: argv only, no shell.
command: echo {{text}}
args:
  type: object
  properties:
    text: {type: string}
  required: [text]
  additionalProperties: false
timeout: 30
---
