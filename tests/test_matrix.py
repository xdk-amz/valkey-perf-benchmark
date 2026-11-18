"""Tests for the matrix compiler, generator pairing, gates and verdicts."""

import copy
import json
from pathlib import Path

import pytest

import config_loader
import generators
import matrix
import matrix_runner

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "configs" / "matrix" / "ratatoskr.json"


def base_config():
    return json.loads(CONFIG.read_text())[0]


def compile_with(profile, overrides=None):
    cfg = config_loader.prepare(base_config(), str(CONFIG), profile, overrides)
    return [c for g in cfg["test_groups"] for c in g["scenarios"]], cfg


def tiny_profile(**scenarios):
    return {
        "name": "t",
        "run": {"reps": 2},
        "values": {
            "generator": "memtier",
            "pipeline_depth": 1,
            "connections": 8,
            "client_threads": 2,
            "generator_hosts": 1,
            "access_pattern": "uniform",
            "keys": 1000,
            "preload_keys": 1000,
            "key_size": 16,
            "value_size": 32,
            "io_threads": 2,
            "server_mode": "CMD",
            "tls": False,
            "server_config": {},
            "run_duration": 4,
            "window": [1, 3],
        },
        "scenarios": scenarios or {"string": {"read_fraction": 0.8, "ttl_range": None}},
    }


# ---------------------------------------------------------------- expansion


def test_shipped_profiles_compile():
    cells, cfg = compile_with("slice-iot4-p1")
    assert len(cells) == 13
    assert {c["template"] for c in cells} == set(base_config()["matrix"]["scenarios"])
    assert all(c["io_threads"] == 4 and c["pipeline"] == 1 for c in cells)
    assert cfg["matrix_run"]["reps"] == 5
    assert [g["group"] for g in cfg["test_groups"]] == list(range(1, 14))
    smoke, _ = compile_with("smoke-loopback")
    assert len(smoke) == 22


def test_lists_cross_into_cells_with_ids_from_varying_parameters():
    profile = tiny_profile(string={"read_fraction": [0.5, 1.0], "ttl_range": None})
    profile["values"]["pipeline_depth"] = [1, 16]
    cells, _ = compile_with(profile)
    assert sorted(c["id"] for c in cells) == [
        "string-pipeline_depth1-read_fraction0.5",
        "string-pipeline_depth1-read_fraction1.0",
        "string-pipeline_depth16-read_fraction0.5",
        "string-pipeline_depth16-read_fraction1.0",
    ]


def test_connections_are_independent_of_pipeline_depth():
    profile = tiny_profile()
    profile["values"]["pipeline_depth"] = [1, 64]
    cells, _ = compile_with(profile)
    assert {c["connections"] for c in cells} == {8}
    assert {c["pipeline"] for c in cells} == {1, 64}


def test_connections_must_split_over_hosts_and_threads():
    profile = tiny_profile()
    profile["values"]["connections"] = 9
    with pytest.raises(ValueError, match="do not split exactly"):
        compile_with(profile)


def test_window_must_end_within_the_run():
    profile = tiny_profile()
    profile["values"]["window"] = [1, 5]
    with pytest.raises(ValueError, match="ends after run_duration"):
        compile_with(profile)


def test_missing_and_unknown_parameters_are_rejected():
    profile = tiny_profile(hget={"fields_per_hash": 10, "field_selector": "index"})
    with pytest.raises(ValueError, match="no value for 'field_index'"):
        compile_with(profile)
    profile = tiny_profile()
    profile["values"]["colour"] = "red"
    with pytest.raises(ValueError, match="undefined parameter 'colour'"):
        compile_with(profile)


def test_field_index_names_an_existing_field():
    cells, _ = compile_with(
        tiny_profile(
            hget={"fields_per_hash": 10, "field_selector": "index", "field_index": 3}
        )
    )
    assert cells[0]["commands"][0]["command"] == "HGET {key} f3"
    with pytest.raises(ValueError, match="outside 0..9"):
        compile_with(
            tiny_profile(
                hget={
                    "fields_per_hash": 10,
                    "field_selector": "index",
                    "field_index": 10,
                }
            )
        )


def test_selector_not_allowed_by_template():
    with pytest.raises(ValueError, match="allows"):
        compile_with(
            tiny_profile(hget={"fields_per_hash": 10, "field_selector": "new"})
        )


def test_hot_keys_split_commands():
    profile = tiny_profile(string={"read_fraction": 0.8, "ttl_range": None})
    profile["values"].update(access_pattern="hotkey", hot_fraction=0.5, hot_keys=2)
    cells, _ = compile_with(profile)
    cmds = cells[0]["commands"]
    hot = [c for c in cmds if c.get("hot")]
    assert len(hot) == 4  # GET and SET on each of 2 hot keys
    assert abs(sum(c["weight"] for c in hot) - 0.5) < 1e-9
    assert {c["command"].split()[1] for c in hot} == {"key:xxxxxxxx1", "key:xxxxxxxx2"}


def test_cme_multi_key_templates_share_a_hash_tag():
    profile = tiny_profile(mset={"keys_per_call": 3})
    profile["values"].update(
        server_mode="CME", generator="valkey-benchmark", key_size=20
    )
    cells, _ = compile_with(profile)
    assert cells[0]["key_prefix"].startswith("{mk}")
    assert cells[0]["cluster_mode"] is True


def test_memtier_rejects_multi_key_commands_in_cme():
    profile = tiny_profile(mget={"keys_per_call": 3})
    profile["values"]["server_mode"] = "CME"
    with pytest.raises(ValueError, match="cluster_multi_key"):
        compile_with(profile)


def test_gate_error_replies():
    reasons = matrix_runner.gate_reasons(
        GATES, 8, 8, {}, {"h": {}}, ops=900, error_replies=100
    )
    assert reasons == ["10.0% of replies in the window were errors (limit 1%)"]


def test_override_changes_one_profile_value():
    cells, _ = compile_with(
        "slice-iot4-p1", {"matrix": {"profile": {"values": {"io_threads": 8}}}}
    )
    assert {c["io_threads"] for c in cells} == {8}


# ---------------------------------------------------------------- pairing


@pytest.mark.parametrize(
    "scenario,values,missing",
    [
        ({"string": {"read_fraction": 0.8, "ttl_range": None}}, {}, "mixed_ratio"),
        ({"string": {"read_fraction": 1.0, "ttl_range": [1, 5]}}, {}, "random_ttl"),
        (
            {"string": {"read_fraction": 1.0, "ttl_range": None}},
            {"access_pattern": "zipfian", "zipf_alpha": 0.99},
            "zipfian",
        ),
        (
            {"sadd": {"set_size": 10, "member_selector": "new"}},
            {},
            "per_request_random_data",
        ),
    ],
)
def test_valkey_benchmark_rejects_what_it_cannot_express(scenario, values, missing):
    profile = tiny_profile(**scenario)
    profile["values"].update(generator="valkey-benchmark", key_size=20, **values)
    with pytest.raises(ValueError, match=f"cannot express.*{missing}"):
        compile_with(profile)


def test_no_generator_offers_random_elements():
    for name in generators.GENERATORS:
        profile = tiny_profile(
            sismember={"set_size": 10, "member_selector": "random_existing"}
        )
        profile["values"].update(generator=name, key_size=20)
        with pytest.raises(ValueError, match="random_element"):
            compile_with(profile)


def test_valkey_benchmark_runs_what_it_can():
    profile = tiny_profile(
        hget={"fields_per_hash": 10, "field_selector": "index", "field_index": 3}
    )
    profile["values"].update(generator="valkey-benchmark", key_size=20)
    cells, _ = compile_with(profile)
    argv = generators.get("valkey-benchmark").argv(
        cells[0],
        binary="vb",
        host="127.0.0.1",
        port=7000,
        connections=8,
        threads=2,
        duration=4,
    )
    assert argv[-3:] == ["HGET", "key:xxxx__rand_int__", "f3"]
    assert ["-r", "1000"] == argv[argv.index("-r") : argv.index("-r") + 2]


def test_memtier_argv_ratio_mode_and_command_mode():
    cells, _ = compile_with(
        tiny_profile(string={"read_fraction": 0.8, "ttl_range": [1, 5]})
    )
    mt = generators.get("memtier")
    argv = mt.argv(
        cells[0], binary="mt", host="h", port=1, connections=8, threads=2, duration=4
    )
    assert argv[argv.index("--ratio") + 1] == "200:800"
    assert argv[argv.index("--expiry-range") + 1] == "1-5"
    assert argv[argv.index("-c") + 1] == "4"
    cells, _ = compile_with(
        tiny_profile(sadd={"set_size": 10, "member_selector": "new"})
    )
    argv = mt.argv(
        cells[0], binary="mt", host="h", port=1, connections=8, threads=2, duration=4
    )
    assert argv[argv.index("--command") + 1] == "SADD __key__ __data__"


def test_key_size_too_short_is_rejected():
    profile = tiny_profile()
    profile["values"]["key_size"] = 5
    with pytest.raises(ValueError, match="too short"):
        compile_with(profile)


def test_preload_key_names_match_the_generator():
    mt, vb = generators.get("memtier"), generators.get("valkey-benchmark")
    assert mt.key_name("key:", 7) == "key:8"
    assert vb.key_name("key:", 7) == "key:000000000007"


def test_write_preload_counts(tmp_path):
    spec = {
        "type": "stream",
        "keys": 3,
        "value_size": 4,
        "elements": 2,
        "fields_per_entry": 1,
        "key_prefix": "k",
    }
    assert (
        matrix_runner.write_preload(spec, generators.get("memtier"), tmp_path / "p")
        == 6
    )
    data = (tmp_path / "p").read_bytes()
    assert data.count(b"XADD") == 6 and b"$2\r\nk1\r\n" in data


# ---------------------------------------------------------------- gates


GATES = dict(matrix_runner.DEFAULT_GATES, rtt_gate_ms=0.15)


def test_gates_pass():
    assert (
        matrix_runner.gate_reasons(GATES, 512, 512, {"h": 40.0}, {"h": {"ops_s": 1}})
        == []
    )


def test_gate_all_clients_connected():
    reasons = matrix_runner.gate_reasons(GATES, 512, 500, {"h": 40.0}, {"h": {}})
    assert reasons == ["500 of 512 clients connected at the window end"]


def test_gate_client_busy():
    reasons = matrix_runner.gate_reasons(GATES, 8, 8, {"h": 95.0}, {"h": {}})
    assert reasons and reasons[0].startswith("client-bound")


def test_gate_generator_error():
    reasons = matrix_runner.gate_reasons(GATES, 8, 8, {}, {"h": {"error": "boom"}})
    assert reasons == ["generator error on h: boom"]


def test_rtt_bound_flag():
    assert matrix_runner.rtt_bound(GATES, 0.1, 0.15)
    assert not matrix_runner.rtt_bound(GATES, 0.1, 0.5)


def test_cpu_busy_from_proc_stat():
    a = "cpu  0 0 0 0 0 0 0 0\ncpu0 10 0 10 80 0 0 0 0\ncpu1 0 0 0 100 0 0 0 0\n"
    b = "cpu  0 0 0 0 0 0 0 0\ncpu0 40 0 40 120 0 0 0 0\ncpu1 0 0 0 200 0 0 0 0\n"
    first, second = matrix_runner.cpu_times(a, [0]), matrix_runner.cpu_times(b, [0])
    assert matrix_runner.busy_pct(first, second) == 60.0
    assert matrix_runner.cpu_list("1-3,8") == [1, 2, 3, 8]


def test_window_throughput_excludes_info():
    first = {
        "server_time_usec": "1000000",
        "cmdstat_get": "calls=100,usec=1",
        "cmdstat_info": "calls=1",
    }
    second = {
        "server_time_usec": "3000000",
        "cmdstat_get": "calls=2100,usec=1",
        "cmdstat_info": "calls=2",
    }
    out = matrix_runner.window_throughput(first, second)
    assert out["server_ops_s"] == 1000.0 and "info" not in out["by_command"]


# ---------------------------------------------------------------- verdicts


RULES = matrix_runner.DEFAULT_VERDICT


def test_verdict_higher_tie_and_variance():
    v = matrix_runner.verdict("a", "b", [100, 101, 102], [120, 121, 119], RULES)
    assert v["verdict"] == "b higher" and v["ratio"] == pytest.approx(1.1881, rel=1e-3)
    v = matrix_runner.verdict("a", "b", [100, 101, 102], [103, 104, 102], RULES)
    assert v["verdict"] == "tie"
    v = matrix_runner.verdict("a", "b", [100, 70, 130], [100, 101, 99], RULES)
    assert v["high_variance"] and v["verdict"] == "tie"


def test_verdict_overlap_is_a_tie_even_past_the_band():
    v = matrix_runner.verdict("a", "b", [100, 130], [110, 140], RULES)
    assert v["verdict"] == "tie"


def test_verdict_missing_with_reason_and_one_sided_flag():
    v = matrix_runner.verdict(
        "a",
        "b",
        [100, 101],
        [],
        RULES,
        [],
        ["500 of 512 clients connected at the window end"],
    )
    assert v["verdict"] == "MISSING" and "b: 0 valid reps" in v["reason"]
    assert v["flags"] == ["b drops connections"]


def test_profile_run_scope_parameters_stay_out_of_cells():
    profile = tiny_profile()
    profile["values"]["reps"] = 3
    with pytest.raises(ValueError, match="run parameter 'reps'"):
        compile_with(profile)


def test_matrix_cannot_be_combined_with_test_groups():
    cfg = base_config()
    cfg["test_groups"] = []
    cfg["matrix"]["profile"] = copy.deepcopy(tiny_profile())
    with pytest.raises(ValueError, match="cannot be combined"):
        matrix.compile_config(cfg)
