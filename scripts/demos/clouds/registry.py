"""A small container registry client: enough to copy one image by its digest.

The OCI distribution API that crane and docker speak: read a manifest, check a
blob, upload one, put a manifest. Every registry here speaks it: JFrog
Container Registry, Amazon ECR, Azure Container Registry, Google Artifact
Registry, and Amazon ECR Public for the base images. A copy moves the exact
bytes, so the manifest digest is the same in every registry, and nothing on
the tools host needs a container tool installed. Standard library only.

    jcr = Registry("localhost:8082", "release-pipeline", token, name="JFrog")
    ecr = Registry("123.dkr.ecr.eu-west-1.amazonaws.com", "AWS", password, name="Amazon ECR")
    copy(jcr, "docker-local/quote-service", digest, ecr, "quote-service", "1.2.0-17")
"""

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

IMAGE_TYPES = ("application/vnd.oci.image.manifest.v1+json",
               "application/vnd.docker.distribution.manifest.v2+json")
INDEX_TYPES = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")
ACCEPT = ", ".join(IMAGE_TYPES + INDEX_TYPES)
USER_AGENT = "release-demo/1.0 (+https://github.com/m-atek/release-demo)"


class RegistryError(Exception):
    pass


def sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Blob downloads redirect to object storage, which refuses the registry's
    credentials. Redirects are followed by hand, without them."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _raw(method, url, headers, data=None, timeout=300):
    request = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with _opener.open(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()
    except urllib.error.URLError as error:
        raise RegistryError(f"Nothing answers at {url.split('/')[2]}: {error.reason}") from None


def parse_challenge(header):
    """'Bearer realm="https://x/token",service="x"' to ("bearer", {"realm": ..., "service": ...})."""
    scheme, _, rest = header.partition(" ")
    return scheme.lower(), dict(re.findall(r'(\w+)="([^"]*)"', rest))


class Registry:
    """One registry, signed in with a user name and password (or token), or anonymously."""

    def __init__(self, host, username=None, password=None, name=None, scheme=None):
        self.host = host
        self.name = name or host
        local = host.split(":")[0] in ("localhost", "127.0.0.1")
        self.base = f"{scheme or ('http' if local else 'https')}://{host}"
        self.basic = base64.b64encode(f"{username}:{password}".encode()).decode() if username else None
        self.tokens = {}

    def _request(self, method, path, scope, headers=None, data=None, url=None):
        """One call with whatever sign-in the registry's challenge asks for."""
        url = url or self.base + path
        headers = dict(headers or {})
        if scope in self.tokens:
            headers["Authorization"] = self.tokens[scope]
        status, reply_headers, body = _raw(method, url, headers, data)
        if status == 401:
            headers["Authorization"] = self._sign_in(reply_headers.get("WWW-Authenticate", ""), scope)
            status, reply_headers, body = _raw(method, url, headers, data)
        for _ in range(5):
            if status not in (301, 302, 303, 307, 308):
                break
            target = urllib.parse.urljoin(url, reply_headers["Location"])
            same_host = urllib.parse.urlsplit(target).netloc == urllib.parse.urlsplit(url).netloc
            follow = headers if same_host else {k: v for k, v in headers.items() if k != "Authorization"}
            status, reply_headers, body = _raw(method, target, follow, data)
            url = target
        return status, reply_headers, body

    def _sign_in(self, challenge, scope):
        kind, params = parse_challenge(challenge)
        if kind == "basic":
            if not self.basic:
                raise RegistryError(f"{self.name} wants a user name and password")
            self.tokens[scope] = f"Basic {self.basic}"
        elif kind == "bearer":
            query = urllib.parse.urlencode({k: v for k, v in {"service": params.get("service"), "scope": scope}.items() if v})
            headers = {"Authorization": f"Basic {self.basic}"} if self.basic else {}
            status, _, body = _raw("GET", f"{params['realm']}?{query}", headers)
            if status != 200:
                raise RegistryError(f"{self.name} refused the sign-in ({status}): {body[:200].decode(errors='replace')}")
            reply = json.loads(body)
            self.tokens[scope] = f"Bearer {reply.get('token') or reply.get('access_token')}"
        else:
            raise RegistryError(f"{self.name} asks for sign-in this client doesn't know: {challenge[:80]}")
        return self.tokens[scope]

    @staticmethod
    def _scope(repository, push=False):
        return f"repository:{repository}:{'pull,push' if push else 'pull'}"

    def manifest(self, repository, reference):
        """(media type, exact bytes, digest) of a manifest, by tag or digest."""
        status, headers, body = self._request("GET", f"/v2/{repository}/manifests/{reference}",
                                              self._scope(repository), {"Accept": ACCEPT})
        if status != 200:
            raise RegistryError(f"{self.name} has no {repository}:{reference} ({status})")
        digest = sha256(body)
        if reference.startswith("sha256:") and digest != reference:
            raise RegistryError(f"{self.name} sent {repository}@{reference} with digest {digest}")
        media_type = headers.get("Content-Type", "").split(";")[0] or json.loads(body).get("mediaType")
        return media_type, body, digest

    def digest(self, repository, reference, push=False):
        """The digest a tag points at, or None when there is no such tag."""
        status, headers, _ = self._request("HEAD", f"/v2/{repository}/manifests/{reference}",
                                           self._scope(repository, push), {"Accept": ACCEPT})
        if status == 404:
            return None
        if status != 200:
            raise RegistryError(f"{self.name} didn't answer for {repository}:{reference} ({status})")
        return headers.get("Docker-Content-Digest") or self.manifest(repository, reference)[2]

    def has_blob(self, repository, digest):
        status, _, _ = self._request("HEAD", f"/v2/{repository}/blobs/{digest}", self._scope(repository, push=True))
        return status == 200

    def blob(self, repository, digest):
        status, _, body = self._request("GET", f"/v2/{repository}/blobs/{digest}", self._scope(repository))
        if status != 200:
            raise RegistryError(f"{self.name} has no blob {digest} in {repository} ({status})")
        if sha256(body) != digest:
            raise RegistryError(f"{self.name} sent blob {digest} with different content")
        return body

    def put_blob(self, repository, digest, data):
        """Upload in one stream, as docker does: start, send everything, finish with the digest."""
        scope = self._scope(repository, push=True)
        status, headers, body = self._request("POST", f"/v2/{repository}/blobs/uploads/", scope)
        if status != 202:
            raise RegistryError(f"{self.name} refused an upload to {repository} ({status}): {body[:200].decode(errors='replace')}")
        location = urllib.parse.urljoin(self.base + "/", headers["Location"])
        status, headers, body = self._request("PATCH", "", scope, {"Content-Type": "application/octet-stream"},
                                              data, url=location)
        if status not in (201, 202, 204):  # ECR answers 201
            raise RegistryError(f"{self.name} refused blob {digest} ({status}): {body[:200].decode(errors='replace')}")
        location = urllib.parse.urljoin(location, headers.get("Location") or location)
        separator = "&" if "?" in location else "?"
        status, _, body = self._request("PUT", "", scope, {"Content-Length": "0"}, b"",
                                        url=f"{location}{separator}digest={urllib.parse.quote(digest)}")
        if status not in (201, 204):
            raise RegistryError(f"{self.name} didn't store blob {digest} ({status}): {body[:200].decode(errors='replace')}")

    def put_manifest(self, repository, reference, media_type, body):
        status, headers, reply = self._request("PUT", f"/v2/{repository}/manifests/{reference}",
                                               self._scope(repository, push=True), {"Content-Type": media_type}, body)
        if status not in (200, 201):
            raise RegistryError(f"{self.name} refused {repository}:{reference} ({status}): {reply[:300].decode(errors='replace')}")
        return headers.get("Docker-Content-Digest") or sha256(body)


class Layout:
    """An OCI image layout on disk, read and written like a registry."""

    def __init__(self, path):
        self.path = Path(path)
        self.name = "the build"

    def _blob_path(self, digest):
        return self.path / "blobs" / "sha256" / digest.removeprefix("sha256:")

    def write_blob(self, data):
        digest = sha256(data)
        path = self._blob_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return digest

    def has_blob(self, repository, digest):
        return self._blob_path(digest).exists()

    def blob(self, repository, digest):
        data = self._blob_path(digest).read_bytes()
        if sha256(data) != digest:
            raise RegistryError(f"The build's blob {digest} changed on disk")
        return data

    def put_blob(self, repository, digest, data):
        if self.write_blob(data) != digest:
            raise RegistryError(f"Blob {digest} arrived with different content")

    def index(self):
        return json.loads((self.path / "index.json").read_text())

    def image(self):
        """The layout's one image: its manifest descriptor."""
        manifests = self.index()["manifests"]
        if len(manifests) != 1:
            raise RegistryError(f"The build holds {len(manifests)} images, expected one")
        return manifests[0]

    def digest(self, repository, reference, push=False):
        return self.image()["digest"] if (self.path / "index.json").exists() else None

    def manifest(self, repository, reference):
        descriptor = self.image()
        if reference not in (descriptor["digest"], "latest"):
            raise RegistryError(f"The build holds {descriptor['digest']}, not {reference}")
        return descriptor["mediaType"], self.blob(repository, descriptor["digest"]), descriptor["digest"]

    def put_manifest(self, repository, reference, media_type, body):
        digest = self.write_blob(body)
        (self.path / "oci-layout").write_text('{"imageLayoutVersion": "1.0.0"}\n')
        index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                 "manifests": [{"mediaType": media_type, "digest": digest, "size": len(body),
                                "annotations": {"org.opencontainers.image.ref.name": reference}}]}
        (self.path / "index.json").write_text(json.dumps(index, indent=2) + "\n")
        return digest


def image_manifest(registry, repository, reference, platform="linux/amd64"):
    """(media type, bytes, digest) of the single-platform image a reference names.
    Lambda accepts no index, so every copy is of one platform's image."""
    media_type, body, digest = registry.manifest(repository, reference)
    if media_type in INDEX_TYPES or "manifests" in json.loads(body):
        os, arch = platform.split("/")
        for entry in json.loads(body)["manifests"]:
            if entry.get("platform", {}).get("os") == os and entry["platform"].get("architecture") == arch:
                return registry.manifest(repository, entry["digest"])
        raise RegistryError(f"{repository}:{reference} has no {platform} image")
    return media_type, body, digest


def copy(source, source_repository, digest, target, target_repository, tag, log=print):
    """Copy one image by digest and tag it. The target ends up with the same
    bytes, so the same digest; a tag already holding another image is an error,
    because no registry here lets the pipeline overwrite one."""
    media_type, body, _ = source.manifest(source_repository, digest)
    existing = target.digest(target_repository, tag, push=True)
    if existing == digest:
        log(f"  {target.name}: {target_repository}:{tag} already holds {short(digest)}")
        return digest
    if existing:
        raise RegistryError(f"{target.name}: {target_repository}:{tag} already holds {existing}, not {digest}")
    manifest = json.loads(body)
    if media_type not in IMAGE_TYPES:
        raise RegistryError(f"{digest} is a {media_type}, not one image")
    sent = 0
    for descriptor in [manifest["config"], *manifest["layers"]]:
        if not target.has_blob(target_repository, descriptor["digest"]):
            target.put_blob(target_repository, descriptor["digest"], source.blob(source_repository, descriptor["digest"]))
            sent += descriptor["size"]
    pushed = target.put_manifest(target_repository, tag, media_type, body)
    if pushed != digest:
        raise RegistryError(f"{target.name} stored {pushed}, not {digest}")
    log(f"  {target.name}: copied {short(digest)} to {target_repository}:{tag} ({sent / 1e6:.1f} MB sent)")
    return pushed


def short(digest):
    return digest[:19]
