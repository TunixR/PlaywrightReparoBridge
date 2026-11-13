"""The typed steps of a Recovery script that FakeReparo plays, in order, blindly."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class Locate:
    """A point target resolved just before sending: the centre of `selector`'s bounding box, in
    CSS pixels, on the active page. A selector that matches nothing fails the script loudly.

    It stands in for the coordinates Reparo reads off a screenshot, so it only makes sense for
    `coordinates` targeting (`start_box` / `end_box`), which the vision Modality owns.
    """

    selector: str


type Target = Locate | Sequence[float]


@dataclass(frozen=True)
class Observe:
    """Ask for one more screenshot, as Reparo does before deciding an action."""


@dataclass(frozen=True, init=False)
class Act:
    """A screenshot request, then an `action` frame.

    `target` (a literal `[x, y]` or a `Locate`) becomes the action's `start_box`; `None` sends
    no target, as for `type`, `hotkey` or `wait`. Other `inputs` go into `action_inputs` as-is,
    except that a `Locate` among them (`end_box=Locate(...)`) is resolved too.
    """

    action_type: str
    target: Target | None
    inputs: Mapping[str, object]

    def __init__(
        self, action_type: str, target: Target | None = None, **inputs: object
    ) -> None:
        object.__setattr__(self, "action_type", action_type)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "inputs", MappingProxyType(dict(inputs)))


@dataclass(frozen=True)
class Continue:
    """End the Recovery: `done` naming where the robot continues.

    `variables=None` echoes the payload's Variables back unchanged.
    """

    event_id: str | None
    loop_iteration: list[int]
    variables: dict[str, object] | None = None
    process_finished: bool = False


@dataclass(frozen=True)
class Fail:
    """End the Recovery as failed: `done` with `success: false`, as Reparo sends when it tried and could not recover.

    `reason` is only logged; the wire has no field for it.
    """

    reason: str


@dataclass(frozen=True)
class SendError:
    """End the Recovery: an `error` frame with `code`, then close (1003 for `INVALID_RECOVERY_PAYLOAD`)."""

    code: str
    content: str = field(default="error sent by FakeReparo's script")


@dataclass(frozen=True)
class SendRaw:
    """Send any frame as-is: `str` as text, `bytes` as binary, anything else as JSON text."""

    frame: object


@dataclass(frozen=True)
class Silence:
    """Send nothing for `seconds` (the keep-alive `ping`s go on)."""

    seconds: float


@dataclass(frozen=True)
class Close:
    """End the Recovery: close the socket without a `done`."""


type Step = Observe | Act | Continue | Fail | SendError | SendRaw | Silence | Close

TERMINAL_STEPS = (Continue, Fail, SendError, Close)
