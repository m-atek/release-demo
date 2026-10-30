"""Put the platform's secrets on the tools host's tmpfs, from SSM.

    host_secrets.py /run/release-demo/secrets

One file per parameter, named like the parameter with / as -: jenkins/admin-password
becomes jenkins-admin-password, which JCasC reads as ${jenkins-admin-password}.
Plus the env files Docker Compose reads. Values still "unset" are left out, and
so is everything the host never needs, such as the Cloudflare API token.
Prints counts only, never values.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import yaml  # noqa: E402
from common import ROOT, SSM_PREFIX, aws_session  # noqa: E402

JENKINS_GID = 1000  # the jenkins user's group inside the controller image
CLOUDFLARED_UID = 65532  # the nonroot user inside the cloudflared image
NOT_ON_HOST = {"cloudflare/api-token", "cloudflare/account-id", "cloudflare/zone-id",
               "grafana/external-id", "terraform/state-bucket", "servicenow/admin-password",
               "servicenow/sam-password", "servicenow/alex-password", "grafana/setup-token",
               # Demo 17: the GitLab account's token is the Mac's only (an IAM deny), and the
               # runner reads its own token from SSM once, so neither goes on the tmpfs.
               "gitlab/api-token", "gitlab/runner-token", "gitlab/trigger-token"}


def write(directory, name, value, uid=0, gid=JENKINS_GID, mode=0o640):
    path = directory / name
    path.write_text(value if value.endswith("\n") else value + "\n")
    os.chown(path, uid, gid)
    path.chmod(mode)


def main():
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    os.chown(directory, 0, JENKINS_GID)
    directory.chmod(0o750)

    # By name, ten at a time, from the catalog: reading by path would skip the
    # IAM denies on the values the host must not see.
    catalog = yaml.safe_load((ROOT / "infra" / "parameters.yaml").read_text())
    wanted = [SSM_PREFIX + name for name in catalog if name not in NOT_ON_HOST]
    ssm = aws_session().client("ssm")
    values = {}
    for start in range(0, len(wanted), 10):
        reply = ssm.get_parameters(Names=wanted[start:start + 10], WithDecryption=True)
        for parameter in reply["Parameters"]:
            if parameter["Value"] != "unset":
                values[parameter["Name"].removeprefix(SSM_PREFIX)] = parameter["Value"]

    for name, value in values.items():
        if name == "cloudflare/tunnel-token":
            write(directory, "cloudflare-tunnel-token", value, uid=CLOUDFLARED_UID, gid=0, mode=0o400)
        else:
            write(directory, name.replace("/", "-"), value)

    write(directory, "splunk.env", f"SPLUNK_PASSWORD={values['splunk/password']}\n"
                                   f"SPLUNK_HEC_TOKEN={values['splunk/hec-token']}\n", gid=0, mode=0o600)
    write(directory, "jcr.env", f"JF_SHARED_DATABASE_PASSWORD={values['jcr/db-password']}\n", gid=0, mode=0o600)
    if "github/app-private-key" in values:
        # Jenkins' GitHub App credential reads only PKCS#8 keys; GitHub hands out PKCS#1.
        pkcs8 = subprocess.run(["openssl", "pkcs8", "-topk8", "-nocrypt"], input=values["github/app-private-key"],
                               capture_output=True, text=True, check=True).stdout
        write(directory, "github-app-private-key-pkcs8", pkcs8)
    if "cloudflare/tunnel-token" not in values:
        # Compose names the file even when the tunnel isn't running.
        write(directory, "cloudflare-tunnel-token", "", gid=0, mode=0o400)
    print(f"{len(values)} secrets on the tmpfs")


if __name__ == "__main__":
    main()
