"""On the tools host, for clipboard.py only: make the take's token and print it, once.

    /opt/release-demo-venv/bin/python scripts/demos/leaked-token/take_token.py

clipboard.py runs this through a Session Manager session, which isn't
logged, and puts what it prints straight on the Mac's clipboard. Run by hand
it would print a live token: don't.
"""

import sys

import take
from common import ssm_client

if __name__ == "__main__":
    if sys.stdout.isatty():
        sys.exit("take_token.py prints a live token: it is for clipboard.py, through Session Manager.")
    sys.stdout.write(take.create(take.admin_client(ssm_client())))
    sys.stdout.flush()
