# Demo 5 on Google Cloud: quote-service in Cloud Run, staging and production,
# from Artifact Registry.
#
# Two layers. Always there, made once by scripts/demos/clouds/bootstrap.sh gcp
# with your gcloud login: the APIs, the workload identity pool whose provider
# trusts the Jenkins issuer (for the job release only), the deploy service
# account Jenkins acts as, the runtime service account the services run as
# (no roles at all). With apps = true, from
# scripts/demo.sh up clouds: the registry and both services. down sets
# apps = false, which deletes them. The pool stays: Google keeps a deleted
# pool's name for 30 days.
#
# Run it through scripts/demos/clouds/tf.sh gcp ..., which takes the project
# from SSM and signs in to AWS for the state and issuer.

terraform {
  required_version = ">= 1.10"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 8.6.0" }
    aws    = { source = "hashicorp/aws", version = "~> 6.69.0" }
  }
  backend "s3" {
    key          = "release-demo/demos/5-three-clouds-gcp.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "google" {
  project = var.project_id
  region  = local.region
  # Budgets and API calls bill to this project, not to gcloud's default.
  user_project_override = true
  billing_project       = var.project_id
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = { Project = "release-demo", Demo = "5-three-clouds", ManagedBy = "terraform" }
  }
}

variable "project_id" {
  description = "The Google Cloud project, from SSM (gcp/project-id) through tf.sh."
  type        = string
}

variable "apps" {
  description = "The registry and both services. scripts/demo.sh up clouds sets true, down false."
  type        = bool
  default     = false
}

data "aws_ssm_parameter" "issuer" {
  name = "/release-demo/jenkins/oidc-issuer"
}

# The image every service starts on, 1.1.0, by digest: seed.py copies it from ECR.
data "aws_ecr_image" "initial" {
  repository_name = "quote-service"
  image_tag       = "1.1.0"
}

data "google_project" "current" {}

locals {
  quote    = yamldecode(file("${path.module}/../../../../services.yaml"))["quote-service"]
  region   = one([for c in local.quote.clouds : c.region if c.key == "gcp"])
  targets  = { for env, e in local.quote.environments : env => e.targets.gcp }
  services = var.apps ? local.targets : {}
  subject  = "https://jenkins-demo.es.nl/job/release/"
  audience = "release-demo-gcp"
  registry = "${local.region}-docker.pkg.dev"
  labels   = { project = "release-demo", demo = "5-three-clouds" }
}

resource "google_project_service" "apis" {
  for_each = toset(["run.googleapis.com", "artifactregistry.googleapis.com", "iam.googleapis.com",
    "iamcredentials.googleapis.com", "sts.googleapis.com", "cloudresourcemanager.googleapis.com",
  "billingbudgets.googleapis.com"])
  service            = each.value
  disable_on_destroy = false
}

# Google Cloud trusts the Jenkins issuer, for the job release only.
resource "google_iam_workload_identity_pool" "jenkins" {
  workload_identity_pool_id = "release-demo-jenkins"
  display_name              = "Jenkins, release demo"
  description               = "ID tokens from the release demo's Jenkins. Demo system, not a client's."
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "jenkins" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.jenkins.workload_identity_pool_id
  workload_identity_pool_provider_id = "jenkins"
  display_name                       = "Jenkins"
  attribute_mapping = {
    "google.subject"  = "assertion.sub"
    "attribute.build" = "string(assertion.build_number)"
  }
  attribute_condition = "assertion.sub == '${local.subject}'"
  oidc {
    issuer_uri        = nonsensitive(data.aws_ssm_parameter.issuer.value)
    allowed_audiences = [local.audience]
  }
}

resource "google_service_account" "deployer" {
  account_id   = "quote-service-deployer"
  display_name = "quote-service deployer: Jenkins acts as it, with the build's ID token"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "runtime" {
  account_id   = "quote-service-runtime"
  display_name = "quote-service runtime: the services run as it, with no roles"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account_iam_member" "jenkins_acts_as_deployer" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/${google_iam_workload_identity_pool.jenkins.name}/subject/${local.subject}"
}

# A deploy names the runtime account, so the deployer may act as it.
resource "google_service_account_iam_member" "deployer_uses_runtime" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

# The billing account's budget alert (EUR 10 a month, to its admins) is set on
# the billing account itself, not here, so tearing down the demo never removes it.

# Up -------------------------------------------------------------------------

resource "google_artifact_registry_repository" "quote" {
  count         = var.apps ? 1 : 0
  repository_id = "quote-service"
  location      = local.region
  format        = "DOCKER"
  description   = "quote-service images, copied by digest from JFrog. Demo system."
  labels        = local.labels

  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-the-starting-version"
    action = "KEEP"
    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["1.1.0"]
    }
  }
  cleanup_policies {
    id     = "keep-the-last-30"
    action = "KEEP"
    most_recent_versions {
      keep_count = 30
    }
  }
  cleanup_policies {
    id     = "delete-the-rest-after-30-days"
    action = "DELETE"
    condition {
      older_than = "2592000s"
    }
  }
}

resource "google_artifact_registry_repository_iam_member" "deployer_push" {
  count      = var.apps ? 1 : 0
  location   = local.region
  repository = google_artifact_registry_repository.quote[0].name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_cloud_run_v2_service" "quote" {
  for_each            = local.services
  name                = each.value.name
  location            = local.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false
  # Public, like the Lambda function URLs and the Container Apps.
  invoker_iam_disabled = true
  labels               = local.labels

  template {
    revision        = "${each.value.name}-v1-1-0-initial"
    service_account = google_service_account.runtime.email
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    containers {
      image = "${local.registry}/${var.project_id}/quote-service/quote-service@${data.aws_ecr_image.initial.image_digest}"
      ports {
        container_port = local.quote.image.port
      }
      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = true
      }
      dynamic "env" {
        for_each = merge({ SERVICE_CLOUD = "gcp", SERVICE_ENVIRONMENT = each.key, SERVICE_REGION = local.region },
        each.value.settings)
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  # The pipeline deploys new revisions and moves traffic; reset.py moves it back.
  lifecycle {
    ignore_changes = [template[0].containers[0].image, template[0].revision, traffic, client, client_version]
  }

  depends_on = [google_artifact_registry_repository.quote]
}

resource "google_cloud_run_v2_service_iam_member" "deployer" {
  for_each = local.services
  name     = google_cloud_run_v2_service.quote[each.key].name
  location = local.region
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.deployer.email}"
}

# What the scripts need to reach Google Cloud, kept out of the public
# repository. Without the services, the registry and addresses are empty.
resource "aws_ssm_parameter" "gcp" {
  name        = "/release-demo/clouds/gcp"
  type        = "String"
  description = "quote-service on Google Cloud: project, identity provider, deploy account, registry, service addresses. Terraform, infra/demos/5-three-clouds/gcp."
  value = jsonencode({
    project_id      = var.project_id
    project_number  = data.google_project.current.number
    provider        = google_iam_workload_identity_pool_provider.jenkins.name
    service_account = google_service_account.deployer.email
    registry        = var.apps ? local.registry : ""
    repository      = "${var.project_id}/quote-service/quote-service"
    urls            = { for env, s in google_cloud_run_v2_service.quote : env => s.uri }
  })
}

output "quote_service_gcp_urls" {
  value = { for env, s in google_cloud_run_v2_service.quote : env => s.uri }
}

# For up.sh: where seed.py copies 1.1.0 before the services exist.
output "registry" {
  value = var.apps ? local.registry : ""
}

output "repository" {
  value = "${var.project_id}/quote-service/quote-service"
}
