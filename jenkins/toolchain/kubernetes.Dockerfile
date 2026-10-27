# The Kubernetes demo's toolchain: the Python toolchain plus Helm 4, which the
# catalog's chart tests render with. The build itself needs no Docker daemon:
# scripts/demos/kubernetes/build.py assembles the image. Built on the agent from
# this file; the pipeline tags it with a hash of this file and requirements-dev.txt.
FROM python:3.13.17-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530
RUN apt-get update \
 && apt-get install -y --no-install-recommends git unzip \
 && rm -rf /var/lib/apt/lists/*
ARG HELM_VERSION=v4.3.1
ARG HELM_SHA256=0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b
RUN python3 -c "import hashlib, sys, urllib.request; \
data = urllib.request.urlopen('https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz', timeout=120).read(); \
digest = hashlib.sha256(data).hexdigest(); \
sys.exit(f'helm checksum {digest} is not ${HELM_SHA256}') if digest != '${HELM_SHA256}' else None; \
open('/tmp/helm.tgz', 'wb').write(data)" \
 && tar -xzf /tmp/helm.tgz -C /usr/local/bin --strip-components=1 linux-amd64/helm \
 && rm /tmp/helm.tgz \
 && helm version --short
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements-dev.txt
ENV HOME=/tmp PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 HELM=/usr/local/bin/helm
