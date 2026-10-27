"""CPU layout v1: parsing, planning, thread and IRQ placement, row records,
legacy normalization and config validation."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import cpu_layout
from benchmark import _resolve_io_threads_list, load_configs, validate_config
from run_manifest import manifest_id
from valkey_server import ServerLauncher

ONLINE = list(range(16))
EIGHT = {"cores": 8, "layout": "main, 3x io, 3x irq, aux", "first_cpu": 1}


@pytest.fixture(autouse=True)
def sixteen_cpus(monkeypatch):
    monkeypatch.setattr(cpu_layout, "online_cpus", lambda: list(ONLINE))


# ---------- parsing and validation -------------------------------------------


@pytest.mark.parametrize(
    "cores, layout, io_threads, workers",
    [
        (1, "main+irq+aux", 1, 1),
        (2, "main+irq, io+irq+aux", 2, 2),
        (2, "main, io+poller+irq+aux", 2, 2),
        (4, "irq, main, io, aux", 2, 2),
        (8, "main, 3x io, 3x irq, aux", 4, 4),
        (13, "4x irq, main, 7x io, aux", 8, 8),
    ],
)
def test_spec_examples(cores, layout, io_threads, workers):
    value = {"cores": cores, "layout": layout}
    cpu_layout.validate_cpu_layout(value)
    assert cpu_layout.io_threads_for(value) == io_threads
    assert cpu_layout.worker_cpus_for(cpu_layout.parse_layout(layout)) == workers


def test_parse_repeats_and_shares():
    assert cpu_layout.parse_layout("main+irq, 2x io") == [
        ["main", "irq"],
        ["io"],
        ["io"],
    ]


@pytest.mark.parametrize(
    "value, message",
    [
        ({"cores": 3, "layout": "main, io"}, "2 slots"),
        ({"cores": 2, "layout": "main, main"}, "exactly one 'main'"),
        ({"cores": 2, "layout": "io, aux"}, "exactly one 'main'"),
        ({"cores": 2, "layout": "main, gpu"}, "not in"),
        ({"cores": 2, "layout": "main, 2x proactor"}, "slots"),
        ({"cores": 3, "layout": "main, 2x proactor"}, "Dragonfly"),
        ({"cores": 2, "layout": "main, io+io"}, "repeats"),
        ({"cores": 3, "layout": "main, poller, poller"}, "more than once"),
        ({"cores": 1, "layout": "main", "first_cpu": -1}, "first_cpu"),
        ({"cores": 1, "layout": "main", "cpus": "1"}, "does not support"),
        ({"cores": 0, "layout": "main"}, "positive"),
        ({"cores": 1, "layout": "3 io"}, "role"),
        ("main", "must be an object"),
    ],
)
def test_validate_rejects(value, message):
    with pytest.raises(ValueError, match=message):
        cpu_layout.validate_cpu_layout(value)


def test_first_cpu_default():
    assert cpu_layout.resolve_first_cpu({"cores": 8}, list(range(16))) == 1
    assert cpu_layout.resolve_first_cpu({"cores": 2}, [0, 1]) == 0
    assert cpu_layout.resolve_first_cpu({"cores": 2, "first_cpu": 5}, [0, 1]) == 5


def test_plan_manifest():
    m = cpu_layout.plan(EIGHT)
    assert m["schema"] == 1
    assert m["cores"] == 8 and m["worker_cpus"] == 4
    assert m["cpus"] == "1-8"
    assert m["slots"][0] == {"cpu": 1, "roles": ["main"]}
    assert m["slots"][-1] == {"cpu": 8, "roles": ["aux"]}
    assert m["server_config"] == {"io-threads": 4}
    assert m["label"] == "8 cores (main + 3 io + 3 irq + aux)"


def test_plan_label_shared_slots():
    m = cpu_layout.plan({"cores": 2, "layout": "main+irq, io+irq+aux", "first_cpu": 0})
    assert m["label"] == "2 cores (main/irq + io/irq/aux)"


def test_plan_rejects_offline_cpus():
    with pytest.raises(ValueError, match="not online"):
        cpu_layout.plan({"cores": 4, "layout": "main, 3x io", "first_cpu": 14})


def test_check_disjoint():
    cpu_layout.check_disjoint(EIGHT, ["9-15"])
    with pytest.raises(ValueError, match="overlap"):
        cpu_layout.check_disjoint(EIGHT, ["8-15"])


# ---------- thread placement -------------------------------------------------

NAMES = {
    100: "valkey-server",
    101: "bio_close_file",
    104: "jemalloc_bg_thd",
    110: "io_thd_1",
    111: "io_thd_2",
    112: "io_thd_3",
    120: "io_epoll",
}


def test_thread_targets_follow_slots():
    want, notes = cpu_layout.thread_targets(cpu_layout.plan(EIGHT), 100, NAMES)
    assert want[100] == {1}
    assert [want[t] for t in (110, 111, 112)] == [{2}, {3}, {4}]
    assert want[120] == {4}
    assert notes["poller"] == "implicit"
    assert want[101] == want[104] == {8}
    assert notes["unplaced"] == []


def test_thread_targets_poller_repl_and_no_aux():
    m = cpu_layout.plan({"cores": 3, "layout": "main, io, poller", "first_cpu": 0})
    names = {
        1: "valkey-server",
        2: "io_thd_1",
        3: "io_epoll",
        4: "repl_sender",
        5: "bio_aof",
    }
    want, notes = cpu_layout.thread_targets(m, 1, names)
    assert want[3] == {2} and want[4] == {2}
    assert want[5] == {0}  # no aux slot: main's CPU
    assert "poller" not in notes


def test_thread_targets_reports_thread_slot_mismatch():
    m = cpu_layout.plan({"cores": 2, "layout": "main, io", "first_cpu": 0})
    names = {1: "valkey-server", 2: "io_thd_1", 3: "io_thd_2"}
    _, notes = cpu_layout.thread_targets(m, 1, names)
    assert notes["unplaced"] == ["io_thd_2:3 (no io slot)"]
    m = cpu_layout.plan({"cores": 3, "layout": "main, 2x io", "first_cpu": 0})
    _, notes = cpu_layout.thread_targets(m, 1, {1: "valkey-server", 2: "io_thd_1"})
    assert notes["unplaced"] == ["1 io slot(s) without a thread"]


def test_pin_threads_sets_and_verifies(monkeypatch):
    aff = {tid: set(range(1, 9)) for tid in NAMES}
    monkeypatch.setattr(cpu_layout, "thread_names", lambda pid: dict(NAMES))
    monkeypatch.setattr(cpu_layout.os, "sched_getaffinity", lambda t: set(aff[t]))
    monkeypatch.setattr(
        cpu_layout.os, "sched_setaffinity", lambda t, c: aff.__setitem__(t, set(c))
    )
    applied = cpu_layout.pin_threads(cpu_layout.plan(EIGHT), 100)
    assert applied["process_cpus"] == "1-8"
    assert applied["threads"]["main:100"] == "1"
    assert applied["threads"]["io_thd_3:112"] == "4"
    assert applied["threads"]["io_epoll:120"] == "4"
    assert applied["threads"]["bio_close_file:101"] == "8"
    assert applied["unplaced"] == [] and applied["poller"] == "implicit"


def test_pin_threads_records_refused_thread(monkeypatch):
    monkeypatch.setattr(cpu_layout, "thread_names", lambda pid: dict(NAMES))
    monkeypatch.setattr(cpu_layout.os, "sched_getaffinity", lambda t: {1, 2})
    monkeypatch.setattr(cpu_layout.os, "sched_setaffinity", lambda t, c: None)
    applied = cpu_layout.pin_threads(cpu_layout.plan(EIGHT), 100)
    assert "main:100 on 1-2" in applied["unplaced"]


# ---------- IRQ and RPS placement --------------------------------------------


class FakeSys(cpu_layout.SysIO):
    def __init__(self, files=None, dirs=None, privileged=True, addrs=""):
        self.files = dict(files or {})
        self.dirs = dict(dirs or {})
        self.priv = privileged
        self.addrs = addrs
        self.writes = []

    def read(self, path):
        return self.files.get(path)

    def listdir(self, path):
        return list(self.dirs.get(path, []))

    def privileged(self):
        return None if self.priv else "not root and no passwordless sudo"

    def write(self, path, value):
        self.writes.append((path, value))
        self.files[path] = value
        if path.endswith("smp_affinity_list"):
            self.files[path.replace("smp_", "effective_")] = value
        return None

    def addresses(self):
        return self.addrs


RPS = "/sys/class/net/lo/queues/rx-0/rps_cpus"
ADDRS = "2: ens5    inet 10.0.0.5/24 brd 10.0.0.255 scope global ens5\n"


def _nic(**kw):
    return FakeSys(
        files={
            "/proc/interrupts": "  40:  1 2 PCI-MSI ens5-Tx-Rx-0\n  41:  1 2 PCI-MSI ens5-Tx-Rx-1\n"
            "  42:  1 2 PCI-MSI ens5-Tx-Rx-2\n  43:  1 2 PCI-MSI ens5-Tx-Rx-3\n",
            **kw.pop("files", {}),
        },
        dirs={
            "/sys/class/net/ens5/device/msi_irqs": ["40", "41", "42", "43"],
            "/sys/class/net/ens5/queues": ["rx-0", "rx-1", "tx-0", "tx-1"],
        },
        addrs=ADDRS,
        **kw,
    )


def test_loopback_sets_rps_on_irq_cpus():
    fs = FakeSys()
    out = cpu_layout.place_irqs(cpu_layout.plan(EIGHT), "127.0.0.1", fs)
    assert fs.writes == [(RPS, "000000e0")]
    assert out["rps"] == {"lo/rx-0": "5-7"}
    assert out["irq_placement"] == "applied"
    assert out["irqs"] == {} and out["problems"] == []


def test_loopback_without_privileges_is_recorded_not_invalid():
    fs = FakeSys(files={RPS: "0"}, privileged=False)
    out = cpu_layout.place_irqs(cpu_layout.plan(EIGHT), "localhost", fs)
    assert out["irq_placement"].startswith("not applied")
    assert out["problems"] == [] and fs.writes == []
    assert out["rps"] == {"lo/rx-0": ""}


def test_nic_irqs_round_robin_over_irq_cpus():
    fs = _nic()
    out = cpu_layout.place_irqs(cpu_layout.plan(EIGHT), "10.0.0.5", fs)
    assert out["interface"] == "ens5" and out["nic_queues"] == 2
    assert [v for _, v in fs.writes] == ["5", "6", "7", "5"]
    assert out["irqs"]["40:ens5-Tx-Rx-0"] == "5"
    assert out["irqs"]["43:ens5-Tx-Rx-3"] == "5"
    assert out["irq_placement"] == "applied" and out["problems"] == []


def test_nic_without_privileges_invalidates():
    out = cpu_layout.place_irqs(
        cpu_layout.plan(EIGHT), "10.0.0.5", _nic(privileged=False)
    )
    assert out["irq_placement"].startswith("not applied")
    assert out["problems"] and "not applied" in out["problems"][0]


def test_nic_without_irq_slot_needs_irqs_inside_layout():
    m = cpu_layout.plan({"cores": 2, "layout": "main, io+aux", "first_cpu": 1})
    eff = {f"/proc/irq/{i}/effective_affinity_list": "1" for i in (40, 41, 42)}
    eff["/proc/irq/43/effective_affinity_list"] = "12"
    out = cpu_layout.place_irqs(m, "10.0.0.5", _nic(files=eff))
    assert out["irq_placement"] == "none requested"
    assert out["problems"] == ["NIC IRQs outside the layout CPUs: 43:ens5-Tx-Rx-3"]


def test_unknown_target_with_irq_slots_invalidates():
    out = cpu_layout.place_irqs(
        cpu_layout.plan(EIGHT), "10.9.9.9", FakeSys(addrs=ADDRS)
    )
    assert out["interface"] is None and out["problems"]


def test_cpu_mask_round_trip():
    assert cpu_layout.cpu_mask([0, 33]) == "00000002,00000001"
    assert cpu_layout.mask_cpus("00000002,00000001") == [0, 33]
    assert cpu_layout.mask_cpus("e0") == [5, 6, 7]


def test_apply_layout_collects_problems(monkeypatch):
    monkeypatch.setattr(cpu_layout.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        cpu_layout,
        "pin_threads",
        lambda m, pid: {
            "process_cpus": "1-8",
            "threads": {},
            "unplaced": ["x:1 (gone)"],
        },
    )
    done = cpu_layout.apply_layout(
        cpu_layout.plan(EIGHT), 1, "10.0.0.5", _nic(privileged=False)
    )
    problems = done["applied"]["problems"]
    assert any("IRQ placement" in p for p in problems)
    assert "threads not pinned: x:1 (gone)" in problems
    assert done["host"]["online_cpus"] == 16 and done["host"]["nic_queues"] == 2


# ---------- config validation ------------------------------------------------


def _cfg(**extra):
    cfg = {
        "cluster_mode": False,
        "tls_mode": False,
        "client_cpu_range": "9-15",
        "cpu_layout": dict(EIGHT),
        "test_groups": [
            {"group": 1, "scenarios": [{"id": "s", "type": "read", "test": "GET"}]}
        ],
    }
    cfg.update(extra)
    return cfg


def test_example_config_loads():
    path = (
        Path(__file__).resolve().parent.parent
        / "configs"
        / "offbench-concept-local.json"
    )
    cfgs = load_configs(str(path))
    assert cfgs[0]["cpu_layout"] == EIGHT
    assert _resolve_io_threads_list(cfgs[0]) == [4]
    assert "thread_pinning" in cfgs[1]  # legacy entry still valid


def test_config_accepts_layout():
    validate_config(_cfg())
    validate_config(_cfg(**{"io-threads": 4}))
    validate_config(_cfg(**{"custom-server-configs": {"io-threads": "4"}}))


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"io-threads": 3}, "io-threads 4"),
        ({"io-threads": [4, 8]}, r"io-threads \[8\]"),
        ({"custom-server-configs": {"io-threads": 2}}, "io-threads 4"),
        ({"server_cpu_range": "1-8"}, "replaces 'server_cpu_range'"),
        ({"thread_pinning": {"main": "1"}}, "replaces 'thread_pinning'"),
        ({"client_cpu_range": "8-15"}, "overlap"),
        ({"cpu_layout": {"cores": 2, "layout": "main"}}, "slots"),
        ({"cluster_nodes": 3}, "cluster_nodes"),
    ],
)
def test_config_rejects(extra, message):
    with pytest.raises(ValueError, match=message):
        validate_config(_cfg(**extra))


def test_config_requires_client_range():
    cfg = _cfg()
    del cfg["client_cpu_range"]
    with pytest.raises(ValueError, match="client_cpu_range"):
        validate_config(cfg)


def test_scenario_layout_validated_and_legacy_keys_allowed():
    cfg = _cfg(server_cpu_range="1-4", **{"io-threads": 2})
    del cfg["cpu_layout"]
    sc = cfg["test_groups"][0]["scenarios"][0]
    sc["cpu_layout"] = {"cores": 3, "layout": "main, io, aux", "first_cpu": 1}
    validate_config(cfg)
    sc["cpu_layout"] = {"cores": 3, "layout": "main, 2x io", "first_cpu": 1}
    with pytest.raises(ValueError, match="scenarios\\[0\\].cpu_layout"):
        validate_config(cfg)


# ---------- server launch and scenario restarts ------------------------------


def test_launch_starts_under_layout(tmp_path):
    sl = ServerLauncher(
        results_dir=str(tmp_path), valkey_path="/tmp/valkey", cpu_range="0-3"
    )
    calls = {}
    with (
        patch.object(sl, "_reset_ext_storage_file"),
        patch.object(sl, "_launch_server", side_effect=lambda **kw: calls.update(kw)),
        patch.object(sl, "_apply_layout") as apply,
        patch.object(sl, "_pin_threads") as legacy_pin,
    ):
        sl.launch(cluster_mode=False, tls_mode=False, io_threads=None, config=_cfg())
    assert calls["cpu_range"] == "1-8" and calls["io_threads"] == 4
    assert apply.call_args[0][1]["label"] == "8 cores (main + 3 io + 3 irq + aux)"
    legacy_pin.assert_not_called()
    assert sl.active_layout == EIGHT


def test_launch_legacy_keeps_cpu_range_and_thread_pinning(tmp_path):
    sl = ServerLauncher(
        results_dir=str(tmp_path), valkey_path="/tmp/valkey", cores="1-4"
    )
    assert sl.cpu_range == "1-4" and sl.cores == "1-4"
    calls = {}
    cfg = {"thread_pinning": {"main": "2"}}
    with (
        patch.object(sl, "_reset_ext_storage_file"),
        patch.object(sl, "_launch_server", side_effect=lambda **kw: calls.update(kw)),
        patch.object(sl, "_pin_threads") as legacy_pin,
    ):
        sl.launch(cluster_mode=False, tls_mode=False, io_threads=3, config=cfg)
    assert calls["cpu_range"] is None and calls["io_threads"] == 3
    legacy_pin.assert_called_once()
    assert sl.layout_manifest is None


def test_build_command_uses_given_range():
    sl = ServerLauncher(results_dir="r", valkey_path="/tmp/valkey")
    sl.config = {}
    cmd = sl._build_server_command(16379, None, "1-8", False, False, 4, None, "log")
    assert cmd[:3] == ["taskset", "-c", "1-8"]
    assert cmd[cmd.index("--io-threads") + 1] == "4"


def test_scenario_layout_change_restarts(minimal_client_runner):
    runner = minimal_client_runner
    runner.config = _cfg()
    runner.server_launcher = MagicMock(active_layout=dict(EIGHT))
    other = {"cores": 4, "layout": "irq, main, io, aux", "first_cpu": 1}
    with patch.object(runner, "_restart_server") as restart:
        runner._prepare_server_state({"id": "a"}, {})
        restart.assert_not_called()
        runner._prepare_server_state({"id": "b", "cpu_layout": other}, {})
        restart.assert_called_once_with(other)
        runner._prepare_server_state({"id": "c", "restart_before": True}, {})
        assert restart.call_args[0][0] == EIGHT


# ---------- rows and legacy normalization ------------------------------------


def _applied(problems=()):
    m = cpu_layout.plan(EIGHT)
    m["applied"] = {
        "threads": {"main:1": "1"},
        "unplaced": [],
        "problems": list(problems),
    }
    return m


def test_stamp_row_records_layout():
    row = cpu_layout.stamp_row({"io_threads": 1, "valid": True}, _applied())
    assert row["cores"] == 8 and row["worker_cpus"] == 4 and row["io_threads"] == 4
    assert row["cpu_layout_label"] == "8 cores (main + 3 io + 3 irq + aux)"
    assert row["cpu_layout"]["applied"]["threads"] == {"main:1": "1"}
    assert row["valid"] is True


def test_stamp_row_invalidates_on_problems():
    row = {"valid": False, "invalid_reasons": "main 50.0% busy < 90%"}
    cpu_layout.stamp_row(row, _applied(["IRQ placement not applied: no sudo"]))
    assert row["valid"] is False
    assert row["invalid_reasons"] == (
        "main 50.0% busy < 90%; IRQ placement not applied: no sudo"
    )


def test_normalize_new_row():
    row = cpu_layout.stamp_row({}, _applied())
    m = cpu_layout.normalize_layout(row)
    assert m["legacy"] is False and m["cores"] == 8 and m["worker_cpus"] == 4


def test_normalize_legacy_from_thread_pinning():
    row = {
        "io_threads": 3,
        "thread_pinning": "main=2,io_thd_1=3,io_thd_2=4,bio_close_file=1",
    }
    m = cpu_layout.normalize_layout(row)
    assert m["legacy"] is True
    assert (m["worker_cpus"], m["cores"], m["cpus"]) == (3, 4, "1-4")
    assert m["label"] == "3 worker cores (4 CPUs total)"
    assert "cpu_layout_label" not in row  # never mutates


def test_normalize_legacy_pinning_with_cpu_lists():
    row = {"io_threads": 2, "thread_pinning": "main=2,io_thd_1=3,bio_aof=1,5"}
    assert cpu_layout.normalize_layout(row)["cores"] == 4


def test_normalize_legacy_from_run_manifest():
    m = cpu_layout.normalize_layout(
        {"io_threads": 8}, {"config": {"server_cpu_range": "0-11"}}
    )
    assert (m["worker_cpus"], m["cores"]) == (8, 12)
    assert m["cores_source"] == "server_cpu_range"


def test_normalize_legacy_unknown_total():
    m = cpu_layout.normalize_layout({"io_threads": 4})
    assert m["cores"] is None and m["cores_reason"]
    assert m["label"] == "4 worker cores (unknown CPUs total)"
    assert cpu_layout.normalize_layout({})["label"] == (
        "worker cores unknown (unknown CPUs total)"
    )


def test_annotate_rows_uses_manifests(tmp_path):
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests" / "abc.json").write_text(
        json.dumps({"config": {"server_cpu_range": "1-4"}})
    )
    new = cpu_layout.stamp_row({}, _applied())
    rows = [{"io_threads": 3, "manifest_id": "abc"}, {"io_threads": 2}, new]
    cpu_layout.annotate_rows(rows, tmp_path / "manifests")
    assert rows[0]["cpu_layout_label"] == "3 worker cores (4 CPUs total)"
    assert rows[0]["layout_legacy"] is True and rows[0]["cores"] == 4
    assert "cores" not in rows[1] and rows[1]["worker_cpus"] == 2
    assert rows[2]["cpu_layout_label"] == "8 cores (main + 3 io + 3 irq + aux)"
    assert "layout_legacy" not in rows[2]


def test_ab_summary_groups_by_layout_label(tmp_path):
    from utils import ab_summary

    d = tmp_path / "c1"
    d.mkdir()
    new = cpu_layout.stamp_row({"server_ops_s": 1.0, "io_threads": 4}, _applied())
    old = {"server_ops_s": 2.0, "io_threads": 4}
    (d / "metrics.json").write_text(json.dumps([new, old]))
    rows = ab_summary.load_rows(tmp_path, "c1")
    keys = {dict(ab_summary.scenario_key(r))["cpu_layout_label"] for r in rows}
    assert keys == {
        "8 cores (main + 3 io + 3 irq + aux)",
        "4 worker cores (unknown CPUs total)",
    }


def test_compare_loader_annotates(tmp_path):
    from utils.compare_benchmark_results import load_benchmark_data

    p = tmp_path / "metrics.json"
    p.write_text(json.dumps([{"io_threads": 1, "rps": 1.0}]))
    assert load_benchmark_data(str(p))[0]["cpu_layout_label"] == (
        "1 worker cores (unknown CPUs total)"
    )


def test_manifest_id_ignores_applied_readback():
    base = {"host": {"cpus": 16}, "argv": ["x"], "cpu_layout": _applied()}
    other = json.loads(json.dumps(base))
    other["cpu_layout"]["applied"]["threads"] = {"main:999": "1"}
    assert manifest_id(base) == manifest_id(other)
    other["cpu_layout"]["layout"] = "something else"
    assert manifest_id(base) != manifest_id(other)
