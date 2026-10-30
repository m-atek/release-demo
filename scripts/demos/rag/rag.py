"""What demo 23's scripts share: the service's settings, the chunk hash that
names a version's table and index, the environments' pointers in SSM, and a
package fetched from JFrog and opened in a cache, checked by its SHA-256.

A pointer is SSM's runbook-assistant/staging or /production, as JSON:
{version, sha256, package, index, build, at}. The answer path reads it to
know which package and index an environment runs; deploy.py writes it,
through the deploy role, which the tools host's own role may assume.
"""

import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE.parents[1]))
from common import REGION, ROOT, aws_session, fail, get_parameter, service, ssm_client  # noqa: E402

SERVICE = "runbook-assistant"
POINTER_PREFIX = "/release-demo/"
ENDPOINT_DOWN = "The search endpoint is down: scripts/demo.sh up rag."
# What the index keeps of each chunk, and the retriever may ask for.
INDEX_COLUMNS = ["chunk_id", "page_id", "title", "kind", "status", "replaced_by", "heading", "text"]


def config():
    return service(SERVICE)


def databricks_settings():
    return config()["databricks"]


def catalog(ssm=None):
    found = get_parameter(ssm or ssm_client(), "databricks/catalog")
    if not re.fullmatch(r"[A-Za-z0-9_]+", found):
        fail(f"databricks/catalog in SSM isn't a plain catalog name: {found!r}")
    return found


# The chunk hash: one table and one index per way of chunking the corpus.

def chunk_files(folder):
    folder = Path(folder)
    files = [folder / "chunking.yaml", folder / "chunker.py"]
    files += sorted(p for p in (folder / "corpus").rglob("*") if p.is_file())
    return files


def chunk_hash(folder, embedding_model=None):
    """SHA-256 of chunking.yaml, chunker.py, the corpus and the embedding model's name."""
    folder = Path(folder)
    digest = hashlib.sha256()
    for path in chunk_files(folder):
        digest.update(path.relative_to(folder).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    digest.update((embedding_model or databricks_settings()["embedding_model"]).encode())
    return digest.hexdigest()


def table_name(chunk, ssm=None, catalog_name=None):
    return f"{catalog_name or catalog(ssm)}.{databricks_settings()['schema']}.chunks_{chunk[:8]}"


def index_name(chunk, ssm=None, catalog_name=None):
    return table_name(chunk, ssm, catalog_name) + "_index"


# Pointers

def pointer_name(environment):
    return config()["environments"][environment]["pointer"]


def read_pointer(environment, ssm=None):
    """The environment's pointer, or None while it is unset."""
    ssm = ssm or ssm_client()
    try:
        value = ssm.get_parameter(Name=POINTER_PREFIX + pointer_name(environment))["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        fail(f"No {POINTER_PREFIX}{pointer_name(environment)} in SSM: apply infra/demos/23-databricks-rag first.")
    return None if value == "unset" else json.loads(value)


def deploy_ssm():
    """SSM in the deploy role, which may write and read the history of the two pointers only."""
    import boto3

    base = aws_session()
    sts = base.client("sts")
    account = sts.get_caller_identity()["Account"]
    role = databricks_settings()["deploy_role"]
    name = f"{os.environ.get('JOB_BASE_NAME', 'mac')}-{os.environ.get('BUILD_NUMBER', 'local')}"
    creds = sts.assume_role(RoleArn=f"arn:aws:iam::{account}:role/{role}",
                            RoleSessionName=re.sub(r"[^\w+=,.@-]", "-", name)[:64])["Credentials"]
    return boto3.client("ssm", region_name=REGION, aws_access_key_id=creds["AccessKeyId"],
                        aws_secret_access_key=creds["SecretAccessKey"], aws_session_token=creds["SessionToken"])


def write_pointer(environment, pointer, ssm=None):
    (ssm or deploy_ssm()).put_parameter(Name=POINTER_PREFIX + pointer_name(environment), Type="String",
                                        Value=json.dumps(pointer, sort_keys=True), Overwrite=True)


def pointer_history(environment, ssm=None):
    """Every value the pointer had, oldest first, as dicts (unset ones left out)."""
    ssm = ssm or deploy_ssm()
    found, token = [], None
    while True:
        reply = ssm.get_parameter_history(Name=POINTER_PREFIX + pointer_name(environment), MaxResults=50,
                                          **({"NextToken": token} if token else {}))
        found += [json.loads(p["Value"]) for p in reply["Parameters"] if p["Value"] != "unset"]
        token = reply.get("NextToken")
        if not token:
            return found


def previous_pointer(history):
    """The last pointer before the current one with another version: what /rollback goes back to."""
    if not history:
        return None
    current = history[-1]
    for pointer in reversed(history[:-1]):
        if pointer["version"] != current["version"]:
            return pointer
    return None


# Packages

def cache_dir():
    path = Path(os.environ.get("RELEASE_DEMO_CACHE") or ROOT / ".local" / "cache") / SERVICE
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch(path, sha256, token=None):
    """The package from JFrog, checked by its SHA-256, opened in the cache. Returns its folder."""
    folder = cache_dir() / sha256
    if (folder / ".verified").exists():
        return folder
    import publish

    token = token or get_parameter(ssm_client(), "jcr/token")
    package = publish.download(token, path)
    actual = hashlib.sha256(package).hexdigest()
    if actual != sha256:
        fail(f"{path} has SHA-256 {actual}, but the pointer says {sha256}. Not using a package that changed.")
    unpack(package, folder)
    return folder


def unpack(package, folder):
    partial = folder.with_name(folder.name + f".{os.getpid()}.partial")
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        archive.extractall(partial)
    (partial / ".verified").write_text(f"{time.time()}\n")
    try:
        partial.rename(folder)
    except OSError:  # another process opened the same package first
        pass
    return folder


def load(folder):
    """The package's assistant: its retriever module, prompt, schema and settings."""
    import yaml

    folder = Path(folder)
    spec = importlib.util.spec_from_file_location(f"retriever_{folder.name[:12]}", folder / "assistant" / "retriever.py")
    retriever = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(retriever)
    return {"retriever": retriever, "prompt": (folder / "assistant" / "prompt.md").read_text(),
            "schema": json.loads((folder / "assistant" / "schema.json").read_text()),
            "settings": yaml.safe_load((folder / "assistant.yaml").read_text()),
            "version": (folder / "VERSION").read_text().strip(), "folder": folder}


def pages_of(folder):
    """The package's pages by id, as its own chunker reads them: front matter and body."""
    folder = Path(folder)
    spec = importlib.util.spec_from_file_location(f"chunker_{folder.name[:12]}", folder / "chunker.py")
    chunker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chunker)
    return {page["id"]: page for page in chunker.pages(folder / "corpus")}


def questions():
    """The question set and its SHA-256."""
    import yaml

    path = ROOT / config()["evaluation"]["questions"]
    data = path.read_bytes()
    return yaml.safe_load(data)["questions"], hashlib.sha256(data).hexdigest()


def smoke_questions(count):
    return [q for q in questions()[0] if q.get("smoke")][:count]
