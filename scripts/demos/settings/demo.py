"""scripts/demo.sh status settings: where video 29 stands, read only. It has no up or down: AWS AppConfig
costs nothing while nobody asks it anything, so there is nothing to start or stop.

    python scripts/demos/settings/demo.py status

From the Mac this reads with your sign-in, as demo 21's does: the deploy role trusts only the tools host.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parents[1]))

import setting  # noqa: E402
from common import aws_session, http_get, service  # noqa: E402

SERVICE = "orders-service"


def status():
    known = setting.ids(SERVICE)
    client = setting.appconfig()
    functions = aws_session().client("lambda")
    for environment, config in service(SERVICE)["environments"].items():
        number, content = setting.current(client, known, environment)
        going = setting.flags.running(client, known, environment)
        print(f"{environment}: version {number} of the file: {json.dumps(content)}"
              f"{f', deployment {going} still running' if going else ''}")
        url = functions.get_function_url_config(FunctionName=config["function"], Qualifier="live")["FunctionUrl"].rstrip("/")
        health = setting.live_health(url, http_get)
        print(f"{environment}: {config['function']} runs {health.get('version', 'nothing that answers')}, and serves "
              f"{json.dumps(health.get('settings', 'no settings'))}")


def main():
    {"status": status}[sys.argv[1] if len(sys.argv) > 1 else "status"]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
