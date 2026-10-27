"""OCI images and Helm charts, built and pushed with the standard library.

The build stage runs in a toolchain container without a Docker daemon, and the
same source must give the same digest. So the catalog image is assembled
here: the pinned base image's layers as they are, plus one layer with the
service's files, written with fixed times and owners. The Helm chart is
packaged the same way and stored as an OCI artifact, which is what JCR's
helm-local repository holds. Both are written as OCI image layouts, which the
deploy stage pushes to JFrog Container Registry.
"""

import gzip
import hashlib
import io
import json
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MANIFEST = "application/vnd.oci.image.manifest.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
DOCKER_LAYER = "application/vnd.docker.image.rootfs.diff.tar.gzip"
HELM_CONFIG = "application/vnd.cncf.helm.config.v1+json"
HELM_LAYER = "application/vnd.cncf.helm.chart.content.v1.tar+gzip"
ACCEPT = ", ".join([INDEX, MANIFEST, DOCKER_LIST, DOCKER_MANIFEST])
EPOCH = "1970-01-01T00:00:00Z"
USER_AGENT = "release-demo/1.0 (+https://github.com/m-atek/release-demo)"


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(document):
    """JSON with sorted keys and no spaces: the same document gives the same digest."""
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def parse_reference(reference):
    """docker.io/library/python:3.13@sha256:... -> (registry URL, repository, tag, digest)."""
    name, _, pinned = reference.partition("@")
    host, _, path = name.partition("/")
    if not path or ("." not in host and ":" not in host and host != "localhost"):
        host, path = "docker.io", name
    repository, _, tag = path.partition(":")
    if host == "docker.io":
        host = "registry-1.docker.io"
        repository = repository if "/" in repository else f"library/{repository}"
    scheme = "http" if host.startswith(("127.0.0.1", "localhost")) else "https"
    return f"{scheme}://{host}", repository, tag or None, pinned or None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Registry:
    """A registry's HTTP API (the OCI distribution spec), with its token handshake.

    JCR takes an access token as a bearer token directly. Docker Hub answers
    401 with a token service to ask, anonymously for public images. Blob
    downloads may redirect to a CDN, which must not get our Authorization header.
    """

    def __init__(self, base, bearer=None, username=None, password=None):
        self.base = base.rstrip("/")
        self.bearer = bearer
        self.basic = (username, password) if username else None
        self.tokens = {}
        self.opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method, path, data=None, headers=None, scope=None):
        """(status, headers, body). Follows redirects without credentials."""
        url = path if path.startswith("http") else self.base + path
        headers = {"User-Agent": USER_AGENT, **(headers or {})}
        token = self.tokens.get(scope) or self.bearer
        for attempt in range(2):
            sent = dict(headers)
            if token:
                sent["Authorization"] = f"Bearer {token}"
            status, reply_headers, body = self._send(method, url, data, sent)
            if status == 401 and attempt == 0 and "Www-Authenticate" in reply_headers:
                token = self._token(reply_headers["Www-Authenticate"], scope)
                continue
            if status in (301, 302, 303, 307, 308) and method in ("GET", "HEAD"):
                location = urllib.parse.urljoin(url, reply_headers["Location"])
                return self._send(method, location, None, {"User-Agent": USER_AGENT})
            return status, reply_headers, body
        return status, reply_headers, body

    def _send(self, method, url, data, headers):
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=120) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def _token(self, challenge, scope):
        fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        if not challenge.lower().startswith("bearer") or "realm" not in fields:
            raise RuntimeError(f"Unexpected registry challenge: {challenge}")
        query = {"service": fields.get("service", "")}
        if scope or fields.get("scope"):
            query["scope"] = scope or fields["scope"]
        request = urllib.request.Request(f"{fields['realm']}?{urllib.parse.urlencode(query)}",
                                         headers={"User-Agent": USER_AGENT})
        if self.basic:
            import base64
            request.add_header("Authorization", "Basic " + base64.b64encode(":".join(self.basic).encode()).decode())
        with urllib.request.urlopen(request, timeout=60) as response:
            reply = json.loads(response.read())
        token = reply.get("token") or reply.get("access_token")
        self.tokens[scope] = token
        return token

    def manifest(self, repository, reference):
        status, headers, body = self.request("GET", f"/v2/{repository}/manifests/{reference}",
                                             headers={"Accept": ACCEPT}, scope=f"repository:{repository}:pull")
        if status != 200:
            raise RuntimeError(f"{self.base}: no manifest {repository}:{reference} ({status})")
        if reference.startswith("sha256:") and digest(body) != reference:
            raise RuntimeError(f"{repository}@{reference}: the registry sent a manifest with another digest")
        return json.loads(body), body

    def tagged(self, repository, tag):
        """The digest of the manifest a tag points at, or None when the tag isn't there."""
        status, _, body = self.request("GET", f"/v2/{repository}/manifests/{tag}",
                                       headers={"Accept": ACCEPT}, scope=f"repository:{repository}:pull")
        return digest(body) if status == 200 else None

    def blob(self, repository, blob_digest):
        status, _, body = self.request("GET", f"/v2/{repository}/blobs/{blob_digest}",
                                       scope=f"repository:{repository}:pull")
        if status != 200:
            raise RuntimeError(f"{self.base}: no blob {repository}@{blob_digest} ({status})")
        if digest(body) != blob_digest:
            raise RuntimeError(f"{repository}@{blob_digest}: the registry sent other content")
        return body

    def has_blob(self, repository, blob_digest):
        status, _, _ = self.request("HEAD", f"/v2/{repository}/blobs/{blob_digest}",
                                    scope=f"repository:{repository}:pull,push")
        return status == 200

    def put_blob(self, repository, blob_digest, data):
        scope = f"repository:{repository}:pull,push"
        status, headers, body = self.request("POST", f"/v2/{repository}/blobs/uploads/", data=b"", scope=scope)
        if status != 202:
            raise RuntimeError(f"{self.base}: can't start an upload to {repository} ({status}): {body[:200]!r}")
        location = urllib.parse.urljoin(self.base + "/", headers["Location"])
        separator = "&" if "?" in location else "?"
        status, _, body = self.request("PUT", f"{location}{separator}digest={urllib.parse.quote(blob_digest)}",
                                       data=data, headers={"Content-Type": "application/octet-stream"}, scope=scope)
        if status != 201:
            raise RuntimeError(f"{self.base}: upload of {blob_digest} to {repository} failed ({status}): {body[:200]!r}")

    def put_manifest(self, repository, reference, media_type, data):
        status, headers, body = self.request("PUT", f"/v2/{repository}/manifests/{reference}", data=data,
                                             headers={"Content-Type": media_type},
                                             scope=f"repository:{repository}:pull,push")
        if status not in (200, 201):
            raise RuntimeError(f"{self.base}: {repository}:{reference} refused ({status}): {body[:300]!r}")
        return headers.get("Docker-Content-Digest") or digest(data)


class Image:
    """A pulled single-platform image: its manifest, config and layer blobs."""

    def __init__(self, reference, manifest_digest, manifest, config, layers):
        self.reference, self.manifest_digest = reference, manifest_digest
        self.manifest, self.config, self.layers = manifest, config, layers


def pull(reference, platform, cache):
    """The image for one platform (linux/amd64), its blobs kept in cache by digest."""
    registry_url, repository, tag, pinned = parse_reference(reference)
    if not pinned:
        raise RuntimeError(f"{reference} isn't pinned by digest")
    registry = Registry(registry_url)
    manifest, raw = registry.manifest(repository, pinned)
    manifest_digest = pinned
    if manifest.get("mediaType") in (INDEX, DOCKER_LIST) or "manifests" in manifest:
        os_name, arch = platform.split("/")
        chosen = [m for m in manifest["manifests"]
                  if m.get("platform", {}).get("os") == os_name and m["platform"].get("architecture") == arch]
        if not chosen:
            raise RuntimeError(f"{reference} has no {platform} image")
        manifest_digest = chosen[0]["digest"]
        manifest, raw = registry.manifest(repository, manifest_digest)

    def cached(blob_digest):
        path = Path(cache) / blob_digest.replace(":", "/")
        if not path.exists() or digest(path.read_bytes()) != blob_digest:
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(f"{path.name}.{id(path)}.partial")
            partial.write_bytes(registry.blob(repository, blob_digest))
            partial.replace(path)
        return path

    config = cached(manifest["config"]["digest"]).read_bytes()
    layers = [(layer, cached(layer["digest"])) for layer in manifest["layers"]]
    return Image(reference, manifest_digest, manifest, config, layers)


def _tar(entries):
    """A tar of (name, bytes or None for a directory, mode), sorted, owned by
    root, dated 1970: the same files always give the same bytes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data, mode in sorted(entries, key=lambda e: e[0]):
            info = tarfile.TarInfo(name)
            info.mtime, info.uid, info.gid, info.uname, info.gname, info.mode = 0, 0, 0, "", "", mode
            if data is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _gzip(data):
    return gzip.compress(data, compresslevel=9, mtime=0)


def tree(source, include, prefix, extra=None):
    """Entries for _tar: the included files and folders of source under prefix/."""
    entries = {prefix: (None, 0o755)}
    for name in include:
        path = Path(source) / name
        paths = [path] + sorted(path.rglob("*")) if path.is_dir() else [path]
        for item in paths:
            if "__pycache__" in item.parts or item.suffix == ".pyc":
                continue
            relative = item.relative_to(source).as_posix()
            entries[f"{prefix}/{relative}"] = (None, 0o755) if item.is_dir() else (item.read_bytes(), 0o644)
    for name, data in (extra or {}).items():
        entries[f"{prefix}/{name}"] = (data, 0o644)
    return [(name, data, mode) for name, (data, mode) in entries.items()]


def app_layer(entries):
    """(gzipped layer, its diff id: the digest of the uncompressed tar)."""
    raw = _tar(entries)
    return _gzip(raw), digest(raw)


def assemble(base, layer, diff_id, settings, labels):
    """The image: base layers plus ours, with our command, user and labels.
    Returns (manifest bytes, {digest: bytes or path} of every blob)."""
    config = json.loads(base.config)
    run = config.setdefault("config", {})
    env = dict(item.split("=", 1) for item in run.get("Env") or [])
    env.update(dict(item.split("=", 1) for item in settings.get("env", [])))
    run.update({"Env": [f"{k}={v}" for k, v in env.items()], "Cmd": settings["cmd"],
                "WorkingDir": settings["workdir"], "User": settings["user"],
                "ExposedPorts": {port: {} for port in settings.get("ports", [])},
                "Labels": {**(run.get("Labels") or {}), **labels}})
    run.pop("Entrypoint", None)
    for key in ("container", "container_config", "docker_version"):
        config.pop(key, None)
    config["created"] = EPOCH
    config.setdefault("history", []).append(
        {"created": EPOCH, "created_by": "scripts/demos/kubernetes/build.py: the service's files in /app"})
    config["rootfs"]["diff_ids"].append(diff_id)
    config_bytes = canonical(config)

    blobs = {digest(config_bytes): config_bytes, digest(layer): layer}
    layers = []
    for descriptor, path in base.layers:
        media_type = LAYER if descriptor["mediaType"] in (LAYER, DOCKER_LAYER) else descriptor["mediaType"]
        layers.append({"mediaType": media_type, "digest": descriptor["digest"], "size": descriptor["size"]})
        blobs[descriptor["digest"]] = path
    layers.append({"mediaType": LAYER, "digest": digest(layer), "size": len(layer)})
    manifest = {
        "schemaVersion": 2, "mediaType": MANIFEST,
        "config": {"mediaType": CONFIG, "digest": digest(config_bytes), "size": len(config_bytes)},
        "layers": layers,
        "annotations": {"org.opencontainers.image.base.name": base.reference.split("@")[0],
                        "org.opencontainers.image.base.digest": base.manifest_digest},
    }
    return canonical(manifest), blobs


def package_chart(chart_dir):
    """The chart as helm package makes it, but reproducible: (tgz bytes, Chart.yaml as a dict)."""
    import yaml

    chart_dir = Path(chart_dir)
    metadata = yaml.safe_load((chart_dir / "Chart.yaml").read_text())
    entries = [(f"{metadata['name']}/{p.relative_to(chart_dir).as_posix()}", p.read_bytes(), 0o644)
               for p in sorted(chart_dir.rglob("*")) if p.is_file()]
    return _gzip(_tar(entries)), metadata


def chart_artifact(tgz, metadata):
    """The chart as an OCI artifact, as helm push stores it, without a timestamp."""
    config = canonical(metadata)
    manifest = {
        "schemaVersion": 2, "mediaType": MANIFEST,
        "config": {"mediaType": HELM_CONFIG, "digest": digest(config), "size": len(config)},
        "layers": [{"mediaType": HELM_LAYER, "digest": digest(tgz), "size": len(tgz)}],
        "annotations": {"org.opencontainers.image.title": metadata["name"],
                        "org.opencontainers.image.version": str(metadata["version"]),
                        "org.opencontainers.image.description": metadata.get("description", "")},
    }
    return canonical(manifest), {digest(config): config, digest(tgz): tgz}


def write_layout(directory, manifest, blobs, name):
    """An OCI image layout: oci-layout, index.json and blobs/sha256/<hex>. Returns the manifest digest."""
    directory = Path(directory)
    (directory / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
    (directory / "oci-layout").write_bytes(canonical({"imageLayoutVersion": "1.0.0"}))
    for blob_digest, content in {digest(manifest): manifest, **blobs}.items():
        data = content if isinstance(content, bytes) else Path(content).read_bytes()
        (directory / "blobs" / blob_digest.replace(":", "/")).write_bytes(data)
    index = {"schemaVersion": 2, "mediaType": INDEX, "manifests": [{
        "mediaType": MANIFEST, "digest": digest(manifest), "size": len(manifest),
        "annotations": {"org.opencontainers.image.ref.name": name}}]}
    (directory / "index.json").write_bytes(canonical(index))
    return digest(manifest)


def read_layout(directory):
    """(manifest digest, manifest bytes, {digest: path}) from an OCI image layout, every blob checked."""
    directory = Path(directory)
    entry = json.loads((directory / "index.json").read_text())["manifests"][0]
    path = lambda d: directory / "blobs" / d.replace(":", "/")
    manifest = path(entry["digest"]).read_bytes()
    if digest(manifest) != entry["digest"]:
        raise RuntimeError(f"{directory}: the manifest doesn't match its digest")
    parsed = json.loads(manifest)
    blobs = {}
    for descriptor in [parsed["config"], *parsed["layers"]]:
        if digest(path(descriptor["digest"]).read_bytes()) != descriptor["digest"]:
            raise RuntimeError(f"{directory}: blob {descriptor['digest']} doesn't match its digest")
        blobs[descriptor["digest"]] = path(descriptor["digest"])
    return entry["digest"], manifest, blobs


def push_layout(registry, repository, directory, tag):
    """Push an OCI layout under tag. Blobs the registry has already are skipped.
    Returns the digest the registry reports, which must be the layout's."""
    manifest_digest, manifest, blobs = read_layout(directory)
    for blob_digest, path in blobs.items():
        if not registry.has_blob(repository, blob_digest):
            registry.put_blob(repository, blob_digest, Path(path).read_bytes())
    stored = registry.put_manifest(repository, tag, MANIFEST, manifest)
    if stored != manifest_digest:
        raise RuntimeError(f"{repository}:{tag}: the registry stored {stored}, not {manifest_digest}")
    return stored
