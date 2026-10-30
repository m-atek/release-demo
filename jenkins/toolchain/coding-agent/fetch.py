"""Downloads and checks for coding-agent.Dockerfile, standard library only.

    fetch.py <url> <file>                            download
    fetch.py --check <file> <sha256>                 stop unless the file has this SHA-256
    fetch.py --keyring <key.asc> <fingerprint> <out> the armored public key as a gpgv
                                                     keyring, if its fingerprint matches
"""

import base64
import hashlib
import sys
import urllib.request


def download(url, path):
    with urllib.request.urlopen(url, timeout=300) as response, open(path, "wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)


def check(path, expected):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        sys.exit(f"{path}: SHA-256 {digest.hexdigest()}, expected {expected}")


def fingerprint(packet_body):
    """An OpenPGP v4 key's fingerprint: SHA-1 over 0x99, the length and the public key packet."""
    return hashlib.sha1(b"\x99" + len(packet_body).to_bytes(2, "big") + packet_body).hexdigest().upper()


def keyring(armored, expected, out):
    lines = open(armored).read().strip().splitlines()
    body = lines[lines.index("") + 1:]
    data = base64.b64decode("".join(line for line in body if not line.startswith(("=", "-----"))))
    # The first packet is the primary public key: an old- or new-format header, then its body.
    tag = data[0]
    if tag & 0x40:  # new format
        length, start = data[1], 2
        if length >= 192:
            length, start = ((data[1] - 192) << 8) + data[2] + 192, 3
    else:
        size = {0: 1, 1: 2, 2: 4}[tag & 0x03]
        length, start = int.from_bytes(data[1:1 + size], "big"), 1 + size
    found = fingerprint(data[start:start + length])
    if found != expected:
        sys.exit(f"{armored}: fingerprint {found}, expected {expected}")
    open(out, "wb").write(data)


if __name__ == "__main__":
    if sys.argv[1] == "--check":
        check(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "--keyring":
        keyring(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        download(sys.argv[1], sys.argv[2])
