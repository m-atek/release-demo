"""The admin's kubeconfig for video 32's terminal: one cluster, one certificate, two contexts.

    .venv/bin/python scripts/demos/one_wrong_context/contexts.py

Video 8's connect.py makes ~/.kube/release-demo/config, a certificate in the group
release-demo:presenters, cluster admin, for 8 hours, and holds the tunnels. This
copies that file to ~/.kube/release-demo/one-wrong-context, mode 600, with the
contexts staging and production, each with its namespace, and staging current. A
context here is a default namespace, because this demo's staging and production
share one cluster. It calls nothing and prints no key. The certificate lasts what
connect.py's does. scripts/demo.sh down one-wrong-context removes the copy.
"""

import json
import os
import sys
from pathlib import Path

FOLDER = Path.home() / ".kube" / "release-demo"
SOURCE = FOLDER / "config"
COPY = FOLDER / "one-wrong-context"
NAMESPACES = ("staging", "production")
FIRST = "staging"


def two_contexts(config):
    """connect.py's kubeconfig with its one context replaced by one per namespace."""
    context = config["contexts"][0]["context"]
    return {**config,
            "contexts": [{"name": name, "context": {"cluster": context["cluster"], "user": context["user"],
                                                    "namespace": name}} for name in NAMESPACES],
            "current-context": FIRST}


def write(source=SOURCE, copy=COPY):
    """Returns the copy's path. Starts on staging whatever the last take left, so use-context prints its line."""
    if not source.exists():
        raise SystemExit(f"No {source}: run .venv/bin/python scripts/demos/kubernetes/connect.py first, "
                         "in a terminal of its own.")
    # Made private before the key goes in, and kept so if it exists.
    copy.touch(mode=0o600)
    os.chmod(copy, 0o600)
    copy.write_text(json.dumps(two_contexts(json.loads(source.read_text())), indent=2) + "\n")
    return copy


def main():
    print(f"Contexts {' and '.join(NAMESPACES)}, on {FIRST}: {write()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
