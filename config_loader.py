"""Load a ``matrix`` config: resolve its run profile, apply overrides, compile.

A profile is given inline in ``matrix.profile``, or as a name that resolves
to ``profiles/<name>.json`` next to the config file, or as a path to a JSON
file. ``overrides`` is merged into every config entry first (objects key by
key, null deletes a key), the same rule ``scripts/resolve_set_matrix.py``
uses, so a dispatch can change any parameter value or swap the profile.

Standard library only (see generators.py).
"""

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import matrix


def resolve_profile(cfg: Dict[str, Any], config_path: str, profile: Any = None) -> None:
    """Replace a profile name or path in ``cfg['matrix']['profile']`` by its JSON."""
    block = cfg["matrix"]
    if profile is not None:
        block["profile"] = profile
    ref = block.get("profile")
    if isinstance(ref, dict):
        return
    if not isinstance(ref, str) or not ref:
        raise ValueError("matrix.profile must be an object, a profile name or a path")
    path = Path(ref)
    if not path.suffix:
        path = Path(config_path).resolve().parent / "profiles" / f"{ref}.json"
    with open(path) as f:
        block["profile"] = json.load(f)


def prepare(
    cfg: Dict[str, Any],
    config_path: str,
    profile: Any = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return ``cfg`` with overrides merged, the profile resolved and compiled.

    ``overrides['matrix']['profile']`` may be a profile name (it replaces the
    config's) or an object (merged into the resolved profile, so a dispatch
    can change one value of a named profile).
    """
    overrides = copy.deepcopy(overrides or {})
    profile_override = (overrides.get("matrix") or {}).pop("profile", None)
    if isinstance(profile_override, str):
        profile = profile_override
    cfg = matrix.deep_merge(cfg, overrides) if overrides else cfg
    resolve_profile(cfg, config_path, profile)
    if isinstance(profile_override, dict):
        cfg["matrix"]["profile"] = matrix.deep_merge(
            cfg["matrix"]["profile"], profile_override
        )
    matrix.compile_config(cfg)
    return cfg


def load(
    config_path: str, profile: Any = None, overrides: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    with open(config_path) as f:
        configs = json.load(f)
    if not isinstance(configs, list):
        configs = [configs]
    out = []
    for cfg in configs:
        if "matrix" not in cfg:
            raise ValueError("config_loader handles matrix configs only")
        out.append(prepare(cfg, config_path, profile, overrides))
    return out
