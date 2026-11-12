"""The Process model: BPMN 2.0 or the compact outline, rendered from the definition alone."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from itertools import pairwise
from typing import Literal

from reparo_playwright_bridge._definition import ProcessDefinition

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
    lines += [
        f"  {event.id} {json.dumps(event.name, ensure_ascii=False)}"
        for event in definition.root.nodes
    ]
    return "\n".join(lines)


def _bpmn(definition: ProcessDefinition) -> str:
    # Plain tags under a default `xmlns`: every element is in the BPMN model namespace.
    root = ET.Element(
        "definitions", xmlns=BPMN_NS, id=DEFINITIONS_ID, targetNamespace=TARGET_NS
    )
    process = ET.SubElement(
        root, "process", id=PROCESS_ID, name=definition.name, isExecutable="false"
    )
    _ = ET.SubElement(process, "startEvent", id=START_ID)
    for event in definition.root.nodes:
        _ = ET.SubElement(process, "task", id=event.id, name=event.name)
    _ = ET.SubElement(process, "endEvent", id=END_ID)

    chain = [START_ID, *(event.id for event in definition.root.nodes), END_ID]
    for n, (source, target) in enumerate(pairwise(chain), start=1):
        _ = ET.SubElement(
            process, "sequenceFlow", id=f"flow-{n}", sourceRef=source, targetRef=target
        )

    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
