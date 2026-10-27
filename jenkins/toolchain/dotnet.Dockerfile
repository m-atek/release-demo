# The .NET toolchain for store-sync (demo 1): the pinned .NET 10 SDK, which
# builds the .NET Framework 4.8 service on Linux against Microsoft's reference
# assemblies, plus Python and PyYAML for the stage scripts, and SonarScanner for
# .NET for C# analysis. No Windows build agent. Runs as any user: HOME=/tmp.
FROM mcr.microsoft.com/dotnet/sdk:10.0.403-noble@sha256:de4fe90a98845d75910b9769662465c84de1a934665c431279d1382b2bddc02b
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-yaml git unzip \
 && rm -rf /var/lib/apt/lists/*
RUN dotnet tool install dotnet-sonarscanner --version 11.3.0 --tool-path /opt/dotnet-tools \
 && chmod -R a+rX /opt/dotnet-tools
ENV HOME=/tmp \
    PATH=/opt/dotnet-tools:$PATH \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    PYTHONDONTWRITEBYTECODE=1
