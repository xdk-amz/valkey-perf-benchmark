"""Row fields that are results or run metadata, never scenario configuration.

Comparisons group rows by their configuration fields, so these must be left
out of the grouping key.
"""

MEASURED_FIELDS = frozenset(
    {
        "generator_version",
        "generator_argv",
        "generator_errors",
        "generator_timeouts",
        "generator_conns_failed",
        "generator_samples",
        "generator_latency_scope",
        "p90_latency_ms",
        "p999_latency_ms",
        "p9999_latency_ms",
        "server_ops_s",
        "window_s",
        "main_busy_pct",
        "main_busy_source",
        "main_cpu_pct",
        "main_runq_wait_pct",
        "main_nvcsw_per_s",
        "steal_pct",
        "steal_scope",
        "io_threads_cores",
        "generator_cores",
        "connected_clients",
        "server_asserts",
        "server_version",
        "valid",
        "invalid_reasons",
        "run_index",
        "order_slot",
        "manifest_id",
        "thread_pinning",
        "pinning_warnings",
        "cyc_per_cmd",
        "ins_per_cmd",
        "ipc",
        "l1i_miss_per_cmd",
        "l1d_miss_per_cmd",
        "br_miss_per_cmd",
        "perf_status",
    }
)


_FLOAT = "DOUBLE PRECISION"
_INT = "INTEGER"
_TEXT = "TEXT"

# Postgres column types for row fields whose type cannot be read off one
# sample: an integer-looking first value would make a float column INTEGER, a
# null one TEXT, and a short first string a VARCHAR(50) that later rows
# overflow.
FIELD_TYPES = {
    # The type a float sample of these upstream columns already gets.
    **{
        k: "DECIMAL(15,6)"
        for k in (
            "rps",
            "avg_latency_ms",
            "min_latency_ms",
            "p50_latency_ms",
            "p95_latency_ms",
            "p99_latency_ms",
            "max_latency_ms",
        )
    },
    **{
        k: _FLOAT
        for k in (
            "p90_latency_ms",
            "p999_latency_ms",
            "p9999_latency_ms",
            "server_ops_s",
            "window_s",
            "main_busy_pct",
            "main_cpu_pct",
            "main_runq_wait_pct",
            "main_nvcsw_per_s",
            "steal_pct",
            "io_threads_cores",
            "generator_cores",
            "cyc_per_cmd",
            "ins_per_cmd",
            "ipc",
            "l1i_miss_per_cmd",
            "l1d_miss_per_cmd",
            "br_miss_per_cmd",
        )
    },
    **{
        k: _INT
        for k in (
            "generator_errors",
            "generator_timeouts",
            "generator_conns_failed",
            "generator_samples",
            "connected_clients",
            "server_asserts",
            "run_index",
            "order_slot",
            "rate",
        )
    },
    "valid": "BOOLEAN",
    **{
        k: _TEXT
        for k in (
            "invalid_reasons",
            "generator",
            "generator_version",
            "generator_argv",
            "generator_latency_scope",
            "main_busy_source",
            "steal_scope",
            "server_version",
            "manifest_id",
            "thread_pinning",
            "pinning_warnings",
            "perf_status",
            "acl_user",
        )
    },
}
