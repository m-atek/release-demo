# The coding agent's container: Claude Code and AWS CLI v2 at pinned versions,
# Python 3.13 and unzip, and for video 16 git, gh, pytest and zip, run as the
# non-root user agent. Video 9's agent.sh runs it on the Mac; video 16's job
# agent-task on the tools host. It holds no credentials: whoever starts it
# passes an AWS session, the Claude credential and, for agent-task, the
# GitHub token as environment variables, and those leave with the container.
#
# Claude Code's settings (coding-agent/managed-settings.json) allow its
# commands and file reads without asking, so its own permission prompt never
# stands between it and AWS: whatever stops it is AWS.
#
# Every download is pinned: the base image by digest, Claude Code by the
# SHA-256 of its binary for each architecture, and AWS CLI by AWS's own
# signature, checked against the AWS CLI Team's public key in
# coding-agent/aws-cli-team.asc. Built with coding-agent/ as the context:
#   docker build -f jenkins/toolchain/coding-agent.Dockerfile jenkins/toolchain/coding-agent
FROM python:3.13.17-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530

ARG TARGETARCH
ARG CLAUDE_CODE_VERSION=2.1.308
ARG AWS_CLI_VERSION=2.38.0
ARG GH_VERSION=2.102.0
ARG PYTEST_VERSION=9.1.1
# The AWS CLI Team signer's public fingerprint, as AWS's install guide prints it.
ARG AWS_CLI_SIGNER=FB5DB77FD5C118B80511ADA8A6310ACC4672475C

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates unzip=6.0-28 zip=3.0-13 git=1:2.39.5-0+deb12u3 \
 && rm -rf /var/lib/apt/lists/*

COPY fetch.py aws-cli-team.asc /opt/coding-agent/

# Claude Code's native binary, from its npm platform package.
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) platform=linux-x64; sha256=356733f185fa988e6bf3aa9a555269fdd167982a2e2a80d3c8a78037bb92ad58 ;; \
      arm64) platform=linux-arm64; sha256=fdd22e6cbb734d580f5664c48df4294eeec850218ff07f186f9af30426396ee1 ;; \
      *) echo "No pinned Claude Code for $arch"; exit 1 ;; \
    esac; \
    cd /tmp; \
    python3 /opt/coding-agent/fetch.py "https://registry.npmjs.org/@anthropic-ai/claude-code-${platform}/-/claude-code-${platform}-${CLAUDE_CODE_VERSION}.tgz" claude.tgz; \
    tar -xzf claude.tgz package/claude; \
    python3 /opt/coding-agent/fetch.py --check package/claude "$sha256"; \
    install -m 0755 package/claude /usr/local/bin/claude; \
    rm -rf claude.tgz package; \
    claude --version

# AWS CLI v2, checked with AWS's signature before it is unpacked.
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in amd64) cli=x86_64 ;; arm64) cli=aarch64 ;; *) echo "No AWS CLI for $arch"; exit 1 ;; esac; \
    cd /tmp; \
    base="https://awscli.amazonaws.com/awscli-exe-linux-${cli}-${AWS_CLI_VERSION}.zip"; \
    python3 /opt/coding-agent/fetch.py "$base" awscliv2.zip; \
    python3 /opt/coding-agent/fetch.py "$base.sig" awscliv2.sig; \
    python3 /opt/coding-agent/fetch.py --keyring /opt/coding-agent/aws-cli-team.asc "$AWS_CLI_SIGNER" aws.gpg; \
    gpgv --keyring /tmp/aws.gpg awscliv2.sig awscliv2.zip; \
    unzip -q awscliv2.zip; \
    ./aws/install --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli; \
    rm -rf aws awscliv2.zip awscliv2.sig aws.gpg; \
    aws --version

# gh, checked against the SHA-256 GitHub publishes for each release asset.
RUN set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) sha256=bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386 ;; \
      arm64) sha256=7862c86c72f43df3a2d93ddde6f473285b4e2af61b494849846827e513ef6484 ;; \
      *) echo "No pinned gh for $arch"; exit 1 ;; \
    esac; \
    cd /tmp; \
    python3 /opt/coding-agent/fetch.py "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" gh.tgz; \
    python3 /opt/coding-agent/fetch.py --check gh.tgz "$sha256"; \
    tar -xzf gh.tgz --strip-components=2 -C /usr/local/bin "gh_${GH_VERSION}_linux_${arch}/bin/gh"; \
    rm gh.tgz; \
    gh --version

RUN pip install --no-cache-dir --disable-pip-version-check "pytest==${PYTEST_VERSION}"

# agent-task mounts a clone the Jenkins agent made, so git meets another owner's repository there.
RUN git config --system safe.directory /work/release-demo

RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
 && mkdir -p /etc/claude-code /work \
 && chown agent:agent /work
# Root-owned, so the agent can't widen its own permissions.
COPY managed-settings.json /etc/claude-code/managed-settings.json
COPY --chown=agent:agent claude.json /home/agent/.claude.json
COPY entrypoint.sh /usr/local/bin/coding-agent-entrypoint

# No pager: Claude Code reads every command's output in full.
ENV AWS_PAGER=""
USER agent
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/coding-agent-entrypoint"]
CMD ["claude"]
