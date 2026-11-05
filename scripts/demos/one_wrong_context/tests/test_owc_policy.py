"""The file on screen: what it says, that it fits, and what Kubernetes' source says it does.

Two small models here restate v1.36.5's source, so a change to the file can't quietly undo what
the source reading found. They aren't the API server: first_day.py asks the real one.
"""

import re
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parents[1]
FILE = HERE / "no-zero.yaml"
RULE = "UPDATE"
# The rows of Menlo 32 in the Terminal at full height, and its columns: tools/take-25.py's bounds.
ROWS, COLUMNS, FOR_THE_PROMPT = 27, 56, 2


def documents():
    return list(yaml.safe_load_all(FILE.read_text()))


def policy():
    return documents()[0]


def binding():
    return documents()[1]


def rule():
    rules = policy()["spec"]["matchConstraints"]["resourceRules"]
    assert len(rules) == 1
    return rules[0]


def rule_matches(resources, resource, subresource):
    """rules.go, Matcher.resource(), at v1.36.5: a name without a slash means no subresource."""
    for item in resources:
        name, _, sub = item.partition("/")
        if (name == "*" or name == resource) and (sub == "*" or sub == subresource):
            return True
    return False


def scale(replicas):
    """What the policy sees for kubectl scale: an autoscaling/v1 Scale, through the unstructured converter.
    ScaleSpec.Replicas is `json:"replicas,omitempty"`, an int32: at zero the field isn't there. spec itself stays,
    because the converter's isEmpty says no to a struct."""
    return {"apiVersion": "autoscaling/v1", "kind": "Scale", "metadata": {"name": "catalog"},
            "spec": {"replicas": replicas} if replicas else {}}


def deployment(replicas):
    """The main resource: DeploymentSpec.Replicas is a pointer, so a zero is there."""
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "catalog"}, "spec": {"replicas": replicas}}


class NoSuchKey(Exception):
    pass


def evaluate(expression, thing):
    """The two forms this demo cares about, as CEL reads them on a map. Anything else fails the test."""
    if expression == "object.spec.?replicas.orValue(0) > 0":
        return thing["spec"].get("replicas", 0) > 0
    if expression == "object.spec.replicas > 0":
        if "replicas" not in thing["spec"]:
            raise NoSuchKey("no such key: replicas")
        return thing["spec"]["replicas"] > 0
    raise AssertionError(f"not a form this model knows: {expression}")


def test_one_file_holds_a_policy_and_its_binding():
    assert [document["kind"] for document in documents()] == ["ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"]
    assert policy()["apiVersion"] == binding()["apiVersion"] == "admissionregistration.k8s.io/v1"
    assert policy()["metadata"]["name"] == binding()["spec"]["policyName"] == binding()["metadata"]["name"] == "no-zero"


def test_it_fits_one_screen_of_the_stage():
    lines = FILE.read_text().splitlines()
    assert len(lines) + FOR_THE_PROMPT <= ROWS
    assert all(len(line) <= COLUMNS for line in lines)


def test_the_rule_lists_the_scale_subresource_because_deployments_alone_doesnt_match_it():
    resources = rule()["resources"]
    assert rule_matches(resources, "deployments", "scale")
    assert rule_matches(resources, "deployments", "")
    assert not rule_matches(["deployments"], "deployments", "scale")  # Kubernetes' tutorial's rule, and the research's worry


def test_kubectl_scale_arrives_as_an_update_on_the_apps_group():
    # The verb is patch, admission's operation is UPDATE, and the rule's group is the resource's, not autoscaling.
    assert rule()["operations"] == [RULE]
    assert rule()["apiGroups"] == ["apps"]
    assert rule()["apiVersions"] == ["v1"]


def test_the_expression_says_what_a_missing_field_means():
    expression = policy()["spec"]["validations"][0]["expression"]
    assert evaluate(expression, scale(0)) is False
    assert evaluate(expression, scale(3)) is True
    assert evaluate(expression, scale(1)) is True
    assert evaluate(expression, deployment(0)) is False
    assert evaluate(expression, deployment(3)) is True


def test_the_plain_form_would_stop_with_an_error_not_a_refusal():
    try:
        evaluate("object.spec.replicas > 0", scale(0))
    except NoSuchKey as error:
        assert "no such key" in str(error)
    else:
        raise AssertionError("the plain form should stop on a Scale at zero")


def test_a_failure_to_evaluate_refuses_too():
    assert policy()["spec"].get("failurePolicy", "Fail") == "Fail"  # Ignore would let a broken expression through


def test_its_own_words_and_status():
    validation = policy()["spec"]["validations"][0]
    assert validation["message"] == "Production doesn't go to zero."
    assert validation["reason"] == "Forbidden"  # the default, Invalid, reads: The deployments "catalog" is invalid: :


def test_it_binds_to_production_alone_and_only_refuses():
    spec = binding()["spec"]
    assert spec["validationActions"] == ["Deny"]
    assert spec["matchResources"] == {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "production"}}}


def test_no_object_selector_a_scale_has_no_labels():
    assert "objectSelector" not in policy()["spec"]["matchConstraints"]
    assert "objectSelector" not in binding()["spec"]["matchResources"]


def test_no_field_outside_what_the_api_reference_lists():
    allowed = {"matchConstraints", "validations"}
    assert set(policy()["spec"]) <= allowed
    assert set(rule()) <= {"apiGroups", "apiVersions", "operations", "resources"}
    assert set(policy()["spec"]["validations"][0]) <= {"expression", "message", "reason"}
    assert set(binding()["spec"]) <= {"policyName", "validationActions", "matchResources"}


def test_nothing_on_screen_that_must_stay_off_it():
    text = FILE.read_text()
    assert not re.search(r"Demo system|DORA|client", text)
    assert chr(0x2014) not in text and chr(0x2013) not in text


def test_the_voice_has_it_right_nothing_to_install():
    # "No webhook": no webhook configuration, no pod and no image in the file.
    kinds = {document["kind"] for document in documents()}
    assert not kinds & {"ValidatingWebhookConfiguration", "Deployment", "Pod", "Service"}
