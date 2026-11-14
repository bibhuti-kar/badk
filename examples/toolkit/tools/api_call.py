"""api_call: call a REST API named in tools/apis.json, with no MCP in between (httpx).

An API entry: base_url, optional headers / params / methods / timeout, and optional
bearer_cmd (argv whose stdout is a fresh token, e.g. `az account get-access-token ...`).
${NAME} in any string comes from tools/secrets.env. Secret values are masked in the reply.
"""

import json
import subprocess
import sys
from typing import Any

import httpx
import toolkit_secrets as ts

MAX_BODY = 50_000


def _client(spec: dict[str, Any]) -> tuple[httpx.Client, str]:
    headers = dict(spec.get("headers") or {})
    token = ""
    if cmd := spec.get("bearer_cmd"):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603
        if r.returncode != 0 or not r.stdout.strip():
            sys.exit(f"bearer_cmd failed ({r.returncode}): {r.stderr.strip()[-300:]}")
        token = r.stdout.strip()
        headers["Authorization"] = f"Bearer {token}"
    timeout = spec.get("timeout", 30)
    client = httpx.Client(
        base_url=spec["base_url"], headers=headers, params=spec.get("params"), timeout=timeout
    )
    return client, token


def main(args: dict[str, Any]) -> str:
    apis = ts.config("apis.json")
    if args["api"] not in apis:
        sys.exit(f"unknown api {args['api']!r}; tools/apis.json has {sorted(apis)}")
    secrets = ts.load()
    spec = ts.fill(apis[args["api"]], secrets)
    method, path = args.get("method", "GET"), args.get("path", "")
    if method not in spec.get("methods", ["GET"]):
        sys.exit(
            f"{args['api']}: method {method} is not in its methods {spec.get('methods', ['GET'])}"
        )
    if "://" in path or path.startswith("//") or ".." in path.split("/"):
        sys.exit("path must be relative to the API base_url")
    client, token = _client(spec)
    with client:
        r = client.request(method, path, params=args.get("query"), json=args.get("json"))
    try:
        body: Any = r.json()
    except ValueError:
        body = r.text[:MAX_BODY]
    out = {"status": r.status_code, "ok": r.is_success, "body": body}
    return ts.redact(json.dumps(out), {**secrets, "_bearer": token})


if __name__ == "__main__":
    try:
        print(main(json.load(sys.stdin)))
    except httpx.HTTPError as e:
        sys.exit(f"request failed: {type(e).__name__}: {e}")
