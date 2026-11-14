#!/usr/bin/env bash
# Start the badk stack (app + writer + PostgreSQL + RabbitMQ) in containers.
#   ./start.sh          build if needed, then start
#   ./start.sh --build  clean no-cache rebuild, then start
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "missing .env: cp .env.example .env and substitute your own values" >&2
  exit 1
fi

# CLOUD_DEPLOY=true adds the `cloud` compose profile (runtime service). Resolved by the same
# parser/validator the app uses (src/badk/config.py): process env over .env, ${VAR} interpolation,
# cloud mode also loads agents/*.md (PROVIDER_NOT_ALLOWED for claude_cli), only true/false accepted, empty is an error, absent is false. Prints "<true|false> <APP_PORT>".
resolved=$(uv run --quiet python -c '
import ipaddress, os, socket, sys
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import ValidationError
from badk.agents.definition import load_agents
from badk.config import Settings, _parse_env
from badk.errors import BadkError
KEYS = ("CLOUD_DEPLOY", "APP_PORT", "LLM_BASE_URL", "LLM_PROVIDER", "LLM_MODEL", "CLAUDE_MODEL", "PROVIDER_OVERRIDE", "PROVIDER_TIMEOUT_SEC")
STALE = "RUNTIME_LLM_BASE_URL"
try:
    m = {**_parse_env(Path(".env").read_text(), os.environ), **os.environ}
    if STALE in m:  # an empty value counts
        print(f"VALIDATION_ERROR: {STALE} is no longer read: LLM_BASE_URL replaces it "
              "(remove the line from .env and unset it in the shell)", file=sys.stderr)
        sys.exit(1)
    s = Settings.model_validate({k: m[k] for k in KEYS if k in m})
except (BadkError, ValidationError) as e:
    print(f"VALIDATION_ERROR: CLOUD_DEPLOY/APP_PORT/LLM_PROVIDER invalid: {e}", file=sys.stderr)
    sys.exit(1)
if s.cloud_deploy and s.provider_override != "fake" and s.llm_provider == "openai":  # only openai reads LLM_BASE_URL; fake makes no call
    host = (urlsplit(s.llm_base_url).hostname or "").lower()
    if not host:
        print("VALIDATION_ERROR: LLM_BASE_URL has no host: set a full URL such as "
              "http://host.docker.internal:11434 (substitute your own)", file=sys.stderr)
        sys.exit(1)
    host = host.rstrip(".")  # "localhost." is localhost
    try:
        ip = ipaddress.ip_address(host)
        ips = [ip, ip.ipv4_mapped] if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else [ip]
    except ValueError:  # inet_aton reads the short forms the resolver accepts: 127.1, 0177.0.0.1, 0
        try:
            ips = [ipaddress.IPv4Address(socket.inet_aton(host))]
        except OSError:
            ips = []
    if host in ("localhost", "localhost6", "localhost6.localdomain6", "ip6-localhost", "ip6-loopback") or host.endswith(".localhost") or any(i.is_loopback or i.is_unspecified for i in ips):
        print(f"VALIDATION_ERROR: LLM_BASE_URL host {host} is loopback; the runtime container cannot "
              "reach it in cloud mode: set a URL the container reaches "
              "(e.g. http://host.docker.internal:11434, substitute your own)", file=sys.stderr)
        sys.exit(1)
if s.cloud_deploy:  # the real loader refuses claude_cli (resolved or literal) before any container starts
    try:
        load_agents(Path("agents"), s)
    except BadkError as e:
        print(f"{e.code.value}: {e}", file=sys.stderr)
        sys.exit(1)
print(str(s.cloud_deploy).lower(), s.app_port)
') || exit 1
read -r deploy APP_PORT <<<"$resolved"
if [ "$deploy" = true ]; then PROFILE=(--profile cloud); else PROFILE=(); fi

# ${PROFILE[@]+...}: an empty array is "unbound" under set -u in bash 3.2 (macOS)
if [ "${1:-}" = "--build" ]; then
  docker compose ${PROFILE[@]+"${PROFILE[@]}"} build --no-cache
fi
docker compose ${PROFILE[@]+"${PROFILE[@]}"} up -d --build --wait

echo "badk up. App port: ${APP_PORT}. Health: GET /health"
