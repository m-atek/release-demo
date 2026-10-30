"""What video 18's scripts share: the service's cost_check settings, Lambda's
REPORT lines and the function's own log lines, and the arithmetic that turns
measured page views into a month at the expected volume. No model anywhere:
every number comes from the run's requests and log lines, prices.yaml and
services.yaml.

The scripts here run from the repository root like the shared ones, which
they import from scripts/.
"""

import json
import re
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(1, str(SCRIPTS))  # after this folder

from common import service  # noqa: E402
from llm import cost as claude_cost, prices  # noqa: E402

SERVICE = "orders-service"
SWITCH = "cost-check/enabled"
REPORT = re.compile(r"REPORT RequestId: (?P<id>[0-9a-f-]+).*?Billed Duration: (?P<billed>\d+) ms.*?"
                    r"Memory Size: (?P<memory>\d+) MB")


def settings(service_name):
    """The service's cost_check in services.yaml."""
    return service(service_name).get("cost_check") or {}


def report_of(message):
    """{request_id, billed_ms, memory_mb} from a REPORT line, in Lambda's text or JSON log format."""
    match = REPORT.search(message)
    if match:
        return {"request_id": match["id"], "billed_ms": int(match["billed"]), "memory_mb": int(match["memory"])}
    line = function_line(message, events=("platform.report",), key="type")
    if line:
        metrics = line.get("record", {}).get("metrics", {})
        return {"request_id": line["record"].get("requestId"), "billed_ms": int(metrics.get("billedDurationMs", 0)),
                "memory_mb": int(metrics.get("memorySizeMB", 0))}
    return None


def function_line(message, events=("claude", "store"), key="event"):
    """One of the function's own JSON lines, or None."""
    text = message.strip()
    if not text.startswith("{"):
        return None
    try:
        line = json.loads(text)
    except ValueError:
        return None
    return line if isinstance(line, dict) and line.get(key) in events else None


def whole(amount):
    """Rounded half up, as people round: 6,824.5 ms is 6,825."""
    return int(Decimal(str(amount)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def dollars(amount):
    """Whole dollars, half up: what Slack, the change and Grafana show."""
    return whole(amount)


def signed(amount):
    """+USD 41,234 or -USD 12: whole dollars with thousands separators."""
    return f"{'-' if amount < 0 else '+'}USD {abs(amount):,}"


def price_text(value):
    """4 for 4.00, 0.20 for 0.2, 0.0000166667 as it is."""
    value = Decimal(str(value))
    if value == value.to_integral_value():
        return f"{int(value):,}"
    return f"{value:.2f}" if value == value.quantize(Decimal("0.01")) else format(value.normalize(), "f")


def claude_usd(line):
    """A call's cost by the model that answered; every attempt when a fallback ran."""
    attempts = line.get("iterations") or [line]
    return sum(claude_cost(attempt.get("model") or line["model"], attempt) for attempt in attempts)


def lambda_usd(report, table=None):
    table = table or prices()["aws"]["lambda"]
    gb_seconds = report["billed_ms"] / 1000 * report["memory_mb"] / 1024
    return gb_seconds * table["per_gb_second"] + table["per_million_requests"] / 1_000_000


def dynamodb_usd(read_units, write_units, table=None):
    table = table or prices()["aws"]["dynamodb"]
    return (read_units * table["per_million_read_units"] + write_units * table["per_million_write_units"]) / 1_000_000


def measure(rows, per_order_calls, per_order_stores=None):
    """Per page view and per new order, from the sample's rows ({request_id,
    billed_ms, memory_mb, claude: [lines], store: [lines]}) and the per-order
    calls to price (the sample's, or the last 7 days' when the sample made
    none). Returns (per_page_view, per_new_order, driver)."""
    calls = [line for row in rows for line in row["claude"]]
    ordered = [line for line in per_order_calls]
    seen, repeated = set(), set()
    for line in (line for line in calls if line.get("per") == "order"):
        for order in line.get("orders", []):
            (repeated if order in seen else seen).add(order)
    driver = "order" if ordered else "page_view" if calls else "none"
    note = None
    if repeated:
        driver, note = "page_view", f"{len(repeated)} order(s) got two per-order calls: the store isn't working"
    page_view_calls = calls if driver == "page_view" else [line for line in calls if line.get("per") != "order"]
    count = len(rows)
    read_units = sum(line.get("read_units", 0) for row in rows for line in row["store"])
    per_page_view = {
        "claude_calls": round(len(page_view_calls) / count, 2),
        "input_tokens": whole(sum(line["input_tokens"] for line in page_view_calls) / count),
        "output_tokens": whole(sum(line["output_tokens"] for line in page_view_calls) / count),
        "models": sorted({line["model"] for line in page_view_calls}),
        "billed_ms": whole(sum(row["billed_ms"] for row in rows) / count),
        "memory_mb": max((row["memory_mb"] for row in rows), default=0),
        "read_units": round(read_units / count, 3),
        "claude_usd": sum(claude_usd(line) for line in page_view_calls) / count,
        "lambda_usd": sum(lambda_usd(row) for row in rows) / count,
        "dynamodb_usd": dynamodb_usd(read_units, 0) / count,
    }
    per_page_view["total_usd"] = per_page_view["claude_usd"] + per_page_view["lambda_usd"] + per_page_view["dynamodb_usd"]
    per_new_order = {"claude_calls": 0, "orders": 0, "input_tokens": 0, "output_tokens": 0, "models": [],
                     "write_units": 0.0, "claude_usd": 0.0, "dynamodb_usd": 0.0, "total_usd": 0.0}
    if driver == "order":
        orders = sum(len(line.get("orders", [])) for line in ordered)
        writes = sum(line.get("write_units", 0) for line in (per_order_stores or []))
        per_new_order.update({
            "claude_calls": len(ordered), "orders": orders,
            "input_tokens": whole(sum(line["input_tokens"] for line in ordered) / orders),
            "output_tokens": whole(sum(line["output_tokens"] for line in ordered) / orders),
            "models": sorted({line["model"] for line in ordered}), "write_units": round(writes / orders, 3),
            "claude_usd": sum(claude_usd(line) for line in ordered) / orders,
            "dynamodb_usd": dynamodb_usd(0, writes) / orders,
        })
        per_new_order["total_usd"] = per_new_order["claude_usd"] + per_new_order["dynamodb_usd"]
    return per_page_view, per_new_order, {"declared": "order" if ordered or any(row["store"] for row in rows)
                                          else "page_view" if calls else "none", "used": driver, "note": note}


def month(per_page_view, per_new_order, volumes):
    return per_page_view["total_usd"] * volumes["page_views"] + per_new_order["total_usd"] * volumes["new_orders"]
