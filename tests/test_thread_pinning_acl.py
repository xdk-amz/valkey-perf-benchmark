"""Unit tests for thread pinning config, the ACL user and generator auth."""

import pytest

from generators import CachecannonGenerator, GeneratorRequest, ValkeyBenchmarkGenerator
from measurement import apply_acl, restore_default_user, validate_measurement
from thread_pinning import validate_thread_pinning


def test_validate_thread_pinning():
    validate_thread_pinning({"main": "1", "io_threads": "2-4", "other": "0"})
    for bad in ({"io_threads": "2-4"}, {"main": "1", "bio": "2"}, {"main": "x"}):
        with pytest.raises(ValueError):
            validate_thread_pinning(bad)


def test_measurement_acl_needs_user_and_password():
    validate_measurement({"acl": {"user": "u", "password": "p"}})
    with pytest.raises(ValueError, match="needs 'user' and 'password'"):
        validate_measurement({"acl": {"user": "u"}})


class _Recorder:
    def __init__(self):
        self.calls = []

    def execute_command(self, *args):
        self.calls.append(args)


def test_acl_user_walks_key_patterns_and_default_is_restored():
    c = _Recorder()
    apply_acl(c, {"user": "bench", "password": "pw"})
    restore_default_user(c)
    setuser, off, on = c.calls
    assert setuser[:5] == ("ACL", "SETUSER", "bench", "on", ">pw")
    assert "~x:*" in setuser and "~?*" in setuser and "~*" not in setuser
    assert off == ("ACL", "SETUSER", "default", "off")
    assert on == ("ACL", "SETUSER", "default", "on")


def _req(tmp_path, scenario):
    return GeneratorRequest(
        scenario=scenario,
        host="127.0.0.1",
        port=1,
        workdir=tmp_path,
        duration_s=5,
        auth_user="bench",
        auth_password="pw",
    )


def test_cachecannon_auth_keys(tmp_path):
    toml = CachecannonGenerator("/bin/cc").toml(_req(tmp_path, {"test": "GET"}))
    assert 'auth_user = "bench"' in toml
    assert 'auth_password = "pw"' in toml


def test_valkey_benchmark_auth_args(tmp_path):
    def builder(scenario, cpu_range=None, seed_val=None, port=None):
        return ["taskset", "-c", "1", "/bin/vb", "-p", str(port), "-t", "GET"]

    gen = ValkeyBenchmarkGenerator("/bin/vb", command_builder=builder)
    argv = gen.build_argv(_req(tmp_path, {"test": "GET"}))
    assert argv[3:8] == ["/bin/vb", "--user", "bench", "-a", "pw"]
