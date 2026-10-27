"""Stage 1 for Kubernetes, Build: the catalog image and its Helm chart, in one package.

    python scripts/demos/kubernetes/build.py catalog [--jira-key ORD-90]

Assembles the container image from the pinned base in image.json plus one
layer with the service's files, and packages the chart. Both are OCI image
layouts inside dist/<service>/<service>-<version>.zip, with release.json
naming their digests. The zip is the one package the pipeline stores, scans
and deploys; the deploy stage pushes the image and chart it holds to JCR.
The same source gives the same zip, image digest and chart digest.

Writes build-info.json with the zip's SHA-256 and the image and chart
digests, which go into the change. Needs no Docker daemon, so it runs in
the toolchain container.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml

# This folder first: its build.py, deploy.py and check.py share names with the Lambda ones in scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parents[2]))
import oci  # noqa: E402
from common import ROOT, fail, git, service, version_of, write_json  # noqa: E402

ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def current_commit():
    """Jenkins sets GIT_COMMIT. Locally, HEAD, marked when the tree has changes."""
    if os.environ.get("GIT_COMMIT"):
        return os.environ["GIT_COMMIT"]
    head = git("rev-parse", "HEAD", check=False)
    if head.returncode != 0:
        return "uncommitted"
    dirty = git("status", "--porcelain", check=False).stdout.strip()
    return head.stdout.strip() + ("-dirty" if dirty else "")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_zip(source, destination):
    """Sorted, dated 1980, mode 0644. Layers are gzip already, so they are stored as they are."""
    with zipfile.ZipFile(destination, "w") as archive:
        for path in sorted(p for p in Path(source).rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(path.relative_to(source).as_posix(), ZIP_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_STORED if "blobs" in path.parts else zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())


def build(name, version_dir=None, out=None, commit=None, jira_key=None):
    """Build the package for a service from its folder (or a demo state). Returns build-info."""
    config = service(name)
    source = Path(version_dir or config["dir"])
    kube = config["kubernetes"]
    version = version_of(source)
    chart = yaml.safe_load((source / "chart" / "Chart.yaml").read_text())
    if str(chart["version"]) != version or str(chart["appVersion"]) != version:
        fail(f"chart/Chart.yaml says version {chart['version']}, appVersion {chart['appVersion']}; "
             f"VERSION says {version}. Make them the same.")
    settings = json.loads((source / "image.json").read_text())
    commit = commit or current_commit()
    out = Path(out or ROOT / "dist") / name
    out.mkdir(parents=True, exist_ok=True)

    base = oci.pull(settings["base"], settings["platform"], Path(os.environ.get("OCI_CACHE") or ROOT / ".local" / "oci-cache"))
    files = oci.tree(source, settings["include"], settings["workdir"].strip("/"),
                     extra={"build_info.json": oci.canonical({"service": name, "version": version, "commit": commit})})
    layer, diff_id = oci.app_layer(files)
    labels = {"org.opencontainers.image.title": name, "org.opencontainers.image.version": version,
              "org.opencontainers.image.revision": commit,
              "org.opencontainers.image.source": "https://github.com/m-atek/release-demo",
              "org.opencontainers.image.description": "Demo system, not a client's."}
    manifest, blobs = oci.assemble(base, layer, diff_id, settings, labels)
    tgz, metadata = oci.package_chart(source / "chart")
    chart_manifest, chart_blobs = oci.chart_artifact(tgz, metadata)

    with tempfile.TemporaryDirectory() as temp:
        package_dir = Path(temp) / "package"
        image_digest = oci.write_layout(package_dir / "image", manifest, blobs, version)
        chart_digest = oci.write_layout(package_dir / "chart", chart_manifest, chart_blobs, version)
        release = {
            "service": name, "version": version, "commit": commit,
            "image": {"repository": kube["image"], "digest": image_digest, "base": settings["base"],
                      "platform": settings["platform"]},
            "chart": {"name": metadata["name"], "version": version, "repository": kube["chart"],
                      "digest": chart_digest, "package": f"{metadata['name']}-{version}.tgz"},
        }
        write_json(package_dir / "release.json", release)
        package = out / f"{name}-{version}.zip"
        write_zip(package_dir, package)

    info = {
        "service": name, "version": version, "commit": commit, "jira_key": jira_key,
        "build_url": os.environ.get("BUILD_URL"), "package": package.name,
        "sha256": sha256(package), "size_bytes": package.stat().st_size,
        "image": f"{kube['image']}@{image_digest}", "image_digest": image_digest, "base_image": settings["base"],
        "chart": f"{metadata['name']} {version}", "chart_digest": chart_digest,
    }
    write_json(out / "build-info.json", info)
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("service")
    parser.add_argument("--jira-key", default=None)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args()

    if not (service(args.service)["dir"] / "VERSION").exists():
        fail(f"{service(args.service)['path']}/VERSION is missing. Stage a version first: "
             f"scripts/stage.py {args.service} 1.8.2")
    info = build(args.service, out=args.out, jira_key=args.jira_key)
    print(f"Built dist/{args.service}/{info['package']}")
    print(f"sha256 {info['sha256']}")
    print(f"image  {info['image']}")
    print(f"chart  {info['chart']} {info['chart_digest']}")


if __name__ == "__main__":
    main()
