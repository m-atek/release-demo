"""Video 18's cost check: what the new version costs to run, measured in
staging before production. The release's stage "Cost check" runs it right
after Check staging, while scripts/demos/cost_gate/switch.py is on.

    python3 scripts/demos/cost_gate/cost_check.py orders-service                   the check
    python3 scripts/demos/cost_gate/cost_check.py orders-service --add-stage        the stage, in this run's Slack list
    python3 scripts/demos/cost_gate/cost_check.py orders-service --no-measurement "<reason>"

1. Sample: GET staging's function URL path 50 times, 5 at a time, keeping
   each response's x-amzn-RequestId and status. Failed requests count and
   are listed; more than one in ten failed is no measurement.
2. Logs: through the evidence reader role, FilterLogEvents on staging's log
   group until every REPORT line of those requests is in (up to 90
   seconds): Billed Duration and Memory Size as Lambda prints them, and the
   function's own "claude" and "store" lines.
3. Per-order calls: if the sample made none (the messages were stored
   already), this version's "claude" lines with "per": "order" from the
   last 7 days of staging's log group. A version that stores messages
   without one anywhere is no measurement.
4. The declared driver, checked: an order with two "order" calls in the
   sample means the store isn't working, and all of the version's calls
   count as per page view.
5. Price and project, with prices.yaml (dated): Claude by the model that
   answered, Lambda per GB-second and per request, DynamoDB per unit. The
   month is the cost per page view times the expected page views plus the
   cost per new order times the expected new orders (services.yaml).
6. Compare: production's version's cost.json from its JFrog release copy,
   at the same volumes. Without one, against zero, and the line says so.
7. Record: .release/reports/cost.json, uploaded next to the candidate
   package (publish.py --release copies it with the package); the run
   state's cost_check, and cost_review above the limit or without a
   measurement, which makes change.py open a normal change for Cost
   approvers; the change's five lines; the Slack line; one event.

It uses no model, and exits 0 in every outcome: it never stops a release
and never approves one. Above the limit, or without a measurement, a cost
approver decides.
"""

import argparse
import concurrent.futures
import datetime
import json
import sys
import time
import urllib.error
import urllib.request

import costs
import run_state
from common import write_json
from llm import prices

LOG_WAIT_SECONDS = 90
LOOKBACK_DAYS = 7
FAILED_SHARE = 0.1
STAGE, AFTER = "Cost check", "Check staging"


class NoMeasurement(Exception):
    """Why there is no measurement, in words the Slack line can carry."""


# 1. The sample.

def get(url):
    """(request id, status) of one page view through the function URL."""
    request = urllib.request.Request(url, headers={"User-Agent": "release-demo cost check"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
            return response.headers.get("x-amzn-RequestId"), response.status
    except urllib.error.HTTPError as error:
        return error.headers.get("x-amzn-RequestId"), error.code
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return None, 0


def sample(url, path, count, concurrency, fetch=get):
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(lambda _: fetch(url.rstrip("/") + path), range(count)))
    return [{"request_id": request_id, "status": status} for request_id, status in results]


# 2 and 3. The logs.

def events(client, group, start, end, pattern=None):
    kwargs = {"logGroupName": group, "startTime": int(start * 1000), "endTime": int(end * 1000)}
    if pattern:
        kwargs["filterPattern"] = pattern
    for page in client.get_paginator("filter_log_events").paginate(**kwargs):
        yield from page["events"]


def read_logs(client, group, ids, start, wait=LOG_WAIT_SECONDS, sleep=time.sleep, clock=time.time):
    """{request id: {billed_ms, memory_mb, claude: [...], store: [...]}} for every id, waiting for its REPORT line."""
    deadline = clock() + wait
    while True:
        rows = {request_id: {"claude": [], "store": []} for request_id in ids}
        for event in events(client, group, start - 5, clock() + 5):
            report = costs.report_of(event["message"])
            if report and report["request_id"] in rows:
                rows[report["request_id"]].update(billed_ms=report["billed_ms"], memory_mb=report["memory_mb"])
                continue
            line = costs.function_line(event["message"])
            if line and line.get("request_id") in rows:
                rows[line["request_id"]][line["event"]].append(line)
        missing = [request_id for request_id, row in rows.items() if "billed_ms" not in row]
        if not missing:
            return rows
        if clock() >= deadline:
            raise NoMeasurement(f"the logs didn't arrive: {len(ids) - len(missing)} of {len(ids)} REPORT lines "
                                f"in {wait} seconds")
        sleep(5)


def earlier_order_calls(client, group, version, now=None):
    """This version's per-order calls, and its page views' store lines, from the last 7 days."""
    now = now or time.time()
    start = now - LOOKBACK_DAYS * 86400
    calls = [costs.function_line(e["message"]) for e in events(
        client, group, start, now, f'{{ $.event = "claude" && $.per = "order" && $.version = "{version}" }}')]
    ids = {line["request_id"] for line in calls if line}
    stores = [costs.function_line(e["message"]) for e in events(
        client, group, start, now, f'{{ $.event = "store" && $.version = "{version}" }}')]
    return [line for line in calls if line], [line for line in stores if line and line.get("request_id") in ids]


# 6. Production's measurement.

def production_cost(reader, service_name, function, jcr_token=None):
    """(production's version, its cost.json or None), from the release copy whose package production runs."""
    import publish
    from common import get_parameter, ssm_client
    from evidence import base64_to_hex

    client = reader.client("lambda")
    number = client.get_alias(FunctionName=function, Name="live")["FunctionVersion"]
    configuration = client.get_function_configuration(FunctionName=function, Qualifier=number)
    sha256 = base64_to_hex(configuration["CodeSha256"])
    version = configuration.get("Description") or "?"
    token = jcr_token or get_parameter(ssm_client(), "jcr/token")
    root = f"{publish.releases_root(service_name)}/{version}"
    status, body = publish.request("GET", f"api/storage/{root}", token)
    if status != 200:
        return version, None
    folders = [root] + [f"{root}/{child['uri'].strip('/')}" for child in json.loads(body).get("children", [])
                        if child.get("folder")]
    for folder in folders:
        status, info = publish.request("GET", f"{folder}/build-info.json", token)
        if status == 200 and json.loads(info).get("sha256") == sha256:
            status, cost = publish.request("GET", f"{folder}/cost.json", token)
            return version, json.loads(cost) if status == 200 else None
    return version, None


# 7. The words.

def calls_text(count):
    shown = f"{count:g}" if count == int(count) else f"{count:.1f}"
    return f"{shown} Claude call{'' if count == 1 else 's'}"


def measured_line(record):
    page, order, size = record["per_page_view"], record["per_new_order"], record["sample"]["page_views"]
    parts = [calls_text(page["claude_calls"])]
    if page["claude_calls"]:
        parts[0] += (f", {page['input_tokens']:,} tokens in and {page['output_tokens']:,} out on "
                     f"{' and '.join(page['models'])}")
    if page["read_units"]:
        parts.append(f"DynamoDB {page['read_units']:g} read units")
    parts.append(f"Lambda {page['billed_ms']:,} ms billed at {page['memory_mb']} MB")
    line = f"Measured per page view in staging ({size} page views): {'; '.join(parts)}."
    if order["orders"]:
        line += (f" Per new order: {order['claude_calls']} Claude call{'' if order['claude_calls'] == 1 else 's'} for "
                 f"{order['orders']} orders, {order['input_tokens']:,} tokens in and {order['output_tokens']:,} out "
                 f"per order on {' and '.join(order['models'])}; DynamoDB {order['write_units']:g} write units"
                 f"{' (from the last 7 days)' if record['driver'].get('from_lookback') else ''}.")
    return line


def prices_line(record):
    table = record["prices"]
    models = sorted(set(record["per_page_view"]["models"]) | set(record["per_new_order"]["models"]))
    parts = [f"{model} USD {costs.price_text(table['claude'][model]['input'])} in and "
             f"USD {costs.price_text(table['claude'][model]['output'])} out per million tokens" for model in models]
    parts.append(f"Lambda USD {costs.price_text(table['lambda']['per_gb_second'])} per GB-second")
    if record["per_page_view"]["read_units"] or record["per_new_order"]["write_units"]:
        parts.append(f"DynamoDB USD {costs.price_text(table['dynamodb']['per_million_read_units'])} and "
                     f"USD {costs.price_text(table['dynamodb']['per_million_write_units'])} per million read and "
                     "write units")
    read = datetime.date.fromisoformat(str(table["read"]))
    return f"Prices: {'; '.join(parts)} (prices.yaml, read {read.day} {read:%B %Y})."


def who(state):
    return "Cost approvers" if change_is_standard(state) else "Cost and change approvers"


def change_is_standard(state):
    """Whether the run asked for a standard change, as change.py open decides it."""
    from common import service

    config = service(state["service"])
    return (state.get("option") in config["servicenow"].get("standard_options", ["standard"])
            or config["servicenow"].get("change") == "standard")


def change_lines(record, state):
    limit, volumes = record["limit_usd"], record["volumes"]
    size = record["sample"]["page_views"]
    if record["result"] == "no-measurement":
        first = (f"Cost check (staging, {size} page views): no measurement ({record['reason']}). "
                 f"{who(state)} decide.")
        return [first, f"Expected volume (services.yaml): {volumes['page_views']:,} page views and "
                       f"{volumes['new_orders']:,} new orders a month."]
    decision = f"{who(state)} decide." if record["result"] == "above" else "Within it, so the change is the one asked for."
    production = record["production"]
    lines = [
        f"Cost check (staging, {size} page views): {costs.signed(record['delta_usd'])} a month at the expected volume. "
        f"Limit: USD {limit:,} a month. {decision}",
        measured_line(record),
        prices_line(record),
        f"Expected volume (services.yaml): {volumes['page_views']:,} page views and {volumes['new_orders']:,} new "
        "orders a month.",
        (f"Production {production['version']}: USD {production['monthly_usd']:,} a month at the same volume."
         if production.get("measured") else
         f"Production {production['version']}: no cost measurement, so the comparison is against zero."),
    ]
    return lines


def slack_line(record):
    if record["result"] == "no-measurement":
        return f"Cost check: no measurement ({record['reason']}). A cost approver decides."
    line = f"Cost check: {costs.signed(record['delta_usd'])} a month at the expected volume. "
    line += (f"Above the USD {record['limit_usd']:,} limit, so a cost approver decides." if record["result"] == "above"
             else f"Within the USD {record['limit_usd']:,} limit.")
    if not record["production"].get("measured"):
        line = line[:-1] + " (production has no cost measurement)."
    return line


def event_body(record, state):
    page, order = record["per_page_view"], record["per_new_order"]
    return {"build": state["build"].get("number"), "build_url": state["build"].get("url"), "service": state["service"],
            "version": state["version"], "environment": "staging", "change": None, "jira_key": state.get("jira_key"),
            "stage": STAGE, "event": "cost_check", "result": record["result"], "reason": record.get("reason"),
            "sample_size": record["sample"]["page_views"],
            "per_page_view_usd": round(page.get("total_usd", 0), 8) if page else None,
            "per_page_view_claude_usd": round(page.get("claude_usd", 0), 8) if page else None,
            "per_page_view_lambda_usd": round(page.get("lambda_usd", 0), 8) if page else None,
            "per_page_view_dynamodb_usd": round(page.get("dynamodb_usd", 0), 8) if page else None,
            "per_new_order_usd": round(order.get("total_usd", 0), 8) if order else None,
            "monthly_usd": record.get("monthly_usd"), "delta_usd": record.get("delta_usd"),
            "limit_usd": record["limit_usd"]}


# The check.

def check(state, settings, reader, fetch=get, logs_wait=LOG_WAIT_SECONDS, jcr_token=None, clock=time.time,
          sleep=time.sleep):
    """cost.json's record for this run: measured, priced and compared, or why not."""
    config = costs.service(state["service"])
    staging = config["environments"]["staging"]
    plan, volumes = settings["sample"], settings["expected_month"]
    table = prices()
    record = {"service": state["service"], "version": state["version"], "build": state["build"].get("number"),
              "commit": state.get("commit"), "measured_at": run_state.iso(clock()), "environment": "staging",
              "sample": {"path": plan["path"], "page_views": plan["page_views"], "concurrency": plan["concurrency"]},
              "prices": {"file": "scripts/demos/ai_notes/prices.yaml", "read": str(table["read"]),
                         "claude": table["claude"]["per_million_tokens"], "lambda": table["aws"]["lambda"],
                         "dynamodb": table["aws"]["dynamodb"]},
              "volumes": {**volumes, "source": "services.yaml"}, "limit_usd": settings["limit_usd_month"],
              "approvers": settings.get("approvers", "Cost approvers")}
    try:
        url = (state.get("deploys") or {}).get("staging", {}).get("url")
        if not url:
            raise NoMeasurement("staging has no address in this run")
        if reader is None:
            raise NoMeasurement("the evidence reader role isn't available")
        started = clock()
        answers = sample(url, plan["path"], plan["page_views"], plan["concurrency"], fetch)
        record["sample"]["seconds"] = round(clock() - started, 1)
        failed = [answer for answer in answers if answer["status"] != 200 or not answer["request_id"]]
        record["sample"]["failed"] = failed
        if len(failed) > FAILED_SHARE * len(answers):
            raise NoMeasurement(f"staging answered {len(failed)} of {len(answers)} page views with an error")
        ok = [answer["request_id"] for answer in answers if answer not in failed]
        logs = reader.client("logs")
        group = f"/aws/lambda/{staging['function']}"
        rows = read_logs(logs, group, ok, started, wait=logs_wait, sleep=sleep, clock=clock)
        record["requests"] = [{"request_id": request_id, "status": 200, **rows[request_id]} for request_id in ok]
        sampled = [line for row in rows.values() for line in row["claude"] if line.get("per") == "order"]
        stores = [line for row in rows.values() for line in row["store"] if line.get("write_units")]
        from_lookback = False
        if not sampled and any(row["store"] for row in rows.values()):
            sampled, stores = earlier_order_calls(logs, group, state["version"], now=clock())
            from_lookback = bool(sampled)
            if not sampled:
                raise NoMeasurement("no per-order call to price, in the sample or the last 7 days")
        page, order, driver = costs.measure(list(rows.values()), sampled, stores)
        driver["from_lookback"] = from_lookback
        record.update(per_page_view=page, per_new_order=order, driver=driver, per_order_calls=sampled)
        monthly = costs.month(page, order, volumes)
        record["monthly_usd_exact"], record["monthly_usd"] = round(monthly, 4), costs.dollars(monthly)
        version, measured = production_cost(reader, state["service"], config["environments"]["production"]["function"],
                                            jcr_token)
        production = {"version": version, "measured": bool(measured and measured.get("per_page_view"))}
        if production["measured"]:
            production["monthly_usd"] = costs.dollars(costs.month(measured["per_page_view"], measured["per_new_order"],
                                                                  volumes))
            production["cost_json_build"] = measured.get("build")
        record["production"] = production
        record["delta_usd"] = record["monthly_usd"] - production.get("monthly_usd", 0)
        record["result"] = "above" if record["delta_usd"] > record["limit_usd"] else "within"
    except NoMeasurement as error:
        record.update(result="no-measurement", reason=str(error))
    except Exception as error:  # the check never stops a release: a person decides instead
        record.update(result="no-measurement", reason=f"the check failed ({error.__class__.__name__})")
    return record


def record_run(state, record):
    """cost.json, JFrog, the run state, the change's lines, Slack and the event. Each part only prints on an error."""
    from notify import post

    path = run_state.REPORTS / "cost.json"
    write_json(path, record)
    state["cost_check"] = {key: record.get(key) for key in ("result", "reason", "monthly_usd", "delta_usd", "limit_usd")}
    lines = change_lines(record, state)
    if record["result"] == "within":
        state["change_work_notes"] = lines
    else:
        state["cost_review"] = True
        state["change_details"] = lines
    state.setdefault("lines", []).append(slack_line(record))
    run_state.save(state)
    for name, write in (("Slack", lambda: post(state)), ("JFrog", lambda: upload(state, path)),
                        ("events", lambda: send_event(event_body(record, state)))):
        try:
            write()
        except BaseException as error:  # fail() exits: still, nothing here stops the release
            print(f"Cost check: {name} not written ({error.__class__.__name__}: {str(error)[:200]})")


def upload(state, path):
    import publish
    from common import get_parameter, ssm_client

    base = state["package"]["path"].rsplit("/", 1)[0]
    publish.upload(get_parameter(ssm_client(), "jcr/token"), f"{base}/cost.json", path.read_bytes())
    state["cost_check"]["jfrog_path"] = f"{base}/cost.json"
    run_state.save(state)


def send_event(body):
    """To Splunk as events.py sends, and to Grafana Logs as a line of its own kind, which the
    Releases dashboard's row "orders-service cost" reads (unwrap monthly_usd, by version)."""
    import base64

    from common import get_parameter, ssm_client
    from events import send, to_splunk

    ssm = ssm_client()
    url = get_parameter(ssm, "grafana/logs-url").rstrip("/") + "/loki/api/v1/push"
    auth = base64.b64encode(f"{get_parameter(ssm, 'grafana/logs-user')}:{get_parameter(ssm, 'grafana/logs-token')}"
                            .encode()).decode()
    labels = {"source": "release-demo", "service": body["service"], "kind": "cost_check", "environment": "staging"}
    loki = send(url, {"streams": [{"stream": labels, "values": [[str(time.time_ns()), json.dumps(body)]]}]},
                {"Authorization": f"Basic {auth}"})
    print(f"event cost_check {body['result']}: Splunk {to_splunk(ssm, body)}, Grafana {loki}")


def add_stage(state):
    """The stage between Check staging and Change, so Slack's list shows it from the first line."""
    if STAGE in state["stages"]:
        return
    stages = {}
    for name, stage in state["stages"].items():
        stages[name] = stage
        if name == AFTER:
            stages[STAGE] = {"status": "waiting"}
    state["stages"] = stages
    run_state.save(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("--add-stage", action="store_true", help="put the stage in this run's stage list")
    parser.add_argument("--no-measurement", metavar="REASON", help="record that there is no measurement, and why")
    args = parser.parse_args()

    state = run_state.load()
    if args.add_stage:
        add_stage(state)
        return 0
    settings = costs.settings(args.service)
    if args.no_measurement:
        record = {"service": args.service, "version": state.get("version"), "result": "no-measurement",
                  "reason": args.no_measurement, "limit_usd": settings.get("limit_usd_month"),
                  "sample": {"page_views": settings.get("sample", {}).get("page_views")},
                  "volumes": settings.get("expected_month"), "per_page_view": None, "per_new_order": None}
        record_run(state, record)
        print(slack_line(record))
        return 0
    try:
        from evidence import reader_session

        reader = reader_session()
    except BaseException as error:
        print(f"Cost check: the evidence reader role isn't available ({error.__class__.__name__})")
        reader = None
    record = check(state, settings, reader)
    record_run(state, record)
    print(slack_line(record))
    for line in change_lines(record, state):
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
