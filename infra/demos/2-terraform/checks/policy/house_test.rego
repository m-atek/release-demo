# Tests for the house rules: conftest verify --policy infra/demos/2-terraform/checks/policy
package main

tags := {"Project": "release-demo", "Demo": "2-terraform", "Owner": "platform-team"}

provider := {"aws": {"name": "aws", "expressions": {"region": {"constant_value": "eu-west-1"}}}}

created(type, name, after) := {
	"address": sprintf("%s.%s", [type, name]), "mode": "managed", "type": type, "name": name,
	"change": {"actions": ["create"], "after": after},
}

private_bucket := {
	"configuration": {"provider_config": provider, "root_module": {"resources": [{
		"type": "aws_s3_bucket_public_access_block",
		"expressions": {"bucket": {"references": ["aws_s3_bucket.exports.id", "aws_s3_bucket.exports"]}},
	}]}},
	"resource_changes": [
		created("aws_s3_bucket", "exports", {"tags_all": tags}),
		created("aws_s3_bucket_public_access_block", "exports", {
			"block_public_acls": true, "block_public_policy": true,
			"ignore_public_acls": true, "restrict_public_buckets": true,
		}),
	],
}

test_a_private_bucket_passes if {
	count(deny) == 0 with input as private_bucket
}

test_a_public_access_block_turned_off_fails if {
	plan := json.patch(private_bucket, [{"op": "replace", "path": "/resource_changes/1/change/after/block_public_policy", "value": false}])
	deny["S3 bucket must not be public: aws_s3_bucket_public_access_block.exports turns off block_public_policy"] with input as plan
}

test_a_bucket_without_an_access_block_fails if {
	plan := json.patch(private_bucket, [
		{"op": "replace", "path": "/configuration/root_module/resources", "value": []},
		{"op": "remove", "path": "/resource_changes/1"},
	])
	deny["S3 bucket must not be public: aws_s3_bucket.exports has no aws_s3_bucket_public_access_block"] with input as plan
}

test_a_policy_for_everyone_fails if {
	policy := created("aws_s3_bucket_policy", "exports", {"policy": json.marshal({"Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject"}]})})
	plan := json.patch(private_bucket, [{"op": "add", "path": "/resource_changes/-", "value": policy}])
	deny["S3 bucket must not be public: aws_s3_bucket_policy.exports allows everyone (Principal *)"] with input as plan
}

test_a_public_acl_fails if {
	acl := created("aws_s3_bucket_acl", "exports", {"acl": "public-read"})
	plan := json.patch(private_bucket, [{"op": "add", "path": "/resource_changes/-", "value": acl}])
	deny["S3 bucket must not be public: aws_s3_bucket_acl.exports grants public-read"] with input as plan
}

test_a_missing_tag_fails if {
	plan := json.patch(private_bucket, [{"op": "remove", "path": "/resource_changes/0/change/after/tags_all/Owner"}])
	deny["Required tags: aws_s3_bucket.exports has no Owner"] with input as plan
}

test_another_region_fails if {
	plan := json.patch(private_bucket, [{"op": "replace", "path": "/configuration/provider_config/aws/expressions/region/constant_value", "value": "us-east-1"}])
	deny["Allowed regions: provider aws uses us-east-1; allowed are eu-central-1, eu-west-1"] with input as plan
}

test_a_large_instance_fails if {
	instance := created("aws_instance", "app", {"instance_type": "m5.24xlarge", "tags_all": tags})
	plan := json.patch(private_bucket, [{"op": "add", "path": "/resource_changes/-", "value": instance}])
	deny["Allowed instance types: aws_instance.app uses m5.24xlarge; allowed are t3.large, t3.medium, t3.micro, t3.small"] with input as plan
}

test_a_destroyed_public_block_is_no_concern if {
	gone := {"address": "aws_s3_bucket_public_access_block.old", "mode": "managed", "type": "aws_s3_bucket_public_access_block",
		"name": "old", "change": {"actions": ["delete"], "after": null}}
	plan := json.patch(private_bucket, [{"op": "add", "path": "/resource_changes/-", "value": gone}])
	count(deny) == 0 with input as plan
}
