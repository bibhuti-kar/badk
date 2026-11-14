---
name: secret_demo
type: bash
description: Example of a script tool that needs a secret. Prints the message and the masked DEMO_TOKEN.
# with-secrets KEYS SCRIPT ARGS: KEYS from tools/secrets.env go into the env of tools/scripts/SCRIPT only.
command: tools/with-secrets DEMO_TOKEN secret_demo.sh {{text}}
args:
  type: object
  properties:
    text: {type: string}
  required: [text]
  additionalProperties: false
timeout: 30
---
