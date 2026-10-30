"""The coding agent's GitHub token for one agent-task run.

    python scripts/demos/agent_ship/agent_token.py --check    the token's rights, never the token

agent-task gets the token in its own process (agent_task.py), so it never
passes through the Jenkins log: an installation token of the coding agent's
app, limited to release-demo and to contents and pull requests write. It
lasts an hour at most. --check asks for one and prints what GitHub says it
may do.
"""

import argparse
import sys

import agent
from common import ssm_client


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args()
    ssm = ssm_client()
    found = agent.installation(ssm)
    print(f"Installation {found['id']} of {found['app_slug']}: repositories {found['repository_selection']}, "
          f"permissions {dict(sorted(found['permissions'].items()))}")
    _, expires = agent.token(ssm)
    print(f"A token with {agent.TOKEN_PERMISSIONS} on release-demo only, until {expires} (not shown)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
