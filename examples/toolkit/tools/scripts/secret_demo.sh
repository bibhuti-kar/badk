#!/usr/bin/env bash
# Example script for a tool that needs a secret. with-secrets puts DEMO_TOKEN in the env.
set -euo pipefail
echo "message: $1"
echo "DEMO_TOKEN has ${#DEMO_TOKEN} characters"
echo "DEMO_TOKEN is $DEMO_TOKEN (with-secrets masks it)"
