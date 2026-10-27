"""Stage 1, Build: build quote-service's container image, once, for linux/amd64.

    python scripts/demos/clouds/build.py quote-service [--jira-key ORD-60]

No Docker daemon is involved. The image is the pinned Python base image from
Amazon ECR Public plus two layers made here: the AWS Lambda Web Adapter, which
lets the same image run on Lambda, and the service's code. The same as:

    FROM <image.base in services.yaml>
    COPY --from=<image.adapter> /lambda-adapter /opt/extensions/lambda-adapter
    COPY services/quote-service/ /app/       (without tests)
    WORKDIR /app
    USER nobody
    CMD ["python3", "/app/server.py"]

One platform, linux/amd64, and no provenance or SBOM attestations: Lambda
rejects an image index. Layers have fixed timestamps and owners, so the same
source and commit give the same image digest.

Writes dist/<service>/<service>-<version>.zip, which holds the image as an OCI
image layout in image/ (so SBOM and scan reads it as the image), and
build-info.json with the zip's SHA-256 and the image digest.
The deploy stage pushes that exact image to JFrog and copies it by digest to
each cloud's registry.
"""

import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))  # scripts/, for the shared helpers, after this folder

from common import ROOT, fail, service, version_of, write_json  # noqa: E402
from registry import Layout, Registry, image_manifest, sha256  # noqa: E402

EXCLUDE_DIRS = {"tests", "__pycache__", ".pytest_cache"}
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
PLATFORM = "linux/amd64"
CACHE = Path(os.environ.get("OCI_CACHE") or Path.home() / ".cache" / "release-demo" / "oci")
LAYER_TYPES = {"application/vnd.oci.image.manifest.v1+json": "application/vnd.oci.image.layer.v1.tar+gzip",
               "application/vnd.docker.distribution.manifest.v2+json": "application/vnd.docker.image.rootfs.diff.tar.gzip"}


def split_reference(reference):
    """public.ecr.aws/docker/library/python:3.13@sha256:... to (host, repository, digest or tag)."""
    name, _, digest = reference.partition("@")
    host, _, rest = name.partition("/")
    repository, _, tag = rest.rpartition(":") if ":" in rest else (rest, "", "latest")
    return host, repository, digest or tag


class CachedRegistry:
    """Base image blobs by digest, kept between builds: a pinned image never changes."""

    def __init__(self, registry):
        self.registry = registry

    def blob(self, repository, digest):
        path = CACHE / digest.replace(":", "/")
        if path.exists() and sha256(path.read_bytes()) == digest:
            return path.read_bytes()
        data = self.registry.blob(repository, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data


def pull(reference):
    """(manifest, config, cached registry, repository) of a pinned image's linux/amd64 variant."""
    host, repository, ref = split_reference(reference)
    registry = Registry(host, name=host)
    _, body, _ = image_manifest(registry, repository, ref, PLATFORM)
    manifest = json.loads(body)
    cached = CachedRegistry(registry)
    config = json.loads(cached.blob(repository, manifest["config"]["digest"]))
    return manifest, config, cached, repository


def tar_layer(entries):
    """A layer from (path, bytes or None for a directory, mode) entries: fixed
    order, owner and time, so the same files give the same bytes."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for path, content, mode in sorted(entries):
            info = tarfile.TarInfo(path)
            info.mtime, info.uid, info.gid, info.uname, info.gname, info.mode = 0, 0, 0, "root", "root", mode
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    uncompressed = raw.getvalue()
    return gzip.compress(uncompressed, compresslevel=9, mtime=0), sha256(uncompressed)


def adapter_binary(reference):
    """The lambda-adapter file from the adapter's image."""
    manifest, _, registry, repository = pull(reference)
    for layer in manifest["layers"]:
        with tarfile.open(fileobj=io.BytesIO(registry.blob(repository, layer["digest"])), mode="r:*") as tar:
            for member in tar.getmembers():
                if member.isfile() and member.name.lstrip("./") == "lambda-adapter":
                    return tar.extractfile(member).read()
    fail(f"{reference} has no lambda-adapter file")


def app_entries(source, build_info):
    entries = [("app", None, 0o755)]
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if EXCLUDE_DIRS & set(relative.parts):
            continue
        name = "app/" + relative.as_posix()
        entries.append((name, None, 0o755) if path.is_dir() else (name, path.read_bytes(), 0o644))
    entries.append(("app/build_info.json", (json.dumps(build_info, indent=2) + "\n").encode(), 0o644))
    return entries


def image_config(base, layers, service_name, version, commit, commit_time, image):
    """The base image's config with the two new layers, the start command and labels."""
    config = json.loads(json.dumps(base))
    settings = config.setdefault("config", {})
    env = [e for e in settings.get("Env", []) if not e.startswith(("PORT=", "AWS_LWA_"))]
    settings["Env"] = env + [
        f"PORT={image['port']}",
        "AWS_LWA_READINESS_CHECK_PATH=/health",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONUNBUFFERED=1",
    ]
    settings.update({"Cmd": ["python3", "/app/server.py"], "Entrypoint": None, "WorkingDir": "/app",
                     "User": "nobody", "ExposedPorts": {f"{image['port']}/tcp": {}}})
    settings["Labels"] = {
        **(settings.get("Labels") or {}),
        "org.opencontainers.image.title": service_name,
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": commit,
        "org.opencontainers.image.source": "https://github.com/m-atek/release-demo",
        "org.opencontainers.image.description": "Demo system, not a client's.",
    }
    config["created"] = commit_time
    config["rootfs"]["diff_ids"] += [diff_id for _, diff_id, _ in layers]
    config["history"] = config.get("history", []) + [
        {"created": commit_time, "created_by": comment} for _, _, comment in layers]
    return config


def current_commit():
    """Jenkins sets GIT_COMMIT. Locally, HEAD, marked when the service has local changes."""
    if os.environ.get("GIT_COMMIT"):
        return os.environ["GIT_COMMIT"]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    if head.returncode != 0:
        return "uncommitted"
    return head.stdout.strip()


def commit_time(commit):
    """The commit's time, as the image's creation time: the same commit, the same image."""
    result = subprocess.run(["git", "log", "-1", "--format=%ct", commit], cwd=ROOT, capture_output=True, text=True)
    seconds = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_image(config, source, version, commit, created, out_dir):
    """Write the image as an OCI layout in out_dir. Returns (digest, size in bytes)."""
    image = config["image"]
    base_manifest, base_config, base_registry, base_repository = pull(image["base"])
    layout = Layout(out_dir)
    for descriptor in base_manifest["layers"]:
        layout.write_blob(base_registry.blob(base_repository, descriptor["digest"]))

    adapter, adapter_diff = tar_layer([("opt", None, 0o755), ("opt/extensions", None, 0o755),
                                       ("opt/extensions/lambda-adapter", adapter_binary(image["adapter"]), 0o755)])
    build_info = {"service": config["name"], "version": version, "commit": commit}
    app, app_diff = tar_layer(app_entries(source, build_info))
    layers = [(adapter, adapter_diff, f"COPY lambda-adapter /opt/extensions/ # {image['adapter'].split('@')[0]}"),
              (app, app_diff, f"COPY {config['path']}/ /app/ # {config['name']} {version}")]

    layer_type = LAYER_TYPES[base_manifest["mediaType"]]
    new_config = json.dumps(image_config(base_config, layers, config["name"], version, commit, created, image),
                            indent=None, separators=(",", ":"), sort_keys=True).encode()
    manifest = {
        "schemaVersion": 2,
        "mediaType": base_manifest["mediaType"],
        "config": {"mediaType": base_manifest["config"]["mediaType"], "digest": layout.write_blob(new_config),
                   "size": len(new_config)},
        "layers": base_manifest["layers"] + [
            {"mediaType": layer_type, "digest": layout.write_blob(data), "size": len(data)} for data, _, _ in layers],
    }
    body = json.dumps(manifest, indent=2).encode()
    digest = layout.put_manifest("", version, manifest["mediaType"], body)
    size = len(new_config) + sum(layer["size"] for layer in manifest["layers"])
    return digest, size


def write_zip(directory, destination):
    """The layout's files under image/, in a fixed order with fixed times.
    Stored, not compressed again: the layers are gzip already."""
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as archive:
        for path in sorted(p for p in Path(directory).rglob("*") if p.is_file()):
            info = zipfile.ZipInfo("image/" + path.relative_to(directory).as_posix(), ZIP_TIME)
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("--jira-key", default=None)
    parser.add_argument("--source", type=Path, help="build this folder instead of the service's, such as a demo state")
    parser.add_argument("--commit", help="the commit to record, instead of HEAD")
    parser.add_argument("--out", type=Path, default=Path(os.environ.get("WORKSPACE", ROOT)) / "dist")
    args = parser.parse_args()

    config = service(args.service)
    source = args.source or config["dir"]
    if not (source / "VERSION").exists():
        fail(f"{source}/VERSION is missing. Stage a version first: scripts/stage.py {args.service} 1.1.0")
    version = version_of(source)
    commit = args.commit or current_commit()

    out = args.out / args.service
    out.mkdir(parents=True, exist_ok=True)
    package = out / f"{args.service}-{version}.zip"
    with tempfile.TemporaryDirectory() as temp:
        digest, size = build_image(config, source, version, commit, commit_time(commit), Path(temp) / "image")
        write_zip(Path(temp) / "image", package)

    info = {
        "service": args.service,
        "version": version,
        "commit": commit,
        "jira_key": args.jira_key,
        "build_url": os.environ.get("BUILD_URL"),
        "package": package.name,
        "sha256": file_sha256(package),
        "size_bytes": package.stat().st_size,
        "image": {"digest": digest, "platform": PLATFORM, "size_bytes": size,
                  "base": config["image"]["base"], "adapter": config["image"]["adapter"]},
    }
    write_json(out / "build-info.json", info)
    print(f"Built {args.service} {version} for {PLATFORM}: image {digest}")
    print(f"  base    {config['image']['base']}")
    print(f"  adapter {config['image']['adapter']}")
    print(f"Package {package.name}, sha256 {info['sha256']}")


if __name__ == "__main__":
    main()
