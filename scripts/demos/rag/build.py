"""Stage 1 for the runbook assistant, Build: one package with everything a version is.

    python scripts/demos/rag/build.py runbook-assistant [--jira-key ORD-110]

A deterministic zip of services/runbook-assistant (sorted entries, fixed
times, tests left out): the retriever, prompt, schema and model settings,
the chunker and its settings, the corpus, requirements.txt for the SBOM.
build-info.json records the zip's SHA-256, the chunk hash that names the
version's table and index, and the model, so the change and the evaluation
name the exact things that ship. The same source gives the same checksum.
"""

import argparse
import hashlib
import os
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1]))
import rag  # noqa: E402
from common import ROOT, fail, git, service, version_of, write_json  # noqa: E402

EXCLUDE_DIRS = {"tests", "__pycache__", ".pytest_cache"}
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


def package_files(folder):
    for path in sorted(Path(folder).rglob("*")):
        relative = path.relative_to(folder)
        if path.is_file() and not EXCLUDE_DIRS & set(relative.parts) and path.suffix != ".pyc":
            yield path, relative


def write_zip(folder, destination):
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, relative in package_files(folder):
            info = zipfile.ZipInfo(relative.as_posix(), ZIP_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())


def build(name, folder=None, out=None, commit=None, jira_key=None):
    import yaml

    config = service(name)
    folder = Path(folder or config["dir"])
    version = version_of(folder)
    settings = yaml.safe_load((folder / "assistant.yaml").read_text())
    allowed = config["evaluation"]["answer_models"]
    if settings.get("model") not in allowed:
        fail(f"assistant.yaml names {settings.get('model')}; services.yaml allows {', '.join(allowed)}.")
    out = Path(out or ROOT / "dist") / name
    out.mkdir(parents=True, exist_ok=True)
    package = out / f"{name}-{version}.zip"
    write_zip(folder, package)
    info = {"service": name, "version": version, "commit": commit or current_commit(), "jira_key": jira_key,
            "build_url": os.environ.get("BUILD_URL"), "package": package.name,
            "sha256": hashlib.sha256(package.read_bytes()).hexdigest(), "size_bytes": package.stat().st_size,
            "chunk_hash": rag.chunk_hash(folder, config["databricks"]["embedding_model"]),
            "embedding_model": config["databricks"]["embedding_model"],
            "model": settings["model"], "effort": settings.get("effort")}
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
             f"scripts/stage.py {args.service} 2.0.0")
    info = build(args.service, out=args.out, jira_key=args.jira_key)
    print(f"Built dist/{args.service}/{info['package']}")
    print(f"sha256 {info['sha256']}")
    print(f"chunk hash {info['chunk_hash'][:8]}, model {info['model']}")


if __name__ == "__main__":
    main()
