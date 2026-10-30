# The coding agent's container: Claude Code and AWS CLI v2 at pinned versions,
# Python 3.13 and unzip, run as the non-root user agent. Video 9's agent.sh
# runs it on the Mac. It holds no credentials: whoever starts it passes an
# AWS session and the Claude credential as environment variables, and those
# leave with the container.
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
# The AWS CLI Team signer's public fingerprint, as AWS's install guide prints it.
ARG AWS_CLI_SIGNER=FB5DB77FD5C118B80511ADA8A6310ACC4672475C

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates unzip=6.0-28 \
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
