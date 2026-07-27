"""Versioned, deterministic DSP pipeline (FR-DSP-001, FR-DSP-010).

A pipeline is an ordered list of (stage_name, params). Stages are pure
functions registered with a semantic version; running a pipeline records
every stage, version, and parameter set so any derived artifact can name
exactly how it was produced (FR-DAT-002) and be reproduced bit-for-bit from
the same raw data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

STAGE_REGISTRY: dict[str, "StageDef"] = {}


@dataclass(frozen=True)
class StageDef:
    name: str
    version: str
    func: Callable
    description: str = ""


def stage(name: str, version: str, description: str = ""):
    def wrap(func):
        STAGE_REGISTRY[name] = StageDef(name, version, func, description)
        return func
    return wrap


@dataclass
class PipelineContext:
    """Everything a stage may consult besides the signal itself."""

    sample_rate_hz: float
    center_frequency_hz: float
    waveform: dict | None = None
    medium: dict | None = None            # propagation model (FR-CAL-008)
    cable_delay_s: float = 0.0            # FR-CAL-002
    background: np.ndarray | None = None  # range-domain baseline (FR-CAL-004)
    extras: dict = field(default_factory=dict)


@dataclass
class PipelineResult:
    output: np.ndarray
    products: dict                        # named intermediate/final products
    record: dict                          # full provenance record
    warnings: list


class Pipeline:
    def __init__(self, stages: list[tuple[str, dict]]):
        for name, _ in stages:
            if name not in STAGE_REGISTRY:
                raise KeyError(f"unknown DSP stage: {name}")
        self.stages = stages

    def describe(self) -> list[dict]:
        return [{
            "stage": name,
            "version": STAGE_REGISTRY[name].version,
            "params": params,
        } for name, params in self.stages]

    def fingerprint(self) -> str:
        blob = json.dumps(self.describe(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def run(self, iq: np.ndarray, ctx: PipelineContext) -> PipelineResult:
        data = np.asarray(iq)
        products: dict = {}
        warnings: list = []
        for name, params in self.stages:
            sdef = STAGE_REGISTRY[name]
            data = sdef.func(data, ctx, products, warnings, **params)
        record = {
            "stages": self.describe(),
            "fingerprint": self.fingerprint(),
            "context": {
                "sample_rate_hz": ctx.sample_rate_hz,
                "center_frequency_hz": ctx.center_frequency_hz,
                "waveform": ctx.waveform,
                "medium": ctx.medium,
                "cable_delay_s": ctx.cable_delay_s,
                "background_applied": ctx.background is not None,
            },
        }
        return PipelineResult(output=data, products=products,
                              record=record, warnings=warnings)
