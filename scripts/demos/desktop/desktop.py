"""What the desktop demo's stage scripts share: settings, the package from
JCR, Jamf, the Windows test instance and the pilot change.

The scripts run on the tools-host agent from the repository root, like the
shared ones, and import them: common, run_state, publish, change.
"""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))          # scripts/: common, run_state, publish, change
sys.path.insert(0, str(HERE.parent / "standins"))  # jamf, apple_notary

import jamf as jamf_module  # noqa: E402
import run_state  # noqa: E402
from common import ROOT, aws_session, fail, get_parameter, service, ssm_client  # noqa: E402


def standin(config, name=None):
    """STANDIN from the environment, else services.yaml settings: 1 means stand-ins
    for Apple, Jamf and Microsoft. STANDIN_JAMF and friends override one of them."""
    settings = config.get("settings", {})
    for key in ([f"STANDIN_{name.upper()}"] if name else []) + ["STANDIN"]:
        value = os.environ.get(key, settings.get(key))
        if value is not None:
            return str(value) not in ("0", "false", "no", "")
    return True


def version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version))


def app_config(config):
    return json.loads((config["dir"] / "mac" / "app.json").read_text())


def package_name(config, version):
    return f"{app_config(config)['package_prefix']}-{version}.pkg"


def in_jenkins():
    return bool(os.environ.get("BUILD_URL"))


def notify(text):
    """A line on the release's Slack message, from inside a Jenkins run only."""
    if in_jenkins():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "notify.py"), "line", text], check=False)


# The package

def jcr_token():
    return get_parameter(ssm_client(), "jcr/token")


def verified_package(state):
    """The run's zip from JCR, checked against the SHA-256 recorded at publish."""
    from publish import download

    package = download(jcr_token(), state["package"]["path"])
    actual = hashlib.sha256(package).hexdigest()
    if actual != state["package"]["sha256"]:
        fail(f"{state['package']['path']} has SHA-256 {actual}, but the build recorded {state['package']['sha256']}. "
             "Not deploying a package that changed after it was published.")
    print(f"SHA-256 verified: {actual}")
    return package


def windows_record(package):
    """windows-build.json from the package, or {} without a Windows track."""
    _, raw = member(package, "windows-build.json")
    return json.loads(raw.decode("utf-8-sig")) if raw else {}


def member(package, suffix):
    """The one file in the zip whose name ends with suffix, as (name, bytes)."""
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        names = [n for n in archive.namelist() if n.endswith(suffix)]
        if not names:
            return None, None
        return Path(names[0]).name, archive.read(names[0])


def released_versions(service_name):
    """Versions in the service's releases folder: the ones that reached production."""
    from publish import releases_root, request

    status, body = request("GET", f"api/storage/{releases_root(service_name)}/", jcr_token())
    if status != 200:
        return []
    return sorted((c["uri"].strip("/") for c in json.loads(body).get("children", []) if c.get("folder")), key=version_key)


def release_package(service_name, version):
    """A released version's zip from JCR, checked against its build-info.json."""
    from publish import download, releases_root

    token = jcr_token()
    base = f"{releases_root(service_name)}/{version}"
    info = json.loads(download(token, f"{base}/build-info.json"))
    package = download(token, f"{base}/{info['package']}")
    if hashlib.sha256(package).hexdigest() != info["sha256"]:
        fail(f"{base}/{info['package']} doesn't match its SHA-256.")
    return package


# Jamf

def jamf(config):
    return jamf_module.connect(standin=standin(config, "jamf"))


def jamf_policy(client, config):
    name = config["jamf"]["policy"]
    policy = client.policy(name)
    if policy is None:
        fail(f"Jamf has no policy {name}. Run scripts/demos/desktop/jamf_setup.py.")
    return policy


def ensure_jamf_package(client, config, version, pkg_name, pkg_bytes):
    """The package in Jamf with this exact content: uploaded unless Jamf has it already."""
    sha512 = hashlib.sha512(pkg_bytes).hexdigest()
    record = client.package(pkg_name)
    if record and (record.get("hashValue") or "").lower() == sha512:
        print(f"Jamf already has {pkg_name} with this SHA-512")
        return record
    work = ROOT / "dist" / config["name"] / "jamf"
    work.mkdir(parents=True, exist_ok=True)
    path = work / pkg_name
    path.write_bytes(pkg_bytes)
    record = client.upload_package(path)
    if record.get("hashType") == "SHA_512" and (record.get("hashValue") or "").lower() != sha512:
        fail(f"Jamf stored {pkg_name} with a different SHA-512 than the package from JCR.")
    print(f"Uploaded {pkg_name} to Jamf: package {record['id']}")
    return record


def policy_version(client, policy):
    """The app version the policy installs, from its package's name."""
    for package in policy["packages"]:
        match = re.search(r"-(\d+\.\d+\.\d+)\.pkg$", package["name"] or "")
        if match:
            return match.group(1), package["id"]
    return None, None


def policy_url(client, policy):
    return f"{client.url}/policies.html?id={policy['id']}" if not client.standin else f"{client.url}/standin/ui"


# The Windows test instance, through Run Command

def deploy_session():
    """The demo's deploy role, which reaches only its bucket and its Windows test
    instance. DESKTOP_DEPLOY_AS_ME=1 uses your own credentials, for runs from the Mac."""
    session = aws_session()
    if os.environ.get("DESKTOP_DEPLOY_AS_ME") == "1":
        return session
    import boto3
    account = session.client("sts").get_caller_identity()["Account"]
    creds = session.client("sts").assume_role(RoleArn=f"arn:aws:iam::{account}:role/release-demo-deploy-store-assistant",
                                              RoleSessionName=f"store-assistant-{int(time.time())}")["Credentials"]
    return boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                         aws_session_token=creds["SessionToken"], region_name=session.region_name)


def windows_instance(session, name):
    """The running instance with this Name tag, or None when it's stopped or missing."""
    reply = session.client("ec2").describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [name]}, {"Name": "instance-state-name", "Values": ["running"]}])
    instances = [i for r in reply["Reservations"] for i in r["Instances"]]
    return instances[0]["InstanceId"] if instances else None


def run_powershell(session, instance, commands, comment, timeout=600):
    """Run PowerShell through Run Command and wait. Returns (status, output).
    Never pass a secret here: parameters show in the command history."""
    ssm = session.client("ssm")
    command = ssm.send_command(InstanceIds=[instance], DocumentName="AWS-RunPowerShellScript", Comment=comment[:100],
                               Parameters={"commands": commands, "executionTimeout": [str(timeout)]})["Command"]["CommandId"]
    deadline = time.monotonic() + timeout + 60
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            reply = ssm.get_command_invocation(CommandId=command, InstanceId=instance)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if reply["Status"] not in ("Pending", "InProgress", "Delayed"):
            return reply["Status"], (reply.get("StandardOutputContent", "") + reply.get("StandardErrorContent", "")).strip()
    return "TimedOut", ""


INSTALLED_VERSION = r"""
$keys = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
$app = Get-ItemProperty $keys -ErrorAction SilentlyContinue | Where-Object DisplayName -eq '{name}' | Select-Object -First 1
$exe = Join-Path ${{env:ProgramFiles}} '{folder}\StoreAssistant.exe'
$signature = if (Test-Path $exe) {{ Get-AuthenticodeSignature $exe }} else {{ $null }}
[pscustomobject]@{{
  version = $app.DisplayVersion
  publisher = $app.Publisher
  signer = if ($signature) {{ $signature.SignerCertificate.Subject }} else {{ $null }}
  signature = if ($signature) {{ [string]$signature.Status }} else {{ $null }}
}} | ConvertTo-Json -Compress
"""


def windows_installed(session, instance, product):
    """What the test instance's registry says is installed, and the installed exe's signature."""
    status, output = run_powershell(session, instance, [INSTALLED_VERSION.format(name=product["name"], folder=product["folder"])],
                                    f"Read the installed version of {product['name']}", timeout=120)
    if status != "Success":
        return {"error": f"Run Command {status}: {output[-300:]}"}
    try:
        return json.loads(output.splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": f"unexpected answer: {output[-300:]}"}


TRUST_TEST_CERTIFICATE = r"""
$ErrorActionPreference = 'Stop'
$certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new([Convert]::FromBase64String('{certificate}'))
foreach ($store in 'Root', 'TrustedPublisher') {{
  if (-not (Get-ChildItem "Cert:\LocalMachine\$store" | Where-Object Thumbprint -eq $certificate.Thumbprint)) {{
    $opened = [Security.Cryptography.X509Certificates.X509Store]::new($store, 'LocalMachine')
    $opened.Open('ReadWrite'); $opened.Add($certificate); $opened.Close()
    Write-Output "Trusted the test certificate $($certificate.Subject) in LocalMachine\$store"
  }}
}}
"""

INSTALL_MSI = r"""
$ErrorActionPreference = 'Stop'
$dir = 'C:\ProgramData\release-demo\installers'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$msi = Join-Path $dir '{file}'
Read-S3Object -BucketName '{bucket}' -Key '{key}' -File $msi -Region eu-west-1 | Out-Null
$actual = (Get-FileHash -Algorithm SHA256 $msi).Hash.ToLower()
if ($actual -ne '{sha256}') {{ throw "SHA-256 $actual doesn't match {sha256}: not installing" }}
Write-Output "SHA-256 verified on the instance: $actual"
$log = Join-Path $dir '{file}.log'
$process = Start-Process msiexec.exe -ArgumentList @('/i', $msi, '/qn', '/norestart', '/l*v', $log) -Wait -PassThru
if ($process.ExitCode -notin 0, 3010) {{ Get-Content $log -Tail 40; throw "msiexec exit code $($process.ExitCode)" }}
Write-Output "msiexec exit code $($process.ExitCode)"
"""


def windows_install(session, instance, bucket, msi_name, msi_bytes, version, test_certificate=None):
    """Install an MSI on the test instance: the bytes go to the demo's bucket, the
    instance reads them with its own role, checks the SHA-256 and runs msiexec.
    A test certificate (its public part, base64) is trusted there first, as on
    any test machine."""
    sha256 = hashlib.sha256(msi_bytes).hexdigest()
    key = f"installers/{version}/{sha256[:16]}/{msi_name}"
    session.client("s3").put_object(Bucket=bucket, Key=key, Body=msi_bytes)
    commands = [TRUST_TEST_CERTIFICATE.format(certificate=test_certificate)] if test_certificate else []
    commands.append(INSTALL_MSI.format(file=msi_name, bucket=bucket, key=key, sha256=sha256))
    status, output = run_powershell(session, instance, commands, f"Install {msi_name}")
    print(output)
    if status != "Success":
        fail(f"Installing {msi_name} on {instance}: Run Command {status}")
    return key


def installer_bucket(session):
    account = session.client("sts").get_caller_identity()["Account"]
    return f"release-demo-desktop-{account}"


# The pilot change: a pre-approved standard change for the pilot deployment

def servicenow():
    import change
    return change.connect()


def open_pilot_change(state, config, scope):
    """A standard change from the template "Pilot of Store Assistant", moved to Implement."""
    import change

    sn, ssm = servicenow()
    name = config["servicenow"]["templates"]["pilot"]
    template = sn.find("std_change_record_producer", f"name={name}^active=true", "sys_id")
    if template is None:
        fail(f"ServiceNow has no standard change template '{name}'. Run scripts/servicenow_setup.py.")
    ci = sn.find("cmdb_ci_appl", f"name={config['servicenow']['cmdb_item']}", "sys_id")
    group = sn.find("sys_user_group", f"name={config['servicenow']['assignment_group']}", "sys_id")
    asked_by = change.requester_user(sn, state.get("requested_by"))
    started = run_state.now()
    when = lambda seconds: run_state.iso(seconds).replace("T", " ").rstrip("Z")
    fields = {
        "short_description": f"Pilot of {config['name']} {state['version']} ({state['jira_key']})",
        "description": f"Deliver {config['name']} {state['version']} to the Macs in the Jamf group {scope} only, "
                       f"through the Jamf policy {config['jamf']['policy']}.",
        "cmdb_ci": ci["sys_id"] if ci else "", "assignment_group": group["sys_id"] if group else "",
        "requested_by": asked_by["sys_id"], "category": "Software",
        "justification": f"{state['jira_key']}: the pilot group tries the release before every other Mac.",
        "implementation_plan": f"Upload the notarized package from JFrog Container Registry, verified by its SHA-256, "
                               f"to Jamf; point the policy at it; scope it to {scope}; flush the policy's logs. "
                               f"Build: {state['build'].get('url', '')}",
        "backout_plan": "Point the policy back at the previous package and flush its logs: the pilot Macs reinstall "
                        "the previous version at their next check-in.",
        "test_plan": f"The pilot Macs report {state['version']} in Jamf inventory.",
        "start_date": when(started), "end_date": when(started + 3600),
    }
    reply = sn.call("POST", f"/api/sn_chg_rest/change/standard/{template['sys_id']}", fields)
    pilot = {"sys_id": reply["sys_id"]["value"], "number": reply["number"]["value"], "type": "standard",
             "url": f"{sn.url}/change_request.do?sys_id={reply['sys_id']['value']}", "requested_by": asked_by["sys_id"]}
    change.implement(sn, {"change": pilot})
    state["pilot_change"] = pilot
    run_state.save(state)
    print(f"{pilot['number']}: pilot change, standard and pre-approved")
    return pilot


def close_pilot_change(state, code, notes):
    import change

    pilot = state.get("pilot_change")
    if not pilot or pilot.get("closed"):
        return
    sn, _ = servicenow()
    change.note(sn, {"change": pilot}, notes)
    change.close(sn, {"change": pilot}, code, notes)
    pilot["closed"] = code
    run_state.save(state)
    print(f"{pilot['number']}: closed {code}")
