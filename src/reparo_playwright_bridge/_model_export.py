"""The Process model: BPMN 2.0 or the compact outline, rendered from the definition alone."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Literal

from reparo_playwright_bridge._definition import (
    ELSE_LABEL,
    EventNode,
    LoopNode,
    ProcessDefinition,
    SequenceNode,
    XorNode,
)

ModelFormat = Literal["bpmn", "compact"]

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
TARGET_NS = "urn:reparo-playwright-bridge:process"

# Generated ids contain "-", which no Event id (a Python identifier) can, so they never collide.
DEFINITIONS_ID = "bpmn-definitions"
PROCESS_ID = "bpmn-process"
START_ID = "process-start"
END_ID = "process-end"


def render(definition: ProcessDefinition, model_format: ModelFormat) -> str:
    match model_format:
        case "bpmn":
            return _bpmn(definition)
        case "compact":
            return _compact(definition)


def _compact(definition: ProcessDefinition) -> str:
    lines = [f"process {definition.name}"]
    _outline(definition.root, 1, lines)
    return "\n".join(lines)


def _outline(sequence: SequenceNode, depth: int, lines: list[str]) -> None:
    indent = "  " * depth
    for node in sequence.nodes:
        match node:
            case EventNode():
                lines.append(
                    f"{indent}{node.id} {json.dumps(node.name, ensure_ascii=False)}"
                )
            case LoopNode():
                lines.append(f"{indent}for {node.id}: {node.as_} in {node.over}")
                _outline(node.body, depth + 1, lines)
            case XorNode():
                for n, arm in enumerate(node.arms):
                    if n == 0:
                        lines.append(f"{indent}if {node.id}: {arm.label}")
                    elif arm.condition is None:
                        lines.append(f"{indent}{ELSE_LABEL}")
                    else:
                        lines.append(f"{indent}elif: {arm.label}")
                    _outline(arm.body, depth + 1, lines)


Exits = list[tuple[str, str | None]]
"""Where control leaves a block: (source node id, name of the flow out of it)."""


class _BpmnGraph:
    """Flow nodes and sequence flows of one BPMN process, in declaration order."""

    def __init__(self, process: ET.Element) -> None:
        self.process: ET.Element = process
        self.flows: list[tuple[str, str, str | None]] = []

    def node(self, tag: str, node_id: str, **attrs: str) -> None:
        _ = ET.SubElement(self.process, tag, {"id": node_id, **attrs})

    def connect(self, exits: Exits, target: str) -> None:
        self.flows += [(source, target, name) for source, name in exits]

    def sequence(self, sequence: SequenceNode, exits: Exits) -> Exits:
        for node in sequence.nodes:
            match node:
                case EventNode():
                    self.node("task", node.id, name=node.name)
                    self.connect(exits, node.id)
                    exits = [(node.id, None)]
                case LoopNode():
                    # A head merge, the body, then a split that goes back or leaves.
                    tail = f"{node.id}_next"
                    self.node(
                        "exclusiveGateway",
                        node.id,
                        name=f"For each {node.as_} in {node.over}",
                        gatewayDirection="Converging",
                    )
                    self.connect(exits, node.id)
                    body_exits = self.sequence(node.body, [(node.id, None)])
                    self.node("exclusiveGateway", tail, gatewayDirection="Diverging")
                    self.connect(body_exits, tail)
                    self.flows.append((tail, node.id, f"next {node.as_}"))
                    exits = [(tail, f"all {node.over} done")]
                case XorNode():
                    join = f"{node.id}_join"
                    self.node("exclusiveGateway", node.id, gatewayDirection="Diverging")
                    self.connect(exits, node.id)
                    arm_exits: Exits = []
                    for arm in node.arms:
                        arm_exits += self.sequence(arm.body, [(node.id, arm.label)])
                    if node.arms[-1].condition is not None:
                        # No else_: when no condition holds, control goes straight on.
                        arm_exits.append((node.id, ELSE_LABEL))
                    self.node("exclusiveGateway", join, gatewayDirection="Converging")
                    self.connect(arm_exits, join)
                    exits = [(join, None)]
        return exits

    def write_flows(self) -> None:
        for n, (source, target, name) in enumerate(self.flows, start=1):
            flow = ET.SubElement(
                self.process,
                "sequenceFlow",
                id=f"flow-{n}",
                sourceRef=source,
                targetRef=target,
            )
            if name is not None:
                flow.set("name", name)


def _bpmn(definition: ProcessDefinition) -> str:
    # Plain tags under a default `xmlns`: every element is in the BPMN model namespace.
    root = ET.Element(
        "definitions", xmlns=BPMN_NS, id=DEFINITIONS_ID, targetNamespace=TARGET_NS
    )
    process = ET.SubElement(
        root, "process", id=PROCESS_ID, name=definition.name, isExecutable="false"
    )
    graph = _BpmnGraph(process)
    graph.node("startEvent", START_ID)
    exits = graph.sequence(definition.root, [(START_ID, None)])
    graph.node("endEvent", END_ID)
    graph.connect(exits, END_ID)
    graph.write_flows()

    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
