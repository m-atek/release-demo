"""The pins, whichever day's they are: scripts/demos/young_dependency/pick.py writes them on the day."""

import re
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


def pins():
    lines = [line.strip() for line in REQUIREMENTS.read_text().splitlines()]
    return dict(line.split("==", 1) for line in lines if line and not line.startswith("#"))


def test_every_line_is_a_pin():
    for line in REQUIREMENTS.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            assert re.fullmatch(r"[A-Za-z0-9._-]+==\S+", line.strip()), line


def test_the_sdk_and_its_core_move_together():
    assert pins()["boto3"] == pins()["botocore"]
