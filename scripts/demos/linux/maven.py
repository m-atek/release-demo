"""Run Maven for the inventory service, in jenkins/toolchain/java.Dockerfile.

Shared by build.py and test.py. Uses the image's warmed repository when it
has one (MAVEN_REPO), so a build in the pipeline downloads nothing new.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import fail  # noqa: E402


def mvn(directory, *goals):
    """mvn -B <goals> in directory. Returns the exit code."""
    if not shutil.which("mvn"):
        fail("Maven isn't here. Run this in the Java toolchain: "
             "docker build -t release-demo/java-tools -f jenkins/toolchain/java.Dockerfile .")
    command = ["mvn", "-B", "-q", "--no-transfer-progress"]
    if os.environ.get("MAVEN_REPO"):
        command.append(f"-Dmaven.repo.local={os.environ['MAVEN_REPO']}")
    return subprocess.run(command + list(goals), cwd=directory).returncode


def pom_version(directory):
    """The project's own <version>, the first one after </parent> or <artifactId>."""
    text = (Path(directory) / "pom.xml").read_text()
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<(dependencies|build|parent|properties)>.*?</\1>", "", text, flags=re.S)
    match = re.search(r"<version>\s*([^<\s]+)\s*</version>", text)
    return match.group(1) if match else None
