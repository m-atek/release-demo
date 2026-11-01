"""On the tools host: prints a log from a byte offset as it grows, and ends at its run's exit line.

    python scripts/follow_log.py /var/log/release-demo-rotate.log 1200

rotate.py runs this through an interactive Session Manager session, which passes each line on
as it comes and closes when this ends. A non-interactive session holds everything back until its
command ends, so a `tail -F` there never shows anything.
"""

import sys
import time

from host import EXIT

GIVE_UP = 900  # seconds, so that a run that never writes its exit line doesn't hold the session open


def follow(path, offset, out=sys.stdout, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + GIVE_UP
    with open(path, "rb") as log:
        log.seek(offset)
        line = b""
        while clock() < deadline:
            line += log.readline()
            if not line.endswith(b"\n"):
                sleep(0.1)
                continue
            text = line.decode(errors="replace")
            out.write(text)
            out.flush()
            line = b""
            if text.startswith(EXIT):
                return 0
    return 1


if __name__ == "__main__":
    sys.exit(follow(sys.argv[1], int(sys.argv[2])))
