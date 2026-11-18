"""Compile a ``matrix`` config block into ordinary ``test_groups``.

The block has three parts, and only the profile holds values:

- ``parameters``: every parameter's meaning, type, unit and valid domain.
  ``scope`` is ``cell`` (one value per cell, and a profile may give a list to
  sweep it) or ``run`` (one value for the whole run, such as ``reps``).
- ``scenarios``: scenario templates. A template names its command shape with
  placeholders, the data it preloads and the template parameters it uses.
- ``profile``: the values. Its ``scenarios`` object names the cells to build:
  each key is a label (the cell id prefix) and its value overrides parameter
  values, and may name the ``template`` when the label is not a template name,
  so one template can appear twice (for example with each generator). It is inline, or a name that the config loader
  resolves to ``profiles/<name>.json`` next to the config. Every list in a
  profile is crossed with the others, per template, into cells.

Connections, pipeline depth and client threads are independent parameters:
nothing here derives one from another.

Placeholders in a template's command: ``{key}``, ``{value}`` and the element
named by the template (``{field}`` or ``{member}``) are filled per cell and per
generator; ``{name}`` for any parameter the template uses, plus the derived
``{range_stop}``, ``{withscores_arg}``, ``{maxlen_arg}``, ``{entry_fields}``,
``{keys_values}`` and ``{keys_list}``.

The compiled cells keep the parameter values under ``point`` and the features
they need from the generator under ``needs``; a cell whose generator lacks a
needed feature is rejected here, before any host is touched.

Standard library only (see generators.py).
"""

import ast
import copy
import itertools
import operator
import re
from typing import Any, Dict, List, Optional, Tuple

import generators

ORIGIN_FIELD = "matrix_origin"

TEMPLATE_KEYS = {
    "kind",
    "description",
    "preload",
    "commands",
    "elements",
    "uses",
    "needs",
    "multi_key",
}
PARAMETER_KEYS = {
    "description",
    "type",
    "unit",
    "values",
    "min",
    "max",
    "min_exclusive",
    "when",
    "scope",
    "source",
    "requires",
}
PROFILE_KEYS = {"name", "description", "run", "values", "scenarios", "id"}
PARAMETER_TYPES = {"int", "real", "bool", "enum", "range", "map", "shapes", "str"}
PRELOAD_TYPES = {"string", "hash", "list", "set", "zset", "stream"}
SELECTORS = {"index", "random_existing", "new", "missing"}
KEY_BASE = "key:"
HASH_TAG = "{mk}"

# Run-wide parameters every cell carries (the template parameters come from
# each template's "uses").
CELL_PARAMETERS = (
    "generator",
    "pipeline_depth",
    "connections",
    "client_threads",
    "generator_hosts",
    "access_pattern",
    "zipf_alpha",
    "hot_fraction",
    "hot_keys",
    "keys",
    "preload_keys",
    "key_size",
    "value_size",
    "io_threads",
    "server_mode",
    "tls",
    "server_config",
    "run_duration",
    "window",
)

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul}


def deep_merge(base: Any, override: Any) -> Any:
    """Merge ``override`` into ``base``: objects key by key, null deletes."""
    if not isinstance(base, dict) or not isinstance(override, dict):
        return copy.deepcopy(override)
    out = copy.deepcopy(base)
    for key, value in override.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = deep_merge(out.get(key), value)
    return out


def _eval_bound(expr: Any, values: Dict[str, Any], where: str) -> Optional[float]:
    """Evaluate a bound: a number, or text like "{keys}" or "{fields_per_hash}-1"."""
    if expr is None or isinstance(expr, (int, float)):
        return expr
    names = _PLACEHOLDER.findall(str(expr))
    if any(values.get(n) is None for n in names):
        return None  # the bound depends on a parameter this cell does not set

    text = _PLACEHOLDER.sub(lambda m: repr(values[m.group(1)]), str(expr))

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.left), walk(node.right))
        raise ValueError(f"{where}: unsupported bound expression {expr!r}")

    return walk(ast.parse(text, mode="eval"))


def _active(definition: Dict[str, Any], values: Dict[str, Any]) -> bool:
    """Whether a parameter applies, given its ``when`` condition."""
    for name, wanted in (definition.get("when") or {}).items():
        wanted = wanted if isinstance(wanted, list) else [wanted]
        if values.get(name) not in wanted:
            return False
    return True


def check_value(
    name: str, definition: Dict[str, Any], value: Any, values: Dict[str, Any]
) -> None:
    """Raise ValueError when ``value`` is outside the parameter's domain."""
    where = f"parameter {name!r}"
    kind = definition["type"]
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where} must be an integer, got {value!r}")
    elif kind == "real":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{where} must be a number, got {value!r}")
    elif kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{where} must be true or false, got {value!r}")
    elif kind == "enum":
        if value not in definition["values"]:
            raise ValueError(
                f"{where} must be one of {definition['values']}, got {value!r}"
            )
    elif kind == "range":
        if value is not None and not (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(v, (int, float)) for v in value)
            and 0 <= value[0] < value[1]
        ):
            raise ValueError(f"{where} must be null or [start, end], got {value!r}")
    elif kind in ("map", "shapes"):
        if value is not None and not (
            isinstance(value, dict)
            and all(isinstance(v, (str, int, float)) for v in value.values())
        ):
            raise ValueError(f"{where} must be an object of scalars, got {value!r}")
    elif kind == "str":
        if not isinstance(value, str):
            raise ValueError(f"{where} must be a string, got {value!r}")
    if kind in ("int", "real"):
        low = _eval_bound(definition.get("min"), values, where)
        high = _eval_bound(definition.get("max"), values, where)
        if low is not None and value < low:
            raise ValueError(f"{where} is {value}, below its minimum {low}")
        if high is not None and value > high:
            raise ValueError(f"{where} is {value}, above its maximum {high}")
        excl = _eval_bound(definition.get("min_exclusive"), values, where)
        if excl is not None and value <= excl:
            raise ValueError(f"{where} is {value}, must be above {excl}")
    for other, wanted in (definition.get("requires") or {}).items():
        if value and values.get(other) != wanted:
            raise ValueError(f"{where} needs {other} = {wanted!r}")


def validate_parameters(parameters: Dict[str, Any]) -> None:
    if not isinstance(parameters, dict) or not parameters:
        raise ValueError("matrix.parameters must be a non-empty object")
    for name, definition in parameters.items():
        if not isinstance(definition, dict):
            raise ValueError(f"matrix.parameters.{name} must be an object")
        unknown = set(definition) - PARAMETER_KEYS
        if unknown:
            raise ValueError(
                f"matrix.parameters.{name}: unknown keys {sorted(unknown)}"
            )
        if definition.get("type") not in PARAMETER_TYPES:
            raise ValueError(
                f"matrix.parameters.{name}.type must be one of {sorted(PARAMETER_TYPES)}"
            )
        if definition["type"] == "enum" and not definition.get("values"):
            raise ValueError(f"matrix.parameters.{name}: an enum needs 'values'")
        if definition.get("scope", "cell") not in ("cell", "run"):
            raise ValueError(f"matrix.parameters.{name}.scope must be cell or run")
    missing = [p for p in CELL_PARAMETERS if p not in parameters]
    if missing:
        raise ValueError(f"matrix.parameters is missing {missing}")


def validate_templates(templates: Dict[str, Any], parameters: Dict[str, Any]) -> None:
    if not isinstance(templates, dict) or not templates:
        raise ValueError("matrix.scenarios must be a non-empty object")
    for name, t in templates.items():
        where = f"matrix.scenarios.{name}"
        if not isinstance(t, dict):
            raise ValueError(f"{where} must be an object")
        unknown = set(t) - TEMPLATE_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
        if t.get("kind", "commands") not in ("string", "commands"):
            raise ValueError(f"{where}.kind must be 'string' or 'commands'")
        if t.get("kind", "commands") == "commands" and not t.get("commands"):
            raise ValueError(f"{where} needs 'commands'")
        if (t.get("preload") or {}).get("type") not in PRELOAD_TYPES:
            raise ValueError(
                f"{where}.preload.type must be one of {sorted(PRELOAD_TYPES)}"
            )
        for p in t.get("uses", []):
            if p not in parameters:
                raise ValueError(f"{where} uses undefined parameter {p!r}")
        for f in t.get("needs", []):
            if f not in generators.FEATURES:
                raise ValueError(f"{where} needs unknown feature {f!r}")
        el = t.get("elements")
        if el:
            for key in ("name", "prefix", "count", "selector", "allows"):
                if key not in el:
                    raise ValueError(f"{where}.elements needs {key!r}")
            if not set(el["allows"]) <= SELECTORS:
                raise ValueError(
                    f"{where}.elements.allows must be within {sorted(SELECTORS)}"
                )


def _render(text: str, values: Dict[str, Any], where: str) -> str:
    """Fill {name} placeholders found in ``values``; keep the generator ones."""
    keep = {"key", "value", "data"}

    def sub(match):
        name = match.group(1)
        if name in values:
            return str(values[name])
        if name in keep:
            return match.group(0)
        raise ValueError(f"{where}: no value for placeholder {{{name}}}")

    return _PLACEHOLDER.sub(sub, text)


def _derived(t: Dict[str, Any], p: Dict[str, Any]) -> Dict[str, Any]:
    """Placeholders computed from parameters."""
    out = dict(p)
    if p.get("range_count") is not None:
        out["range_stop"] = p.get("range_start", 0) + p["range_count"] - 1
    out["withscores_arg"] = " WITHSCORES" if p.get("withscores") else ""
    out["maxlen_arg"] = f" MAXLEN ~ {p['maxlen']}" if p.get("maxlen") else ""
    if p.get("fields_per_entry"):
        out["entry_fields"] = " ".join(
            f"f{i} {{value}}" for i in range(p["fields_per_entry"])
        )
    if p.get("keys_per_call"):
        out["keys_values"] = " ".join(["{key} {value}"] * p["keys_per_call"])
        out["keys_list"] = " ".join(["{key}"] * p["keys_per_call"])
    return out


def _element(t: Dict[str, Any], p: Dict[str, Any], where: str) -> Tuple[str, List[str]]:
    """Return the element placeholder text and the features it needs."""
    el = t["elements"]
    selector = p[el["selector"]]
    if selector not in el["allows"]:
        raise ValueError(
            f"{where}: {el['selector']} {selector!r} is not valid here; "
            f"this template allows {el['allows']}"
        )
    count = p[el["count"]]
    if selector == "index":
        index = p[el["index"]]
        if not 0 <= index < count:
            raise ValueError(
                f"{where}: {el['index']} {index} is outside 0..{count - 1}; "
                "use the 'missing' selector for an absent element"
            )
        return f"{el['prefix']}{index}", []
    if selector == "missing":
        return f"{el['prefix']}missing", []
    if selector == "new":
        return generators.DATA, ["per_request_random_data"]
    return generators.DATA, ["random_element"]


def _key_prefix(gen, p: Dict[str, Any], t: Dict[str, Any], where: str) -> str:
    base = KEY_BASE
    if t.get("multi_key") and p["server_mode"] == "CME":
        base = HASH_TAG + base  # every key in one slot, so MSET/MGET do not fail
    pad = p["key_size"] - len(base) - gen.key_digits(p["keys"])
    if pad < 0:
        raise ValueError(
            f"{where}: key_size {p['key_size']} is too short for prefix {base!r} "
            f"and {gen.key_digits(p['keys'])} digits"
        )
    return base + "x" * pad


def _commands(t: Dict[str, Any], p: Dict[str, Any], values: Dict[str, Any], where):
    if t.get("kind") == "string":
        read = p["read_fraction"]
        out = []
        if read > 0:
            out.append({"command": "GET {key}", "weight": read, "read": True})
        if read < 1:
            out.append({"command": "SET {key} {value}", "weight": 1 - read})
        return out
    out = []
    for c in t["commands"]:
        out.append(
            {
                "command": _render(c["command"], values, where),
                "weight": c.get("weight", 1.0),
                "read": bool(c.get("read")),
            }
        )
    total = sum(c["weight"] for c in out)
    for c in out:
        c["weight"] = c["weight"] / total
    return out


def _hot(commands, gen, prefix: str, hot_fraction: float, hot_keys: int):
    """Split each command into one per hot key plus one over the whole keyspace."""
    out = []
    for c in commands:
        for i in range(hot_keys):
            name = gen.key_name(prefix, i)
            out.append(
                dict(
                    c,
                    command=c["command"].replace("{key}", name),
                    weight=c["weight"] * hot_fraction / hot_keys,
                    hot=True,
                )
            )
        if hot_fraction < 1:
            out.append(dict(c, weight=c["weight"] * (1 - hot_fraction)))
    return out


def compile_cell(
    name: str,
    template: str,
    t: Dict[str, Any],
    point: Dict[str, Any],
    parameters: Dict[str, Any],
    cell_id: str,
) -> Dict[str, Any]:
    """Turn one template and one set of parameter values into a scenario."""
    where = f"cell {cell_id!r}"
    p = {k: v for k, v in point.items() if _active(parameters[k], point)}
    for k, v in p.items():
        check_value(k, parameters[k], v, p)
    for k in list(CELL_PARAMETERS) + list(t.get("uses", [])):
        if _active(parameters[k], p) and k not in p:
            raise ValueError(f"{where}: the profile gives no value for {k!r}")

    if p["tls"]:
        # shortcut: TLS is a defined parameter but the matrix runner does not
        # start TLS servers yet; wire certificates in when a profile needs it.
        raise ValueError(
            f"{where}: tls = true is not supported by the matrix runner yet"
        )
    hosts_threads = p["generator_hosts"] * p["client_threads"]
    if p["connections"] % hosts_threads:
        raise ValueError(
            f"{where}: {p['connections']} connections do not split exactly over "
            f"{p['generator_hosts']} generator hosts x {p['client_threads']} threads"
        )
    if p["preload_keys"] > p["keys"]:
        raise ValueError(
            f"{where}: preload_keys {p['preload_keys']} exceeds keys {p['keys']}"
        )
    start, end = p["window"]
    if end > p["run_duration"]:
        raise ValueError(
            f"{where}: window {p['window']} ends after run_duration {p['run_duration']}"
        )
    if (
        t.get("kind") == "string"
        and p.get("ttl_range")
        and p["access_pattern"] == "hotkey"
    ):
        raise ValueError(
            f"{where}: random TTLs with hot keys cannot be expressed: memtier "
            "applies --expiry-range only to --ratio runs, and hot keys need --command"
        )
    if p["access_pattern"] == "hotkey" and p["hot_keys"] > p["preload_keys"]:
        raise ValueError(f"{where}: hot_keys must be preloaded keys")

    gen = generators.get(p["generator"])
    needs = set(t.get("needs", ["custom_command", "random_key"]))
    values = _derived(t, p)
    if t.get("elements"):
        text, extra = _element(t, p, where)
        values[t["elements"]["name"]] = text
        needs.update(extra)
    if t.get("kind") == "string":
        needs.add("random_key")
        if 0 < p["read_fraction"] < 1:
            needs.add("mixed_ratio")
        if p.get("ttl_range"):
            needs.add("random_ttl")
    if p["access_pattern"] == "zipfian":
        needs.add("zipfian")
    if p["access_pattern"] == "hotkey":
        needs.add("weighted_commands")
    if p["server_mode"] == "CME":
        needs.add("cluster_aware")
        if t.get("multi_key"):
            needs.add("cluster_multi_key")
    commands = _commands(t, p, values, where)
    if len(commands) > 1 and t.get("kind") != "string":
        needs.add("weighted_commands")
    missing = gen.missing_features(sorted(needs))
    if missing:
        raise ValueError(
            f"{where}: generator {gen.name!r} cannot express this cell; "
            f"it lacks {missing}"
        )

    prefix = _key_prefix(gen, p, t, where)
    if p["access_pattern"] == "hotkey":
        commands = _hot(commands, gen, prefix, p["hot_fraction"], p["hot_keys"])
    preload = {
        k: (_render(v, values, where) if isinstance(v, str) else v)
        for k, v in t["preload"].items()
    }
    for k in ("elements", "fields_per_entry"):
        if k in preload:
            preload[k] = int(preload[k])
    preload.update(
        {"keys": p["preload_keys"], "key_prefix": prefix, "value_size": p["value_size"]}
    )
    return {
        ORIGIN_FIELD: True,
        "id": cell_id,
        "scenario": name,
        "template": template,
        "description": t.get("description", template),
        "point": p,
        "generator": gen.name,
        "needs": sorted(needs),
        "kind": t.get("kind", "commands"),
        "commands": commands,
        "preload": preload,
        "key_prefix": prefix,
        "keys": p["keys"],
        "value_size": p["value_size"],
        "pipeline": p["pipeline_depth"],
        "connections": p["connections"],
        "client_threads": p["client_threads"],
        "generator_hosts": p["generator_hosts"],
        "io_threads": p["io_threads"],
        "cluster_mode": p["server_mode"] == "CME",
        "duration": p["run_duration"],
        "window": list(p["window"]),
        "server_config": p.get("server_config") or {},
        "access": {
            "pattern": p["access_pattern"],
            "alpha": p.get("zipf_alpha"),
            "hot_fraction": p.get("hot_fraction"),
            "hot_keys": p.get("hot_keys"),
        },
        "read_fraction": p.get("read_fraction"),
        "ttl_range": p.get("ttl_range"),
        "restart_before": True,
        # VPB's usual scenario names for the same things
        "clients": p["connections"],
        "keyspacelen": p["keys"],
        "data_size": p["value_size"],
    }


def _sweep_list(name: str, parameters: Dict[str, Any], value: Any) -> List[Any]:
    """A profile value as the list of values to sweep (a range is one value)."""
    if parameters[name]["type"] == "range":
        if value is None or (value and not isinstance(value[0], list)):
            return [value]
        return value
    return value if isinstance(value, list) else [value]


def expand_profile(
    profile: Dict[str, Any], templates: Dict[str, Any], parameters: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (cells, run settings) for a profile."""
    unknown = set(profile) - PROFILE_KEYS
    if unknown:
        raise ValueError(f"profile: unknown keys {sorted(unknown)}")
    run = dict(profile.get("run") or {})
    for name, value in run.items():
        if name not in parameters or parameters[name].get("scope") != "run":
            raise ValueError(f"profile.run.{name} is not a run-scope parameter")
        check_value(name, parameters[name], value, run)
    values = profile.get("values") or {}
    chosen = profile.get("scenarios") or {}
    if chosen == "all":
        chosen = {name: {} for name in templates}
    for name, overrides in chosen.items():
        template = (overrides or {}).get("template", name)
        if template not in templates:
            raise ValueError(
                f"profile entry {name!r} names unknown template {template!r}"
            )

    cells, ids = [], set()
    for name, overrides in chosen.items():
        overrides = dict(overrides or {})
        template = overrides.pop("template", name)
        t = templates[template]
        merged = dict(values, **overrides)
        allowed = set(CELL_PARAMETERS) | set(t.get("uses", []))
        for k in merged:
            if k not in parameters:
                raise ValueError(f"profile sets undefined parameter {k!r}")
            if parameters[k].get("scope") == "run":
                raise ValueError(
                    f"profile sets run parameter {k!r} per cell; use 'run'"
                )
        names = [k for k in merged if k in allowed]
        lists = [_sweep_list(k, parameters, merged[k]) for k in names]
        varying = [k for k, vs in zip(names, lists) if len(vs) > 1]
        for combo in itertools.product(*lists):
            point = dict(zip(names, combo))
            if profile.get("id"):
                cell_id = _render(
                    profile["id"], dict(point, scenario=name), "profile.id"
                )
            else:
                cell_id = "-".join([name] + [f"{k}{point[k]}" for k in varying])
            if cell_id in ids:
                raise ValueError(f"profile gives two cells the id {cell_id!r}")
            ids.add(cell_id)
            cells.append(compile_cell(name, template, t, point, parameters, cell_id))
    if not cells:
        raise ValueError("profile selects no cells")
    return cells, run


def compile_config(cfg: Dict[str, Any]) -> None:
    """Expand ``cfg['matrix']`` in place into ``test_groups`` (one per template)."""
    block = cfg["matrix"]
    if "test_groups" in cfg or "commands" in cfg or "set_matrix" in cfg:
        raise ValueError(
            "'matrix' generates test_groups, so it cannot be combined with "
            "'test_groups', 'commands' or 'set_matrix'"
        )
    parameters, templates = block.get("parameters"), block.get("scenarios")
    validate_parameters(parameters)
    validate_templates(templates, parameters)
    profile = block.get("profile")
    if not isinstance(profile, dict):
        raise ValueError(
            "matrix.profile must be an object here; a profile name is resolved "
            "by the config loader (profiles/<name>.json next to the config)"
        )
    cells, run = expand_profile(profile, templates, parameters)
    groups: Dict[str, Dict[str, Any]] = {}
    for cell in cells:
        group = groups.setdefault(
            cell["scenario"],
            {
                "group": len(groups) + 1,
                "description": cell["description"],
                "scenarios": [],
            },
        )
        cell["group"] = group["group"]
        group["scenarios"].append(cell)
    cfg["test_groups"] = list(groups.values())
    cfg["matrix_run"] = dict(run, profile=profile.get("name", "inline"))
