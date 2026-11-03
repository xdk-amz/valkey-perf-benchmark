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
        "io_threads_cores",
        "generator_cores",
        "connected_clients",
        "server_asserts",
        "valid",
        "invalid_reasons",
        "run_index",
        "cyc_per_cmd",
        "ins_per_cmd",
        "ipc",
        "l1i_miss_per_cmd",
        "l1d_miss_per_cmd",
        "br_miss_per_cmd",
        "perf_status",
    }
)
