"""Static loop detection: which automations can trigger each other.

Automation A "feeds" B when A sets a property (or any state, for telemetry) of a device
that one of B's triggers watches. A cycle in that graph is a potential loop. It is
reported as a warning when saving, not rejected: whether it actually loops depends on
values (A turns the light on, B turns it off when it is on...), which the runtime guard
in `engine.admit` catches.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from smarthome.modules.automations.domain.definition import (
    CommandStep,
    Definition,
    SceneStep,
    SourceKind,
)
from smarthome.modules.automations.domain.model import Scene


@dataclass(frozen=True, slots=True)
class Node:
    id: UUID
    name: str
    definition: Definition


def writes(definition: Definition, scenes: Mapping[UUID, Scene]) -> set[tuple[str, str]]:
    """(device, property) pairs the actions set."""
    found: set[tuple[str, str]] = set()
    for action in definition.actions:
        if isinstance(action, CommandStep) and action.desired:
            found |= {(action.device_id, p) for p in action.desired}
        elif isinstance(action, SceneStep) and action.scene_id in scenes:
            for state in scenes[action.scene_id].states:
                found |= {(state.device_id, p) for p in state.desired}
    return found


def feeds(source: Node, target: Node, scenes: Mapping[UUID, Scene]) -> bool:
    written = writes(source.definition, scenes)
    devices = {device for device, _ in written}
    for _, trigger in target.definition.device_triggers():
        watched = trigger.predicate.source
        if watched.kind is SourceKind.TELEMETRY and watched.device_id in devices:
            return True  # setting a device's state changes what it measures (power...)
        if watched.kind is SourceKind.DEVICE_STATE and (watched.device_id, watched.key) in written:
            return True
    return False


def loops(nodes: list[Node], scenes: Mapping[UUID, Scene]) -> list[list[Node]]:
    """Groups of automations that can trigger each other (strongly connected
    components with a cycle), via Tarjan's algorithm."""
    edges = {n.id: [m for m in nodes if feeds(n, m, scenes)] for n in nodes}
    index: dict[UUID, int] = {}
    low: dict[UUID, int] = {}
    stack: list[Node] = []
    on_stack: set[UUID] = set()
    found: list[list[Node]] = []
    counter = 0

    def visit(node: Node) -> None:
        nonlocal counter
        index[node.id] = low[node.id] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node.id)
        for successor in edges[node.id]:
            if successor.id not in index:
                visit(successor)
                low[node.id] = min(low[node.id], low[successor.id])
            elif successor.id in on_stack:
                low[node.id] = min(low[node.id], index[successor.id])
        if low[node.id] == index[node.id]:
            component: list[Node] = []
            while True:
                member = stack.pop()
                on_stack.discard(member.id)
                component.append(member)
                if member.id == node.id:
                    break
            if len(component) > 1 or any(m.id == node.id for m in edges[node.id]):
                found.append(sorted(component, key=lambda n: n.name))

    for node in nodes:
        if node.id not in index:
            visit(node)
    return found


def loop_warnings(subject: UUID, nodes: list[Node], scenes: Mapping[UUID, Scene]) -> list[str]:
    warnings = []
    for component in loops(nodes, scenes):
        if any(n.id == subject for n in component):
            if len(component) == 1:
                warnings.append(
                    f"{component[0].name!r} changes a device its own trigger watches and may"
                    " re-trigger itself"
                )
            else:
                names = ", ".join(repr(n.name) for n in component)
                warnings.append(f"{names} can trigger each other in a loop")
    return warnings
