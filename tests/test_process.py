"""The `Process` builder and `Process.to_model()`, with no browser."""

import xml.etree.ElementTree as ET

import pytest

from reparo_playwright_bridge import EventContext, Process

BPMN = "{http://www.omg.org/spec/BPMN/20100524/MODEL}"


async def _noop(_ctx: EventContext) -> None:
    pass


def greet_process() -> Process:
    p = Process("greet", variables={"who": str})
    with p.activity("Open the greeter"):

        @p.event("Open the greeter page")
        async def open_greeter(_ctx: EventContext) -> None:
            pass

    with p.activity("Greet"):

        @p.event('Type the name into "Name"')
        async def type_name(_ctx: EventContext) -> None:
            pass

        @p.event("Click Greet")
        async def click_greet(_ctx: EventContext) -> None:
            pass

    return p


# --------------------------------------------------------------------------- definition time


def test_input_variables_must_be_types() -> None:
    with pytest.raises(TypeError, match="approval_limit"):
        _ = Process("p", variables={"approval_limit": 500})  # pyright: ignore[reportArgumentType]


def test_input_variables_reject_generic_aliases() -> None:
    with pytest.raises(TypeError, match="pending"):
        _ = Process("p", variables={"pending": list[str]})


def test_duplicate_event_ids_are_rejected() -> None:
    p = Process("p")
    with p.activity("A"):
        _ = p.event("first")(_noop)
        with pytest.raises(ValueError, match="_noop"):
            _ = p.event("second")(_noop)


def test_two_events_may_share_a_grounded_name() -> None:
    p = Process("p")
    with p.activity("A"):

        @p.event("Click OK")
        async def first_ok(_ctx: EventContext) -> None:
            pass

        @p.event("Click OK")
        async def second_ok(_ctx: EventContext) -> None:
            pass

    assert p.to_model("compact").splitlines()[1:] == [
        '  first_ok "Click OK"',
        '  second_ok "Click OK"',
    ]


def test_duplicate_activity_names_are_rejected() -> None:
    p = Process("p")
    with p.activity("Open the request"):
        pass
    with (
        pytest.raises(ValueError, match="Open the request"),
        p.activity("Open the request"),
    ):
        pass


def test_activities_whose_ids_collide_are_rejected() -> None:
    p = Process("p")
    with p.activity("Open the request"):
        pass
    with (
        pytest.raises(ValueError, match="open the request!"),
        p.activity("open the request!"),
    ):
        pass


def test_an_event_outside_an_activity_is_rejected() -> None:
    p = Process("p")
    with pytest.raises(ValueError, match="_noop"):
        _ = p.event("Click OK")(_noop)


def test_nested_activities_are_rejected() -> None:
    p = Process("p")
    with (
        p.activity("Outer"),
        pytest.raises(ValueError, match="Inner"),
        p.activity("Inner"),
    ):
        pass


def test_an_event_needs_a_grounded_name() -> None:
    p = Process("p")
    with p.activity("A"):
        with pytest.raises(TypeError, match="grounded name"):
            _ = p.event(_noop)  # pyright: ignore[reportArgumentType]
        with pytest.raises(ValueError, match="grounded name"):
            _ = p.event("  ")(_noop)


def test_an_event_must_be_async() -> None:
    p = Process("p")
    with p.activity("A"):

        def not_async(_ctx: EventContext) -> None:
            pass

        with pytest.raises(TypeError, match="not_async"):
            _ = p.event("Click OK")(not_async)  # pyright: ignore[reportArgumentType]


def test_a_failed_activity_block_leaves_the_builder_usable() -> None:
    p = Process("p")
    with pytest.raises(RuntimeError), p.activity("A"):
        raise RuntimeError
    with p.activity("B"):
        _ = p.event("Click OK")(_noop)

    assert p.to_model("compact").splitlines()[1:] == ['  _noop "Click OK"']


# --------------------------------------------------------------------------- compact


def test_compact_outline_of_a_sequence() -> None:
    assert greet_process().to_model("compact") == (
        "process greet\n"
        '  open_greeter "Open the greeter page"\n'
        '  type_name "Type the name into \\"Name\\""\n'
        '  click_greet "Click Greet"'
    )


# --------------------------------------------------------------------------- BPMN


def test_bpmn_is_the_default_format() -> None:
    p = greet_process()

    assert p.to_model() == p.to_model("bpmn")


def test_bpmn_of_a_sequence_is_one_task_per_event_between_a_start_and_an_end() -> None:
    root = ET.fromstring(greet_process().to_model("bpmn"))

    assert root.tag == f"{BPMN}definitions"
    assert root.get("targetNamespace")
    processes = root.findall(f"{BPMN}process")
    assert len(processes) == 1
    process = processes[0]
    assert process.get("name") == "greet"

    starts = process.findall(f"{BPMN}startEvent")
    ends = process.findall(f"{BPMN}endEvent")
    tasks = process.findall(f"{BPMN}task")
    assert len(starts) == 1
    assert len(ends) == 1
    assert [(t.get("id"), t.get("name")) for t in tasks] == [
        ("open_greeter", "Open the greeter page"),
        ("type_name", 'Type the name into "Name"'),
        ("click_greet", "Click Greet"),
    ]

    # The flows chain start -> each task in order -> end, and reference only nodes that exist.
    flows = {
        f.get("sourceRef"): f.get("targetRef")
        for f in process.findall(f"{BPMN}sequenceFlow")
    }
    chain = [starts[0].get("id")]
    while chain[-1] in flows:
        chain.append(flows[chain[-1]])
    assert chain == [
        starts[0].get("id"),
        "open_greeter",
        "type_name",
        "click_greet",
        ends[0].get("id"),
    ]
    assert len(flows) == 4

    # Every id in the document is unique.
    ids = [el.get("id") for el in root.iter() if el.get("id") is not None]
    assert len(ids) == len(set(ids))


def test_bpmn_has_no_diagram_section() -> None:
    root = ET.fromstring(greet_process().to_model("bpmn"))

    assert not [
        el
        for el in root.iter()
        if "BPMNDiagram" in el.tag or "BPMN/20100524/DI" in el.tag
    ]


def test_bpmn_generated_ids_never_collide_with_event_ids() -> None:
    p = Process("start")
    with p.activity("A"):
        for event_id in (
            "start",
            "end",
            "process",
            "definitions",
            "flow_1",
            "StartEvent_1",
        ):

            async def fn(_ctx: EventContext) -> None:
                pass

            fn.__name__ = event_id
            _ = p.event(f"Do {event_id}")(fn)

    root = ET.fromstring(p.to_model("bpmn"))

    ids = [el.get("id") for el in root.iter() if el.get("id") is not None]
    assert len(ids) == len(set(ids))


def test_an_empty_process_goes_straight_from_start_to_end() -> None:
    root = ET.fromstring(Process("empty").to_model("bpmn"))
    process = root.findall(f"{BPMN}process")[0]
    start = process.findall(f"{BPMN}startEvent")[0]
    end = process.findall(f"{BPMN}endEvent")[0]

    flows = [
        (f.get("sourceRef"), f.get("targetRef"))
        for f in process.findall(f"{BPMN}sequenceFlow")
    ]
    assert flows == [(start.get("id"), end.get("id"))]
