"""Fill the empty Jira project ORD, in key order, so every key the demos use
lands exactly as written. The gaps get a plausible backlog for a fictional
retailer: epics, stories, tasks and bugs, some done, a few in progress.

    python scripts/jira_seed.py            create ORD-1 to ORD-90
    python scripts/jira_seed.py --dry-run  print what it would create

Signs in as Release Pipeline (jira/* in SSM), which administers ORD, and puts
issues on Mateusz as reporter and assignee: Sam has no Jira account. Stops if
ORD already has issues, because Jira never reuses keys.
"""

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from common import fail, get_parameter, ssm_client

PROJECT = "ORD"
TODO, DOING, DONE = "To Do", "In Progress", "Done"

# The keys the demo specs name. Their issues stay open: the releases close them.
SPEC = {
    1: ("Story", "Show the order count on the order page", "orders-service"),
    2: ("Story", "Return order totals with two decimals in the API", "orders-service"),
    3: ("Story", "Add the store name to each order", "orders-service"),
    4: ("Story", "Sort today's orders by order number", "orders-service"),
    5: ("Story", "Read the store list from the regional setting", "orders-service"),
    6: ("Bug", "Keep the store list in code until every environment has the setting", "orders-service"),
    7: ("Story", "Show the order status in the orders table", "orders-service"),
    8: ("Story", "Add a cache header to the orders API", "orders-service"),
    9: ("Story", "Read the page title from the brand setting", "orders-service"),
    10: ("Bug", "Fall back to the default page title when the brand setting is missing", "orders-service"),
    11: ("Story", "Show the environment in the page title", "orders-service"),
    12: ("Story", "Show the delivery estimate on the order page", "orders-service"),
    13: ("Story", "Read the delivery estimate from the regional setting", "orders-service"),
    22: ("Story", "Store exports bucket and queue", "network"),
    31: ("Story", "Store sync uploads every 5 minutes instead of 15", "store-sync"),
    44: ("Story", "Show stock per store in the inventory API", "inventory"),
    52: ("Story", "Regional prices for Belgian stores", "pricing-api"),
    60: ("Story", "Quotes in EUR and GBP", "quote-service"),
    71: ("Story", "Show today's orders in Store Assistant", "store-assistant"),
    80: ("Story", "Show stock alerts on the store floor", "store-floor"),
    81: ("Story", "Show stock alerts on the store floor: store app releases", "store-floor"),
    90: ("Story", "Search the catalog by supplier", "catalog"),
}

# Epics: key -> summary. Backlog items name their epic by key.
EPICS = {
    14: "Delivery estimates on every order channel",
    15: "Store sync you can trust",
    16: "Inventory API for stores and apps",
    17: "Pricing for new markets",
    18: "Store apps on every device",
    19: "Self-service releases",
    20: "Catalog search",
}

# The gaps, in key order: (type, summary, status, epic key, label).
BACKLOG = [
    ("Task", "Agree the delivery estimate rules with logistics", DONE, 14, "orders-service"),
    ("Story", "Store sync retries a failed upload with backoff", DONE, 15, "store-sync"),
    ("Bug", "Store sync logs the full order payload at info level", DONE, 15, "store-sync"),
    ("Story", "Alert when a store hasn't synced for 30 minutes", TODO, 15, "store-sync"),
    ("Task", "Move store sync credentials to the secrets store", DONE, 15, "store-sync"),
    ("Story", "Store sync reports its version to the fleet dashboard", TODO, 15, "store-sync"),
    ("Spike", "Measure upload size per store over a week", DONE, 15, "store-sync"),
    ("Story", "Inventory API pages results by 100", DONE, 16, "inventory"),
    ("Bug", "Inventory API returns 500 for an unknown store id", DONE, 16, "inventory"),
    ("Story", "Return the last stock count time per store", TODO, 16, "inventory"),
    ("Task", "Load test the inventory API at 50 requests a second", DOING, 16, "inventory"),
    ("Story", "Cache stock per store for 60 seconds", TODO, 16, "inventory"),
    ("Task", "Document the inventory API for the store app team", DONE, 16, "inventory"),
    ("Story", "Price rules per country", DONE, 17, "pricing-api"),
    ("Story", "Round Belgian prices to 5 cents", TODO, 17, "pricing-api"),
    ("Bug", "Pricing API ignores the store's country for promotions", DONE, 17, "pricing-api"),
    ("Task", "Contract tests for the pricing API's consumers", DOING, 17, "pricing-api"),
    ("Story", "Show the price source in the pricing API response", TODO, 17, "pricing-api"),
    ("Task", "Ask finance for the VAT table per market", DONE, 17, "pricing-api"),
    ("Story", "Quote service keeps quotes for 30 days", DONE, 17, "quote-service"),
    ("Bug", "Quote totals differ by a cent from the till", DONE, 17, "quote-service"),
    ("Story", "Quote service reads exchange rates hourly", TODO, 17, "quote-service"),
    ("Task", "Run quote service in a second cloud for the UK stores", TODO, 17, "quote-service"),
    ("Story", "Quote PDF shows the store address", TODO, 17, "quote-service"),
    ("Task", "Sign the Store Assistant installer", DONE, 18, "store-assistant"),
    ("Story", "Store Assistant shows the store's opening hours", DONE, 18, "store-assistant"),
    ("Bug", "Store Assistant crashes when the store has no orders today", DONE, 18, "store-assistant"),
    ("Story", "Store Assistant updates itself overnight", TODO, 18, "store-assistant"),
    ("Task", "Pilot Store Assistant in three stores", DOING, 18, "store-assistant"),
    ("Story", "Store Assistant works offline for an hour", TODO, 18, "store-assistant"),
    ("Task", "Collect feedback from the pilot stores", TODO, 18, "store-assistant"),
    ("Story", "Store floor app scans shelf labels", DONE, 18, "store-floor"),
    ("Bug", "Store floor app loses the session after the device sleeps", DONE, 18, "store-floor"),
    ("Story", "Store floor app shows the product photo", TODO, 18, "store-floor"),
    ("Task", "Enrol the floor devices in device management", DONE, 18, "store-floor"),
    ("Story", "Store floor app in Dutch and French", TODO, 18, "store-floor"),
    ("Task", "Test the store floor app on the older handhelds", DOING, 18, "store-floor"),
    ("Story", "Store floor app sends crash reports", TODO, 18, "store-floor"),
    ("Task", "Release checklist: replace the manual steps with the pipeline", DONE, 19, "release"),
    ("Story", "Start a release from Slack", DONE, 19, "release"),
    ("Story", "Open the change request with the evidence attached", DONE, 19, "release"),
    ("Story", "Roll back automatically when the production check fails", DONE, 19, "release"),
    ("Task", "Give the change approvers a group of their own", DONE, 19, "release"),
    ("Story", "Delivery metrics dashboard for the team", DONE, 19, "release"),
    ("Task", "Retire the release share on the file server", DOING, 19, "release"),
    ("Story", "Release the Windows store servers in waves", TODO, 19, "release"),
    ("Story", "Release infrastructure changes through the same flow", TODO, 19, "release"),
    ("Task", "Add the data-center servers to the release flow", TODO, 19, "release"),
    ("Story", "Catalog search returns results in under 200 ms", DONE, 20, "catalog"),
    ("Bug", "Catalog search misses products with accents in the name", DONE, 20, "catalog"),
    ("Story", "Catalog search by EAN code", DONE, 20, "catalog"),
    ("Task", "Move catalog search to the shared cluster", DOING, 20, "catalog"),
    ("Story", "Catalog search suggests as you type", TODO, 20, "catalog"),
    ("Task", "Index the supplier name in catalog search", TODO, 20, "catalog"),
    ("Story", "Show the supplier on the product page", TODO, 20, "catalog"),
    ("Bug", "Order page shows yesterday's orders just after midnight", DONE, 14, "orders-service"),
    ("Story", "Show the delivery estimate in the confirmation email", TODO, 14, "orders-service"),
    ("Story", "Show the delivery estimate in the store app", TODO, 14, "orders-service"),
    ("Task", "Ask the carriers for their cut-off times per region", DOING, 14, "orders-service"),
    ("Story", "Delivery estimate per postcode area", TODO, 14, "orders-service"),
    ("Bug", "Orders API drops the store id for click-and-collect orders", DONE, None, "orders-service"),
]

# Which epic each spec key belongs to. ORD-1 to ORD-11 are small order-page changes, outside any epic.
SPEC_EPICS = {12: 14, 13: 14, 22: 19, 31: 15, 44: 16, 52: 17, 60: 17, 71: 18, 80: 18, 81: 18, 90: 20}

GAPS = [n for n in range(1, 91) if n not in SPEC and n not in EPICS]
assert len(GAPS) == len(BACKLOG), (len(GAPS), len(BACKLOG))


def plan():
    """Every key from 1 to 90: (type, summary, status, epic key, label)."""
    items, backlog = {}, iter(BACKLOG)
    for key in range(1, 91):
        if key in SPEC:
            kind, summary, label = SPEC[key]
            items[key] = (kind, summary, TODO, SPEC_EPICS.get(key), label)
        elif key in EPICS:
            items[key] = ("Epic", EPICS[key], DOING, None, None)
        else:
            items[key] = next(backlog)
    return items


class Jira:
    def __init__(self):
        ssm = ssm_client()
        self.site = get_parameter(ssm, "jira/site-url").rstrip("/")
        email, token = get_parameter(ssm, "jira/email"), get_parameter(ssm, "jira/api-token")
        self.auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    def call(self, method, path, body=None):
        request = urllib.request.Request(
            self.site + path, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": self.auth, "Accept": "application/json", "Content-Type": "application/json"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                if error.code == 429 and attempt < 4:
                    time.sleep(int(error.headers.get("Retry-After", "5")))
                    continue
                fail(f"Jira {method} {path}: {error.code} {error.read().decode()[:300]}")


def paragraph(text):
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    items = plan()
    if args.dry_run:
        for key, (kind, summary, status, epic, label) in items.items():
            print(f"{PROJECT}-{key:<3} {kind:<6} {status:<12} {f'{PROJECT}-{epic}' if epic else '':<7} {summary}")
        return

    jira = Jira()
    existing = jira.call("GET", f"/rest/api/3/search/jql?jql=project%3D{PROJECT}&maxResults=1&fields=key")
    if existing["issues"]:
        fail(f"{PROJECT} already has issues. Keys can't be reused: recreate the space empty, then run this again.")

    types = {t["name"]: t["id"] for t in jira.call("GET", f"/rest/api/3/issue/createmeta/{PROJECT}/issuetypes")["issueTypes"]}
    people = jira.call("GET", "/rest/api/3/users/search?maxResults=100")
    owner = next(u["accountId"] for u in people if u["accountType"] == "atlassian" and u["displayName"] != "Release Pipeline")

    for key, (kind, summary, status, epic, label) in items.items():
        kind = kind if kind in types else "Task"
        fields = {"project": {"key": PROJECT}, "issuetype": {"id": types[kind]}, "summary": summary,
                  "reporter": {"id": owner}, "assignee": {"id": owner},
                  "description": paragraph(f"{summary}.")}
        if label:
            fields["labels"] = [label]
        created = jira.call("POST", "/rest/api/3/issue", {"fields": fields})
        if created["key"] != f"{PROJECT}-{key}":
            fail(f"Expected {PROJECT}-{key}, Jira gave {created['key']}. Stopped so the keys stay as the specs say.")
        print(f"{created['key']:<8} {kind:<6} {summary}")

    for key, (kind, summary, status, epic, label) in items.items():
        if epic:
            jira.call("PUT", f"/rest/api/3/issue/{PROJECT}-{key}", {"fields": {"parent": {"key": f"{PROJECT}-{epic}"}}})
        if status != TODO:
            transitions = jira.call("GET", f"/rest/api/3/issue/{PROJECT}-{key}/transitions")["transitions"]
            target = next((t for t in transitions if t["to"]["name"].lower() == status.lower()), None)
            if target:
                jira.call("POST", f"/rest/api/3/issue/{PROJECT}-{key}/transitions", {"transition": {"id": target["id"]}})
            else:
                print(f"{PROJECT}-{key}: no transition to {status}")
    print(f"Seeded {PROJECT}-1 to {PROJECT}-{max(items)}.")


if __name__ == "__main__":
    sys.exit(main())
