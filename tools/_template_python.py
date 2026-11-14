"""Template python tool: read JSON args from stdin, print one JSON object."""

import json
import sys

args = json.load(sys.stdin)
print(json.dumps({"echo": args["text"]}))
