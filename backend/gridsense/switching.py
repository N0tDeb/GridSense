"""Explicit device positions and structural interlocks for clocked commands."""

from typing import Literal

import networkx as nx

from .domain import CircuitState, Record
from .network import graph_for, transformer_ids, unavailable, validate_equipment


class SwitchOperation(Record):
    equipment: str
    closed: bool

    @property
    def key(self):
        return self.equipment + (":close" if self.closed else ":open")


def device_positions(state, network):
    return {
        **{"line:" + r["id"]: r["id"] in state.closed_lines for r in network["lines"]},
        **{
            "transformer:" + name: name not in state.open_transformers
            for name in transformer_ids(network)
        },
        "source:" + network["source_bus"]: state.source_connected,
    }


def position_key(state, network):
    return tuple(sorted(device_positions(state, network).items()))


def normal_positions(network):
    return {
        **{"line:" + r["id"]: not r["normally_open"] for r in network["lines"]},
        **{"transformer:" + name: True for name in transformer_ids(network)},
        "source:" + network["source_bus"]: True,
    }


def change_position(state: CircuitState, operation: SwitchOperation, network):
    """Change a native-model input; never veto on hidden load/voltage outcomes.

    Availability and the declared radial-operation interlock can block closure.
    The caller must solve the resulting state, even if it violates electrical limits.
    """
    if operation.equipment not in device_positions(state, network):
        raise ValueError("Unknown switchable equipment")
    kind, name = operation.equipment.split(":", 1)
    if operation.closed and (
        name in unavailable(state, kind) or kind == "line" and name in state.locked_open
    ):
        raise ValueError("Equipment is unavailable or locked open")
    if kind == "line":
        closed = set(state.closed_lines)
        (closed.add if operation.closed else closed.discard)(name)
        changed = state.model_copy(update={"closed_lines": tuple(sorted(closed))})
    elif kind == "transformer":
        opened = set(state.open_transformers)
        (opened.discard if operation.closed else opened.add)(name)
        changed = state.model_copy(update={"open_transformers": tuple(sorted(opened))})
    else:
        changed = state.model_copy(update={"source_connected": operation.closed})
    validate_equipment(changed, network)
    if not nx.is_forest(graph_for(changed, network)):
        raise ValueError("Radial-operation interlock blocked a mesh")
    return changed


class Command(Record):
    id: str
    plan_id: str
    operation: SwitchOperation
    issued_at_s: int
    deadline_s: int
    status: Literal[
        "pending", "confirmed", "failed", "unknown", "confirmed_late", "failed_late"
    ] = "pending"
    received_at_s: int | None = None
    measured_at_s: int | None = None
    confirmed_position: bool | None = None
    feedback_sequence: int = -1


class Actuation(Record):
    command_id: str
    due_s: int


class PositionFeedback(Record):
    command_id: str
    sequence: int
    equipment: str
    closed: bool
    measured_at_s: int
    due_s: int


def confirm_commands(commands, pending, at_s):
    """Correlated feedback precedes timeout. Duplicates cannot settle a new command."""
    commands, events = dict(commands), []
    due = sorted((r for r in pending if r.due_s <= at_s), key=lambda r: (r.due_s, r.sequence))
    for row in due:
        command = commands.get(row.command_id)
        accepted = bool(
            command
            and row.equipment == command.operation.equipment
            and command.issued_at_s < row.measured_at_s <= at_s
            and row.sequence > command.feedback_sequence
            and command.status in {"pending", "unknown"}
        )
        if accepted:
            late = command.status == "unknown" or at_s > command.deadline_s
            status = "confirmed" if row.closed == command.operation.closed else "failed"
            command = command.model_copy(
                update={
                    "status": status + ("_late" if late else ""),
                    "received_at_s": at_s,
                    "confirmed_position": row.closed,
                    "measured_at_s": row.measured_at_s,
                    "feedback_sequence": row.sequence,
                }
            )
            commands[command.id] = command
        events.append(
            {
                "kind": "position_feedback",
                "row": row.model_dump(mode="json"),
                "received_at_s": at_s,
                "accepted": accepted,
                "status": command.status if command else "unknown_command",
            }
        )
    for key, command in commands.items():
        if command.status == "pending" and at_s >= command.deadline_s:
            commands[key] = command.model_copy(update={"status": "unknown"})
            events.append({"kind": "confirmation_timeout", "command_id": key, "at_s": at_s})
    return commands, tuple(r for r in pending if r.due_s > at_s), events
