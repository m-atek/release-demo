# squawk, for demo 20's migration check: it lints Postgres migrations and names
# what could break a running version. The pinned Python image of
# python.Dockerfile with squawk-cli 2.70.0, the newest release at least two
# weeks old on 30 October 2026, installed by its hash.
# scripts/demos/rollback-trap/migration_check.py builds it, tagged with a hash
# of this file, and runs it without a network.
FROM python:3.13.17-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530
RUN echo "squawk-cli==2.70.0 --hash=sha256:e67651bb984b01276752ca685f870b281cf493d5d7bd318607148b657c359b52" \
      > /tmp/squawk.txt \
 && pip install --no-cache-dir --disable-pip-version-check --require-hashes --only-binary=:all: -r /tmp/squawk.txt \
 && rm /tmp/squawk.txt
ENV HOME=/tmp PIP_DISABLE_PIP_VERSION_CHECK=1
ENTRYPOINT ["squawk"]
