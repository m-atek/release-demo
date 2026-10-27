#!/usr/bin/env bash
# Stage 2, Test: store-sync's unit tests, in the .NET toolchain container.
#
#   scripts/demos/windows/test.sh store-sync
#
# Writes .release/reports/junit.xml for Jenkins, store-sync.trx and
# coverage.opencover.xml for SonarQube. Exits non-zero on a failed test.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
SERVICE=${1:?usage: test.sh <service>}
REPORTS=${WORKSPACE:-$ROOT}/.release/reports
TESTS=$ROOT/services/$SERVICE/tests/StoreSync.Tests
RESULTS=$(mktemp -d)
trap 'rm -rf "$RESULTS"' EXIT

mkdir -p "$REPORTS"
status=0
dotnet test "$TESTS" --configuration Release --nologo --verbosity quiet \
  -p:RestoreLockedMode=true \
  --results-directory "$RESULTS" \
  --logger "junit;LogFilePath=$REPORTS/junit.xml;MethodFormat=Class;FailureBodyFormat=Verbose" \
  --logger "trx;LogFileName=$SERVICE.trx" \
  --collect "XPlat Code Coverage" \
  -- DataCollectionRunSettings.DataCollectors.DataCollector.Configuration.Format=opencover || status=$?

cp "$RESULTS/$SERVICE.trx" "$REPORTS/" 2>/dev/null || true
coverage=$(find "$RESULTS" -name coverage.opencover.xml | head -1)
[[ -n $coverage ]] && cp "$coverage" "$REPORTS/coverage.opencover.xml"
exit $status
