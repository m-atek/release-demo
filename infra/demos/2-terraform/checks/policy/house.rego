# The house rules for the network stack, checked by Conftest against the plan
# as JSON (terraform show -json). Each message starts with the rule's name,
# which the pull request and Slack show as it is. The rules live outside the
# stack's folder, next to the pipeline that runs them.
package main

required_tags := {"Project", "Demo", "Owner"}

allowed_regions := {"eu-west-1", "eu-central-1"}

allowed_instance_types := {"t3.micro", "t3.small", "t3.medium", "t3.large"}

instance_type_attribute := {
	"aws_instance": "instance_type",
	"aws_launch_template": "instance_type",
	"aws_spot_instance_request": "instance_type",
}

public_access_flags := ["block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"]

# What the plan creates or changes, with its planned values.
planned contains resource if {
	some resource in input.resource_changes
	resource.mode == "managed"
	some action in resource.change.actions
	action in {"create", "update"}
}

# Required tags: every taggable resource carries them, set directly or
# through the provider's default tags (tags_all holds both).
deny contains msg if {
	some resource in planned
	tags := resource.change.after.tags_all
	is_object(tags)
	missing := required_tags - {key | some key, _ in tags}
	count(missing) > 0
	msg := sprintf("Required tags: %s has no %s", [resource.address, concat(", ", sort(missing))])
}

# S3 bucket must not be public: its public access block keeps all four settings on.
deny contains msg if {
	some resource in planned
	resource.type == "aws_s3_bucket_public_access_block"
	off := [flag | some flag in public_access_flags; resource.change.after[flag] != true]
	count(off) > 0
	msg := sprintf("S3 bucket must not be public: %s turns off %s", [resource.address, concat(", ", off)])
}

# S3 bucket must not be public: a new bucket comes with a public access block.
deny contains msg if {
	some resource in input.resource_changes
	resource.type == "aws_s3_bucket"
	"create" in resource.change.actions
	not has_access_block(resource)
	msg := sprintf("S3 bucket must not be public: %s has no aws_s3_bucket_public_access_block", [resource.address])
}

# S3 bucket must not be public: no ACL that opens it.
deny contains msg if {
	some resource in planned
	resource.type == "aws_s3_bucket_acl"
	resource.change.after.acl in {"public-read", "public-read-write", "authenticated-read"}
	msg := sprintf("S3 bucket must not be public: %s grants %s", [resource.address, resource.change.after.acl])
}

# S3 bucket must not be public: no policy that allows everyone. Checked when the
# plan knows the policy; a new bucket's policy names its ARN, unknown until the
# apply, and then the public access block above is what stops it.
deny contains msg if {
	some resource in planned
	resource.type == "aws_s3_bucket_policy"
	is_string(resource.change.after.policy)
	some statement in statements(json.unmarshal(resource.change.after.policy))
	statement.Effect == "Allow"
	everyone(statement.Principal)
	msg := sprintf("S3 bucket must not be public: %s allows everyone (Principal *)", [resource.address])
}

# Allowed regions: each AWS provider names an allowed region in the code.
deny contains msg if {
	some key, provider in input.configuration.provider_config
	provider.name == "aws"
	region := object.get(provider, ["expressions", "region", "constant_value"], "")
	not region in allowed_regions
	msg := sprintf("Allowed regions: provider %s uses %s; allowed are %s", [key, shown(region), concat(", ", sort(allowed_regions))])
}

# Allowed instance types.
deny contains msg if {
	some resource in planned
	attribute := instance_type_attribute[resource.type]
	value := resource.change.after[attribute]
	not value in allowed_instance_types
	msg := sprintf("Allowed instance types: %s uses %s; allowed are %s", [resource.address, value, concat(", ", sort(allowed_instance_types))])
}

has_access_block(bucket) if {
	some block in input.configuration.root_module.resources
	block.type == "aws_s3_bucket_public_access_block"
	some reference in block.expressions.bucket.references
	reference == sprintf("%s.%s", [bucket.type, bucket.name])
}

statements(policy) := policy.Statement if is_array(policy.Statement)

statements(policy) := [policy.Statement] if is_object(policy.Statement)

everyone(principal) if principal == "*"

everyone(principal) if principal.AWS == "*"

everyone(principal) if {
	is_array(principal.AWS)
	"*" in principal.AWS
}

shown(region) := "no region in the code" if region == ""

shown(region) := region if region != ""
