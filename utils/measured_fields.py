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
    }
)
