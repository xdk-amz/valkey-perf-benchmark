"""Pluggable load generators. valkey-benchmark is the default backend."""

from .base import (
    Executor,
    GeneratorError,
    GeneratorRequest,
    GeneratorResult,
    LoadGenerator,
    RunHandle,
)
from .cachecannon import CachecannonGenerator
from .valkey_benchmark_gen import ValkeyBenchmarkGenerator

DEFAULT_GENERATOR = "valkey-benchmark"
GENERATORS = {
    ValkeyBenchmarkGenerator.name: ValkeyBenchmarkGenerator,
    CachecannonGenerator.name: CachecannonGenerator,
}

__all__ = [
    "CachecannonGenerator",
    "DEFAULT_GENERATOR",
    "Executor",
    "GENERATORS",
    "GeneratorError",
    "GeneratorRequest",
    "GeneratorResult",
    "LoadGenerator",
    "RunHandle",
    "ValkeyBenchmarkGenerator",
]
