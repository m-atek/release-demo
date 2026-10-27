"""Run Flutter: in the pinned toolchain container for Android and the tests,
natively on the Mac agent for iOS.

The container image comes from jenkins/toolchain/flutter.Dockerfile, built on
first use and tagged with a hash of that file, as the shared toolchains are.
It is always linux/amd64: Flutter ships no Android compiler for Linux on Arm,
and the tools host is amd64 anyway. Gradle's cache lives outside the
container, on the data volume on the tools host, so builds stay fast.
"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import mobile


def dockerfile():
    return mobile.ROOT / mobile.config()["toolchain"]


def pinned_version():
    return mobile.config()["app"]["flutter"]


def image():
    digest = hashlib.sha256(dockerfile().read_bytes()).hexdigest()[:12]
    tag = f"release-demo/flutter-tools:{digest}"
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0:
        print(f"Building the Flutter toolchain image {tag} (once per Dockerfile change)")
        subprocess.run(["docker", "build", "--platform", "linux/amd64", "-q", "-t", tag, "-f", str(dockerfile()),
                        str(dockerfile().parent)], check=True, stdout=subprocess.DEVNULL)
    return tag


def cache_dir():
    """The agent's cache on the tools host's data volume, or ~/.cache elsewhere."""
    agent_cache = Path("/data/jenkins-agent/cache")
    base = agent_cache if agent_cache.is_dir() else Path.home() / ".cache" / "release-demo"
    path = base / "flutter"
    (path / "gradle").mkdir(parents=True, exist_ok=True)
    return path


def in_container(command, cwd, env_names=(), mounts=(), check=True, capture=False):
    """A shell command in the toolchain container, as the calling user.

    The repository is mounted at its own path, so paths inside and outside
    match. env_names are passed by name only: their values never appear on a
    command line."""
    cache = cache_dir()
    args = ["docker", "run", "--rm", "--platform", "linux/amd64", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{mobile.ROOT}:{mobile.ROOT}", "-v", f"{cache}:/cache", "-e", "GRADLE_USER_HOME=/cache/gradle",
            "-w", str(cwd)]
    for name in env_names:
        args += ["-e", name]
    for source, target in mounts:
        args += ["-v", f"{source}:{target}:ro"]
    args += [image(), "bash", "-c", f"mkdir -p \"$HOME\" && {command}"]
    return subprocess.run(args, check=check, capture_output=capture, text=capture)


def native(args, cwd, env=None):
    """Flutter on this machine (the Mac agent), after checking its version is the pinned one."""
    flutter = shutil.which("flutter")
    if not flutter:
        mobile.fail(f"Flutter isn't installed on this agent. Install Flutter {pinned_version()} (see README).")
    reported = subprocess.run([flutter, "--version", "--machine"], capture_output=True, text=True).stdout
    if f'"frameworkVersion": "{pinned_version()}"' not in reported and \
            f'"frameworkVersion":"{pinned_version()}"' not in reported:
        mobile.fail(f"This agent's Flutter isn't {pinned_version()}, the version the Android container pins.")
    return subprocess.run([flutter, *args], cwd=cwd, env={**os.environ, **(env or {})}, check=True)


if __name__ == "__main__":
    # python3 scripts/demos/mobile/flutter.py image: the toolchain image's tag, built if missing.
    import sys

    if sys.argv[1:] == ["image"]:
        print(image())
