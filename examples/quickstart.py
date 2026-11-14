"""Smallest badk program: run a coordinator with two dependent sub-agents and print the run URL.

Needs the stack (`./start.sh`). The project root is examples/, which has no .env, so run it
from the repo root and let the process env supply the settings (process env wins):

    uv run --env-file .env python examples/quickstart.py ["your ask"]

On success stdout is the answer, then the viewer URL. Any other status exits 3."""

import sys
from pathlib import Path

import badk

ASK = "Explain in two sentences what a hash map is."

if __name__ == "__main__":
    result = badk.run(
        sys.argv[1] if len(sys.argv) > 1 else ASK,
        agents_dir=Path(__file__).parent / "agents",
    )
    if result.status != "COMPLETED":
        sys.stderr.write(f"{result.status}: {result.error}\n")
        sys.exit(3)
    sys.stdout.write(f"{result.answer}\n{result.url}\n")
