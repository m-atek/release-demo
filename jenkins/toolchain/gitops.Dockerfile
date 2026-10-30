# Demo 19's toolchain for pull requests to deploy/production/catalog: the
# Python toolchain plus Helm 4, which renders the chart with the pull
# request's values, and kubeconform, which checks the result against the
# cluster host's Kubernetes version. Both pinned and checked by SHA-256.
# kubeconform reads its schemas from github.com/yannh/kubernetes-json-schema
# when it runs. Built on the agent from this file; the pipeline tags it with
# a hash of this file and requirements-dev.txt.
FROM python:3.13.17-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530
RUN apt-get update \
 && apt-get install -y --no-install-recommends git unzip curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ARG HELM_VERSION=v4.3.1
ARG HELM_SHA256=0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b
ARG KUBECONFORM_VERSION=v0.8.0
ARG KUBECONFORM_SHA256=9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883
RUN curl -fsSL -o /tmp/helm.tgz "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
 && echo "${HELM_SHA256}  /tmp/helm.tgz" | sha256sum -c - \
 && curl -fsSL -o /tmp/kubeconform.tgz \
      "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-amd64.tar.gz" \
 && echo "${KUBECONFORM_SHA256}  /tmp/kubeconform.tgz" | sha256sum -c - \
 && tar -xzf /tmp/helm.tgz -C /usr/local/bin --strip-components=1 linux-amd64/helm \
 && tar -xzf /tmp/kubeconform.tgz -C /usr/local/bin kubeconform \
 && rm /tmp/helm.tgz /tmp/kubeconform.tgz \
 && helm version --short && kubeconform -v
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements-dev.txt
ENV HOME=/tmp PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 HELM=/usr/local/bin/helm
