# The Java toolchain inventory builds and tests in: JDK 11 and Maven, plus
# Python with PyYAML for the stage scripts. JDK 11 compiles both releases:
# 2.7.3 with --release 8, 2.8.0 with --release 11.
#
# Built from the repository root, like python.Dockerfile. Maven's repository
# is warmed here from services/inventory, so a build downloads nothing new
# unless the pom changes. It is writable by any user: Jenkins runs the
# container as the agent's user.
FROM maven:3.9.16-eclipse-temurin-11@sha256:7bb84a451705605a47d24eb844d4d9ab03e33e131a507db76aafe25db3907f64
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-yaml python-is-python3 git unzip \
 && rm -rf /var/lib/apt/lists/*
ENV MAVEN_REPO=/opt/m2
COPY services/inventory/ /tmp/warmup/
# Tests may fail here (a broken demo state): this run only fills the cache.
RUN (mvn -B -q -f /tmp/warmup/pom.xml -Dmaven.repo.local=$MAVEN_REPO -Dmaven.test.failure.ignore=true package || true) \
 && rm -rf /tmp/warmup \
 && chmod -R a+rwX $MAVEN_REPO
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1
# The image's entrypoint copies settings into /root/.m2, which the agent's
# user can't write. Nothing here needs it.
ENTRYPOINT []
