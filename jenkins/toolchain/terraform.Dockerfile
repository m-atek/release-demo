# The Terraform toolchain for demo 2's pull requests and drift check:
# Terraform with the AWS provider mirrored in, tflint with its AWS rules,
# Checkov, Conftest, and the Infracost CLI with the two plugins it needs.
# Every download is pinned and checked against its SHA-256, so a run
# downloads nothing.
#
# Built on the agent with infra/demos/2-terraform/checks as the context, for
# tflint.hcl; the pipeline tags it with a hash of both files.
FROM python:3.13.17-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530

ARG TARGETARCH
ARG TERRAFORM_VERSION=1.17.0
ARG AWS_PROVIDER_VERSION=6.69.1
ARG TFLINT_VERSION=0.64.0
ARG CONFTEST_VERSION=0.71.0
ARG CHECKOV_VERSION=3.3.25
ARG INFRACOST_VERSION=2.17.0
# Infracost's plugins, from the same weeks as the CLI.
ARG INFRACOST_TERRAFORM_PLUGIN=v0.0.76
ARG INFRACOST_AWS_PLUGIN=v0.0.22

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git unzip \
 && rm -rf /var/lib/apt/lists/*

# The SHA-256 of each download, for amd64 (the tools host) and arm64 (a Mac).
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) terraform=c168cab7c637a8769fdb90089ddd19de701bc6ff8aca64dc2e2185fe46f59f67; \
             tflint=cca9d13e2e1d7a2c627af60ff899a3c9b74212899416aeb96ec764d2ef954537; \
             conftest=765dfefdf0730693d7541ea0147e10d530a54ed257bbfec055ff34c837ffdf72; conftest_arch=x86_64; \
             infracost=970043bf619d0b2fcdb5e97e52088c691720cc57f71210c142e5a867dc5bd204; \
             parser=92d6481fa730017155976629564f37a4f99d13864afc4ed9a878a14255e69f70; \
             provider=f0d0f956fd0f7fbffb3443218544ec8f7b4cdb696bd56c78045563dd2c79d4ce ;; \
      arm64) terraform=f32478134ffc6d710eec474bbd316652cdddc0f4e336973a3745e8d3a3309cdf; \
             tflint=560da89aacf59389d4eb029730dd5b109b7288096c32f2726a0d9e783a5ea8eb; \
             conftest=542f255081cfab9919cba327d3c4014fd46d6ee3f0441c634b14b1465e9469d9; conftest_arch=arm64; \
             infracost=fff92f22d012ba232e63758e49af4aa3cb9fb091693e989519833f6d62569dd9; \
             parser=fc76b4fa9b8104f02b11bc375152b027e23d947578f52ee1f553925a3d296ed7; \
             provider=3f5e92c299e523c774f42bd53c40012419c9e176fc876cfca600217e1e7ddd18 ;; \
      *) echo "No pinned downloads for $arch"; exit 1 ;; \
    esac; \
    get() { curl -fsSL --retry 3 -o "$1" "$2"; echo "$3  $1" | sha256sum -c -; }; \
    cd /tmp; \
    get terraform.zip "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${arch}.zip" "$terraform"; \
    unzip -q terraform.zip terraform -d /usr/local/bin; \
    get tflint.zip "https://github.com/terraform-linters/tflint/releases/download/v${TFLINT_VERSION}/tflint_linux_${arch}.zip" "$tflint"; \
    unzip -q tflint.zip tflint -d /usr/local/bin; \
    get conftest.tar.gz "https://github.com/open-policy-agent/conftest/releases/download/v${CONFTEST_VERSION}/conftest_${CONFTEST_VERSION}_Linux_${conftest_arch}.tar.gz" "$conftest"; \
    tar -xzf conftest.tar.gz -C /usr/local/bin conftest; \
    get infracost.tar.gz "https://github.com/infracost/cli/releases/download/v${INFRACOST_VERSION}/infracost-linux-${arch}.tar.gz" "$infracost"; \
    tar -xzf infracost.tar.gz -C /usr/local/bin infracost; \
    mkdir -p /opt/infracost/plugins; \
    get parser.tar.gz "https://releases.infracost.io/infracost-parser-terraform/linux/${arch}/${INFRACOST_TERRAFORM_PLUGIN}/data.tar.gz" "$parser"; \
    tar -xzf parser.tar.gz -C /opt/infracost/plugins; \
    get provider.tar.gz "https://releases.infracost.io/infracost-provider-aws/linux/${arch}/${INFRACOST_AWS_PLUGIN}/data.tar.gz" "$provider"; \
    tar -xzf provider.tar.gz -C /opt/infracost/plugins; \
    chmod -R a+rX /opt/infracost; \
    rm -f /tmp/*.zip /tmp/*.tar.gz

# The AWS provider from a local mirror: terraform init needs no registry.
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    mkdir -p /tmp/mirror /opt/terraform; \
    printf 'terraform {\n  required_providers {\n    aws = { source = "hashicorp/aws", version = "%s" }\n  }\n}\n' \
      "$AWS_PROVIDER_VERSION" > /tmp/mirror/main.tf; \
    terraform -chdir=/tmp/mirror providers mirror -platform="linux_${arch}" /opt/terraform/providers; \
    printf 'provider_installation {\n  filesystem_mirror {\n    path    = "/opt/terraform/providers"\n    include = ["registry.terraform.io/hashicorp/aws"]\n  }\n  direct {\n    exclude = ["registry.terraform.io/hashicorp/aws"]\n  }\n}\n' \
      > /opt/terraform/terraformrc; \
    chmod -R a+rX /opt/terraform; \
    rm -rf /tmp/mirror

# tflint's AWS rules, at the version tflint.hcl names.
COPY tflint.hcl /opt/tflint/tflint.hcl
ENV TFLINT_PLUGIN_DIR=/opt/tflint/plugins
RUN tflint --init --config /opt/tflint/tflint.hcl && chmod -R a+rX /opt/tflint

RUN pip install --no-cache-dir --disable-pip-version-check "checkov==${CHECKOV_VERSION}"

ENV HOME=/tmp \
    TF_CLI_CONFIG_FILE=/opt/terraform/terraformrc \
    TF_IN_AUTOMATION=1 \
    TF_INPUT=0 \
    CHECKOV_RUN_SCA_PACKAGE_SCAN_V2=false \
    INFRACOST_CLI_PLUGIN_DIR=/opt/infracost/plugins \
    INFRACOST_CLI_PLUGIN_AUTO_UPDATE=false \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
