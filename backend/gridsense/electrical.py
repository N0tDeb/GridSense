"""Real OpenDSS solves; graph connectivity never substitutes for electrical results."""

from math import isfinite, sqrt
import networkx as nx
from opendssdirect import dss
from .domain import Action, ActionOutcome, CircuitState, ElectricalResult, Violation
from .network import (
    energized,
    graph_for,
    load_connected,
    load_network,
    transformer_ids,
    validate_equipment,
)

CONSTANT_PQ_VOLTAGE_PU = (0.8, 1.2)


class InvalidSwitching(ValueError):
    pass


class ElectricalEngine:
    def __init__(
        self,
        network: dict | None = None,
        *,
        restoration_baseline_kw: dict[str, float] | None = None,
    ):
        self.network = network or load_network()
        self.restoration_baseline_kw = dict(restoration_baseline_kw or {})
        # Each instance owns a native DSS context, including all mutable circuit state.
        self.ctx = dss.NewContext()
        self.ctx.Basic.AllowChangeDir(False)
        self.ctx.Basic.AllowForms(False)

    @property
    def version(self) -> str:
        return self.ctx.Basic.Version()

    def _commands(self, state: CircuitState) -> list[str]:
        n = self.network
        commands = [
            "Clear",
            "New Circuit.gridsense bus1=source basekv=33 pu=1.0 phases=3 mvasc3=1000 mvasc1=1000",
            "New Transformer.main phases=3 windings=2 buses=[source sub] conns=[delta wye] kvs=[33 11] kvas=[6000 6000] %rs=[0.25 0.25] xhl=3",
            "New Linecode.mv nphases=3 r1=0.18 x1=0.20 r0=0.45 x0=0.40 c1=0 c0=0 units=km",
        ]
        for line in n["lines"]:
            commands.append(
                f"New Line.{line['id']} bus1={line['from_bus']} bus2={line['to_bus']} phases=3 "
                f"linecode=mv length={line['length_km']} units=km normamps={line['normal_amps']}"
            )
        for load in n["loads"]:
            bus, kva = load["bus"], load["kva"]
            commands.extend(
                [
                    f"New Transformer.tx_{load['id']} phases=3 windings=2 buses=[{bus} {bus}_lv] conns=[delta wye] kvs=[11 0.415] kvas=[{kva} {kva}] %rs=[0.3 0.3] xhl=2",
                    f"New Load.{load['id']} bus1={bus}_lv phases=3 conn=wye kv=0.415 kw={load['kw'] * state.load_factors[load['feeder']]} pf=0.95 model=1 vminpu={CONSTANT_PQ_VOLTAGE_PU[0]} vmaxpu={CONSTANT_PQ_VOLTAGE_PU[1]}",
                ]
            )
        commands += [
            "Set voltagebases=[33 11 0.415]",
            "CalcVoltageBases",
            "Set controlmode=off",
            "Set maxiterations=100",
            "Set tolerance=0.00000001",
        ]
        return commands

    def _validate_state(self, state: CircuitState):
        try:
            validate_equipment(state, self.network)
        except ValueError as exc:
            raise InvalidSwitching(str(exc)) from exc
        ids = {line["id"] for line in self.network["lines"]}
        if not set(state.closed_lines).issubset(ids) or not set(state.locked_open).issubset(ids):
            raise InvalidSwitching("Unknown switch or locked section")
        if set(state.closed_lines) & set(state.locked_open):
            raise InvalidSwitching("A faulted, locked-open section cannot be energized")
        if not nx.is_forest(graph_for(state, self.network)):
            raise InvalidSwitching("The modeled operating policy requires a radial network")
        if set(state.load_factors) != {"a", "b", "c"} or any(
            not isfinite(v) or v <= 0 for v in state.load_factors.values()
        ):
            raise ValueError("Positive finite factors for feeders a, b, c are required")

    def solve(self, state: CircuitState) -> ElectricalResult:
        self._validate_state(state)
        for cmd in self._commands(state):
            self.ctx.Text.Command(cmd)
        # Opening both terminals isolates the physical section while leaving the
        # healthy bus available for backfeeding. No graph-only switching exists.
        for line in self.network["lines"]:
            self.ctx.Circuit.SetActiveElement(f"Line.{line['id']}")
            for terminal in (1, 2):
                if line["id"] in state.closed_lines:
                    self.ctx.CktElement.Close(terminal, 0)
                else:
                    self.ctx.CktElement.Open(terminal, 0)
        for name in sorted(transformer_ids(self.network)):
            self.ctx.Circuit.SetActiveElement(f"Transformer.{name}")
            for terminal in (1, 2):
                if name in state.open_transformers:
                    self.ctx.CktElement.Open(terminal, 0)
        if not state.source_connected:
            self.ctx.Circuit.SetActiveElement("Vsource.source")
            for terminal in range(1, self.ctx.CktElement.NumTerminals() + 1):
                self.ctx.CktElement.Open(terminal, 0)
        live = energized(state, self.network)
        # Disconnected demand is explicitly unserved. Disable its constant-power
        # load in DSS to avoid a singular zero-voltage island load equation.
        for load in self.network["loads"]:
            self.ctx.Text.Command(
                f"Edit Load.{load['id']} enabled={'yes' if load_connected(state, load, live) else 'no'}"
            )
        self.ctx.Solution.Solve()
        converged = bool(self.ctx.Solution.Converged())
        violations = []
        if not converged:
            violations.append(
                Violation(
                    kind="solver",
                    element="circuit",
                    message="OpenDSS did not converge; result is not feasible.",
                )
            )
        voltages = {}
        for bus in self.ctx.Circuit.AllBusNames():
            self.ctx.Circuit.SetActiveBus(bus)
            values = self.ctx.Bus.puVmagAngle()[::2]
            if values:
                voltages[bus] = min(values)
        low, high = self.network["voltage_limits_pu"]
        live_buses = live | {
            f"{load['bus']}_lv"
            for load in self.network["loads"]
            if load_connected(state, load, live)
        }
        if state.source_connected:
            live_buses.add(self.network["source_bus"])
        for bus, minimum in voltages.items():
            if bus not in live_buses:
                continue
            self.ctx.Circuit.SetActiveBus(bus)
            for value in self.ctx.Bus.puVmagAngle()[::2]:
                if not isfinite(value) or value < low or value > high:
                    violations.append(
                        Violation(
                            kind="voltage",
                            element=bus,
                            value=value if isfinite(value) else None,
                            limit=low if value < low else high,
                            message=f"{bus}: {value:.4f} pu outside {low:.2f}–{high:.2f} pu.",
                        )
                    )
                    break
        loading = {}
        for line in self.network["lines"]:
            self.ctx.Circuit.SetActiveElement(f"Line.{line['id']}")
            amps = max(self.ctx.CktElement.CurrentsMagAng()[::2], default=0)
            pct = 100 * amps / line["normal_amps"] if line["id"] in state.closed_lines else 0.0
            loading[line["id"]] = pct
            if pct > 100 + 1e-6:
                violations.append(
                    Violation(
                        kind="line_loading",
                        element=line["id"],
                        value=pct,
                        limit=100,
                        message=f"{line['id']}: {pct:.1f}% of its {line['normal_amps']} A continuous rating.",
                    )
                )
        tx_loading = {}
        tx_specs = [("main", 6000, 33, 11)] + [
            (f"tx_{l['id']}", l["kva"], 11, 0.415) for l in self.network["loads"]
        ]
        for name, kva, kv1, kv2 in tx_specs:
            self.ctx.Circuit.SetActiveElement(f"Transformer.{name}")
            currents = self.ctx.CktElement.CurrentsMagAng()[::2]
            conductors = self.ctx.CktElement.NumConductors()
            ratios = []
            for terminal, kv in enumerate((kv1, kv2)):
                phase_amps = currents[terminal * conductors : terminal * conductors + 3]
                ratios.append(100 * max(phase_amps, default=0) / (kva / (sqrt(3) * kv)))
            pct = max(ratios)
            tx_loading[name] = pct
            if pct > 100 + 1e-6:
                violations.append(
                    Violation(
                        kind="transformer_loading",
                        element=name,
                        value=pct,
                        limit=100,
                        message=f"{name}: {pct:.1f}% winding current loading.",
                    )
                )
        rows, total, served, critical = [], 0.0, 0.0, 0.0
        native_kw, native_kvar, voltage_compliant = 0.0, 0.0, 0.0
        for load in self.network["loads"]:
            requested = load["kw"] * state.load_factors[load["feeder"]]
            total += requested
            is_live = load_connected(state, load, live)
            self.ctx.Circuit.SetActiveElement(f"Load.{load['id']}")
            power = self.ctx.CktElement.Powers()
            p, q = sum(power[::2]), sum(power[1::2])
            native_kw += p
            native_kvar += q
            actual = max(0.0, min(p, requested)) if is_live else 0.0
            self.ctx.Circuit.SetActiveBus(f"{load['bus']}_lv")
            phases = self.ctx.Bus.puVmagAngle()[::2]
            voltage_ok = (
                converged and is_live and bool(phases) and all(low <= v <= high for v in phases)
            )
            if voltage_ok:
                voltage_compliant += actual
            served += actual
            if load["critical"]:
                critical += actual
            rows.append(
                {
                    "load_id": load["id"],
                    "label": load["label"],
                    "critical": load["critical"],
                    "requested_kw": requested,
                    "served_kw": actual,
                    "native_kw": p if converged else None,
                    "native_kvar": q if converged else None,
                    "quality": "solved" if converged else "unconverged",
                    "voltage_compliant": voltage_ok,
                    "energized": is_live,
                    "voltage_pu": voltages.get(f"{load['bus']}_lv", 0) if is_live else 0.0,
                }
            )
        live_voltages = [v for b, v in voltages.items() if b in live_buses]
        source_p, source_q = (-x for x in self.ctx.Circuit.TotalPower())
        loss_p, loss_q = (x / 1000 for x in self.ctx.Circuit.Losses())
        return ElectricalResult(
            converged=converged,
            feasible=converged and not violations,
            voltages_pu=voltages,
            line_loading_pct=loading,
            transformer_loading_pct=tx_loading,
            served_kw=served,
            critical_served_kw=critical,
            requested_kw=total,
            unserved_kw=max(0, total - served),
            restored_kw=0.0,
            min_voltage_pu=min(live_voltages) if live_voltages else None,
            max_loading_pct=max([*loading.values(), *tx_loading.values()]),
            losses_kw=loss_p,
            energized_buses=tuple(sorted(live)),
            violations=tuple(violations),
            load_measurements=tuple(rows),
            native_load_kw=native_kw if converged else None,
            native_load_kvar=native_kvar if converged else None,
            source_kw=source_p if converged else None,
            source_kvar=source_q if converged else None,
            losses_kvar=loss_q if converged else None,
            balance_residual_kw=source_p - native_kw - loss_p if converged else None,
            balance_residual_kvar=source_q - native_kvar - loss_q if converged else None,
            voltage_compliant_served_kw=voltage_compliant if converged else None,
        ).with_restoration_baseline(self.restoration_baseline_kw)

    def execute(self, state: CircuitState, action: Action) -> ActionOutcome:
        self._validate_state(state)
        current, steps = state, []
        for operation in action.operations:
            if operation.line not in {l["id"] for l in self.network["lines"]}:
                raise InvalidSwitching("Unknown switch")
            closed = set(current.closed_lines)
            if operation.closed:
                closed.add(operation.line)
            else:
                closed.discard(operation.line)
            current = current.model_copy(update={"closed_lines": tuple(sorted(closed))})
            steps.append(self.solve(current))
        result = steps[-1] if steps else self.solve(current)
        # An invalid intermediate electrical state invalidates the full sequence.
        earlier = [v for step in steps[:-1] for v in step.violations]
        if earlier:
            result = result.model_copy(
                update={"feasible": False, "violations": tuple([*earlier, *result.violations])}
            )
        return ActionOutcome(action=action, final_state=current, result=result, steps=tuple(steps))
