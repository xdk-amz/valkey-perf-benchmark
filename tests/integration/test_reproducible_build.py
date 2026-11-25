"""Two builds of one commit in separate directories give the same binary: ServerBuilder sets SOURCE_DATE_EPOCH, so
mkreleasehdr.sh embeds the commit time where it would embed the build host and time.

Needs a local Valkey checkout (VPB_TEST_VALKEY, a commit in VPB_TEST_VALKEY_COMMIT, default its HEAD) and builds it
twice, so it is skipped without one. Pin it as a benchmark is pinned:

    VPB_TEST_VALKEY=../valkey taskset -c 60-63 python -m pytest tests/integration/test_reproducible_build.py
"""

import hashlib
import os
import subprocess

import pytest

import vpb_result
from valkey_build import ServerBuilder

SOURCE = os.environ.get("VPB_TEST_VALKEY")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not SOURCE, reason="VPB_TEST_VALKEY names no Valkey checkout"),
]


def test_two_builds_of_one_commit_have_one_sha256(tmp_path):
    commit = os.environ.get("VPB_TEST_VALKEY_COMMIT", "HEAD")
    sha = subprocess.run(
        ["git", "-C", SOURCE, "rev-parse", commit],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    digests = []
    for name in ("a", "b"):
        tree = tmp_path / name
        subprocess.run(["git", "clone", "-q", SOURCE, str(tree)], check=True)
        ServerBuilder(sha, False, str(tree)).build()
        binary = tree / "src" / "valkey-server"
        assert binary.is_file(), f"build in {tree} made no server"
        digests.append(hashlib.sha256(binary.read_bytes()).hexdigest())
    assert digests[0] == digests[1]
    # the run file's build records the SOURCE_DATE_EPOCH, read from the build stamp as the writer reads it
    epoch = subprocess.run(
        ["git", "-C", SOURCE, "log", "-1", "--format=%ct", sha],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    built = vpb_result.build_info({}, vpb_result.local_run, str(binary), sha)
    assert (built["source_date_epoch"], built["binary_sha256"]) == (
        int(epoch),
        digests[1],
    )
