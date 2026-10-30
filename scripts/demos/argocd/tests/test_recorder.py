import datetime

import recorder
from conftest import application, audit

ARGOCD = recorder.ARGOCD_CONTROLLER
PIETER = {"user": "pieter-oncall", "groups": ["release-demo:break-glass", "system:authenticated"]}
SYNCED = [("abc1234def", "2026-10-30T10:10:00Z"), ("abc1234def", "2026-10-30T10:15:14Z")]
EVENTS = [{"message": "Updated sync status: Synced -> OutOfSync", "lastTimestamp": "2026-10-30T10:15:08Z"},
          {"message": "Updated health status: Healthy -> Progressing", "lastTimestamp": "2026-10-30T10:15:08Z"}]


def config():
    from common import service

    return service("catalog")


def scale(at="2026-10-30T10:15:07.123456Z", replicas=5, **extra):
    return audit(PIETER["user"], at, subresource="scale", body={"spec": {"replicas": replicas}},
                 groups=PIETER["groups"], uri="/apis/apps/v1/namespaces/production/deployments/catalog/scale"
                                              "?fieldManager=kubectl-scale", **extra)


def put_back(at="2026-10-30T10:15:14.654321Z"):
    return audit(ARGOCD, at, body={"spec": {"replicas": 3}}, agent="argocd-application-controller/v3.5.5")


def certificate_reader(csr):
    return ("pieter-oncall", datetime.datetime(2026, 10, 30, 11, 5, 0, tzinfo=datetime.timezone.utc))


CSR = {"metadata": {"name": "break-glass-pieter-oncall-1", "creationTimestamp": "2026-10-30T10:05:00Z"},
       "status": {"conditions": [{"type": "Approved", "lastUpdateTime": "2026-10-30T10:05:00Z"}],
                  "certificate": "x"}}


def build(audit_events, deployment, app=None, events=EVENTS, csrs=(CSR,)):
    original = recorder.certificate_of
    recorder.certificate_of = certificate_reader
    try:
        return recorder.build_event(app or application(SYNCED), audit_events, deployment, list(csrs), events, config())
    finally:
        recorder.certificate_of = original


def test_a_scale_put_back_by_argocd_is_one_drift_event(deployment):
    event, why = build([scale(), put_back()], deployment)
    assert why == "" and event["kind"] == "drift"
    assert event["edits"][0]["user"] == "pieter-oncall"
    assert event["edits"][0]["field_manager"] == "kubectl-scale"
    assert event["edits"][0]["request"] == {"spec": {"replicas": 5}}
    assert event["diff"] == [{"path": "spec.replicas", "live": 5, "git": 3}]
    assert event["edits"][0]["at"] == "2026-10-30T10:15:07.123456Z"
    assert event["reverted_at"] == "2026-10-30T10:15:14.654321Z"
    # 7.53 seconds, rounded down, as the audit log's two times give it.
    assert event["seconds"] == 7
    assert event["out_of_sync_at"] == "2026-10-30T10:15:08.000000Z"
    assert event["break_glass"] is True
    assert event["certificate"] == {"user": "pieter-oncall", "issued": "2026-10-30T10:05:00.000000Z",
                                    "expires": "2026-10-30T11:05:00.000000Z"}
    assert event["path"] == "deploy/production/catalog" and event["cmdb_item"] == "catalog-production"
    assert event["id"] == "catalog-production:2026-10-30T10:15:14Z"


def test_a_sync_after_an_unrelated_commit_still_counts(deployment):
    """main moved without a sync, so the self-heal is at a new revision: the audit log decides."""
    moved = application([("abc1234def", "2026-10-30T10:10:00Z"), ("fedcba98765", "2026-10-30T10:15:14Z")])
    event, why = build([scale(), put_back()], deployment, app=moved)
    assert event and event["revision"] == "fedcba98765"


def test_a_git_driven_sync_sends_nothing(deployment):
    merged = application([("abc1234def", "2026-10-30T10:10:00Z"), ("0123456789a", "2026-10-30T10:15:14Z")])
    event, why = build([put_back()], deployment, app=merged)
    assert event is None and "Git's change" in why


def test_kubernetes_own_writes_are_not_edits(deployment):
    controller = audit("system:serviceaccount:kube-system:deployment-controller", "2026-10-30T10:15:09Z",
                       verb="update", body={"metadata": {"annotations": {"deployment.kubernetes.io/revision": "4"}}})
    event, why = build([controller, put_back()], deployment)
    assert event is None


def test_edits_before_the_sync_before_dont_count(deployment):
    event, why = build([scale(at="2026-10-30T10:09:00Z"), put_back()], deployment)
    assert event is None


def test_a_manual_or_failed_sync_is_not_recorded(deployment):
    assert build([scale(), put_back()], deployment, app=application(SYNCED, automated=False))[0] is None
    assert build([scale(), put_back()], deployment, app=application(SYNCED, phase="Failed"))[0] is None


def test_an_edit_git_already_says_sends_nothing(deployment):
    event, why = build([scale(replicas=3), put_back()], deployment)
    assert event is None and "nothing was put back" in why


def test_two_edits_give_the_last_value_and_the_first_time(deployment):
    event, _ = build([scale(), scale(at="2026-10-30T10:15:09Z", replicas=6), put_back()], deployment)
    assert event["diff"] == [{"path": "spec.replicas", "live": 6, "git": 3}]
    assert len(event["edits"]) == 2 and event["seconds"] == 7


def test_refused_writes_and_other_deployments_are_ignored(deployment):
    refused = scale()
    refused["responseStatus"] = {"code": 403}
    other = audit("pieter-oncall", "2026-10-30T10:15:07Z", body={"spec": {"replicas": 9}}, name="orders")
    assert build([refused, other, put_back()], deployment)[0] is None


def test_field_manager_falls_back_to_the_user_agent():
    event = audit("someone", "2026-10-30T10:15:07Z", subresource="scale")
    assert recorder.field_manager(event) == "kubectl-scale"
    assert recorder.field_manager(audit("someone", "2026-10-30T10:15:07Z")) == "kubectl-patch"
    assert recorder.field_manager(audit("x", "2026-10-30T10:15:07Z", agent="curl/8.7 x")) == "curl/8.7"


def test_times_keep_their_microseconds():
    moment = recorder.parse_time("2026-10-30T10:15:07.123456789Z")
    assert moment.microsecond == 123456 and recorder.stamp(moment) == "2026-10-30T10:15:07.123456Z"


def test_the_certificate_must_have_been_valid_at_the_edit():
    late = datetime.datetime(2026, 10, 30, 12, 0, tzinfo=datetime.timezone.utc)
    assert recorder.certificate([CSR], "pieter-oncall", late, read=certificate_reader) is None
    assert recorder.certificate([CSR], "someone-else", late, read=certificate_reader) is None


def test_metadata_and_status_are_not_part_of_the_diff():
    event = audit("x", "2026-10-30T10:15:07Z", body={"metadata": {"labels": {"a": "b"}}, "spec": {"paused": True}})
    assert recorder.edited_fields(event) == [("spec.paused", True)]


def test_the_listener_only_takes_pods_and_the_node():
    import ipaddress

    allowed = [ipaddress.ip_address(a) for a in ("10.42.0.17", "127.0.0.1")]
    refused = ipaddress.ip_address("10.40.0.38")
    assert all(any(a in n for n in recorder.CALLERS) for a in allowed)
    assert not any(refused in n for n in recorder.CALLERS)
