# The Python toolchain the build and test stages run in: the pinned Python
# image plus the pinned requirements. Built on the agent from this file; the
# pipeline tags it with a hash of this file and requirements-dev.txt.
FROM python:3.13.17-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530
RUN apt-get update \
 && apt-get install -y --no-install-recommends git unzip \
 && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements-dev.txt
ENV HOME=/tmp PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
