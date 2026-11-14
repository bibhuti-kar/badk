"""web_fetch: GET a public http(s) page and return it as markdown text.

The request goes through badk's own http tool executor: redirects are checked hop by hop,
private and loopback addresses are refused, the body is capped at 100 kB.
"""

import json
import sys
from typing import Any

import html2text

from badk.engine.envelope import ToolCall
from badk.errors import BadkError
from badk.tools.definition import ToolDef
from badk.tools.http import execute_http

AGENT = "Mozilla/5.0 (compatible; badk-web-fetch)"
TOOL = ToolDef(
    name="web_fetch",
    type="http",
    description="web_fetch",
    request={"method": "GET", "url": "{{url}}", "headers": {"User-Agent": AGENT}},
    args={"type": "object"},
    timeout=25,
)


def main(args: dict[str, Any]) -> dict[str, Any]:
    r = execute_http(ToolCall(name="web_fetch", args={"url": args["url"]}), TOOL)
    ctype = str(r["headers"].get("content-type", ""))
    text = html2text.html2text(r["body"]) if "html" in ctype else r["body"]
    limit = args.get("max_chars", 20000)
    return {
        "url": args["url"],
        "status": r["status"],
        "content_type": ctype,
        "text": text[:limit],
        "truncated": bool(r["truncated"]) or len(text) > limit,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(main(json.load(sys.stdin))))
    except BadkError as e:
        sys.exit(f"{e.code}: {e.message}")
