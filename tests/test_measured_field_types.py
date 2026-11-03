"""The measured-field column types agree with dashboards/schema.sql."""

import re
from pathlib import Path

from utils.measured_fields import FIELD_TYPES, MEASURED_FIELDS
from utils.push_to_postgres import analyze_metrics_schema

SCHEMA = Path(__file__).resolve().parents[1] / "dashboards" / "schema.sql"


def _schema_columns():
    text = SCHEMA.read_text()
    cols = dict(re.findall(r"ADD COLUMN IF NOT EXISTS (\w+) ([A-Z ]+?)[,;]", text))
    return {k: v.strip() for k, v in cols.items()}


def test_schema_lists_every_typed_measured_field():
    cols = _schema_columns()
    for field, typ in FIELD_TYPES.items():
        if typ.startswith("DECIMAL"):
            continue  # upstream columns created with the table
        assert cols.get(field) == typ, field
    assert set(cols) <= set(FIELD_TYPES)


def test_measured_fields_have_types():
    assert MEASURED_FIELDS - set(FIELD_TYPES) == set()


def test_types_do_not_depend_on_the_first_sample():
    schema = analyze_metrics_schema(
        [{"server_ops_s": 1, "invalid_reasons": "", "avg_latency_ms": None}]
    )
    assert schema["server_ops_s"] == "DOUBLE PRECISION"
    assert schema["invalid_reasons"] == "TEXT"
    assert schema["avg_latency_ms"] == "DECIMAL(15,6)"
