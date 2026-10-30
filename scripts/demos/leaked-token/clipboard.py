"""Before a take of video 10: a JFrog token of release-pipeline on your clipboard, for 60 seconds.

    python scripts/demos/leaked-token/clipboard.py                    the take's own token (decision 16)
    python scripts/demos/leaked-token/clipboard.py --pipeline-token   the pipeline's token from SSM, jcr/token

By default the tools host makes a second token of release-pipeline for the
take, valid for a day, with the pipeline's own rights, and hands it over
through a Session Manager session, which isn't logged: nothing stores it.
With --pipeline-token it copies the token the pipeline publishes with. Either
way rotate.py jcr revokes it. It prints only the last four characters, and
after 60 seconds clears the clipboard if it still holds the token.
"""

import argparse
import json
import subprocess
import sys
import time

import take  # noqa: F401  (puts scripts/ on the path)
from common import get_parameter, ssm_client  # noqa: E402

SECONDS = 60
COMMAND = "cd /opt/release-demo && /opt/release-demo-venv/bin/python scripts/demos/leaked-token/take_token.py"


def from_host(the_host=None, run=subprocess.run):
    import host

    the_host = the_host or host.Host()
    result = run(["aws", "ssm", "start-session", "--target", the_host.id,
                  "--document-name", "AWS-StartNonInteractiveCommand",
                  "--parameters", json.dumps({"command": [COMMAND]})],
                 capture_output=True, text=True, timeout=120)
    from rotators import records

    tokens = records.tokens_in(result.stdout)
    if result.returncode != 0 or len(tokens) != 1:
        raise SystemExit("The tools host didn't hand over a token. Is it up (scripts/tools.sh status), "
                         "and the Session Manager plugin installed (brew install --cask session-manager-plugin)?")
    return tokens[0]


def copy(token, seconds=SECONDS, run=subprocess.run, sleep=time.sleep, out=sys.stdout):
    run(["pbcopy"], input=token, text=True, check=True)
    print(f"On the clipboard: a JFrog token of release-pipeline ending in {token[-4:]}. "
          f"It clears itself in {seconds} seconds.", file=out, flush=True)
    sleep(seconds)
    held = run(["pbpaste"], capture_output=True, text=True).stdout
    if held == token:
        run(["pbcopy"], input="", text=True, check=True)
        print("Cleared the clipboard.", file=out)
    else:
        print("The clipboard holds something else now: left as it is.", file=out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pipeline-token", action="store_true", help="the pipeline's own token, jcr/token")
    args = parser.parse_args()
    token = get_parameter(ssm_client(), "jcr/token") if args.pipeline_token else from_host()
    copy(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
