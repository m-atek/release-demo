"""Video 32's right-hand window, and the first-day check's count: calls to catalog in production.

    python3 scripts/demos/one_wrong_context/calls.py                 until stopped, at most 20 minutes
    python3 scripts/demos/one_wrong_context/calls.py --seconds 90    the first-day check: ends with a summary

On the tools host, through production's NodePort, as video 25's check calls
staging: ten calls a second, each on a new connection, each allowed a second. A
200 that lists products counts as answered; anything else, refused, reset, timed
out or another status, as failed. One line a second holds the running totals,
as of that second. Once calls have failed and five seconds have passed without
one, one line says so. Standard library only, so it starts at once from any
folder. window.py shows it on the Mac, through a Session Manager session.
"""

import argparse
import concurrent.futures
import datetime
import http.client
import json
import sys
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PORT = 30082  # services.yaml, catalog's production node_port. A test holds it
ROUTE = "/api/products"
RATE = 10
TIMEOUT_SECONDS = 1.0
QUIET_SECONDS = 5
GIVE_UP_SECONDS = 20 * 60  # so a session nobody ended stops by itself
HEADER_EVERY = 10
HEADER = "TIME      ANSWERED  FAILED"
RED, GREEN, PLAIN = "\033[31m", "\033[32m", "\033[0m"


def call(port=PORT, route=ROUTE, timeout=TIMEOUT_SECONDS):
    """One call on a new connection: True when it answered 200 with products."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", route)
        response = connection.getresponse()
        return response.status == 200 and b"products" in response.read()
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


class Tally:
    """Running totals. Calls finish on their own threads, so each count takes the lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.answered = self.failed = 0
        self.first_failed = self.last_failed = None

    def add(self, answered, at):
        with self.lock:
            if answered:
                self.answered += 1
            else:
                self.failed += 1
                self.first_failed = self.first_failed if self.first_failed is not None else at
                self.last_failed = at

    def totals(self):
        with self.lock:
            return self.answered, self.failed, self.last_failed


class Lines:
    """What the window shows: a line a second, the header again every ten, and the line when calls are back."""

    def __init__(self, out=sys.stdout):
        self.out = out
        self.count = 0
        self.shown_failed = 0
        self.said_back = True

    def second(self, label, answered, failed, last_failed, now):
        if self.count % HEADER_EVERY == 0:
            print(HEADER, file=self.out)
        self.count += 1
        rose = failed > self.shown_failed
        colour, end = (RED, PLAIN) if rose else ("", "")
        print(f"{label}  {answered:>8,}  {colour}{failed:>6,}{end}", file=self.out)
        if rose:
            self.said_back = False
        self.shown_failed = failed
        if not self.said_back and last_failed is not None and now - last_failed >= QUIET_SECONDS:
            print(f"{GREEN}Answering again: {failed:,} calls failed.{PLAIN}", file=self.out)
            self.said_back = True
        self.out.flush()


def clock_label(moment):
    """The time as Slack shows it to Mateusz: Europe/Amsterdam, or UTC where the host has no time zones."""
    try:
        zone = ZoneInfo("Europe/Amsterdam")
    except ZoneInfoNotFoundError:
        zone = datetime.timezone.utc
    return datetime.datetime.fromtimestamp(moment, zone).strftime("%H:%M:%S")


def run(seconds=None, out=sys.stdout, caller=call, clock=time.monotonic, wall=time.time, sleep=time.sleep):
    """Calls at the rate until seconds pass, or the give-up time. Returns the tally."""
    tally, lines = Tally(), Lines(out)
    limit = GIVE_UP_SECONDS if seconds is None else seconds

    def one():
        answered = caller()
        tally.add(answered, wall())

    start = clock()
    with concurrent.futures.ThreadPoolExecutor(max_workers=RATE * 3) as pool:
        for tick in range(limit * RATE):
            wait = start + tick / RATE - clock()
            if wait > 0:
                sleep(wait)
            pool.submit(one)
            if (tick + 1) % RATE == 0:
                answered, failed, last_failed = tally.totals()
                moment = wall()
                lines.second(clock_label(moment), answered, failed, last_failed, moment)
    return tally


def summary(tally):
    """The last line of a timed run, for check.py: times are the host's clock, in seconds."""
    return "== summary " + json.dumps({"answered": tally.answered, "failed": tally.failed,
                                       "first_failed": tally.first_failed, "last_failed": tally.last_failed})


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=int, help="end after this long, with a summary line")
    args = parser.parse_args()
    tally = run(args.seconds)
    if args.seconds is not None:
        print(summary(tally), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
