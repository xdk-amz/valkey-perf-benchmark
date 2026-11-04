"""The ssh layer for remote server and generator hosts, without a network."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import benchmark
import measurement
import procstat
import remote
from generators import CachecannonGenerator, GeneratorRequest
from remote import RemoteProcess, SshExecutor, SshHost, build_dir
from valkey_server import ServerLauncher

AGENT = Path(__file__).resolve().parents[1] / "vpb_agent.py"


class FakeHost:
    """Records what an SshHost would have done on the host."""

    name = "fake"
    root = "/r"

    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def put_tree(self, local, remote_dir):
        self.calls.append(("put_tree", str(local), remote_dir))

    def put_file(self, local, remote_path):
        self.calls.append(("put_file", str(local), remote_path))

    def get_file(self, remote_path, local):
        self.calls.append(("get_file", remote_path, str(local)))
        Path(local).write_text("fetched")
        return True

    def run(self, argv, cwd=None, timeout=None, check=True):
        self.calls.append(("run", list(argv), cwd))
        return subprocess.CompletedProcess(argv, 0, "out", "")

    def script(self, text, **kw):
        self.calls.append(("script", text))
        return subprocess.CompletedProcess(text, 0, "", "")

    def agent(self, action, **payload):
        self.calls.append(("agent", action, payload))
        reply = self.replies.get(action, {})
        return reply(payload) if callable(reply) else reply


# ---------- procstat -----------------------------------------------------------


def test_pids_of_binary_matches_the_executable_not_the_command_line(tmp_path):
    binary = tmp_path / "valkey-server"
    shutil.copy2(shutil.which("sleep"), binary)
    server = subprocess.Popen([str(binary), "30"])
    # A shell whose arguments name the binary, as the agent's own ssh
    # session does, must not match.
    bystander = subprocess.Popen(["sh", "-c", f"sleep 30; echo {binary}"])
    try:
        time.sleep(0.2)
        assert procstat.pids_of_binary(str(binary)) == [server.pid]
    finally:
        server.kill()
        bystander.kill()


def test_ticks_from_json_restores_int_keys():
    t = procstat.ticks([os.getpid()], [], None)
    back = procstat.ticks_from_json(json.loads(json.dumps(t)))
    assert back["main"] == t["main"]
    assert back["steal"] == t["steal"]


# ---------- vpb_agent ------------------------------------------------------------


def _agent(payload):
    r = subprocess.run(
        [sys.executable, str(AGENT), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_agent_ships_every_module_it_imports():
    assert set(_agent({"action": "hello"})["files"]) == set(remote.AGENT_FILES)
    assert all(_agent({"action": "hello"})["files"].values())


def test_agent_spawn_poll_and_signal(tmp_path):
    out = str(tmp_path / "p.out")
    pid = _agent(
        {
            "action": "spawn",
            "argv": ["sh", "-c", "echo hi; exec sleep 30"],
            "out": out,
            "err": str(tmp_path / "p.err"),
        }
    )["pid"]
    assert _agent({"action": "poll", "pid": pid, "out": out}) == {
        "alive": True,
        "rc": None,
    }
    assert _agent({"action": "signal", "pid": pid, "sig": 15})["sent"]
    deadline = time.time() + 10
    while _agent({"action": "poll", "pid": pid, "out": out})["alive"]:
        assert time.time() < deadline
        time.sleep(0.1)
    assert _agent({"action": "poll", "pid": pid, "out": out})["rc"] == -15
    assert Path(out).read_text() == "hi\n"


def test_agent_spawn_reports_a_binary_that_cannot_start(tmp_path):
    r = _agent(
        {
            "action": "spawn",
            "argv": [str(tmp_path / "missing")],
            "out": str(tmp_path / "p.out"),
            "err": str(tmp_path / "p.err"),
        }
    )
    assert "cannot start" in r["error"] and "FileNotFoundError" in r["error"]


def test_agent_window_reads_at_both_edges_on_its_own_clock(tmp_path):
    busy = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\nt=time.time()+30\nwhile time.time()<t: pass",
        ]
    )
    try:
        at = time.time() + 0.5
        r = _agent({"action": "window", "at": at, "window": 1, "gen_pids": [busy.pid]})
    finally:
        busy.kill()
    assert r["a"]["t"] == pytest.approx(at, abs=0.1)
    assert r["b"]["t"] - r["a"]["t"] == pytest.approx(1.0, abs=0.1)
    used = (r["b"]["ticks"]["gen"] - r["a"]["ticks"]["gen"]) / measurement.CLK_TCK
    assert used == pytest.approx(1.0, abs=0.3)


def test_agent_refuses_a_window_whose_start_passed():
    r = _agent({"action": "window", "at": time.time() - 1, "window": 1})
    assert "already passed" in r["error"]


# ---------- SshHost and SshExecutor ------------------------------------------


def test_ssh_argv_uses_one_shared_connection():
    host = SshHost("u@h", key="/k", known_hosts="/kh", root="/r")
    argv = host.argv("echo hi")
    assert argv[0] == "ssh" and argv[-2] == "u@h"
    assert "ControlMaster=auto" in argv and "-i" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert argv[-1] == "bash -lc 'echo hi'"


def test_forwarder_routes_every_address_through_the_host(monkeypatch):
    host = SshHost("u@h", private_ip="10.0.0.5", root="/r")
    monkeypatch.setattr(host, "forward", lambda port: 40000 + port)
    route = host.forwarder()
    assert route("10.0.0.5", 6379) == ("127.0.0.1", 46379)
    assert route("127.0.0.1", 7000) == ("127.0.0.1", 47000)
    only = host.forwarder("10.0.0.5")
    assert only("192.0.2.1", 6379) == ("192.0.2.1", 6379)


def test_connect_addr_is_direct_without_a_forwarder():
    remote.set_forwarder(None)
    assert remote.connect_addr("10.0.0.5", 6379) == ("10.0.0.5", 6379)


def test_executor_maps_only_paths_under_its_roots(tmp_path):
    host = FakeHost()
    ex = SshExecutor(host, local_roots=[tmp_path])
    cfg = tmp_path / "run" / "x.toml"
    cfg.parent.mkdir()
    cfg.write_text("x")
    argv = ex.map_argv(["/usr/bin/taskset", "-c", "1", "/opt/cc", str(cfg)])
    assert argv[:4] == ["/usr/bin/taskset", "-c", "1", "/opt/cc"]
    assert argv[4] == f"/r/stage{cfg.resolve()}"
    assert ("put_file", str(cfg.resolve()), argv[4]) in host.calls


def test_executor_stages_a_directory_once(tmp_path):
    host = FakeHost()
    ex = SshExecutor(host, local_roots=[tmp_path])
    tls = tmp_path / "tls"
    tls.mkdir()
    assert ex.path(tls) == ex.path(tls) == f"/r/stage{tls.resolve()}"
    assert [c[0] for c in host.calls] == ["put_tree"]


def test_cachecannon_on_a_remote_host_names_staged_certificates(tmp_path):
    host = FakeHost(replies={"spawn": {"pid": 4242}})
    ex = SshExecutor(host, local_roots=[tmp_path])
    tls = tmp_path / "tls"
    tls.mkdir()
    gen = CachecannonGenerator("/opt/cachecannon", executor=ex)
    req = GeneratorRequest(
        scenario={"id": "g", "test": "GET", "clients": 2},
        host="10.0.0.5",
        port=6379,
        workdir=tmp_path / "run",
        duration_s=10,
        tls=True,
        tls_dir=tls,
    )
    handle = gen.start(req)
    toml = (tmp_path / "run" / "cachecannon.toml").read_text()
    assert f'tls_ca_file = "/r/stage{tls.resolve()}/ca.crt"' in toml
    assert 'tls_hostname = "localhost"' in toml
    spawn = next(c for c in host.calls if c[:2] == ("agent", "spawn"))[2]
    assert spawn["argv"] == [
        "/opt/cachecannon",
        f"/r/stage{(tmp_path / 'run' / 'cachecannon.toml').resolve()}",
    ]
    assert spawn["out"] == f"/r/stage{(tmp_path / 'run' / 'cachecannon.out').resolve()}"
    assert (
        "put_tree",
        str(tmp_path / "run"),
        f"/r/stage{(tmp_path / 'run').resolve()}",
    ) in host.calls
    assert handle.proc.pid == 4242


def test_remote_process_fetches_output_when_it_exits(tmp_path):
    state = {"alive": True}
    host = FakeHost(
        replies={
            "poll": lambda p: {
                "alive": state["alive"],
                "rc": None if state["alive"] else 0,
            }
        }
    )
    proc = RemoteProcess(host, 7, ["x"], "/r/o", "/r/e", tmp_path / "o", tmp_path / "e")
    assert proc.poll() is None
    assert not (tmp_path / "o").exists()
    state["alive"] = False
    assert proc.wait(timeout=5) == 0
    assert (tmp_path / "o").read_text() == "fetched"
    proc.send_signal(15)  # after exit: no signal sent
    assert not [c for c in host.calls if c[:2] == ("agent", "signal")]


def test_cpu_ranges_on_separate_hosts_fit_each_host():
    from utils.cpu_utils import validate_explicit_cpu_ranges

    # The same CPU numbers on two hosts are not an overlap.
    validate_explicit_cpu_ranges("1-16", "1-95", host_cpus=(96, 96))
    with pytest.raises(ValueError, match="client_cpu_range names CPU 95"):
        validate_explicit_cpu_ranges("1-16", "33-95", host_cpus=(96, 36))
    with pytest.raises(ValueError, match="overlap"):
        validate_explicit_cpu_ranges("1-16", "1-95")


def test_build_dir_names_the_tls_build_apart():
    host = SimpleNamespace(root="/r")
    assert build_dir(host, "abc", tls=False) == "/r/builds/abc"
    assert build_dir(host, "abc", tls=True) == "/r/builds/abc-BUILDTLSyes"


# ---------- server on a remote host ------------------------------------------


def test_launcher_uses_the_agent_on_a_remote_host():
    host = FakeHost(
        replies={"pids": {"pids": [11, 12]}, "pin": {"layout": {"main": "2"}}}
    )
    launcher = ServerLauncher(results_dir="r", valkey_path="/r/builds/abc", host=host)
    assert launcher._get_valkey_pids() == ["11", "12"]
    launcher._signal_servers(9)
    assert (
        "agent",
        "kill",
        {"binary": "/r/builds/abc/src/valkey-server", "sig": 9},
    ) in host.calls
    launcher._run(["src/valkey-server", "--port", "1"], cwd="/r/builds/abc")
    assert ("run", ["src/valkey-server", "--port", "1"], "/r/builds/abc") in host.calls


def test_remote_ext_storage_is_refused():
    launcher = ServerLauncher(results_dir="r", valkey_path="/v", host=FakeHost())
    launcher.config = {
        "custom-server-configs": {"ext-storage-enabled": "yes", "ext-storage-path": "f"}
    }
    with pytest.raises(RuntimeError, match="ext-storage"):
        launcher._reset_ext_storage_file()


# ---------- measurement window on the hosts ----------------------------------

INFO = """# Server
redis_version:7.2.4
valkey_version:9.0.7
process_id:{pid}
# Clients
connected_clients:{clients}
# Stats
total_commands_processed:{cmds}
used_active_time_main_thread:{active}
# Keyspace
db0:keys=1000,expires=0,avg_ttl=0
"""


def test_parse_info_text_reads_the_snapshot_fields():
    info = measurement.parse_info_text(
        INFO.format(pid=5, clients=11, cmds=100, active=1.5)
    )
    snap = measurement._snapshot_from_infos([info], 3.0)
    assert snap == {
        "t": 3.0,
        "cmds": 100,
        "active_main_s": [1.5],
        "clients": 10,
        "pids": [5],
        "version": "9.0.7",
        "keys": 1000,
    }


def _window_reply(t0, span, wait_ns):
    def snap(t, cmds, active, main, sched):
        return {
            "t": t,
            "infos": [INFO.format(pid=5, clients=11, cmds=cmds, active=active)],
            "ticks": {
                "main": {"5": main},
                "sched": {"5": {"wait_ns": sched, "nvcsw": 0}},
                "io": 0.0,
                "gen": 0.0,
                "steal": [0, 100],
            },
        }

    return {
        "a": snap(t0, 1000, 10.0, 0, 0),
        "b": snap(t0 + span, 1000 + 500000, 10.0 + span * 0.9, span * 100, wait_ns),
        "main_cpus": [2],
        "local_pids": [5],
    }


def test_timed_window_reads_the_server_on_its_host(monkeypatch):
    monkeypatch.setattr(measurement.time, "sleep", lambda s: None)
    t0 = time.time()
    server = FakeHost()
    server.agent_start = lambda action, **p: (
        lambda timeout: _window_reply(t0, 2.0, 0.2e9)
    )
    gen_host = FakeHost()
    gen_host.agent_start = lambda action, **p: (
        lambda timeout: {"a": {"ticks": {"gen": 100.0}}, "b": {"ticks": {"gen": 300.0}}}
    )
    probe = SimpleNamespace(
        host=server, nodes=[{"port": 1, "cli": ["cli"]}], local=False
    )
    gen = SimpleNamespace(executor=SimpleNamespace(remote=True, host=gen_host))
    handle = SimpleNamespace(proc=SimpleNamespace(pid=99))
    a, b, ta, tb, pids, cpus, perf_text, status = measurement._timed_window(
        [handle], [(gen, None)], probe, {"pids": [5]}, {}, 2
    )
    assert b["t"] - a["t"] == pytest.approx(2.0)
    assert b["cmds"] - a["cmds"] == 500000
    assert pids == [5] and cpus == {2}
    assert tb["main"][5] - ta["main"][5] == 200
    assert tb["sched"][5]["wait_ns"] == 0.2e9
    assert tb["gen"] - ta["gen"] == 200.0


# ---------- command line -------------------------------------------------------


def _args(**kw):
    base = dict(
        server_host="u@s",
        generator_host=None,
        use_running_server=False,
        mode="both",
        target_ip="10.0.0.5",
        module_path=None,
        valkey_benchmark_path=None,
        ssh_key=None,
        ssh_known_hosts=None,
        remote_root="~/vpb",
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    "kw, message",
    [
        ({"server_host": None, "generator_host": "u@g"}, "needs --server-host"),
        ({"target_ip": "127.0.0.1"}, "needs --target-ip"),
        ({"use_running_server": True}, "--mode both"),
        ({"generator_host": "u@g"}, "--valkey-benchmark-path"),
    ],
)
def test_remote_setup_refuses_incomplete_flags(kw, message):
    with pytest.raises(ValueError, match=message):
        benchmark.remote_setup(
            _args(**kw), [{"test_groups": [{"scenarios": [{"test": "GET"}]}]}]
        )


def test_remote_setup_routes_server_connections():
    setup = benchmark.remote_setup(_args(), [{}])
    try:
        assert setup.server.private_ip == "10.0.0.5"
        assert setup.executor is None
        assert remote._forwarder is not None
    finally:
        remote.set_forwarder(None)
