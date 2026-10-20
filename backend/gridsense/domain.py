"""Serializable domain types. Planner inputs intentionally exclude simulator truth."""

from typing import Literal
from math import isfinite
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class Scenario(Record):
    title: str = Field(default="Stale west-feeder telemetry", min_length=1, max_length=100)
    load_a: float = Field(default=1.0, ge=0.5, le=1.5)
    load_b: float = Field(default=1.5, ge=0.5, le=2.0)
    load_c: float = Field(default=0.9, ge=0.5, le=2.0)
    reported_b: float = Field(default=0.85, ge=0.5, le=2.0)
    telemetry_age_s: int = Field(default=120, ge=0, le=3600)
    uncertainty_pct: float = Field(default=80, ge=0, le=80)
    policy: Literal["nominal", "guarded"] = "guarded"

    @model_validator(mode="after")
    def fresh_measurements_are_current(self):
        if self.telemetry_age_s == 0 and abs(self.load_b - self.reported_b) > 1e-9:
            raise ValueError(
                "Fresh telemetry must equal the actual load; use a positive age for stale readings."
            )
        return self


class Observation(Record):
    recorded_at: str
    measurement_at: str
    age_s: int
    load_factors: dict[str, float]
    uncertainty_pct: float
    rows: list[dict]


class EquipmentOutage(Record):
    incident_id: str = Field(min_length=1, max_length=100)
    kind: Literal["line", "transformer", "source"]
    equipment: str = Field(min_length=1, max_length=64)


class CircuitState(Record):
    load_factors: dict[str, float]
    closed_lines: tuple[str, ...]
    locked_open: tuple[str, ...] = ("feed_a",)
    equipment_outages: tuple[EquipmentOutage, ...] = ()
    open_transformers: tuple[str, ...] = ()
    source_connected: bool = True


class Operation(Record):
    line: str
    closed: bool


class Action(Record):
    id: str
    label: str
    operations: tuple[Operation, ...]


class Violation(Record):
    kind: str
    element: str
    value: float | None = None
    limit: float | None = None
    message: str


class ElectricalResult(Record):
    converged: bool
    feasible: bool
    voltages_pu: dict[str, float]
    line_loading_pct: dict[str, float]
    transformer_loading_pct: dict[str, float]
    served_kw: float
    critical_served_kw: float
    requested_kw: float
    unserved_kw: float
    restored_kw: float
    min_voltage_pu: float | None
    max_loading_pct: float
    losses_kw: float
    energized_buses: tuple[str, ...]
    violations: tuple[Violation, ...]
    load_measurements: tuple[dict, ...]
    # Optional because not every adapter supplies this accounting. None means
    # unavailable/unconverged, never a measured zero or a passing balance.
    native_load_kw: float | None = None
    native_load_kvar: float | None = None
    source_kw: float | None = None
    source_kvar: float | None = None
    losses_kvar: float | None = None
    balance_residual_kw: float | None = None
    balance_residual_kvar: float | None = None
    voltage_compliant_served_kw: float | None = None
    # Empty means no incident-specific restoration baseline was assigned.
    restoration_baseline_kw: dict[str, float] = Field(default_factory=dict)

    def with_restoration_baseline(self, baseline: dict[str, float]):
        """Credit service gained by specified loads against their incident baseline."""
        rows = {row["load_id"]: row for row in self.load_measurements}
        if set(baseline) - set(rows) or any(
            not isfinite(value) or value < 0 for value in baseline.values()
        ):
            raise ValueError("Restoration baseline requires known loads and finite nonnegative kW")
        restored = (
            sum(max(0.0, rows[key]["served_kw"] - value) for key, value in baseline.items())
            if self.converged
            else 0.0
        )
        return self.model_copy(
            update={"restored_kw": restored, "restoration_baseline_kw": dict(baseline)}
        )


class Candidate(Record):
    action: Action
    nominal: ElectricalResult
    feasible: bool
    sample_count: int
    worst_loading_pct: float
    worst_voltage_pu: float | None
    rejected_reasons: tuple[str, ...]


class Decision(Record):
    policy: str
    selected_action: Action
    feasible_choice: bool
    rationale: str
    candidates: tuple[Candidate, ...]
    observation: Observation
    objective: str


class ActionOutcome(Record):
    action: Action
    final_state: CircuitState
    result: ElectricalResult
    steps: tuple[ElectricalResult, ...]


class ReviewInput(Record):
    verdict: Literal["accepted", "questioned"]
    note: str = Field(default="", max_length=2000)


class SweepInput(Record):
    base: Scenario = Field(default_factory=Scenario)
    minimum: float = Field(default=0.6, ge=0.5, le=2)
    maximum: float = Field(default=1.9, ge=0.5, le=2)
    points: int = Field(default=9, ge=3, le=15)

    @model_validator(mode="after")
    def increasing(self):
        if self.minimum >= self.maximum:
            raise ValueError("minimum must be below maximum")
        if self.base.telemetry_age_s == 0:
            raise ValueError("Use stale telemetry to sweep actual load with a fixed observation.")
        return self
