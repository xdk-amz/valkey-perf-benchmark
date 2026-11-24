"""Identifier scan: fail when a file holds something that names a private host or account.

Results go public, so a data file must hold no IP addresses, cloud resource or account ids, private hostnames,
e-mail addresses, local home paths or key file names, and every link must point at a public host on the allowlist.
"""

import re
from pathlib import Path

PATTERNS = {
    "IPv4 address": re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"),
    "IPv6 address": re.compile(
        r"(?<![\w:])(?:[0-9a-f]{1,4}:){4,7}[0-9a-f]{1,4}(?![\w:])", re.I
    ),
    "cloud resource id": re.compile(
        r"\b(?:i|ami|sg|subnet|vpc|vol|eni|snap|igw|rtb|nat|pg|lt|eipalloc|cpg)-[0-9a-f]{8,17}\b"
    ),
    "cloud account id": re.compile(r"(?<![\w.])\d{12}(?![\w.])"),
    "ARN": re.compile(r"\barn:aws[\w-]*:"),
    "private hostname": re.compile(
        r"\b[\w.-]+\.(?:internal|local|lan|corp|intranet)\b|\bip-\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}\b",
        re.I,
    ),
    "e-mail address": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "local path": re.compile(r"(?:/home/|/Users/|/root/|/tmp/|[A-Z]:\\Users\\)"),
    "key file": re.compile(r"\b[\w-]+\.pem\b"),
}
# Links may only point at these public hosts (and their subdomains).
LINK = re.compile(r"\b[a-z][a-z0-9+.-]*://([^/\s\"'<>)]+)", re.I)
PUBLIC_HOSTS = (
    "github.com",
    "github.io",
    "githubusercontent.com",
    "valkey.io",
    "w3.org",
    "example.invalid",
)
# Allowed on purpose: loopback and the 0.0.0.0 wildcard never identify a host, and zero-padded key names in
# command lines (set:a:000000000000) are 12 digits of zeros or a 1 and zeros.
ALLOW = {"127.0.0.1", "0.0.0.0"}
ALLOW_RX = re.compile(r"^(?:0+|10+)$")


def _public(host: str) -> bool:
    host = host.split("@")[-1].split(":")[0].lower()
    return any(host == h or host.endswith("." + h) for h in PUBLIC_HOSTS)


def scan(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    line = lambda pos: text.count("\n", 0, pos) + 1  # noqa: E731
    for name, rx in PATTERNS.items():
        for m in rx.finditer(text):
            if m.group(0) in ALLOW or ALLOW_RX.match(m.group(0)):
                continue
            yield f"{path}:{line(m.start())}: {name}: {m.group(0)}"
    for m in LINK.finditer(text):
        if not _public(m.group(1)):
            yield f"{path}:{line(m.start())}: link to a host off the allowlist: {m.group(0)}"
