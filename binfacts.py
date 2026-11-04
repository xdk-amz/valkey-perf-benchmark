"""Identity of a built binary: digest, text-segment size and compiler.

Standard library only, so the same facts can be read on a remote host.
"""

import hashlib
import subprocess
from pathlib import Path
from typing import Dict, List, Optional


def _cmd(argv: List[str], cwd=None) -> Optional[str]:
    try:
        r = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=30,
            cwd=cwd,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def sha256(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with Path(path).open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def text_size(path: Path) -> Optional[int]:
    out = _cmd(["size", "-A", str(path)])
    for line in (out or "").splitlines():
        parts = line.split()
        if parts and parts[0] == ".text":
            return int(parts[1])
    return None


def compiler(path: Path) -> Optional[str]:
    out = _cmd(["readelf", "-p", ".comment", str(path)])
    for line in (out or "").splitlines():
        if "]" in line:
            return line.split("]", 1)[1].strip()
    return None


def binary_facts(path: Path) -> Dict[str, object]:
    return {
        "path": str(path),
        "sha256": sha256(path),
        "text_size": text_size(path),
        "compiler": compiler(path),
    }
