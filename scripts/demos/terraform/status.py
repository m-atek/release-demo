"""What demo 2 has in AWS and on GitHub right now, and what it costs.

    python scripts/demos/terraform/status.py

Reads only: the stack's resources by their Demo tag and names, the two
roles, and the demo's open pull request. Nothing in the stack runs by the
hour: no instances and no NAT gateway; the bucket and queue cost cents.
"""

import sys

import stack
from common import aws_session


def main():
    from botocore.exceptions import BotoCoreError, ClientError

    session = aws_session()
    ec2, iam = session.client("ec2"), session.client("iam")
    s3, sqs = session.client("s3"), session.client("sqs")
    demo = [{"Name": "tag:Demo", "Values": ["2-terraform"]}]

    for kind in ("plan", "apply"):
        name = stack.settings()[f"{kind}_role"]
        try:
            iam.get_role(RoleName=name)
            print(f"role {name}: there")
        except ClientError:
            print(f"role {name}: missing (scripts/demo.sh up terraform)")

    # infra/demos/2-terraform/drift_trigger.tf and trail.tf
    try:
        session.client("lambda").get_function(FunctionName="release-demo-drift-trigger")
        logging = session.client("cloudtrail").get_trail_status(Name="release-demo-management-events")["IsLogging"]
        print(f"drift trigger: there, CloudTrail {'logging' if logging else 'NOT logging, so no run starts by itself'}")
    except ClientError:
        print("drift trigger: missing (scripts/demo.sh up terraform): network-drift runs at 07:00 and by hand only")

    vpcs = ec2.describe_vpcs(Filters=demo)["Vpcs"]
    subnets = ec2.describe_subnets(Filters=demo)["Subnets"]
    groups = ec2.describe_security_groups(Filters=demo + [{"Name": "group-name", "Values": ["network-stack-app"]}])
    print(f"network: {len(vpcs)} VPC, {len(subnets)} subnets" if vpcs else "network: not there (scripts/demo.sh up terraform)")
    for group in groups["SecurityGroups"]:
        rules = [f"{r.get('IpProtocol')} {r.get('FromPort', '')} from "
                 f"{', '.join(x['CidrIp'] for x in r.get('IpRanges', [])) or 'groups'}" for r in group["IpPermissions"]]
        extra = "" if len(rules) <= 1 else "  <- more than the baseline: a rule added by hand? reset.py removes it"
        print(f"security group {group['GroupName']}: {len(rules)} inbound rule(s): {'; '.join(rules)}{extra}")

    buckets = [b["Name"] for b in s3.list_buckets()["Buckets"] if b["Name"].startswith("release-demo-store-exports-")]
    queues = sqs.list_queues(QueueNamePrefix="release-demo-store-exports").get("QueueUrls", [])
    if buckets or queues:
        print(f"exports: {len(buckets)} bucket(s), {len(queues)} queue(s), applied from ORD-22 (cents a month)")
    else:
        print("exports: none, the baseline")

    try:
        import github

        gh = github.GitHub()
        open_pulls = [p for p in gh.open_pulls() if "ORD-22" in p["title"]]
        for pull in open_pulls:
            print(f"pull request #{pull['number']}: {pull['title']} ({pull['html_url']})")
        if not open_pulls:
            print("pull request: none open")
    except (SystemExit, BotoCoreError, ClientError, OSError):
        print("pull request: GitHub not checked")
    print("cost while idle: USD 0 an hour")


if __name__ == "__main__":
    sys.exit(main())
