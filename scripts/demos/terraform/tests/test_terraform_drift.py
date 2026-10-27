import drift

HTTPS = {"protocol": "tcp", "from_port": 443, "to_port": 443, "cidr_blocks": ["10.42.0.0/16"],
         "description": "HTTPS from inside the VPC", "ipv6_cidr_blocks": [], "prefix_list_ids": [],
         "security_groups": [], "self": False}
SSH = {**HTTPS, "from_port": 22, "to_port": 22, "cidr_blocks": ["0.0.0.0/0"], "description": ""}


def test_an_inbound_rule_added_by_hand_reads_as_one_line():
    plan = {"resource_drift": [{"address": "aws_security_group.app", "change": {
        "actions": ["update"], "before": {"ingress": [HTTPS], "tags_all": {}}, "after": {"ingress": [HTTPS, SSH], "tags_all": {"x": 1}}}}]}
    assert drift.describe(plan) == ["aws_security_group.app: inbound rule added: tcp 22 from 0.0.0.0/0"]


def test_rules_tags_and_deletions():
    plan = {"resource_drift": [
        {"address": "aws_security_group.app", "change": {"actions": ["update"],
                                                         "before": {"egress": [HTTPS]}, "after": {"egress": []}}},
        {"address": "aws_vpc.main", "change": {"actions": ["update"], "before": {"tags": {"Name": "a"}},
                                               "after": {"tags": {"Name": "b"}}}},
        {"address": "aws_subnet.private[\"b\"]", "change": {"actions": ["delete"], "before": {}, "after": None}},
    ]}
    assert drift.describe(plan) == [
        "aws_security_group.app: outbound rule removed: tcp 443 to 10.42.0.0/16 (\"HTTPS from inside the VPC\")",
        "aws_vpc.main: tag Name changed from a to b",
        "aws_subnet.private[\"b\"]: deleted outside Terraform",
    ]


def test_all_traffic():
    rule = {"protocol": "-1", "from_port": 0, "to_port": 0, "cidr_blocks": [], "self": True}
    assert drift.rule_text(rule, "from") == "all traffic from itself"
