"""Run Ansible against the four inventory servers, from wherever this runs.

The servers come from services.yaml (inventory, production), in rollout order,
in two sites:

- cloud: inv-cloud-01 and inv-cloud-02 on EC2, reached through Systems Manager
  (amazon.aws.aws_ssm, files through the demo's S3 bucket). Ansible runs in
  jenkins/toolchain/ansible.Dockerfile with short-lived credentials of the
  deploy role, on the tools host or the Mac.
- data-center: inv-dc-01 and inv-dc-02, Multipass virtual machines that only
  dc-agent reaches, over SSH. On dc-agent itself (the Jenkins job
  inventory-dc-wave), Ansible runs directly. From the tools host, this module
  starts that job and waits for it; from the Mac, it runs the same on dc-agent
  with multipass exec.

Every run returns each server's result: one JSON line per server from
ansible/tasks/result.yml. Servers the play never reached have none.
"""

import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import run_state  # noqa: E402
from common import ROOT, aws_session, fail, service  # noqa: E402

SERVICE = "inventory"
HERE = Path(__file__).resolve().parent
ANSIBLE = HERE / "ansible"
DOCKERFILE = ROOT / "jenkins" / "toolchain" / "ansible.Dockerfile"
DEPLOY_ROLE = "release-demo-deploy-inventory"
DEMO_TAG = "3-linux"
# The drift the video shows: this server still runs Java 8.
DRIFT_SERVER = "inv-dc-01"
DC_AGENT = "dc-agent"
DC_HOME = "/var/lib/jenkins-agent"
DC_ANSIBLE = "/opt/ansible/bin/ansible-playbook"


def servers():
    """[{name, site, location, agent}] in rollout order."""
    production = service(SERVICE)["environments"]["production"]
    sites = production["sites"]
    return [{**s, **{k: v for k, v in sites[s["site"]].items()}} for s in production["servers"]]


def settings():
    return service(SERVICE)["environments"]["production"].get("settings", {})


def by_site(names=None):
    """{site: [server, ...]} in rollout order, limited to names if given."""
    grouped = {}
    for server in servers():
        if names is None or server["name"] in names:
            grouped.setdefault(server["site"], []).append(server)
    return grouped


# Cloud: AWS


def deploy_session():
    """A boto3 session as the deploy role, from the tools host's role or the Mac's sign-in."""
    import boto3

    session = aws_session()
    account = session.client("sts").get_caller_identity()["Account"]
    creds = session.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account}:role/{DEPLOY_ROLE}",
        RoleSessionName=f"inventory-{int(time.time())}")["Credentials"]
    return boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                         aws_session_token=creds["SessionToken"], region_name=session.region_name), account


def instances(session):
    """{name: {id, state}} for the cloud servers that exist (not terminated)."""
    reply = session.client("ec2").describe_instances(Filters=[
        {"Name": "tag:Demo", "Values": [DEMO_TAG]}, {"Name": "tag:Service", "Values": [SERVICE]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
    found = {}
    for reservation in reply["Reservations"]:
        for instance in reservation["Instances"]:
            name = next((t["Value"] for t in instance.get("Tags", []) if t["Key"] == "Name"), None)
            found[name] = {"id": instance["InstanceId"], "state": instance["State"]["Name"],
                           "private_ip": instance.get("PrivateIpAddress")}
    return found


def tools_host_private_ip(session):
    reply = session.client("ec2").describe_instances(Filters=[
        {"Name": "tag:Name", "Values": ["release-demo-tools-host"]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
    for reservation in reply["Reservations"]:
        for instance in reservation["Instances"]:
            return instance["PrivateIpAddress"]
    fail("No tools host: its private address is where the cloud servers fetch packages.")


def cloud_inventory(session, account, names):
    found = instances(session)
    hosts = {}
    for server in [s for s in servers() if s["name"] in names]:
        if server["name"] not in found or found[server["name"]]["state"] != "running":
            fail(f"{server['name']} isn't running. Start the demo: scripts/demo.sh up linux")
        instance = found[server["name"]]["id"]
        hosts[server["name"]] = {"ansible_host": instance, "ansible_aws_ssm_instance_id": instance,
                                 "site": server["site"], "server_location": server["location"]}
    return {"all": {"vars": {
        "ansible_connection": "amazon.aws.aws_ssm",
        "ansible_aws_ssm_bucket_name": f"release-demo-inventory-ssm-{account}",
        "ansible_aws_ssm_region": session.region_name,
        "ansible_aws_ssm_s3_addressing_style": "virtual",
        "ansible_aws_ssm_timeout": 120,
        # The aws_ssm connection breaks with pipelining ("Cannot send input after starting communication").
        "ansible_pipelining": False,
        "ansible_python_interpreter": "/usr/bin/python3",
    }, "hosts": hosts}}


def image_tag():
    digest = hashlib.sha256(DOCKERFILE.read_bytes() + (ANSIBLE / "requirements.txt").read_bytes()).hexdigest()[:12]
    return f"release-demo/ansible-tools:{digest}"


def ensure_image():
    tag = image_tag()
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0:
        print(f"Building {tag} from {DOCKERFILE.relative_to(ROOT)}")
        subprocess.run(["docker", "build", "-q", "-t", tag, "-f", str(DOCKERFILE), str(ROOT)], check=True,
                       stdout=subprocess.DEVNULL)
    return tag


def run_cloud(playbook, extra_vars, names, tags=None, prefix="cloud | "):
    """ansible-playbook in the toolchain container. Returns (exit code, results)."""
    session, account = deploy_session()
    extra_vars = {**extra_vars, "settings": settings()}
    with run_dir() as work:
        write_json(work / "inventory.json", cloud_inventory(session, account, names))
        package_mount = []
        if extra_vars.get("package", {}).get("source") == "file":
            path = Path(extra_vars["package"]["file"]).resolve()
            package_mount = ["-v", f"{path.parent}:/package:ro"]
            extra_vars["package"] = {**extra_vars["package"], "file": f"/package/{path.name}"}
        write_json(work / "vars.json", {**extra_vars, "results_file": "/work/results.jsonl"})
        creds = session.get_credentials().get_frozen_credentials()
        env = {**os.environ, "AWS_ACCESS_KEY_ID": creds.access_key, "AWS_SECRET_ACCESS_KEY": creds.secret_key,
               "AWS_SESSION_TOKEN": creds.token}
        command = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
                   "-e", "AWS_ACCESS_KEY_ID", "-e", "AWS_SECRET_ACCESS_KEY", "-e", "AWS_SESSION_TOKEN",
                   "-e", f"AWS_REGION={session.region_name}", "-e", f"AWS_DEFAULT_REGION={session.region_name}",
                   "-e", "ANSIBLE_CONFIG=/ansible/ansible.cfg", "-e", "USER=ansible", "-e", "LOGNAME=ansible",
                   "-e", "ANSIBLE_REMOTE_TMP=/tmp/.ansible/tmp",
                   "-v", f"{ANSIBLE}:/ansible:ro", "-v", f"{work}:/work", *package_mount,
                   "-w", "/ansible", ensure_image(),
                   "ansible-playbook", "-i", "/work/inventory.json", "-e", "@/work/vars.json", playbook]
        if tags:
            command += ["--tags", tags]
        code = stream(command, env, prefix)
        return code, read_results(work / "results.jsonl")


# Data center: dc-agent


def dc_inventory(names):
    hosts = {s["name"]: {"site": s["site"], "server_location": s["location"]}
             for s in servers() if s["name"] in names}
    return {"all": {"vars": {
        "ansible_user": "deploy",
        "ansible_ssh_private_key_file": f"{DC_HOME}/.ssh/id_ed25519",
        "ansible_ssh_common_args": f"-o UserKnownHostsFile={DC_HOME}/.ssh/known_hosts -o StrictHostKeyChecking=yes",
        "ansible_python_interpreter": "/usr/bin/python3",
    }, "hosts": hosts}}


def run_dc_here(playbook, extra_vars, names, tags=None, results=None, prefix=""):
    """On dc-agent itself: ansible-playbook over SSH. Returns (exit code, results)."""
    ansible = DC_ANSIBLE if Path(DC_ANSIBLE).exists() else shutil.which("ansible-playbook")
    if not ansible:
        fail("No ansible-playbook on this machine: is this dc-agent?")
    with run_dir() as work:
        results = Path(results) if results else work / "results.jsonl"
        write_json(work / "inventory.json", dc_inventory(names))
        write_json(work / "vars.json", {**extra_vars, "settings": settings(), "results_file": str(results)})
        command = [ansible, "-i", str(work / "inventory.json"), "-e", f"@{work / 'vars.json'}", playbook]
        if tags:
            command += ["--tags", tags]
        env = {**os.environ, "ANSIBLE_CONFIG": str(ANSIBLE / "ansible.cfg")}
        code = stream(command, env, prefix, cwd=ANSIBLE)
        return code, read_results(results)


def dc_mode():
    """here (on dc-agent), multipass (the Mac) or jenkins (the tools host)."""
    if os.environ.get("INVENTORY_DC_HERE") == "1":
        return "here"
    if shutil.which("multipass"):
        return "multipass"
    return "jenkins"


def run_dc(playbook, extra_vars, names, tags=None, secrets=None, prefix="data center | "):
    """The data-center servers, through dc-agent. Returns (exit code, results).
    secrets: {JCR_TOKEN, CF_ACCESS_CLIENT_ID, ...} for the Mac; Jenkins binds its own."""
    mode = dc_mode()
    if mode == "here":
        return run_dc_here(playbook, extra_vars, names, tags, prefix=prefix)
    if mode == "multipass":
        return run_dc_multipass(playbook, extra_vars, names, tags, secrets or {}, prefix)
    import jenkins_job

    return jenkins_job.run({"playbook": playbook, "extra_vars": extra_vars, "names": names, "tags": tags}, prefix)


def run_dc_multipass(playbook, extra_vars, names, tags, secrets, prefix):
    """From the Mac: copy this checkout's scripts to dc-agent and run there as
    jenkins. Secrets go through stdin, never a command line."""
    remote = f"{DC_HOME}/local-run"
    request = {"playbook": playbook, "extra_vars": dict(extra_vars), "names": names, "tags": tags}
    with tempfile.TemporaryDirectory() as temp:
        bundle = Path(temp) / "bundle.tar"
        with tarfile.open(bundle, "w") as tar:
            for path in ["services.yaml", "people.yaml", "scripts"]:
                tar.add(ROOT / path, arcname=path, filter=lambda info: None if "__pycache__" in info.name else info)
            package = request["extra_vars"].get("package", {})
            if package.get("source") == "file":
                tar.add(package["file"], arcname=f"package/{Path(package['file']).name}")
                request["extra_vars"]["package"] = {**package, "file": f"{remote}/package/{Path(package['file']).name}"}
            data = json.dumps(request).encode()
            info = tarfile.TarInfo("request.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        multipass("exec", DC_AGENT, "--", "sudo", "-u", "jenkins", "sh", "-c",
                  f"rm -rf {remote} && mkdir -p {remote} && tar -x -C {remote}", stdin=bundle.read_bytes())
    script = (f"set -a; . /dev/stdin; set +a; cd {remote} && INVENTORY_DC_HERE=1 python3 "
              f"scripts/demos/linux/datacenter.py --request-file request.json --results {remote}/results.jsonl")
    env_lines = "".join(f"{key}={shlex.quote(value)}\n" for key, value in secrets.items())
    code = stream(["multipass", "exec", DC_AGENT, "--", "sudo", "-u", "jenkins", "-H", "bash", "-c", script],
                  os.environ, prefix, stdin=env_lines)
    raw = multipass("exec", DC_AGENT, "--", "sudo", "-u", "jenkins", "cat", f"{remote}/results.jsonl", check=False)
    return code, parse_results(raw.stdout.decode() if raw.returncode == 0 else "")


def multipass(*args, stdin=None, check=True):
    result = subprocess.run(["multipass", *args], input=stdin, capture_output=True)
    if check and result.returncode != 0:
        fail(f"multipass {' '.join(args[:3])}: {result.stderr.decode().strip()}")
    return result


# Both


def run(playbook, extra_vars, names=None, tags=None, secrets=None, stop_on_failure=True):
    """The servers in rollout order: the cloud first, then the data center. With
    stop_on_failure, a failed cloud server stops the rollout before the data center.
    Returns (ok, results)."""
    grouped = by_site(names)
    results = []
    ok = True
    for site, members in grouped.items():
        names_here = [s["name"] for s in members]
        if site == "cloud":
            code, found = run_cloud(playbook, extra_vars, names_here, tags)
        else:
            code, found = run_dc(playbook, extra_vars, names_here, tags, secrets)
        results += found
        if code != 0:
            ok = False
            if stop_on_failure:
                break
    return ok, results


def stream(command, env, prefix, cwd=None, stdin=None):
    """Run a command, printing its output line by line with a prefix. Returns its exit code."""
    process = subprocess.Popen(command, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL, text=True)
    if stdin is not None:
        process.stdin.write(stdin)
        process.stdin.close()
    for line in process.stdout:
        if line.strip():
            print(f"{prefix}{line.rstrip()}", flush=True)
    return process.wait()


def read_results(path):
    return parse_results(Path(path).read_text()) if Path(path).exists() else []


def parse_results(text):
    return [json.loads(line) for line in text.splitlines() if line.strip().startswith("{")]


class run_dir:
    """A scratch folder in the run's .release folder, which Docker can mount."""

    def __enter__(self):
        base = run_state.PATH.parent
        base.mkdir(parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix="ansible-", dir=base))
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + "\n")
