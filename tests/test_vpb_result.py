"""vpb_result.py: every scenario config in configs/ writes vpb-result run files that pass the explorer's check
with every fingerprint fact filled, one file per build and fingerprint; the host facts are parsed from what the
hosts print, and nothing identifies a host.

The rows are built with the runners' own code (MetricsProcessor for benchmark.py, the matrix row fields for
matrix_runner.py), one invocation per compiled scenario, so each config's scenario shapes go through the writer.
"""

import copy
import json
from pathlib import Path

import pytest

import benchmark
import config_loader
import vpb_result
from dashboards.explorer import fingerprint, idscan, schema
from process_metrics import MetricsProcessor

ROOT = Path(__file__).resolve().parents[1]
SHA = "c" * 40
HARNESS = {"repo": "o/vpb", "sha": "d" * 40}
CONFIGS = sorted(p.name for p in (ROOT / "configs").glob("*.json"))
BENCH = {
    "rps": 1000.0,
    "avg_latency_ms": 0.5,
    "p50_latency_ms": 0.4,
    "p95_latency_ms": 0.9,
    "p99_latency_ms": 1.2,
}
HOST = {
    "instance_type": "r7g.2xlarge",
    "arch": "aarch64",
    "cpu_model": "Arm Neoverse-V1 r1p1",
    "cpu_microcode": "midr 0x411fd401 revidr 0x1",
    "cpu_topology": "8 CPUs, 1 sockets x 8 cores x 1 threads, 1 NUMA nodes",
    "memory": "62 GiB",
    "nic": "ena, speed not reported",
    "virtualization": "vm amazon",
    "cgroup_cpu_limit": "none",
    "os": "Amazon Linux 2023",
    "kernel": "6.1.0",
    "kernel_boot_options": "ro quiet",
    "kernel_runtime_settings": {"thp": "madvise", "aslr": "full"},
}
FACTS = {
    "server_host": HOST,
    "client_host": dict(HOST, instance_type="c8g.4xlarge"),
    "server_cpus": "0-3",
    "client_cpus": "4-7",
    "build_info": {
        "version": "9.0.0",
        "settings": {"malloc": "jemalloc-5.3.0", "bits": "64"},
        "args": ["BUILD_TLS=no"],
        "binary_sha256": "e" * 64,
    },
    "server_config": {"io-threads": "4", "save": ""},
    "load_generator": "valkey-benchmark 9.0.0",
    "network_path": "network, separate hosts",
    "io_threads": 4,
}
BUILD = {
    "repo": "valkey-io/valkey",
    "ref": "unstable",
    "sha": SHA,
    "time": "2026-11-19T10:00:00Z",
}


@pytest.fixture(autouse=True)
def facts(monkeypatch):
    monkeypatch.setattr(vpb_result, "harness", lambda: HARNESS)


def rows_for(proc, scenario):
    """The metric rows one invocation of scenario writes to metrics.json."""
    subs = (
        scenario.get("writes", []) + scenario.get("reads", [])
        if scenario.get("type") == "mixed"
        else [scenario]
    )
    rows = []
    for sub in subs:
        row = proc.create_metrics(
            BENCH,
            sub.get("command") or sub.get("test"),
            sub.get("data_size", scenario.get("data_size", 100)),
            sub.get("pipeline", scenario.get("pipeline", 1)),
            sub.get("clients", scenario.get("clients", 1)),
            duration=scenario.get("duration"),
        )
        row["status"] = "success"
        if scenario.get("window"):
            row.update(server_ops_s=900.0, window_complete=True, window_error_replies=0)
        rows.append(row)
    return rows


def invocations(cfg):
    proc = MetricsProcessor(SHA, False, False, "2026-11-19T10:00:00Z", None, None, "aarch64",
                            "valkey-io/valkey")  # fmt: skip
    return [
        {
            "scenario": s,
            "group": g["group"],
            "rows": rows_for(proc, s),
            "time": "2026-11-19T10:01:00Z",
        }
        for g in cfg["test_groups"]
        for s in g["scenarios"]
    ]


def emit(path, cfg, facts=None, invs=None):
    invs = invs if invs is not None else invocations(cfg)
    doc = vpb_result.benchmark_run(
        cfg,
        invs,
        data_set="official-ci",
        build=BUILD,
        config=vpb_result.harness_config(cfg, str(ROOT / "configs" / path)),
        facts=copy.deepcopy(facts or FACTS),
    )
    return doc, invs


def nulls(run):
    return [k for k, v in fingerprint.fields(run).items() if v is None]


@pytest.mark.parametrize("name", CONFIGS)
def test_every_scenario_config_writes_one_run_with_every_fact(name, tmp_path):
    for cfg in benchmark.load_configs(str(ROOT / "configs" / name)):
        doc, invs = emit(name, cfg)
        assert doc["schema"] == "vpb-result" and schema.check_doc(doc, name) == []
        # the config is named by its path in the harness, so the inline copy is the only null
        assert nulls(doc["run"]) == ["config_inline"]
        assert doc["run"]["harness"]["config"] == f"configs/{name}"
        f = vpb_result.write_run(tmp_path / name, doc, merge=False)
        assert f.parent.name == "vpb-runs" and f.name == doc["run"]["id"] + ".json"
        assert list(idscan.scan(f)) == []
        assert len(doc["cells"]) == len(invs), "one cell per compiled scenario"
        fp = doc["run"]["fingerprint"]
        assert (fp["io_threads"], fp["cores"], fp["pinning"]) == (
            4,
            4,
            "server CPUs 0-3, client CPUs 4-7",
        )
        assert (
            doc["run"]["id"].startswith("2026-11-19-") and SHA[:9] in doc["run"]["id"]
        )
        assert doc["run"]["reproduce"].count("python benchmark.py") == 1


def test_a_fact_the_host_cannot_report_is_null():
    cfg = benchmark.load_configs(str(ROOT / "configs" / "sampler-smoke.json"))[0]
    facts = copy.deepcopy(FACTS)
    facts["server_host"]["cpu_microcode"] = None
    facts["build_info"]["args"] = None
    doc, _ = emit("sampler-smoke.json", cfg, facts)
    assert schema.check_doc(doc, "x") == []
    assert nulls(doc["run"]) == ["build_args", "config_inline", "cpu_microcode"]


def test_set_scenarios_keep_shape_mix_and_server_throughput():
    cfg = benchmark.load_configs(str(ROOT / "configs" / "set-commands-smoke.json"))[0]
    doc, _ = emit("set-commands-smoke.json", cfg)
    c = next(x for x in doc["cells"] if x["id"] == "intset512.exp_half.sismember.P1")
    assert (c["family"], c["command"], c["mix"], c["pipeline"]) == (
        "set commands (smoke)",
        "sismember",
        "exp_half",
        1,
    )
    assert c["shape"] == {
        "type": "set",
        "encoding": "intset",
        "size": 512,
        "size_unit": "members",
    }
    assert (
        c["metrics"]["server_ops"]["median"] == 900.0
        and c["runs"][0]["server_ops"] == 900.0
    )


def test_mixed_scenario_sums_clients_and_keeps_both_commands():
    cfg = benchmark.load_configs(str(ROOT / "configs" / "tiering.json"))[0]
    doc, _ = emit("tiering.json", cfg)
    c = next(x for x in doc["cells"] if x["id"] == "1.zipf80")
    assert (c["clients"], c["command_line"], c["metrics"]["ops"]["median"]) == (
        200,
        "SET | GET",
        2000.0,
    )


def test_failed_invocation_is_a_missing_cell_and_a_later_one_fills_it():
    cfg = benchmark.load_configs(str(ROOT / "configs" / "sampler-smoke.json"))[0]
    proc = MetricsProcessor(SHA, False, False, "t", None, None, None, None)
    s = cfg["test_groups"][0]["scenarios"][0]
    bad = proc.build_base_metadata(s["test"], 64, 1, 4, duration=15)
    bad.update(status="failed", error="No results")
    one, _ = emit(
        "sampler-smoke.json", cfg, invs=[{"scenario": s, "group": 1, "rows": [bad]}]
    )
    c = one["cells"][0]
    assert (c["metrics"], c["missing"], c["runs"][0]["ok"], c["runs"][0]["why"]) == (
        {},
        "No results",
        False,
        "No results",
    )
    both, _ = emit(
        "sampler-smoke.json",
        cfg,
        invs=[
            {"scenario": s, "group": 1, "rows": [bad]},
            {"scenario": s, "group": 1, "rows": rows_for(proc, s)},
        ],
    )
    c = both["cells"][0]
    assert (
        c["missing"],
        c["metrics"]["ops"]["median"],
        [r["rep"] for r in c["runs"]],
    ) == (None, 1000.0, [1, 2])


def test_write_joins_a_later_invocation_of_the_same_run(tmp_path):
    cfg = benchmark.load_configs(str(ROOT / "configs" / "sampler-smoke.json"))[0]
    doc, _ = emit("sampler-smoke.json", cfg)
    p1 = vpb_result.write_run(tmp_path, doc)
    p2 = vpb_result.write_run(tmp_path, copy.deepcopy(doc))
    assert p1 == p2 and len(list((tmp_path / "vpb-runs").glob("*.json"))) == 1
    merged = json.loads(p1.read_text())
    assert schema.check_doc(merged, "merged") == []
    assert all([r["rep"] for r in c["runs"]] == [1, 2] for c in merged["cells"])
    other = copy.deepcopy(FACTS)
    other["io_threads"] = 8  # another setup: another run, another file
    p3 = vpb_result.write_run(tmp_path, emit("sampler-smoke.json", cfg, other)[0])
    assert p3 != p1 and len(list((tmp_path / "vpb-runs").glob("*.json"))) == 2


def matrix_docs(io_threads=None, rows_extra=None):
    cfg = config_loader.load(
        str(ROOT / "configs" / "matrix" / "perf-matrix.json"), profile="slice-iot4-p1"
    )[0]
    cells_ = [c for g in cfg["test_groups"] for c in g["scenarios"]]
    for c, n in zip(cells_, io_threads or []):
        c["io_threads"] = n
    built = {"info": FACTS["build_info"], "time": "2026-11-19T10:00:00+00:00"}
    rows = [
        {"scenario_id": c["id"], "build": label, "commit": sha, "run": rep, "status": "ok",
         "timestamp": f"2026-11-19T10:0{rep}:00+00:00", "server_ops_s": 1000.0 + rep, "generator_ops_s": 990.0,
         "p50_ms": 0.3, "p99_ms": 0.9, "p999_ms": None, "window_server_build": built,
         "window_server_config": {"io-threads": str(c["io_threads"])}, **(rows_extra or {})}
        for c in cells_ for label, sha in (("unstable", "a" * 40), ("candidate", "b" * 40)) for rep in (1, 2, 3)
    ]  # fmt: skip
    manifest = {
        "vpb_hosts": {"server": HOST, "server_cpus": "0-63", "generators": [FACTS["client_host"]]},
        "memtier": "memtier_benchmark 2.5.2", "valkey_benchmark": "valkey-benchmark 9.0.0", "reps": 3,
    }  # fmt: skip
    docs = vpb_result.matrix_runs(cfg, cells_, rows, manifest, data_set="community", repository=None,
                                  config_path=str(ROOT / "configs" / "matrix" / "perf-matrix.json"),
                                  server_cpus={c["id"]: "1-6" for c in cells_}, client_cpus="16-31",
                                  network="network, separate hosts")  # fmt: skip
    return docs, cells_


def test_matrix_run_writes_one_file_per_build_and_fingerprint():
    docs, cells_ = matrix_docs()
    assert sorted(d["run"]["build"]["ref"] for d in docs) == ["candidate", "unstable"]
    for d in docs:
        assert schema.check_doc(d, d["run"]["id"]) == []
        assert nulls(d["run"]) == ["config_inline"]
        assert len(d["cells"]) == len(cells_)
    d = next(x for x in docs if x["run"]["build"]["ref"] == "candidate")
    c = next(x for x in d["cells"] if x["id"] == "hget")
    assert (
        c["family"],
        c["command"],
        c["clients"],
        c["metrics"]["server_ops"]["median"],
    ) == ("perf matrix", "hget", 512, 1002.0)
    assert c["shape"] == {
        "type": "hash",
        "encoding": None,
        "size": 10,
        "size_unit": "members",
    }
    assert [r["pair"] for r in c["runs"]] == ["r1", "r2", "r3"] and "p999_ms" not in c[
        "metrics"
    ]
    fp = d["run"]["fingerprint"]
    assert (
        fp["io_threads"],
        fp["cores"],
        fp["load_generator"],
        fp["server"]["instance_type"],
    ) == (4, 6, "memtier_benchmark 2.5.2", "r7g.2xlarge")
    assert (d["run"]["started"], d["run"]["finished"]) == (
        "2026-11-19T10:01:00+00:00",
        "2026-11-19T10:03:00+00:00",
    )


def test_matrix_cells_with_other_io_threads_are_other_runs():
    docs, cells_ = matrix_docs(io_threads=[4, 4, 8] + [4] * 20)
    assert len(docs) == 4
    eight = [d for d in docs if d["run"]["fingerprint"]["io_threads"] == 8]
    assert len(eight) == 2 and all(
        [c["id"] for c in d["cells"]] == [cells_[2]["id"]] for d in eight
    )


def test_matrix_rows_without_server_facts_leave_those_facts_null():
    docs, _ = matrix_docs(
        rows_extra={"window_server_build": None, "window_server_config": None}
    )
    run = docs[0]["run"]
    assert {"server_config", "build_version", "build_binary_sha256"} <= set(nulls(run))
    assert schema.check_doc(docs[0], "x") == []


# ---------- host facts
ARM_OUT = """@@kernel
6.12.115-140.210.amzn2023.aarch64
@@arch
aarch64
@@os
Amazon Linux 2023.12.20261120
@@lscpu
Architecture:                            aarch64
CPU(s):                                  64
Thread(s) per core:                      1
Core(s) per cluster:                     64
Socket(s):                               -
Cluster(s):                              1
NUMA node(s):                            1
L3 cache:                                32 MiB (1 instance)
@@cpuinfo
processor\t: 0
CPU implementer\t: 0x41
CPU variant\t: 0x1
CPU part\t: 0xd40
CPU revision\t: 1
@@midr
0x00000000411fd401
@@revidr
0x0000000000000001
@@mem
518835404 kB
@@vm
amazon
@@container
docker
@@dmi_vendor
Amazon EC2
@@dmi_product
r7g.16xlarge
@@imds

@@cpumax
max 100000
@@affinity
60-63
@@cmdline
BOOT_IMAGE=(hd0,gpt1)/boot/vmlinuz-6.12 root=UUID=12345678-aaaa-4bbb-8ccc-1234567890ab ro console=ttyS0,115200n8 ip=10.0.0.5 quiet
@@thp
always defer defer+madvise [madvise] never
@@thp_defrag
always defer defer+madvise [madvise] never
@@aslr
2
@@numa_balancing
0
@@governor
@@boost
@@no_turbo
@@idle
@@nic
dev=lo
"""

X86_OUT = """@@kernel
6.1.0
@@arch
x86_64
@@os
Ubuntu 24.04 LTS
@@lscpu
CPU(s):                                  16
Model name:                              AMD EPYC 9R14
Thread(s) per core:                      2
Core(s) per socket:                      8
Socket(s):                               1
NUMA node(s):                            1
@@cpuinfo
processor\t: 0
microcode\t: 0xa101148
@@mem
65000000 kB
@@vm
none
@@container
none
@@imds
m7a.4xlarge
@@cpumax
400000 100000
@@affinity
0-15
@@cmdline
root=/dev/nvme0n1p1 mitigations=off
@@thp
always [never]
@@thp_defrag
[always] never
@@aslr
0
@@numa_balancing
1
@@governor
performance
@@boost
@@no_turbo
1
@@idle
1
1
@@nic
dev=eth0
driver=ena
speed=-1
"""


def test_host_facts_from_an_arm_vm_in_a_container():
    host, cpus = vpb_result.parse_host(ARM_OUT)
    assert cpus == "60-63"
    assert host == {
        "instance_type": "r7g.16xlarge",  # DMI, when the metadata endpoint is out of reach
        "arch": "aarch64",
        "cpu_model": "Arm Neoverse-V1 r1p1",
        "cpu_microcode": "midr 0x00000000411fd401 revidr 0x0000000000000001",
        "cpu_topology": "64 CPUs, 1 sockets x 64 cores x 1 threads, 1 NUMA nodes, L3 32 MiB",
        "memory": "495 GiB",
        "nic": "loopback",
        "virtualization": "vm amazon, container docker",
        "cgroup_cpu_limit": "none",
        "os": "Amazon Linux 2023.12.20261120",
        "kernel": "6.12.115-140.210.amzn2023.aarch64",
        "kernel_boot_options": "ro console=ttyS0,115200n8 quiet",
        "kernel_runtime_settings": {"thp": "madvise", "thp_defrag": "madvise", "aslr": "full",
                                    "numa_balancing": "off", "cpufreq_governor": "not available",
                                    "turbo": "not available", "idle_states": "not available"},
    }  # fmt: skip


def test_host_facts_from_x86_bare_metal():
    host, _ = vpb_result.parse_host(X86_OUT)
    assert (host["instance_type"], host["cpu_model"], host["cpu_microcode"]) == (
        "m7a.4xlarge",
        "AMD EPYC 9R14",
        "0xa101148",
    )
    assert (host["virtualization"], host["cgroup_cpu_limit"], host["nic"]) == (
        "bare metal",
        "4 CPUs",
        "ena, speed not reported",
    )
    assert host["kernel_boot_options"] == "mitigations=off"
    assert (
        host["kernel_runtime_settings"]["turbo"] == "off"
        and host["kernel_runtime_settings"]["idle_states"] == "all disabled"
    )


def test_a_host_that_reports_nothing_leaves_its_facts_null():
    host, cpus = vpb_result.parse_host("@@kernel\n6.1\n")
    assert host["kernel"] == "6.1" and cpus is None
    assert {k for k, v in host.items() if v is None} >= {
        "instance_type",
        "cpu_model",
        "os",
        "memory",
    }


def test_collect_host_runs_the_script_through_the_runner():
    seen = {}

    def run(cmd, timeout=60, stdin=None):
        seen.update(cmd=cmd, stdin=stdin)
        return 0, ARM_OUT, ""

    host, _ = vpb_result.collect_host(run, pid=42, peer="127.0.0.1; rm -rf x")
    assert (
        seen["cmd"] == "VPB_PID=42 bash -s" and seen["stdin"] == vpb_result.HOST_SCRIPT
    )
    assert host["arch"] == "aarch64"


def test_server_on_an_unreachable_host_records_what_info_says():
    info = {"os": "Linux 6.1.0 aarch64", "valkey_version": "9.0.0", "mem_allocator": "jemalloc-5.3.0",
            "arch_bits": 64, "process_id": 7, "executable": "/opt/valkey/src/valkey-server"}  # fmt: skip
    s = vpb_result.server_side(
        info,
        {"io-threads": "4", "requirepass": "x"},
        run=None,
        sha=SHA,
        instance_type="r7g.2xlarge",
    )
    assert (s["host"]["kernel"], s["host"]["arch"], s["host"]["instance_type"]) == (
        "6.1.0",
        "aarch64",
        "r7g.2xlarge",
    )
    assert (
        s["build_info"]["version"] == "9.0.0"
        and s["build_info"]["binary_sha256"] is None
    )
    assert s["server_config"] == {"io-threads": "4"} and s["cpus"] is None


def test_build_args_come_from_vpbs_build_stamp():
    def run(cmd, timeout=60, stdin=None):
        if cmd.startswith("sha256sum"):
            return 0, "f" * 64 + "  x\n", ""
        return 0, f"{SHA} tls=no args=BUILD_EXT_STORAGE=yes\n", ""

    binfo = vpb_result.build_info(
        {"valkey_version": "9.0.0", "mem_allocator": "libc"},
        run,
        "/v/src/valkey-server",
        SHA,
    )
    assert binfo == {
        "version": "9.0.0",
        "settings": {"malloc": "libc"},
        "args": ["BUILD_TLS=no", "BUILD_EXT_STORAGE=yes"],
        "binary_sha256": "f" * 64,
    }
    binfo = vpb_result.build_info(
        {}, lambda *a, **k: (0, "", ""), "/v/src/valkey-server", SHA
    )
    assert binfo["args"] is None


def test_config_get_never_asks_for_secrets_and_falls_back_per_key():
    asked = []

    def old_server(*keys):
        asked.append(keys)
        if len(keys) > 1:
            raise RuntimeError("ERR wrong number of arguments")
        return [keys[0], "1"]

    got = vpb_result.config_get(
        old_server, ["requirepass", "ext-storage-path", "maxmemory"]
    )
    flat = {k for ks in asked for k in ks}
    assert (
        "requirepass" not in flat
        and "ext-storage-path" not in flat
        and "maxmemory" in got
    )
    assert vpb_result.server_config(
        {"tls-key-file": "k", "io-threads": 4, "masterauth": "x"}
    ) == {"io-threads": "4"}


def test_boot_options_drop_devices_paths_and_addresses():
    line = "BOOT_IMAGE=/boot/vmlinuz root=UUID=12345678-aaaa-4bbb-8ccc-1234567890ab ro ip=10.0.0.5::10.0.0.1 hostname=h1 mitigations=off isolcpus=4-7"
    assert vpb_result.boot_options(line) == "ro mitigations=off isolcpus=4-7"


def test_check_rejects_host_identifier_keys():
    doc, _ = emit(
        "sampler-smoke.json",
        benchmark.load_configs(str(ROOT / "configs" / "sampler-smoke.json"))[0],
    )
    for bad in (
        "hostname",
        "instance_id",
        "private_ip",
        "availability_zone",
        "mac_address",
    ):
        d = copy.deepcopy(doc)
        d["run"]["fingerprint"]["server_config"][bad] = "x"
        assert any(
            "names a host identifier" in e for e in schema.check_doc(d, "x")
        ), bad
    d = copy.deepcopy(doc)
    d["cells"][0]["runs"][0]["hostname"] = "x"
    assert schema.check_doc(d, "x")


def test_idscan_flags_macs_uuids_zones_and_cloud_hostnames(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"a": "0a:1b:2c:3d:4e:5f", "b": "12345678-aaaa-4bbb-8ccc-1234567890ab", "c": "us-east-1a", '
                 '"d": "use1-az4", "e": "ec2-3-4-5-6", "f": "set:a:000000000000", "g": "6.12.115-140.210"}')  # fmt: skip
    hits = "\n".join(idscan.scan(f))
    for what in ("MAC address", "UUID", "availability zone", "public cloud hostname"):
        assert what in hits
    assert "set:a:" not in hits and "6.12.115" not in hits


def test_cpu_count_and_build_ref():
    assert (
        vpb_result.cpu_count("0-8"),
        vpb_result.cpu_count("0,2-3"),
        vpb_result.cpu_count("1,1-2"),
    ) == (9, 3, 2)
    assert (
        vpb_result.cpu_list("60,61-61,61") == "60-61"
        and vpb_result.cpu_count(None) is None
    )
    assert vpb_result.build_ref("a" * 40, None, None) == "unknown"
    assert vpb_result.build_ref("v8.1.0", None, None) == "v8.1.0"
    assert vpb_result.build_ref("a" * 40, "unstable", None) == "unstable"
