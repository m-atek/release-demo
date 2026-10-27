"""What the stand-ins and the real store clients share.

A stand-in answers the same calls as the real API it replaces, from state it
keeps in the demo's bucket, as fast as it can: no made-up waits. It refuses
what the real service refuses, with the same kind of message. It says it is a
stand-in once, when it starts; Mateusz's videos carry their own disclaimer.
Each stand-in also keeps its last requests, which the console shows.
"""

import json
import time
import urllib.error
import urllib.request

LOG_SIZE = 30


class ServiceError(Exception):
    """A refusal from a store, Jamf, or their stand-in: status and message."""

    def __init__(self, status, message):
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message


class Standin:
    """State under one key in the store, loaded per call and saved after a change."""

    key = ""          # the state document, such as jamf.json
    name = ""         # who it stands in for, such as Jamf Pro
    switch = ""       # the STANDIN name that turns it off

    def __init__(self, store, clock=time.time, quiet=False):
        self.store, self.clock = store, clock
        if not quiet:
            print(f"{self.name} stand-in: answers the same calls as {self.name} from its own state "
                  f"({store.where()}). Set STANDIN without {self.switch} to use {self.name} itself.")

    def load(self):
        state = self.store.read(self.key)
        if state is None:
            raise ServiceError(503, f"The {self.name} stand-in has no state yet. Run: scripts/demos/mobile/reset.py --standins")
        return state

    def save(self, state):
        self.store.write(self.key, state)

    def record(self, state, request, result="200"):
        """One line per call, newest first, for the console."""
        state.setdefault("requests", []).insert(0, {"at": self.clock(), "request": request, "result": result})
        del state["requests"][LOG_SIZE:]


def http(method, url, headers=None, body=None, timeout=120):
    """One HTTP call for the real clients. Returns (status, bytes); a refusal
    with its body raises ServiceError, so stand-in and real API fail alike."""
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        raise ServiceError(error.code, error.read().decode(errors="replace")[:500]) from None
    except urllib.error.URLError as error:
        raise ServiceError(0, f"nothing answered at {url.split('/')[2]}: {error.reason}") from None


def json_or_none(raw):
    return json.loads(raw) if raw else None
