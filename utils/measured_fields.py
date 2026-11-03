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
        )
    },
    **{
        k: _INT
        for k in (
            "generator_errors",
            "generator_timeouts",
            "generator_conns_failed",
            "generator_samples",
            "rate",
        )
    },
    **{
        k: _TEXT
        for k in (
            "generator",
            "generator_version",
            "generator_argv",
            "generator_latency_scope",
        )
    },
}
