"""Put the Jenkins secrets of the agents outside the tools host in SSM.

    agent_secrets.py /run/release-demo/secrets

Demo 6's Mac and Windows build agents connect over WebSocket with their
node's secret, which Jenkins derives from its own key, so it stays the same
across restarts. They read it from SSM, jenkins/agent-secret/<node>. Run by
boot.sh; a node casc.yaml doesn't define is skipped. Prints no secret.
"""

import base64
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from common import SSM_PREFIX, aws_session, put_parameter  # noqa: E402

JENKINS = "http://localhost:8080"
NODES = ["mac", "windows"]


def node_secret(node, password):
    """The node's secret from its agent page, or None when Jenkins has no such node."""
    auth = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
    request = urllib.request.Request(f"{JENKINS}/computer/{node}/jenkins-agent.jnlp", headers={"Authorization": auth})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            found = re.search(r"\b[0-9a-f]{64}\b", response.read().decode())
    except urllib.error.HTTPError:
        return None
    return found.group(0) if found else None


def main():
    password = (Path(sys.argv[1]) / "jenkins-admin-password").read_text().strip()
    ssm = aws_session().client("ssm")
    for node in NODES:
        secret = node_secret(node, password)
        if not secret:
            print(f"agent secret {node}: Jenkins has no node {node}, skipped")
            continue
        name = f"jenkins/agent-secret/{node}"
        try:
            stored = ssm.get_parameter(Name=SSM_PREFIX + name, WithDecryption=True)["Parameter"]["Value"]
        except ssm.exceptions.ParameterNotFound:
            stored = None
        if stored == secret:
            print(f"agent secret {node}: already in SSM")
            continue
        put_parameter(ssm, name, secret)
        print(f"agent secret {node}: stored in SSM")


if __name__ == "__main__":
    main()
