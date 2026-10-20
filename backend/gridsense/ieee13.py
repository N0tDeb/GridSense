"""Native IEEE 13-node adapter. Phases, load models, capacitors and taps retained.

The validation circuit has no added ties. Restoration uses explicitly declared
experimental ties/ampacities, whole-line faults, and fixed pre-incident taps.
"""

import json
import math
from datetime import datetime, timedelta
from importlib.resources import files
import networkx as nx
from opendssdirect import dss
from .domain import (
    Action,
    ActionOutcome,
    CircuitState,
    ElectricalResult,
    Observation,
    Operation,
    Violation,
)

ROOT = files("gridsense").joinpath("circuits/ieee13")
TIES = {"backup_633": ("633", "671", 1800, 650), "backup_rg": ("rg60", "675", 3200, 500)}


def context():
    ctx = dss.NewContext()
    ctx.Basic.AllowForms(False)
    ctx.Basic.AllowChangeDir(False)
    # Bundled, reviewed commands only. No user supplied DSS text or paths.
    ctx.Text.Commands(ROOT.joinpath("model.dss").read_text(encoding="utf-8").splitlines())
    return ctx


def phase_voltages(ctx):
    values = {}
    for bus in ctx.Circuit.AllBusNames():
        ctx.Circuit.SetActiveBus(bus)
        for node, value in zip(ctx.Bus.Nodes(), ctx.Bus.puVmagAngle()[::2]):
            if node > 0:
                values[f"{bus}.{node}"] = value
    return values


def load_voltages(ctx):
    """Read the active native load's connected branch voltages and both bases."""
    phases, conductors = ctx.CktElement.NumPhases(), ctx.CktElement.NumConductors()
    nodes = ctx.CktElement.NodeOrder()[:conductors]
    raw = ctx.CktElement.Voltages()[: 2 * conductors]
    volts = [complex(real, imaginary) for real, imaginary in zip(raw[::2], raw[1::2])]
    delta, rated_kv = ctx.Loads.IsDelta(), ctx.Loads.kV()
    pairs = (
        [(index, (index + 1) % conductors) for index in range(phases)]
        if delta
        else [(index, phases) for index in range(phases)]
    )
    branches = {
        f"{nodes[first]}-{nodes[second]}": abs(volts[first] - volts[second])
        for first, second in pairs
    }
    bus = ctx.CktElement.BusNames()[0].split(".")[0].lower()
    ctx.Circuit.SetActiveBus(bus)
    bus_base_kv = ctx.Bus.kVBase() * (math.sqrt(3) if delta else 1)
    load_base_kv = rated_kv / (math.sqrt(3) if phases > 1 and not delta else 1)
    if bus_base_kv <= 0 or load_base_kv <= 0:
        raise ValueError("Native load voltage bases must be positive")
    return {
        "connection": "delta" if delta else "wye",
        "phase_nodes": nodes,
        "voltage_basis": "phase-to-phase" if delta else "phase-to-neutral",
        "voltage_base_kv": bus_base_kv,
        "load_voltage_base_kv": load_base_kv,
        "voltage_branches_v": branches,
        "voltage_branches_pu": {
            key: value / (1000 * bus_base_kv) for key, value in branches.items()
        },
        "voltage_on_load_base_pu": {
            key: value / (1000 * load_base_kv) for key, value in branches.items()
        },
    }


def validate_reference():
    ctx = context()
    ctx.Solution.Solve()
    reference = json.loads(ROOT.joinpath("reference.json").read_text(encoding="utf-8"))
    actual = phase_voltages(ctx)
    rows = [
        {
            "node": k,
            "published_pu": v,
            "actual_pu": actual[k],
            "error_pu": abs(actual[k] - v),
            "passed": abs(actual[k] - v) <= reference["tolerance_pu"],
        }
        for k, v in reference["voltages_pu"].items()
    ]
    return {
        "feeder": "IEEE 13-node electrical reference (no restoration ties)",
        "converged": bool(ctx.Solution.Converged()),
        "passed": bool(ctx.Solution.Converged()) and all(r["passed"] for r in rows),
        "max_error_pu": max(r["error_pu"] for r in rows),
        "rows": rows,
        "tolerance_pu": reference["tolerance_pu"],
        "reference": reference["source"],
        "solver_version": ctx.Basic.Version(),
        "scope": "Voltage magnitude agreement against independently published values, allowing documented modeling differences. Not validation of added ties, ampacities, protection, or restoration policy.",
    }


class IEEE13Plant:
    def __init__(self, case):
        self.case = case
        ctx = context()
        self.version = ctx.Basic.Version()
        self.lines = {}
        for name in ctx.Lines.AllNames():
            ctx.Lines.Name(name)
            self.lines[name] = tuple(
                b.split(".")[0].lower() for b in (ctx.Lines.Bus1(), ctx.Lines.Bus2())
            )
        self.lines.update({k: v[:2] for k, v in TIES.items()})
        self.fixed_edges = [("sourcebus", "650"), ("650", "rg60"), ("633", "634")]
        self.loads = {}
        for name in ctx.Loads.AllNames():
            ctx.Loads.Name(name)
            self.loads[name] = {
                "bus": ctx.CktElement.BusNames()[0].split(".")[0].lower(),
                "kw": ctx.Loads.kW(),
                "kvar": ctx.Loads.kvar(),
            }
        self.truth = CircuitState(
            load_factors={"all": case.scenario.load_a},
            closed_lines=tuple(sorted(set(self.lines) - set(TIES) - {case.fault})),
            locked_open=(case.fault,),
        )
        self.affected = {
            name for name, load in self.loads.items() if load["bus"] not in self.live(self.truth)
        }

    def graph(self, state):
        g = nx.Graph()
        g.add_edges_from(self.fixed_edges)
        g.add_nodes_from(b for pair in self.lines.values() for b in pair)
        g.add_edges_from(self.lines[n] for n in state.closed_lines)
        return g

    def live(self, state):
        return set(nx.node_connected_component(self.graph(state), "sourcebus"))

    def observation(self, at):
        s = self.case.scenario
        measurement_at = (
            datetime.fromisoformat(at) - timedelta(seconds=s.telemetry_age_s)
        ).isoformat()
        return Observation(
            recorded_at=at,
            measurement_at=measurement_at,
            age_s=s.telemetry_age_s,
            uncertainty_pct=s.uncertainty_pct,
            load_factors={"all": s.reported_b},
            rows=[
                {
                    "sensor_id": f"load_{name}",
                    "measurement": "active_demand_kw",
                    "value": load["kw"] * s.reported_b,
                    "unit": "kW",
                    "measurement_at": measurement_at,
                    "source": "synthetic aggregate multiplier on published load allocations",
                }
                for name, load in self.loads.items()
            ],
        )

    def observed_state(self, observation):
        return self.truth.model_copy(update={"load_factors": dict(observation.load_factors)})

    def samples(self, observation):
        multipliers = (
            (1 - observation.uncertainty_pct / 100, 1, 1 + observation.uncertainty_pct / 100)
            if observation.age_s
            else (1,)
        )
        return [{"all": observation.load_factors["all"] * m} for m in multipliers]

    def actions(self, state):
        live = self.live(state)
        actions = [Action(id="hold", label="Keep section isolated", operations=())]
        for name, (a, b, _, _) in TIES.items():
            if (a in live) != (b in live):
                actions.append(
                    Action(
                        id=name,
                        label=f"Close experimental tie {name}",
                        operations=(Operation(line=name, closed=True),),
                    )
                )
        return tuple(actions)

    def solve(self, state):
        if state.equipment_outages or state.open_transformers or not state.source_connected:
            raise ValueError("Equipment availability is supported by the teaching adapter only")
        if not set(state.closed_lines).issubset(self.lines) or set(state.closed_lines) & set(
            state.locked_open
        ):
            raise ValueError("Unknown switch or faulted line closure")
        if not nx.is_forest(self.graph(state)):
            raise ValueError("Experimental restoration requires a radial topology")
        if set(state.load_factors) != {"all"} or not 0 < state.load_factors["all"] < 10:
            raise ValueError("A finite positive aggregate demand multiplier is required")
        ctx = context()
        for name, (a, b, length, rating) in TIES.items():
            ctx.Text.Command(
                f"New Line.{name} phases=3 bus1={a}.1.2.3 bus2={b}.1.2.3 linecode=mtx601 length={length} units=ft normamps={rating}"
            )
        live = self.live(state)
        for name in self.lines:
            ctx.Circuit.SetActiveElement(f"Line.{name}")
            for terminal in (1, 2):
                (ctx.CktElement.Close if name in state.closed_lines else ctx.CktElement.Open)(
                    terminal, 0
                )
        for name, load in self.loads.items():
            f = state.load_factors["all"]
            ctx.Text.Command(
                f"Edit Load.{name} kw={load['kw'] * f} kvar={load['kvar'] * f} enabled={'yes' if load['bus'] in live else 'no'}"
            )
        for cap in ctx.Capacitors.AllNames():
            ctx.Capacitors.Name(cap)
            connected = ctx.CktElement.BusNames()[0].split(".")[0].lower() in live
            ctx.Text.Command(f"Edit Capacitor.{cap} enabled={'yes' if connected else 'no'}")
        ctx.Solution.Solve()
        converged = bool(ctx.Solution.Converged())
        violations = []
        if not converged:
            violations.append(
                Violation(kind="solver", element="circuit", message="OpenDSS did not converge")
            )
        volts = phase_voltages(ctx)
        # A disclosed experimental envelope; published healthy phase voltages
        # already exceed 1.05 pu. Do not relabel these as IEEE/utility limits.
        for node, voltage in volts.items():
            if node.split(".")[0] in live and (
                not math.isfinite(voltage) or not 0.9 <= voltage <= 1.1
            ):
                violations.append(
                    Violation(
                        kind="voltage",
                        element=node,
                        value=voltage if math.isfinite(voltage) else None,
                        limit=0.9 if voltage < 0.9 else 1.1,
                        message=f"{node}: {voltage:.4f} pu outside experimental 0.90–1.10 envelope",
                    )
                )
        loading = {}
        for name in self.lines:
            ctx.Lines.Name(name)
            rating = (
                TIES[name][3] if name in TIES else (700 if ctx.CktElement.NumPhases() == 3 else 300)
            )
            amps = max(ctx.CktElement.CurrentsMagAng()[::2], default=0)
            loading[name] = 100 * amps / rating if name in state.closed_lines else 0
            if loading[name] > 100 + 1e-6:
                violations.append(
                    Violation(
                        kind="line_loading",
                        element=name,
                        value=loading[name],
                        limit=100,
                        message=f"{name}: {loading[name]:.1f}% of assumed {rating} A rating",
                    )
                )
        transformers = {}
        for name in ctx.Transformers.AllNames():
            ctx.Transformers.Name(name)
            phases, ncond = ctx.CktElement.NumPhases(), ctx.CktElement.NumConductors()
            currents = ctx.CktElement.CurrentsMagAng()[::2]
            values = []
            for wdg in (1, 2):
                ctx.Transformers.Wdg(wdg)
                rating = (
                    ctx.Transformers.kVA()
                    / ctx.Transformers.kV()
                    / (math.sqrt(3) if phases == 3 else 1)
                )
                values.append(
                    100
                    * max(currents[(wdg - 1) * ncond : (wdg - 1) * ncond + phases], default=0)
                    / rating
                )
            transformers[name] = max(values)
            if max(values) > 100 + 1e-6:
                violations.append(
                    Violation(
                        kind="transformer_loading",
                        element=name,
                        value=max(values),
                        limit=100,
                        message=f"{name}: {max(values):.1f}% winding current",
                    )
                )
        rows = []
        for name, load in self.loads.items():
            connected = load["bus"] in live
            # Select through Loads too: SetActiveElement alone does not update
            # the collection index used by IsDelta and kV.
            ctx.Loads.Name(name)
            requested = load["kw"] * state.load_factors["all"]
            powers = ctx.CktElement.Powers()
            p, q = sum(powers[::2]), sum(powers[1::2])
            actual = max(0, p) if connected else 0
            voltage = load_voltages(ctx)
            branch_pu = list(voltage["voltage_branches_pu"].values())
            voltage_ok = converged and connected and all(0.9 <= v <= 1.1 for v in branch_pu)
            rows.append(
                {
                    "load_id": name,
                    "label": f"Load {name}",
                    "critical": name == "675a",
                    "requested_kw": requested,
                    "served_kw": min(actual, requested),
                    "native_kw": p if converged else None,
                    "native_kvar": q if converged else None,
                    "quality": "solved" if converged else "unconverged",
                    "voltage_compliant": voltage_ok,
                    "energized": connected,
                    "voltage_pu": min(branch_pu) if connected else 0,
                    "voltage_max_pu": max(branch_pu) if connected else 0,
                    "bus_min_voltage_pu": min(
                        (v for k, v in volts.items() if k.split(".")[0] == load["bus"]), default=0
                    )
                    if connected
                    else 0,
                    **voltage,
                }
            )
        total = sum(r["requested_kw"] for r in rows)
        served = sum(r["served_kw"] for r in rows)
        return ElectricalResult(
            converged=converged,
            feasible=converged and not violations,
            voltages_pu=volts,
            line_loading_pct=loading,
            transformer_loading_pct=transformers,
            served_kw=served,
            requested_kw=total,
            unserved_kw=max(0, total - served),
            critical_served_kw=sum(r["served_kw"] for r in rows if r["critical"]),
            restored_kw=0.0,
            min_voltage_pu=min(v for k, v in volts.items() if k.split(".")[0] in live),
            max_loading_pct=max([*loading.values(), *transformers.values()]),
            losses_kw=ctx.Circuit.Losses()[0] / 1000,
            energized_buses=tuple(sorted(live)),
            violations=tuple(violations),
            load_measurements=tuple(rows),
        ).with_restoration_baseline({name: 0.0 for name in sorted(self.affected)})

    def execute(self, state, action):
        steps = []
        for op in action.operations:
            if op.line not in self.lines:
                raise ValueError("Unknown switching device")
            closed = set(state.closed_lines)
            (closed.add if op.closed else closed.discard)(op.line)
            state = state.model_copy(update={"closed_lines": tuple(sorted(closed))})
            steps.append(self.solve(state))
        result = steps[-1] if steps else self.solve(state)
        earlier = tuple(v for step in steps[:-1] for v in step.violations)
        if earlier:
            result = result.model_copy(
                update={"feasible": False, "violations": earlier + result.violations}
            )
        return ActionOutcome(action=action, final_state=state, result=result, steps=tuple(steps))
