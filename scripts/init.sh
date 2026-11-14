#!/usr/bin/env bash
# badk init: make the current folder a badk project, or bring an existing one up to date.
# Usage: mkdir my-app && cd my-app && <badk clone>/scripts/init.sh   (run it again any time)
# Installs the `badk` command (editable, from the clone, with the standard toolset libraries),
# then adds what is missing: agents/, tools/ (templates + standard toolset), .env,
# tools/secrets.env. It never overwrites your files. Only README.md and TOOLSET.md are
# replaced; a changed one is first saved as README.md.bak / TOOLSET.md.bak. Ends with `badk check`.
set -euo pipefail

CLONE=$(cd "$(dirname "$0")/.." && pwd)
PROJ=$(pwd)
KIT="$CLONE/examples/toolkit/tools"
[ "$PROJ" != "$CLONE" ] || { echo "init: run this in a project folder, not in the clone" >&2; exit 2; }

add() {  # add SRC DEST: copy when DEST is missing; never overwrite
  if [ -e "$2" ]; then echo "    kept $2"; else mkdir -p "$(dirname "$2")"; cp -p "$1" "$2"; echo "    + $2"; fi
}

doc() {  # doc SRC DEST: replace DEST; keep a changed one as DEST.bak
  if [ -e "$2" ] && ! cmp -s "$1" "$2"; then mv "$2" "$2.bak"; echo "    $2 changed: old copy in $2.bak"; fi
  cp "$1" "$2"; echo "    = $2"
}

echo "1/5 install the badk command (uv tool, editable from $CLONE, with the toolset libraries)"
uv tool install --quiet --force --editable "$CLONE[toolkit]"
BADK="$(uv tool dir --bin)/badk"
command -v badk >/dev/null || echo "    badk is not on PATH: run 'uv tool update-shell' and open a new shell" >&2

echo "2/5 agents/"
if [ -d agents ]; then
  echo "    kept agents/ (your agents are not changed)"
else
  mkdir agents
  sed 's/^agents: \[\]/agents: [sub-demo]/; s/^always: \[\]/always: [sub-demo]/' \
    "$CLONE/agents/coordinator.md" > agents/coordinator.md
  grep -q '^always: \[sub-demo\]' agents/coordinator.md || {
    echo "init: agents/coordinator.md template changed; demo not added" >&2; exit 1; }
  cp "$CLONE/agents/_template.md" "$CLONE/examples/project/agents/sub-demo.md" \
    "$CLONE/examples/project/agents/coordinator-flow.md" agents/
  add "$CLONE/examples/project/tools/demo_echo.md" tools/demo_echo.md
  echo "    + agents/coordinator.md agents/sub-demo.md agents/coordinator-flow.md agents/_template.md"
fi

echo "3/5 tools/ (templates and the standard toolset; delete a file and run again to get a new copy)"
for f in "$CLONE"/tools/_template_*; do add "$f" "tools/$(basename "$f")"; done
(cd "$KIT" && find . -type f ! -name '*.pyc' ! -path '*/__pycache__/*' | sort) | while read -r f; do
  add "$KIT/${f#./}" "tools/${f#./}"
done

echo "4/5 .env, tools/secrets.env, README.md, TOOLSET.md"
SRC="$CLONE/.env"; [ -f "$SRC" ] || SRC="$CLONE/.env.example"
if [ -e .env ]; then echo "    kept .env"; else install -m 600 "$SRC" .env; echo "    + .env"; fi
if [ -e tools/secrets.env ]; then echo "    kept tools/secrets.env"; else
  install -m 600 "$KIT/secrets.env.example" tools/secrets.env; echo "    + tools/secrets.env"; fi
grep -qxF 'tools/secrets.env' .gitignore 2>/dev/null || echo 'tools/secrets.env' >> .gitignore
doc "$CLONE/scripts/init-README.md" README.md
doc "$CLONE/examples/toolkit/README.md" TOOLSET.md

echo "5/5 badk check"
if "$BADK" check; then
  hint=""
  [ -f agents/coordinator-flow.md ] && hint='  (no model: badk run "/flow hello" --headless)'
  echo "Ready. Next: badk run \"say hello\" --headless$hint"
else
  echo "Fix each FAIL line (see README.md, step 2), then run: badk check" >&2
  exit 1
fi
