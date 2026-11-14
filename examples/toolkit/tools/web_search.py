"""web_search: DuckDuckGo results through ddgs (no API key). JSON args on stdin."""

import json
import sys
from typing import Any

from ddgs import DDGS
from ddgs.exceptions import DDGSException


def main(args: dict[str, Any]) -> dict[str, Any]:
    """DuckDuckGo first; if it blocks or rate-limits, ddgs's other keyless engines ("auto")."""
    error: DDGSException | None = None
    for engine in ("duckduckgo", "auto"):
        try:
            hits = DDGS().text(
                args["query"], max_results=args.get("max_results", 5), backend=engine
            )
        except DDGSException as e:
            error = e
            continue
        rows = [
            {"title": h.get("title"), "url": h.get("href"), "snippet": h.get("body")} for h in hits
        ]
        return {"engine": engine, "results": rows}
    raise error or DDGSException("no engine answered")


if __name__ == "__main__":
    try:
        print(json.dumps(main(json.load(sys.stdin))))
    except DDGSException as e:
        sys.exit(f"search failed: {e}")
