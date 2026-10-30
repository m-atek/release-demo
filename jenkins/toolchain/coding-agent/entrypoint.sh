#!/bin/sh
# The coding agent's entrypoint. Claude Code asks once, in a terminal, before it uses an API
# key from the environment; the container's own ~/.claude.json records that
# answer here, so the take starts at Claude Code's input. The record is the
# key's last 20 characters, as Claude Code keeps it, in the container's
# writable layer only: it leaves with the container.
set -eu
# What the agent writes in a mounted clone, the Jenkins agent that made it may remove.
umask 0000
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  python3 - <<'PY'
import json
import os

path = os.path.expanduser("~/.claude.json")
config = json.load(open(path)) if os.path.exists(path) else {}
config["customApiKeyResponses"] = {"approved": [os.environ["ANTHROPIC_API_KEY"][-20:]], "rejected": []}
with open(path, "w") as file:
    json.dump(config, file)
PY
fi
exec "$@"
