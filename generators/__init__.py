"""Pluggable load generators.

valkey-benchmark is the default and runs through ``ClientRunner`` as before;
other generators implement ``LoadGenerator``.
"""

from .base import (
    Executor,
    GeneratorError,
    GeneratorRequest,
    GeneratorResult,
    LoadGenerator,
    RunHandle,
    run_for_duration,
)
from .cachecannon import CachecannonGenerator
from .valkey_benchmark_gen import ValkeyBenchmarkGenerator

DEFAULT_GENERATOR = "valkey-benchmark"
GENERATORS = {
    CachecannonGenerator.name: CachecannonGenerator,
}
GENERATOR_NAMES = frozenset({DEFAULT_GENERATOR, *GENERATORS})

__all__ = [
    "CachecannonGenerator",
    "DEFAULT_GENERATOR",
    "Executor",
    "GENERATORS",
    "GENERATOR_NAMES",
    "GeneratorError",
    "GeneratorRequest",
    "GeneratorResult",
    "LoadGenerator",
    "RunHandle",
    "ValkeyBenchmarkGenerator",
    "run_for_duration",
]
