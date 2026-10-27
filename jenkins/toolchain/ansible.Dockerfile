# Ansible for the inventory rollout to the cloud servers, through Systems
# Manager: ansible-core, boto3, the amazon.aws collection (the aws_ssm
# connection) and AWS's Session Manager plugin. scripts/demos/linux/fleet.py
# builds it from the repository root and runs it on the tools host or the Mac,
# with short-lived credentials of the deploy role passed in as environment
# variables. Every download is pinned and checked by its SHA-256.
FROM python:3.13.17-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Session Manager plugin 1.2.896.0, for the machine's own architecture.
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
      amd64) dir=ubuntu_64bit; sum=53f51fcc67314a4bf85888a38a3967cd41f8d0f28a60a9469a63acf935ba713e ;; \
      arm64) dir=ubuntu_arm64; sum=914eeca87b18b55356c58be197080f73742b3ca60d3823f0a45670dc9056aa9c ;; \
    esac; \
    curl -fsSL -o /tmp/session-manager-plugin.deb \
      "https://s3.amazonaws.com/session-manager-downloads/plugin/1.2.896.0/$dir/session-manager-plugin.deb"; \
    echo "$sum  /tmp/session-manager-plugin.deb" | sha256sum -c -; \
    dpkg -i /tmp/session-manager-plugin.deb; \
    rm /tmp/session-manager-plugin.deb

COPY scripts/demos/linux/ansible/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.txt

# amazon.aws 12.0.0 from Ansible Galaxy.
RUN curl -fsSL -o /tmp/amazon-aws.tar.gz \
      https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/artifacts/amazon-aws-12.0.0.tar.gz \
 && echo "521982d74fa6a5268887fc1b9aff5477b89fb8889a1428a05737ff5615f26685  /tmp/amazon-aws.tar.gz" | sha256sum -c - \
 && ansible-galaxy collection install --offline -p /usr/share/ansible/collections /tmp/amazon-aws.tar.gz \
 && rm /tmp/amazon-aws.tar.gz

ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 ANSIBLE_FORCE_COLOR=0
