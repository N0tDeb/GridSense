"""The versioned network definition is shared by DSS construction and graph checks."""

import hashlib
import json
from importlib.resources import files
import networkx as nx
from .domain import CircuitState, EquipmentOutage


def load_network() -> dict:
    return json.loads(
        files("gridsense").joinpath("circuits/feeder.json").read_text(encoding="utf-8")
    )


def network_hash(network: dict) -> str:
    return hashlib.sha256(
        json.dumps(network, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def initial_state(factors: dict[str, float], network: dict | None = None) -> CircuitState:
    """The isolated incident used by existing snapshot experiments."""
    n = network or load_network()
    faulted = n["faulted_line"]
    return CircuitState(
        load_factors=factors,
        closed_lines=tuple(
            sorted(l["id"] for l in n["lines"] if not l["normally_open"] and l["id"] != faulted)
        ),
        locked_open=(faulted,),
    )


def healthy_state(
    factors: dict[str, float] | None = None, network: dict | None = None
) -> CircuitState:
    n = network or load_network()
    return CircuitState(
        load_factors=dict(factors) if factors is not None else {"a": 1, "b": 1, "c": 1},
        closed_lines=tuple(sorted(l["id"] for l in n["lines"] if not l["normally_open"])),
        locked_open=(),
    )


def unavailable(state: CircuitState, kind: str) -> set[str]:
    return {o.equipment for o in state.equipment_outages if o.kind == kind}


def transformer_ids(network: dict) -> set[str]:
    return {"main", *(f"tx_{load['id']}" for load in network["loads"])}


def validate_equipment(state: CircuitState, network: dict) -> None:
    assets = {
        "line": {line["id"] for line in network["lines"]},
        "transformer": transformer_ids(network),
        "source": {network["source_bus"]},
    }
    ids = [o.incident_id for o in state.equipment_outages]
    if len(ids) != len(set(ids)):
        raise ValueError("Equipment incidents must have unique IDs")
    if not set(state.open_transformers).issubset(assets["transformer"]):
        raise ValueError("Unknown isolated transformer")
    for outage in state.equipment_outages:
        if outage.equipment not in assets[outage.kind]:
            raise ValueError("Unknown outage equipment")
        connected = (
            outage.equipment in state.closed_lines
            if outage.kind == "line"
            else outage.equipment not in state.open_transformers
            if outage.kind == "transformer"
            else state.source_connected
        )
        if connected:
            raise ValueError("Unavailable equipment must remain isolated")


def isolate_outage(
    state: CircuitState, outage: EquipmentOutage, network: dict | None = None
) -> CircuitState:
    """Apply an availability event and its declared protective isolation.

    This is an explicit whole-equipment trip assumption, not fault localization
    or a protection-waveform simulation. Repair does not reverse the opening.
    """
    changes = {"equipment_outages": (*state.equipment_outages, outage)}
    if outage.kind == "line":
        changes["closed_lines"] = tuple(sorted(set(state.closed_lines) - {outage.equipment}))
    elif outage.kind == "transformer":
        changes["open_transformers"] = tuple(
            sorted(set(state.open_transformers) | {outage.equipment})
        )
    else:
        changes["source_connected"] = False
    changed = state.model_copy(update=changes)
    validate_equipment(changed, network or load_network())
    return changed


def repair_outage(state: CircuitState, incident_id: str) -> CircuitState:
    """Clear one incident's availability restriction; retain all device positions."""
    if incident_id not in {o.incident_id for o in state.equipment_outages}:
        raise ValueError("Unknown equipment incident")
    return state.model_copy(
        update={
            "equipment_outages": tuple(
                o for o in state.equipment_outages if o.incident_id != incident_id
            )
        }
    )


def graph_for(state: CircuitState, network: dict) -> nx.Graph:
    g = nx.Graph()
    g.add_nodes_from(b["id"] for b in network["buses"])
    g.add_edges_from(
        (l["from_bus"], l["to_bus"])
        for l in network["lines"]
        if l["id"] in state.closed_lines and l["id"] not in unavailable(state, "line")
    )
    return g


def energized(state: CircuitState, network: dict) -> set[str]:
    if (
        not state.source_connected
        or unavailable(state, "source")
        or "main" in set(state.open_transformers) | unavailable(state, "transformer")
    ):
        return set()
    return set(nx.node_connected_component(graph_for(state, network), network["distribution_bus"]))


def load_connected(state: CircuitState, load: dict, live: set[str]) -> bool:
    return load["bus"] in live and f"tx_{load['id']}" not in set(
        state.open_transformers
    ) | unavailable(state, "transformer")


def restoration_baseline_kw(state: CircuitState, network: dict) -> dict[str, float]:
    """Explicit zero-service baseline for loads disconnected by this incident state."""
    live = energized(state, network)
    return {load["id"]: 0.0 for load in network["loads"] if not load_connected(state, load, live)}
