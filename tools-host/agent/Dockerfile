# The Jenkins agent for local mode. On the EC2 host the agent runs on the host
# itself with the same tools: Java (from this base), git, Python with PyYAML,
# and the Docker CLI for the pinned toolchain containers.
FROM docker:28.5.1-cli AS docker-cli

FROM jenkins/inbound-agent:3391.va_37fa_a_305d6d-5-jdk21
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends git python3 python3-yaml curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
USER jenkins
