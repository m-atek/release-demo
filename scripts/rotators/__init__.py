"""Rotators for scripts/rotate.py: one module per secret, each a run(steps, reason).

What they share: the step lines the terminal shows and video 10 reads,

    14:02:16  revoked   old token 7f3a2c1e

at most 52 columns, in Europe/Amsterdam time; the timeline the incident's
work note repeats; and the leak's duration, rounded down to the second.
Nothing here prints a token: ids only, first 8 characters.
"""

import base64
import datetime
import json
import math
import sys
import time
from zoneinfo import ZoneInfo

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
WIDTH = 52
STEP = 9
NAMES = ("jcr", "sonar")


def clock(seconds):
    """HH:MM:SS in Europe/Amsterdam, the time zone of Slack, ServiceNow and the terminal."""
    return datetime.datetime.fromtimestamp(seconds, AMSTERDAM).strftime("%H:%M:%S")


def leak_to_useless(seconds):
    """0m 15s, rounded down: the line and the incident give the same."""
    whole = max(0, math.floor(seconds))
    return f"{whole // 60}m {whole % 60:02d}s"


def short_id(token_id):
    return (token_id or "unknown")[:8]


def jwt_claims(token):
    """A JFrog access token's claims (sub, jti, scp, iat, exp), unverified; None if it isn't a JWT."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, UnicodeDecodeError):
        return None


class Failed(Exception):
    """A step that didn't do what it must: rotate.py says so and exits 1."""


class Steps:
    """Prints each step as it happens and keeps the timeline for the incident.
    now and sleep are injectable, so tests run without waiting."""

    def __init__(self, out=None, now=time.time, sleep=time.sleep):
        self.out = out or sys.stdout
        self.now, self.sleep = now, sleep
        self.started = now()
        self.timeline = []  # (epoch seconds, text for the incident)

    def line(self, step, what, at=None):
        at = self.now() if at is None else at
        text = f"{clock(at)}  {step:<{STEP}} {what}"
        if len(text) > WIDTH:
            text = text[:WIDTH - 3] + "..."
        print(text, file=self.out, flush=True)
        return at

    def record(self, text, at=None):
        self.timeline.append((self.now() if at is None else at, text))

    def timeline_text(self):
        return "\n".join(f"{clock(at)}  {text}" for at, text in self.timeline)

    def last(self, text):
        print(text, file=self.out, flush=True)


def rotator(name):
    if name not in NAMES:
        raise SystemExit(f"No rotator for {name}. Rotators: {', '.join(NAMES)}")
    module = __import__(f"rotators.{name}", fromlist=["run"])
    return module
