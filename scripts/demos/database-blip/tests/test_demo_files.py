"""What video 25 adds around the check: the database, the block, the two versions, the role,
the stage in the pipeline, the bundle that carries it, and the windows the take films."""

import base64
import difflib
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest
import yaml

import lines
import postgres
import run_state
from common import ROOT, service

HERE = Path(__file__).resolve().parents[1]
STATES = ROOT / "demo-states" / "catalog"
LABEL = {"release-demo/demo": "database-blip"}


def documents(path):
    return [document for document in yaml.safe_load_all(Path(path).read_text()) if document]


def manifest():
    return {document["kind"]: document for document in documents(HERE / "catalog-postgres.yaml")}


def kube_host():
    """The Kubernetes demo's host.py, which scripts/ has a namesake of."""
    spec = importlib.util.spec_from_file_location("kube_host", ROOT / "scripts/demos/kubernetes/host.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The database.

def test_every_object_carries_the_label_and_the_pods_do_too():
    found = manifest()
    assert set(found) == {"Service", "StatefulSet"}
    for document in found.values():
        assert document["metadata"]["name"] == "catalog-postgres"
        assert LABEL.items() <= document["metadata"]["labels"].items()
    assert LABEL.items() <= found["StatefulSet"]["spec"]["template"]["metadata"]["labels"].items()


def test_it_never_counts_as_a_catalog_pod():
    """kube.Api.pods() selects instance catalog, and the Service selects only the database."""
    found = manifest()
    labels = found["StatefulSet"]["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/instance"] == labels["app.kubernetes.io/name"] == "catalog-postgres"
    assert found["Service"]["spec"]["selector"] == found["StatefulSet"]["spec"]["selector"]["matchLabels"]
    assert found["Service"]["spec"]["type"] == "ClusterIP"
    assert found["Service"]["spec"]["ports"][0]["port"] == 5432


def test_one_pod_with_a_short_name_on_a_pinned_image():
    spec = manifest()["StatefulSet"]["spec"]
    assert spec["replicas"] == 1 and spec["serviceName"] == "catalog-postgres"
    container = spec["template"]["spec"]["containers"][0]
    assert re.fullmatch(r"docker\.io/library/postgres:17\.\d+-alpine@sha256:[0-9a-f]{64}", container["image"])
    assert container["args"] == ["-c", "shared_buffers=32MB", "-c", "max_connections=40"]
    assert container["resources"] == {"requests": {"cpu": "50m", "memory": "64Mi"},
                                      "limits": {"cpu": "500m", "memory": "192Mi"}}


def test_it_passes_pod_security_baseline_and_more():
    template = manifest()["StatefulSet"]["spec"]["template"]["spec"]
    assert template["automountServiceAccountToken"] is False
    security = template["securityContext"]
    assert (security["runAsUser"], security["fsGroup"], security["runAsNonRoot"]) == (70, 70, True)
    assert security["seccompProfile"] == {"type": "RuntimeDefault"}
    container = template["containers"][0]["securityContext"]
    assert container == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
    assert not {"hostNetwork", "hostPID", "hostIPC"} & set(template)


def test_its_folders_are_empty_dirs_and_it_seeds_itself():
    template = manifest()["StatefulSet"]["spec"]["template"]["spec"]
    volumes = {volume["name"]: volume for volume in template["volumes"]}
    mounts = {mount["mountPath"]: mount["name"] for mount in template["containers"][0]["volumeMounts"]}
    assert {path: mounts[path] for path in ("/var/lib/postgresql/data", "/var/run/postgresql", "/tmp")} == {
        "/var/lib/postgresql/data": "data", "/var/run/postgresql": "run", "/tmp": "tmp"}
    for name in ("data", "run", "tmp"):
        assert volumes[name] == {"name": name, "emptyDir": {}}
    assert mounts["/docker-entrypoint-initdb.d"] == "seed"
    assert volumes["seed"]["configMap"]["name"] == "catalog-postgres-seed"


def test_its_data_folder_is_inside_the_mount_because_initdb_must_own_it():
    env = {item["name"]: item for item in manifest()["StatefulSet"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["PGDATA"]["value"].startswith("/var/lib/postgresql/data/")
    assert (env["POSTGRES_USER"]["value"], env["POSTGRES_DB"]["value"]) == ("catalog", "catalog")
    assert env["POSTGRES_PASSWORD"] == {"name": "POSTGRES_PASSWORD",
                                        "valueFrom": {"secretKeyRef": {"name": "catalog-postgres", "key": "password"}}}


def test_only_a_readiness_probe_and_it_is_an_exec_probe_over_tcp():
    container = manifest()["StatefulSet"]["spec"]["template"]["spec"]["containers"][0]
    assert "livenessProbe" not in container
    command = container["readinessProbe"]["exec"]["command"]
    assert command[0] == "pg_isready" and "127.0.0.1" in command


def seed_rows(version="1.10.0"):
    return re.findall(r"\('(P-\d+)', '(.+?)', '(.+?)', ([\d.]+)\)", (STATES / version / "database" / "seed.sql").read_text())


def test_the_seed_holds_the_files_24_products_and_not_its_note():
    catalog = json.loads((STATES / "1.9.1" / "data" / "catalog.json").read_text())
    rows = seed_rows()
    assert [(p["id"], p["name"], p["supplier"], f"{p['price_eur']:.2f}") for p in catalog["products"]] == rows
    assert len(rows) == 24
    text = (STATES / "1.10.0" / "database" / "seed.sql").read_text()
    assert catalog["note"] not in text and "Demo system" not in text
    assert "products (\n  id " in text


def test_both_versions_hold_the_same_seed_and_the_same_driver_pins():
    for name in ("database/seed.sql", "requirements.txt"):
        assert (STATES / "1.10.0" / name).read_bytes() == (STATES / "1.10.1" / name).read_bytes()
    pins = (STATES / "1.10.0" / "requirements.txt").read_text()
    demo_20 = (ROOT / "demo-states" / "orders-service" / "1.6.0" / "requirements.txt").read_text()
    wanted = [line for line in demo_20.splitlines() if "==" in line]
    assert wanted == [line for line in pins.splitlines() if "==" in line]


class Kubectl:
    """postgres.py's k3s.kubectl: records each call and answers what the script asks."""

    def __init__(self, secret=False, restarts=0):
        self.calls, self.secret, self.restarts = [], secret, restarts

    def __call__(self, *args, check=True, stdin=None):
        self.calls.append((args, stdin))
        if args[:2] == ("get", "secret"):
            return "secret/catalog-postgres" if self.secret else ""
        if args[:2] == ("get", "pod"):
            return json.dumps({"status": {"containerStatuses": [{"ready": True, "restartCount": self.restarts}]}})
        if args[0] == "exec":
            return "24\n"
        if args[:2] == ("delete", "pod"):
            self.restarts = 0
        return ""


@pytest.fixture
def cluster(monkeypatch):
    fake = Kubectl()
    monkeypatch.setattr(postgres.k3s, "kubectl", fake)
    monkeypatch.setattr(postgres.k3s, "wait", lambda what, check, timeout: None)
    monkeypatch.setattr(postgres, "running", lambda: True)
    return fake


def test_up_makes_the_password_once_and_prints_it_nowhere(cluster, capsys):
    postgres.up()
    posted = [json.loads(stdin) for _, stdin in cluster.calls if stdin]
    secrets = [document for document in posted if document["kind"] == "Secret"]
    assert len(secrets) == 2 and secrets[0]["stringData"]["password"] != secrets[1]["stringData"]["password"]
    assert len(secrets[0]["stringData"]["password"]) >= 32
    output = capsys.readouterr().out
    for secret in secrets:
        assert secret["stringData"]["password"] not in output
        assert LABEL.items() <= secret["metadata"]["labels"].items()
    assert not [args for args, _ in cluster.calls if any(secrets[0]["stringData"]["password"] in str(a) for a in args)]
    assert output.rstrip().endswith("Ready") and "24 products" in output


def test_up_leaves_an_existing_password_alone_and_seeds_from_the_state(cluster):
    cluster.secret = True
    postgres.up()
    posted = [json.loads(stdin) for _, stdin in cluster.calls if stdin]
    assert not [document for document in posted if document["kind"] == "Secret"]
    seeds = [document for document in posted if document["kind"] == "ConfigMap"]
    assert len(seeds) == 2
    assert seeds[0]["metadata"]["name"] == "catalog-postgres-seed"
    assert seeds[0]["data"]["seed.sql"] == (STATES / "1.10.0" / "database" / "seed.sql").read_text()
    assert LABEL.items() <= seeds[0]["metadata"]["labels"].items()
    namespaces = {args[2] for args, stdin in cluster.calls if args[0] == "apply"}
    assert namespaces == {"staging", "production"}


def test_a_k3s_restart_gets_the_database_a_new_pod_so_its_row_reads_0(cluster, capsys):
    cluster.restarts = 1
    postgres.up()
    assert ("delete", "pod", "catalog-postgres-0", "--namespace", "staging") in [args for args, _ in cluster.calls]
    assert "a new pod, seeded again" in capsys.readouterr().out


def test_up_says_when_the_cluster_isnt_up(monkeypatch):
    monkeypatch.setattr(postgres, "running", lambda: False)
    with pytest.raises(SystemExit, match="scripts/demo.sh up kubernetes"):
        postgres.up()


def test_down_goes_by_the_label_alone(cluster, capsys):
    postgres.down()
    removed = [args for args, _ in cluster.calls if args[0] == "delete"]
    assert ("delete", "statefulset,service,configmap,secret", "--namespace", "staging", "--selector",
            "release-demo/demo=database-blip", "--ignore-not-found") in removed
    assert ("delete", "networkpolicy", "block-database", "--namespace", "staging", "--ignore-not-found") in removed
    assert len(removed) == 4
    assert "this went by the label alone" in capsys.readouterr().out


def test_down_asks_first():
    script = (HERE / "down.sh").read_text()
    assert 'read -r -p "Remove catalog-postgres' in script and '[[ $answer == yes ]]' in script
    # Nothing touches the cluster before the answer.
    assert script.index("[[ $answer == yes ]]") < script.index('host.py"')


def test_the_scripts_are_executable_for_demo_sh():
    for name in ("up.sh", "down.sh", "status.sh", "pods.sh", "reveal.sh"):
        assert (HERE / name).stat().st_mode & stat.S_IXUSR, name
        subprocess.run(["bash", "-n", str(HERE / name)], check=True)


# The block.

def test_the_block_closes_every_way_in_to_the_database():
    block = documents(HERE / "block.yaml")[0]
    assert block["kind"] == "NetworkPolicy"
    assert block["metadata"] == {"name": "block-database", "namespace": "staging"}
    assert block["spec"] == {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "catalog-postgres"}},
                             "policyTypes": ["Ingress"], "ingress": []}


# The role.

def cluster_documents():
    return documents(ROOT / "scripts/demos/kubernetes/host/cluster.yaml")


def test_the_role_may_write_only_the_policy_and_only_in_staging():
    roles = [document for document in cluster_documents() if document["metadata"]["name"] == "blip-check"]
    role = next(document for document in roles if document["kind"] == "Role")
    binding = next(document for document in roles if document["kind"] == "RoleBinding")
    assert role["metadata"]["namespace"] == binding["metadata"]["namespace"] == "staging"
    assert role["rules"] == [
        {"apiGroups": ["networking.k8s.io"], "resources": ["networkpolicies"], "verbs": ["create"]},
        {"apiGroups": ["networking.k8s.io"], "resources": ["networkpolicies"], "resourceNames": ["block-database"],
         "verbs": ["get", "delete"]}]
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "blip-check"}
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "release-pipeline", "namespace": "jenkins"}]
    assert len(roles) == 2


def test_nothing_else_grants_network_policies():
    for document in cluster_documents():
        if document["kind"] in ("Role", "ClusterRole") and document["metadata"]["name"] != "blip-check":
            assert "networkpolicies" not in json.dumps(document), document["metadata"]["name"]


# The stage.

def stages(version, name="catalog"):
    return run_state.stages_for({**service(name), "dir": STATES / version})


@pytest.mark.parametrize("version", ["1.8.2", "1.9.0", "1.9.1"])
def test_earlier_catalog_versions_keep_their_twelve_stages(version):
    assert stages(version) == run_state.STAGES and len(run_state.STAGES) == 12


@pytest.mark.parametrize("version", ["1.10.0", "1.10.1"])
def test_the_postgres_versions_have_the_blip_check_between_check_staging_and_change(version):
    found = stages(version)
    assert found[found.index("Check staging") + 1] == "Blip check"
    assert found[found.index("Blip check") + 1] == "Change"
    assert [stage for stage in found if stage != "Blip check"] == run_state.STAGES


def test_orders_service_never_has_it_even_with_a_database_folder(tmp_path):
    (tmp_path / "database").mkdir()
    assert "Blip check" not in run_state.stages_for({**service("orders-service"), "dir": tmp_path})
    assert "Blip check" not in run_state.stages_for({**service("orders-service"),
                                                     "dir": ROOT / "demo-states/orders-service/1.4.0"})


def test_without_the_script_key_no_version_has_the_stage():
    """The switch for the spec's fallback: one line in services.yaml."""
    config = service("catalog")
    del config["scripts"]["blip_check"]
    assert run_state.stages_for({**config, "dir": STATES / "1.10.0"}) == run_state.STAGES


def test_services_yaml_says_how_the_check_runs():
    config = service("catalog")
    assert config["scripts"]["blip_check"] == "scripts/demos/database-blip/blip_check.py"
    assert config["blip_check"] == {"environment": "staging", "seconds": 30, "calls_per_second": 10,
                                    "data_route": "/api/products", "alive_route": "/health", "recover_seconds": 60}
    assert config["environments"]["staging"]["replicas"] == 3 == config["environments"]["production"]["replicas"]
    # Video 8's 1.9.0 still needs staging's old setting; 1.10.x read the new one and ignore it.
    assert "CATALOG_DB_URL" in config["environments"]["staging"]["settings"]


def test_the_pipeline_runs_it_before_the_change_and_archives_what_it_wrote():
    text = (ROOT / "jenkins/shared-library/vars/releasePipeline.groovy").read_text()
    stage = text.index("if (planned('Blip check')) {")
    assert text.index("releaseStage('Evaluation'") < stage < text.index("if (cfg.scripts.plan)") < text.index("releaseStage('Change')")
    block = text[stage:text.index("if (cfg.scripts.plan)")]
    assert "serviceConfig.command(cfg, 'blip_check')" in block
    assert "archiveArtifacts artifacts: '.release/blip.json', allowEmptyArchive: true" in block
    assert "finally" in block and "environment: 'staging'" in block


def test_sonar_leaves_the_seed_out():
    exclusions = service("catalog")["sonarqube"]["properties"]["sonar.exclusions"]
    assert exclusions == "services/catalog/tests/**,services/catalog/database/**"


# The two versions.

def source(version, name):
    return (STATES / version / name).read_text()


def changed(before, after, name):
    return [line for line in difflib.unified_diff(source(before, name).splitlines(), source(after, name).splitlines(),
                                                  lineterm="", n=0)
            if line[:1] in "+-" and not line.startswith(("+++", "---"))]


def test_against_1_9_1_the_only_probe_change_is_the_one_line():
    probes = [line for line in changed("1.9.1", "1.10.0", "chart/templates/deployment.yaml")
              if "Probe" in line or "httpGet" in line or "Seconds" in line or "failureThreshold" in line or "preStop" in line]
    assert probes == ["-            httpGet: { path: /health, port: http }", "+            httpGet: { path: /ready, port: http }"]


def test_1_10_1_changes_the_chart_by_one_line():
    assert changed("1.10.0", "1.10.1", "chart/templates/deployment.yaml") == [
        "-            httpGet: { path: /ready, port: http }", "+            httpGet: { path: /health, port: http }"]


def test_ready_reads_as_the_video_shows_it():
    assert re.search(r'def ready\(\):\n    """Ready while the catalog database answers\."""\n    return ask_database\(\)\n',
                     source("1.10.0", "app.py"))
    assert re.search(r'def ready\(\):\n    """Ready once the catalog has loaded\. Never asks again\."""\n    return LOADED\.is_set\(\)\n',
                     source("1.10.1", "app.py"))


def test_1_10_1_changes_nothing_else_that_bears_on_the_block():
    lines_ = changed("1.10.0", "1.10.1", "app.py")
    code = [line for line in lines_ if line[1:].strip() and not line[1:].startswith(("environment,", "/ready", "and the", "with the",
                                                                                     "searchable", "answer 503"))]
    assert code == ["+import time",
                    "+LOADED = threading.Event()  # set once, when this pod's first query succeeds",
                    "+def load():",
                    '+    """A start-up thread: until the catalog database first answers, this pod waits."""',
                    "+    while not ask_database():",
                    "+        time.sleep(1)",
                    "+    LOADED.set()",
                    '-    """Ready while the catalog database answers."""',
                    "-    return ask_database()",
                    '+    """Ready once the catalog has loaded. Never asks again."""',
                    "+    return LOADED.is_set()",
                    "-    print(f\"{SERVICE} {VERSION} in {environment()} on :{port}, {'ready' if ready() else 'not ready'}\", flush=True)",
                    "+    threading.Thread(target=load, daemon=True).start()",
                    '+    print(f"{SERVICE} {VERSION} in {environment()} on :{port}", flush=True)']
    for name in ("chart/values.yaml", "chart/templates/service.yaml", "chart/templates/_helpers.tpl", "image.json"):
        assert source("1.10.0", name) == source("1.10.1", name)


def test_the_new_setting_and_no_file_to_fall_back_on():
    values = yaml.safe_load(source("1.10.0", "chart/values.yaml"))
    assert values["settings"] == {"CATALOG_POSTGRES_URL": "postgresql://catalog@catalog-postgres:5432/catalog"}
    assert values["resources"]["requests"]["memory"] == "48Mi" and values["resources"]["limits"]["memory"] == "96Mi"
    for version in ("1.10.0", "1.10.1"):
        assert not (STATES / version / "data").exists()
        assert "CATALOG_DB_URL" not in source(version, "app.py") and "catalog.json" not in source(version, "app.py")
        assert json.loads(source(version, "image.json"))["include"] == ["app.py", "VERSION"]
        assert "import pg8000" not in source(version, "app.py").split("def connect")[0]


# The bundle.

def folders_of(script):
    return re.findall(r"--folder (\S+)", (HERE / script).read_text())


@pytest.mark.parametrize("script", ["up.sh", "down.sh", "status.sh"])
def test_what_each_command_sends_fits_ssms_limit(script):
    host = kube_host()
    size = len(host.bundle(folders=folders_of(script)))
    assert size <= host.LIMIT, f"{script}: {size} bytes against {host.LIMIT}"


def test_up_sends_the_seed_and_no_whole_state():
    host = kube_host()
    names = tarfile.open(fileobj=io.BytesIO(base64.b64decode(host.bundle(folders=folders_of("up.sh"))))).getnames()
    assert "demo-states/catalog/1.10.0/database/seed.sql" in names
    assert "scripts/demos/database-blip/catalog-postgres.yaml" in names
    assert "demo-states/catalog/1.10.0/app.py" not in names


def test_the_database_script_starts_from_the_bundle_alone(tmp_path):
    host = kube_host()
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(host.bundle(folders=folders_of("up.sh"))))) as archive:
        archive.extractall(tmp_path, filter="data")
    reply = subprocess.run([sys.executable, "scripts/demos/database-blip/postgres.py", "status"], cwd=tmp_path,
                           capture_output=True, text=True, env={**os.environ, "PYTHONPATH": ""})
    assert "ModuleNotFoundError" not in reply.stderr
    assert "Run as root" in reply.stderr or os.geteuid() == 0


# The windows the take films.

def test_lines_colours_a_seconds_results_and_nothing_else():
    green, amber, red, end = "\033[32m", "\033[38;5;214m", "\033[31m", "\033[0m"
    assert lines.coloured("0:01  503 x10           200 x10") == f"0:01  {amber}503 x10{end}           {green}200 x10{end}"
    assert lines.coloured("0:11  refused x10       refused x10") == f"0:11  {red}refused x10{end}       {red}refused x10{end}"
    assert lines.coloured("-0:01 200 x10           200 x10").startswith(f"-0:01 {green}200 x10{end}")
    for plain in ("catalog 1.10.0, staging, 10 calls a second", "TIME  /api/products     /health",
                  "NetworkPolicy block-database: no way in", "0:00  database blocked", "0:30  database back",
                  "0:09  all 3 pods not ready", "0:27  4xk2p: failed liveness, restarting",
                  "Back at 0:38. 3 of 3 pods restarted."):
        assert lines.coloured(plain) == plain


def test_lines_shows_the_session_without_its_noise(capsys):
    class Process:
        stdout = ["Starting session with SessionId: abc\r\n", "\r\n", "== reset 2026\r\n", "0:00  database blocked\r\n",
                  "Exiting session with sessionId: abc\r\n"]
        terminated = False

        def terminate(self):
            self.terminated = True

    process = Process()
    made = []

    def popen(command, **kwargs):
        made.append(command)
        return process

    class Host:
        id = "i-123"

    assert lines.stream(Host, 1200, popen=popen) is True
    assert capsys.readouterr().out == "0:00  database blocked\n"
    assert process.terminated
    assert "AWS-StartInteractiveCommand" in made[0]
    assert f"{lines.LOG} 1200" in json.loads(made[0][made[0].index("--parameters") + 1])["command"][0]


def fake_bin(folder, name, text):
    path = folder / name
    path.write_text(f"#!/bin/sh\n{text}\n")
    path.chmod(0o755)


def test_the_top_window_is_real_kubectl_redrawn_in_place(tmp_path):
    fake_bin(tmp_path, "kubectl", f'echo "$@" > {tmp_path}/asked\nprintf "POD  READY  RESTARTS\\ncatalog-postgres-0  true  0\\n"')
    fake_bin(tmp_path, "clear", "true")
    process = subprocess.Popen(["bash", str(HERE / "pods.sh")], stdout=subprocess.PIPE, text=True,
                               env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "KUBECONFIG": "/dev/null"})
    time.sleep(0.5)
    process.terminate()
    output = process.communicate(timeout=5)[0]
    asked = (tmp_path / "asked").read_text()
    assert asked.strip() == ("-n staging get pods -o custom-columns=POD:.metadata.name,READY:.status.containerStatuses[0].ready,"
                             "RESTARTS:.status.containerStatuses[0].restartCount")
    assert "\033[HPOD  READY  RESTARTS\033[K\ncatalog-postgres-0  true  0\033[K\033[J" in output


@pytest.fixture
def stage_repository(tmp_path):
    """A clone with two stage commits of catalog and a copy of reveal.sh where it expects to live."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    run = lambda *args, cwd=work: subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
    subprocess.run(["git", "init", "--quiet", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], check=True, capture_output=True)
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    script = work / "scripts/demos/database-blip/reveal.sh"
    script.parent.mkdir(parents=True)
    script.write_text((HERE / "reveal.sh").read_text())
    script.chmod(0o755)
    target = work / "services/catalog"
    target.parent.mkdir()
    for version in ("1.10.0", "1.10.1"):
        if target.exists():
            subprocess.run(["rm", "-rf", str(target)], check=True)
        subprocess.run(["cp", "-R", str(STATES / version), str(target)], check=True)
        run("add", "--all")
        run("commit", "--quiet", "-m", f"demo: stage catalog {version} [skip ci]")
    run("push", "--quiet", "origin", "HEAD")
    run("branch", "--set-upstream-to", f"origin/{subprocess.run(['git', 'branch', '--show-current'], cwd=work, capture_output=True, text=True).stdout.strip()}")
    return work


def reveal(work, which, enters, tmp_path):
    fake_bin(tmp_path, "clear", "true")
    return subprocess.run(["bash", str(work / "scripts/demos/database-blip/reveal.sh"), which], input="\n" * enters, text=True,
                          capture_output=True, env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"})


def test_reveal_b_shows_the_two_pairs_as_the_script_outlines_them(stage_repository, tmp_path):
    reply = reveal(stage_repository, "b", 2, tmp_path)
    assert reply.returncode == 0, reply.stderr
    shown = reply.stdout.splitlines()
    assert shown[0] == "$ git diff $BEFORE -U0 -- $CHART app.py | pairs"
    assert shown[1:5] == ["-  httpGet: { path: /ready, port: http }", "+  httpGet: { path: /health, port: http }",
                          "-    return ask_database()", "+    return LOADED.is_set()"]
    assert "demo-states" not in reply.stdout
    assert max(len(line) for line in shown) <= 56


def test_reveal_a_shows_the_liveness_line_then_ready_each_on_its_enter(stage_repository, tmp_path):
    run = lambda *args: subprocess.run(["git", *args], cwd=stage_repository, check=True, capture_output=True)
    run("reset", "--quiet", "--hard", "HEAD~1")
    run("push", "--quiet", "--force", "origin", "HEAD")
    reply = reveal(stage_repository, "a", 3, tmp_path)
    assert reply.returncode == 0, reply.stderr
    assert reply.stdout.splitlines() == [
        "$ grep -A1 livenessProbe $CHART | sed 's/^ *//'", "livenessProbe:", "httpGet: { path: /ready, port: http }", "",
        "$ grep -A2 'def ready' app.py | sed 's/^ *//'", "def ready():", '"""Ready while the catalog database answers."""',
        "return ask_database()", ""]
    assert "demo-states" not in reply.stdout
    assert max(len(line) for line in reply.stdout.splitlines()) <= 56


def test_no_em_or_en_dash_in_anything_this_demo_adds():
    folders = [HERE, STATES / "1.10.0", STATES / "1.10.1"]
    paths = [path for folder in folders for path in folder.rglob("*") if path.is_file() and path.suffix != ".pyc"]
    for path in paths:
        text = path.read_text()
        assert chr(0x2014) not in text and chr(0x2013) not in text, path
