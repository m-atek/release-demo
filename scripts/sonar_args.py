"""Print the SonarQube scanner arguments for a service, for stage 3.

    python scripts/sonar_args.py orders-service 1.4.0
    python scripts/sonar_args.py store-sync 3.3.0 --organization   the organization only, for a service's own scanner

One SonarQube Cloud project per service (a monorepo), analysed from CI, and
the scanner waits for the quality gate: a failed gate fails the stage.
"""

import sys

import ci_env
from common import get_parameter, service, ssm_client


def main():
    name, version = sys.argv[1], sys.argv[2]
    config = service(name)
    path = config["path"]
    organization = get_parameter(ssm_client(), "sonar/organization")
    if "--organization" in sys.argv[3:]:
        print(organization)
        return
    args = {
        "sonar.organization": organization,
        "sonar.projectKey": config["sonarqube"]["project_key"],
        "sonar.projectName": name,
        "sonar.projectVersion": version,
        "sonar.sources": path,
        "sonar.qualitygate.wait": "true",
        "sonar.qualitygate.timeout": "300",
    }
    if (config["dir"] / "tests").is_dir():
        args["sonar.exclusions"] = f"{path}/tests/**"
        args["sonar.tests"] = f"{path}/tests"
    if config.get("runtime", "").startswith("python"):
        args["sonar.python.version"] = config["runtime"].removeprefix("python")
        args["sonar.python.coverage.reportPaths"] = ".release/reports/coverage.xml"
    # A demo's own scanner settings, for example its language's coverage report.
    args.update({key: str(value) for key, value in config["sonarqube"].get("properties", {}).items()})
    if ci_env.current().commit:
        args["sonar.scm.revision"] = ci_env.current().commit
    print(" ".join(f"-D{key}={value}" for key, value in args.items()))


if __name__ == "__main__":
    main()
