"""The `Process` builder and `Process.to_model()`, with no browser."""

import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import pytest
import xmlschema

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


# --------------------------------------------------------------------------- gateways and loops: definition time


def _yes(_v: object) -> bool:
    return True


@pytest.mark.parametrize(
    "declare_second",
    [
        "event",
        "gateway",
        "loop",
    ],
)
@pytest.mark.parametrize("declare_first", ["event", "gateway", "loop"])
def test_gateway_loop_and_event_ids_share_one_namespace(
    declare_first: str, declare_second: str
) -> None:
    p = Process("p")
    names = iter(range(2))

    def declare(kind: str) -> None:
        match kind:
            case "event":
                with p.activity(f"A {next(names)}"):

                    async def decide(_ctx: EventContext) -> None:
                        pass

                    _ = p.event("Decide")(decide)
            case "gateway":
                with p.if_("decide", _yes, label="yes"):
                    pass
            case _:
                with p.for_each("decide", over="items", as_="item"):
                    pass

    declare(declare_first)
    with pytest.raises(ValueError, match="decide"):
        declare(declare_second)


@pytest.mark.parametrize(
    ("block", "generated"), [("gateway", "decide_join"), ("loop", "decide_next")]
)
def test_ids_the_bpmn_export_generates_are_reserved(block: str, generated: str) -> None:
    def declare_block(p: Process) -> None:
        if block == "gateway":
            with p.if_("decide", _yes, label="yes"):
                pass
        else:
            with p.for_each("decide", over="items", as_="item"):
                pass

    def declare_event(p: Process) -> None:
        async def fn(_ctx: EventContext) -> None:
            pass

        fn.__name__ = generated
        with p.activity("A"):
            _ = p.event("Do it")(fn)

    before = Process("p")
    declare_block(before)
    with pytest.raises(ValueError, match=generated):
        declare_event(before)

    after = Process("p")
    declare_event(after)
    with pytest.raises(ValueError, match=generated):
        declare_block(after)


def test_an_arm_needs_a_label() -> None:
    p = Process("p")
    with pytest.raises(TypeError, match="label"):
        _ = p.if_("decide", _yes)  # pyright: ignore[reportCallIssue, reportUnknownVariableType]
    with pytest.raises(ValueError, match="label"), p.if_("decide", _yes, label=" "):
        pass
    with p.if_("decide2", _yes, label="yes"):
        pass
    with pytest.raises(ValueError, match="label"), p.elif_(_yes, label=""):
        pass


def test_a_condition_must_be_callable() -> None:
    p = Process("p")
    with (
        pytest.raises(TypeError, match="condition"),
        p.if_("decide", True, label="yes"),  # pyright: ignore[reportArgumentType]
    ):
        pass


@pytest.mark.parametrize("keyword", ["elif_", "else_"])
def test_elif_and_else_need_an_if_right_before_them(keyword: str) -> None:
    def follow(p: Process) -> None:
        block = p.elif_(_yes, label="yes") if keyword == "elif_" else p.else_()
        with block:
            pass

    with pytest.raises(ValueError, match=keyword):
        follow(Process("p"))

    p = Process("p")
    with p.if_("decide", _yes, label="yes"):
        pass
    with p.activity("A"):
        _ = p.event("Click OK")(_noop)
    with pytest.raises(ValueError, match=keyword):
        follow(p)

    nested = Process("p")
    with nested.if_("outer", _yes, label="yes"):
        pass
    with (
        nested.for_each("each", over="items", as_="item"),
        pytest.raises(ValueError, match=keyword),
    ):
        follow(nested)


def test_nothing_follows_else() -> None:
    p = Process("p")
    with p.if_("decide", _yes, label="yes"):
        pass
    with p.else_():
        pass
    with pytest.raises(ValueError, match="else_"), p.elif_(_yes, label="late"):
        pass
    with pytest.raises(ValueError, match="else_"), p.else_():
        pass


def test_a_loop_cannot_bind_its_item_to_its_list() -> None:
    p = Process("p")
    with (
        pytest.raises(ValueError, match="items"),
        p.for_each("each", over="items", as_="items"),
    ):
        pass


# --------------------------------------------------------------------------- gateways and loops: export


def _event(p: Process, event_id: str, name: str) -> None:
    async def fn(_ctx: EventContext) -> None:
        pass

    fn.__name__ = event_id
    with p.activity(f"Activity of {event_id}"):
        _ = p.event(name)(fn)


def spending_process() -> Process:
    """A loop with an XOR (if / elif / else) inside it, and a nested loop in one arm."""
    p = Process("spending", variables={"pending": list})
    _event(p, "open_requests", "Open the requests page")
    with p.for_each("each_request", over="pending", as_="request_id"):
        _event(p, "open_request", "Open the request")
        with p.if_("decide", _yes, label="amount within limit"):
            _event(p, "click_approve", "Click Approve")
        with p.elif_(_yes, label="amount over the limit"):
            with p.for_each("each_reason", over="reasons", as_="reason"):
                _event(p, "pick_reason", "Pick the reason")
            _event(p, "click_reject", "Click Reject")
        with p.else_():
            _event(p, "click_hold", "Click Hold")
        _event(p, "return_to_list", "Return to the list")
    _event(p, "read_final_budget", "Read the final budget")
    return p


def test_compact_outline_of_branches_and_loops() -> None:
    assert spending_process().to_model("compact") == "\n".join(  # noqa: FLY002  # one item per outline line
        [
            "process spending",
            '  open_requests "Open the requests page"',
            "  for each_request: request_id in pending",
            '    open_request "Open the request"',
            "    if decide: amount within limit",
            '      click_approve "Click Approve"',
            "    elif: amount over the limit",
            "      for each_reason: reason in reasons",
            '        pick_reason "Pick the reason"',
            '      click_reject "Click Reject"',
            "    otherwise",
            '      click_hold "Click Hold"',
            '    return_to_list "Return to the list"',
            '  read_final_budget "Read the final budget"',
        ]
    )


def _graph(
    p: Process,
) -> tuple[ET.Element, dict[str, ET.Element], list[tuple[str, str, str | None]]]:
    """The BPMN process element, its flow nodes by id, and its flows as (source, target, name)."""
    root = ET.fromstring(p.to_model("bpmn"))
    process = root.findall(f"{BPMN}process")[0]
    nodes = {el.get("id", ""): el for el in process if el.tag != f"{BPMN}sequenceFlow"}
    flows = [
        (f.get("sourceRef", ""), f.get("targetRef", ""), f.get("name"))
        for f in process.findall(f"{BPMN}sequenceFlow")
    ]
    return root, nodes, flows


def _out(
    flows: list[tuple[str, str, str | None]], source: str
) -> set[tuple[str, str | None]]:
    return {(t, n) for s, t, n in flows if s == source}


def test_bpmn_of_an_xor_is_a_split_and_a_join_with_flows_named_by_arm_labels() -> None:
    _, nodes, flows = _graph(spending_process())

    assert nodes["decide"].tag == f"{BPMN}exclusiveGateway"
    assert nodes["decide_join"].tag == f"{BPMN}exclusiveGateway"
    assert _out(flows, "decide") == {
        ("click_approve", "amount within limit"),
        ("each_reason", "amount over the limit"),
        ("click_hold", "otherwise"),
    }
    assert {s for s, t, _ in flows if t == "decide_join"} == {
        "click_approve",
        "click_reject",
        "click_hold",
    }
    assert _out(flows, "decide_join") == {("return_to_list", None)}


def test_bpmn_of_a_loop_is_a_head_merge_and_a_next_split_with_a_back_edge() -> None:
    _, nodes, flows = _graph(spending_process())

    for loop, as_, over, first, last, after in [
        (
            "each_request",
            "request_id",
            "pending",
            "open_request",
            "return_to_list",
            "read_final_budget",
        ),
        (
            "each_reason",
            "reason",
            "reasons",
            "pick_reason",
            "pick_reason",
            "click_reject",
        ),
    ]:
        head, tail = nodes[loop], nodes[f"{loop}_next"]
        assert head.tag == tail.tag == f"{BPMN}exclusiveGateway"
        assert head.get("name") == f"For each {as_} in {over}"
        assert _out(flows, loop) == {(first, None)}
        assert _out(flows, last) == {(f"{loop}_next", None)}
        assert _out(flows, f"{loop}_next") == {
            (loop, f"next {as_}"),
            (after, f"all {over} done"),
        }
    # The inner loop's gateway pair sits inside the outer loop's arm, which rejoins before its tail.
    assert {s for s, t, _ in flows if t == "each_request"} == {
        "open_requests",
        "each_request_next",
    }


def test_bpmn_of_an_xor_with_no_else_has_an_otherwise_path_to_the_join() -> None:
    p = Process("p")
    with p.if_("decide", _yes, label="yes"):
        _event(p, "do_it", "Do it")

    _, _, flows = _graph(p)

    assert _out(flows, "decide") == {("do_it", "yes"), ("decide_join", "otherwise")}


def test_bpmn_of_empty_blocks_still_connects() -> None:
    p = Process("p")
    with p.if_("decide", _yes, label="yes"):
        pass
    with p.for_each("each", over="items", as_="item"):
        pass

    _, _, flows = _graph(p)

    assert _out(flows, "decide") == {
        ("decide_join", "yes"),
        ("decide_join", "otherwise"),
    }
    assert _out(flows, "each") == {("each_next", None)}


def test_bpmn_of_branches_and_loops_is_a_well_formed_graph() -> None:
    root, nodes, flows = _graph(spending_process())

    ids = [el.get("id") for el in root.iter() if el.get("id") is not None]
    assert len(ids) == len(set(ids))
    assert all(s in nodes and t in nodes for s, t, _ in flows)
    tags = {el.tag for el in nodes.values()}
    assert tags == {
        f"{BPMN}{t}" for t in ("startEvent", "endEvent", "task", "exclusiveGateway")
    }
    starts = [i for i, el in nodes.items() if el.tag == f"{BPMN}startEvent"]
    ends = [i for i, el in nodes.items() if el.tag == f"{BPMN}endEvent"]
    assert len(starts) == len(ends) == 1

    def reachable(start: str, edges: list[tuple[str, str]]) -> set[str]:
        seen, todo = {start}, [start]
        while todo:
            node = todo.pop()
            for s, t in edges:
                if s == node and t not in seen:
                    seen.add(t)
                    todo.append(t)
        return seen

    forward = [(s, t) for s, t, _ in flows]
    assert reachable(starts[0], forward) == set(nodes)
    assert reachable(ends[0], [(t, s) for s, t in forward]) == set(nodes)


# --------------------------------------------------------------------------- BPMN 2.0 schema


@pytest.fixture(scope="module")
def bpmn_schema() -> xmlschema.XMLSchema:
    return xmlschema.XMLSchema(Path(__file__).parent / "bpmn20" / "BPMN20.xsd")


def _no_else() -> Process:
    p = Process("no_else")
    with p.if_("decide", _yes, label="yes"):
        _event(p, "do_it", "Do it")
    return p


def _empty_blocks() -> Process:
    p = Process("empty_blocks")
    with p.if_("decide", _yes, label="yes"):
        pass
    with p.for_each("each", over="items", as_="item"):
        pass
    return p


@pytest.mark.parametrize(
    "make",
    [
        greet_process,
        spending_process,
        _no_else,
        _empty_blocks,
        lambda: Process("empty"),
    ],
    ids=["sequence", "branches and loops", "no else", "empty blocks", "empty process"],
)
def test_bpmn_validates_against_the_bpmn_2_0_schema(
    bpmn_schema: xmlschema.XMLSchema, make: Callable[[], Process]
) -> None:
    errors = [str(e) for e in bpmn_schema.iter_errors(make().to_model("bpmn"))]

    assert errors == []
