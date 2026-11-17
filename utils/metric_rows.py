"""Turn metrics.json entries into benchmark_metrics rows.

``push_to_postgres.py`` and ``build_static_dashboard.py`` share these helpers,
so the static dashboard sees the same rows and columns as the
``benchmark_metrics`` table. Pure standard library: no database driver needed.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DESCRIPTION_MAX_LENGTH = 500
CONFIG_NAME_MAX_LENGTH = 50

# Columns the database fills in itself; they never come from metrics.json.
AUTO_COLUMNS = ("id", "created_at")
TIMESTAMP_COLUMNS = ("timestamp", "module_commit_timestamp")
MAX_TEXT_LENGTHS = {
    "group_description": DESCRIPTION_MAX_LENGTH,
    "scenario_description": DESCRIPTION_MAX_LENGTH,
    "config_name": CONFIG_NAME_MAX_LENGTH,
}


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp, returning None when empty or invalid."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None


def load_metrics(metrics_file: Path, test_type: str) -> List[Any]:
    """Read a metrics.json list and tag every entry with ``test_type``.

    Returns an empty list when the file holds no entries.
    """
    with open(metrics_file, encoding="utf-8") as f:
        metrics_data = json.load(f)
    if not metrics_data:
        return []
    for metric in metrics_data:
        if isinstance(metric, dict):
            metric["test_type"] = test_type
    return metrics_data


def normalize_metrics(
    metrics_data: List[Any],
) -> Tuple[List[Dict[str, Any]], int]:
    """Keep the entries that can become rows and cut long text to column width.

    An entry is skipped when it is not a non-empty dict or lacks ``timestamp``
    or ``commit``. Returns the kept entries (copies) and the skipped count.
    """
    rows = []
    skipped = 0
    for i, metric in enumerate(metrics_data):
        if not metric or not isinstance(metric, dict):
            print(f"  Skipping entry {i+1}: invalid data")
            skipped += 1
            continue
        if not metric.get("timestamp") or not metric.get("commit"):
            print(f"  Skipping entry {i+1}: missing required fields")
            skipped += 1
            continue
        row = dict(metric)
        for column, limit in MAX_TEXT_LENGTHS.items():
            value = row.get(column)
            if isinstance(value, str) and len(value) > limit:
                row[column] = value[:limit]
        rows.append(row)
    return rows, skipped


def metric_columns(rows: List[Dict[str, Any]]) -> List[str]:
    """Return the table columns for ``rows``: every field name, sorted."""
    return sorted({field for row in rows for field in row})
