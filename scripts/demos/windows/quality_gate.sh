#!/usr/bin/env bash
# Stage 3, Quality gate, for C#: SonarScanner for .NET, in the .NET toolchain
# container. The shared step's sonar-scanner CLI skips C# files.
#
#   SONAR_TOKEN=... SONAR_ORGANIZATION=... scripts/demos/windows/quality_gate.sh store-sync 3.3.0
#
# begin, a build of the service, end: the scanner waits for the gate, so a
# failed gate fails the stage. Reads test results and coverage from the Test
# stage's .release/reports/. The token comes from the environment only.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
SERVICE=${1:?usage: quality_gate.sh <service> <version>}
VERSION=${2:?usage: quality_gate.sh <service> <version>}
: "${SONAR_TOKEN:?SONAR_TOKEN is not set}" "${SONAR_ORGANIZATION:?SONAR_ORGANIZATION is not set}"
REPORTS=${WORKSPACE:-$ROOT}/.release/reports
KEY=$(python3 -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))[sys.argv[2]]["sonarqube"]["project_key"])' \
  "$ROOT/services.yaml" "$SERVICE")

cd "$ROOT/services/$SERVICE"
dotnet-sonarscanner begin /k:"$KEY" /o:"$SONAR_ORGANIZATION" /n:"$SERVICE" /v:"$VERSION" \
  /d:sonar.host.url=https://sonarcloud.io /d:sonar.token="$SONAR_TOKEN" \
  /d:sonar.cs.opencover.reportsPaths="$REPORTS/coverage.opencover.xml" \
  /d:sonar.cs.vstest.reportsPaths="$REPORTS/*.trx" \
  /d:sonar.qualitygate.wait=true /d:sonar.qualitygate.timeout=300 \
  ${GIT_COMMIT:+/d:sonar.scm.revision="$GIT_COMMIT"}
dotnet build src/StoreSync/StoreSync.csproj --configuration Release --no-incremental --nologo
dotnet-sonarscanner end /d:sonar.token="$SONAR_TOKEN"
