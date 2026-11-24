"""vpb_result.py: every scenario config in configs/ emits vpb-result/2 records that pass the explorer's check
with every fact filled, the host facts are parsed from what the hosts print, and nothing identifies a host.

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
from dashboards.explorer import fingerprint, idscan, records, schema
from process_metrics import MetricsProcessor

ROOT = Path(__file__).resolve().parents[1]
SHA = "c" * 40
HARNESS = {"name": "valkey-perf-benchmark", "repo": "o/vpb", "sha": "d" * 40}
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
    "why": {},
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


def ctx_for(path, cfg, facts=None):
    return vpb_result.run_context(
        data_set="official-ci",
        build={"repo": "valkey-io/valkey", "ref": "unstable", "sha": SHA, "time": "2026-11-19T10:00:00Z"},
        io_threads=4,
        config=vpb_result.config_for_rerun(cfg, str(ROOT / "configs" / path)),
        rerun="",
        facts=copy.deepcopy(facts or FACTS),
    )  # fmt: skip


def emit(path, cfg, facts=None):
    proc = MetricsProcessor(SHA, False, False, "2026-11-19T10:00:00Z", None, None, "aarch64",
                            "valkey-io/valkey")  # fmt: skip
    invs = [
        {
            "scenario": s,
            "group": g["group"],
            "rows": rows_for(proc, s),
            "time": "2026-11-19T10:01:00Z",
        }
        for g in cfg["test_groups"]
        for s in g["scenarios"]
    ]
    return vpb_result.benchmark_records(cfg, invs, ctx_for(path, cfg, facts)), invs


@pytest.mark.parametrize("name", CONFIGS)
def test_every_scenario_config_records_every_fact(name, tmp_path):
    for cfg in benchmark.load_configs(str(ROOT / "configs" / name)):
        doc, invs = emit(name, cfg)
        assert doc["schema"] == "vpb-result/2"
        assert schema.check_doc(doc, name) == []
        assert vpb_result.null_fields(doc["records"]) == []
        assert doc["sources"][0]["lacks"] == []
        f = vpb_result.write(tmp_path, doc, merge=False)
        assert list(idscan.scan(f)) == []
        assert len({r["scenario"] for r in doc["records"]}) == len(
            invs
        ), "one cell per compiled scenario"
        r = doc["records"][0]
        assert r["hardware"] == {
            "server": "r7g.2xlarge",
            "cores": 4,
            "client": "c8g.4xlarge",
            "arch": "aarch64",
        }
        assert r["pinning"] == "server CPUs 0-3, client CPUs 4-7"
        assert r["parameters"]["id"] in r["scenario"]
        fp = fingerprint.fingerprint(doc["records"])
        assert fp["missing"] == [], fp["missing"]


def test_a_null_fact_is_named_with_its_reason():
    cfg = benchmark.load_configs(str(ROOT / "configs" / "sampler-smoke.json"))[0]
    facts = copy.deepcopy(FACTS)
    facts["server_host"]["cpu_microcode"] = None
    facts["build_info"]["args"] = None
    facts["why"] = {
        "host.server.cpu_microcode": "the kernel reports no microcode or CPU revision"
    }
    doc, _ = emit("sampler-smoke.json", cfg, facts)
    assert schema.check_doc(doc, "x") == []
    assert doc["sources"][0]["lacks"] == [
        "build_info.args not recorded: not reported by the run",
        "host.server.cpu_microcode not recorded: the kernel reports no microcode or CPU revision",
    ]


def test_set_scenarios_keep_shape_mix_and_server_throughput():
    cfg = benchmark.load_configs(str(ROOT / "configs" / "set-commands-smoke.json"))[0]
    doc, _ = emit("set-commands-smoke.json", cfg)
    r = next(
        x
        for x in doc["records"]
        if x["scenario"] == "intset512.exp_half.sismember.P1"
        and x["metric"] == "server_ops"
    )
    assert (r["family"], r["command"], r["mix"], r["pipeline"], r["value"]) == (
        "set commands (smoke)",
        "sismember",
        "exp_half",
        1,
        900.0,
    )
    assert r["shape"] == {
        "type": "set",
        "encoding": "intset",
        "size": 512,
        "size_unit": "members",
    }
    # expired members make the preload a hashtable (the shape's encoding_with_ttl); the shape keeps intset
    assert r["parameters"]["preload"][0]["encoding"] == "hashtable"


def test_mixed_scenario_sums_clients_and_keeps_both_commands():
    cfg = benchmark.load_configs(str(ROOT / "configs" / "tiering.json"))[0]
    doc, _ = emit("tiering.json", cfg)
    r = next(
        x
        for x in doc["records"]
        if x["scenario"] == "1.zipf80" and x["metric"] == "ops"
    )
    assert (r["clients"], r["command_line"], r["value"]) == (200, "SET | GET", 2000.0)


def test_failed_invocation_is_a_missing_cell():
    cfg = benchmark.load_configs(str(ROOT / "configs" / "sampler-smoke.json"))[0]
    proc = MetricsProcessor(SHA, False, False, "t", None, None, None, None)
    s = cfg["test_groups"][0]["scenarios"][0]
    bad = proc.build_base_metadata(s["test"], 64, 1, 4, duration=15)
    bad.update(status="failed", error="No results")
    good = rows_for(proc, s)
    ctx = ctx_for("sampler-smoke.json", cfg)
    one = vpb_result.benchmark_records(
        cfg, [{"scenario": s, "group": 1, "rows": [bad]}], ctx
    )
    r = one["records"][0]
    assert (r["metric"], r["value"], r["missing"], r["runs"][0]["why"]) == (
        "ops",
        None,
        "no valid run",
        "No results",
    )
    both = vpb_result.benchmark_records(
        cfg,
        [
            {"scenario": s, "group": 1, "rows": [bad]},
            {"scenario": s, "group": 1, "rows": good},
        ],
        ctx,
    )
    r = next(x for x in both["records"] if x["metric"] == "ops")
    assert (r["value"], r["n"], len(r["runs"])) == (1000.0, 1, 2)


def test_write_merges_each_invocation_into_the_results_file(tmp_path):
    cfg = benchmark.load_configs(str(ROOT / "configs" / "sampler-smoke.json"))[0]
    doc, _ = emit("sampler-smoke.json", cfg)
    vpb_result.write(tmp_path, doc)
    vpb_result.write(tmp_path, doc)
    merged = json.loads((tmp_path / vpb_result.FILENAME).read_text())
    assert schema.check_doc(merged, "merged") == []
    assert len(merged["records"]) == len(doc["records"])
    assert all(r["n"] == 2 for r in merged["records"])


def matrix_doc(rows_extra=None, **kw):
    cfg = config_loader.load(
        str(ROOT / "configs" / "matrix" / "perf-matrix.json"), profile="slice-iot4-p1"
    )[0]
    cells = [c for g in cfg["test_groups"] for c in g["scenarios"]]
    built = {
        "info": FACTS["build_info"],
        "time": "2026-11-19T10:00:00+00:00",
        "why": {},
    }
    rows = [
        {"scenario_id": c["id"], "build": label, "commit": sha, "run": rep, "status": "ok",
         "timestamp": "2026-11-19T10:00:00+00:00", "server_ops_s": 1000.0 + rep, "generator_ops_s": 990.0,
         "p50_ms": 0.3, "p99_ms": 0.9, "p999_ms": None, "window_server_build": built,
         "window_server_config": {"io-threads": "4"}, **(rows_extra or {})}
        for c in cells for label, sha in (("unstable", "a" * 40), ("candidate", "b" * 40)) for rep in (1, 2, 3)
    ]  # fmt: skip
    manifest = {
        "vpb_hosts": {"server": HOST, "server_cpus": "0-63", "generators": [FACTS["client_host"]], "why": {}},
        "memtier": "memtier_benchmark 2.5.2", "valkey_benchmark": "valkey-benchmark 9.0.0", "reps": 3,
    }  # fmt: skip
    doc = vpb_result.matrix_records(cfg, cells, rows, manifest, data_set="community", repository=None,
                                    config_path=str(ROOT / "configs" / "matrix" / "perf-matrix.json"),
                                    server_cpus={c["id"]: "1-6" for c in cells}, client_cpus="16-31",
                                    network="network, separate hosts", **kw)  # fmt: skip
    return doc, cells


def test_matrix_run_records_every_fact_per_cell_build_and_metric():
    doc, cells = matrix_doc()
    assert schema.check_doc(doc, "matrix") == []
    assert vpb_result.null_fields(doc["records"]) == []
    assert len(doc["records"]) == len(cells) * 2 * len(vpb_result.MATRIX_METRICS)
    r = next(
        x
        for x in doc["records"]
        if x["scenario"] == "hget"
        and x["metric"] == "server_ops"
        and x["build"]["ref"] == "candidate"
    )
    assert (r["family"], r["command"], r["io_threads"], r["clients"], r["value"]) == (
        "perf matrix",
        "hget",
        4,
        512,
        1002.0,
    )
    assert r["shape"] == {
        "type": "hash",
        "encoding": None,
        "size": 10,
        "size_unit": "members",
    }
    assert r["hardware"] == {
        "server": "r7g.2xlarge",
        "cores": 6,
        "client": "c8g.4xlarge",
        "arch": "aarch64",
    }
    assert (
        r["load_generator"] == "memtier_benchmark 2.5.2"
        and r["parameters"]["io_threads"] == 4
    )
    assert [x["p"] for x in r["runs"]] == ["r1", "r2", "r3"]
    p999 = next(
        x
        for x in doc["records"]
        if x["scenario"] == "hget" and x["metric"] == "p999_ms"
    )
    assert p999["value"] is None and p999["missing"] == "no valid run"


def test_matrix_rows_without_server_facts_name_the_gap():
    doc, _ = matrix_doc({"window_server_build": None, "window_server_config": None})
    lacks = doc["sources"][0]["lacks"]
    assert "server_config not recorded: no rep reached the server's CONFIG GET" in lacks
    assert any(x.startswith("build_info.version not recorded: no rep") for x in lacks)


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
    host, cpus, why = vpb_result.parse_host(ARM_OUT)
    assert why == {} and cpus == "60-63"
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
    host, cpus, why = vpb_result.parse_host(X86_OUT)
    assert why == {}
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


def test_a_host_that_reports_nothing_names_every_null():
    host, cpus, why = vpb_result.parse_host("@@kernel\n6.1\n")
    nulls = {k for k, v in host.items() if v is None}
    assert nulls and nulls == set(why) and cpus is None


def test_collect_host_runs_the_script_through_the_runner():
    seen = {}

    def run(cmd, timeout=60, stdin=None):
        seen.update(cmd=cmd, stdin=stdin)
        return 0, ARM_OUT, ""

    host, _, _ = vpb_result.collect_host(run, pid=42, peer="127.0.0.1; rm -rf x")
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
    assert s["server_config"] == {"io-threads": "4"}
    assert (
        "--server-ssh" in s["why"]["host.server.os"]
        and "--server-ssh" in s["why"]["build_info.binary_sha256"]
    )


def test_build_args_come_from_vpbs_build_stamp(tmp_path):
    def run(cmd, timeout=60, stdin=None):
        if cmd.startswith("sha256sum"):
            return 0, "f" * 64 + "  x\n", ""
        return 0, f"{SHA} tls=no args=BUILD_EXT_STORAGE=yes\n", ""

    binfo, why = vpb_result.build_info(
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
    assert why == {}
    binfo, why = vpb_result.build_info(
        {}, lambda *a, **k: (0, "", ""), "/v/src/valkey-server", SHA
    )
    assert binfo["args"] is None and "not built by VPB" in why["build_info.args"]


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


# ---------- schema
def test_a_v1_record_upgrades_with_its_fingerprint_unchanged():
    v1 = json.loads(
        json.dumps(
            emit(
                "sampler-smoke.json",
                benchmark.load_configs(str(ROOT / "configs" / "sampler-smoke.json"))[0],
            )[0]
        )
    )
    for r in v1["records"]:
        for k in schema.V2_FIELDS:
            r.pop(k)
    v1 = schema.make_doc(v1["sources"], v1["records"], v1["reproduce"], v1["metrics"])
    assert v1["schema"] == "vpb-result/1" and schema.check_doc(v1, "v1") == []
    up = [schema.upgrade(r) for r in v1["records"]]
    assert fingerprint.for_records(up) == v1["fingerprints"]


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
        d["records"][0]["parameters"][bad] = "x"
        assert any(
            "names a host identifier" in e for e in schema.check_doc(d, "x")
        ), bad
    d = copy.deepcopy(doc)
    d["records"][0]["host"]["server"]["hostname"] = "x"
    assert schema.check_doc(d, "x")


def test_idscan_flags_macs_uuids_zones_and_cloud_hostnames(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"a": "0a:1b:2c:3d:4e:5f", "b": "12345678-aaaa-4bbb-8ccc-1234567890ab", "c": "us-east-1a", '
                 '"d": "use1-az4", "e": "ec2-3-4-5-6", "f": "set:a:000000000000", "g": "6.12.115-140.210"}')  # fmt: skip
    hits = "\n".join(idscan.scan(f))
    for what in ("MAC address", "UUID", "availability zone", "public cloud hostname"):
        assert what in hits
    assert "set:a:" not in hits and "6.12.115" not in hits


def test_merge_of_v1_and_v2_files_is_v2():
    cfg = benchmark.load_configs(str(ROOT / "configs" / "sampler-smoke.json"))[0]
    v2, _ = emit("sampler-smoke.json", cfg)
    v1 = json.loads(json.dumps(v2))
    for r in v1["records"]:
        for k in schema.V2_FIELDS:
            r.pop(k)
        r["build"]["sha"] = "f" * 40
    v1 = schema.make_doc(v1["sources"], v1["records"], v1["reproduce"], v1["metrics"])
    m = records.merge_docs([v1, v2])
    assert m["schema"] == "vpb-result/2" and schema.check_doc(m, "m") == []


def test_cpu_count_and_build_ref():
    assert (
        vpb_result.cpu_count("0-8"),
        vpb_result.cpu_count("0,2-3"),
        vpb_result.cpu_count("1,1-2"),
    ) == (9, 3, 2)
    assert vpb_result.build_ref("a" * 40, None, None) == "unknown"
    assert vpb_result.build_ref("v8.1.0", None, None) == "v8.1.0"
    assert vpb_result.build_ref("a" * 40, "unstable", None) == "unstable"
