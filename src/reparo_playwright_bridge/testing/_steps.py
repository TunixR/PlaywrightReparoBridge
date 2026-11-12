"""The typed steps of a Recovery script that FakeReparo plays, in order, blindly."""

from __future__ import annotations

from dataclasses import dataclass, field


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
class Decline:
    """End the Recovery: `done` with `success: false`. `reason` is only logged; the wire has no field for it."""

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


type Step = Continue | Decline | SendError | SendRaw | Silence | Close

TERMINAL_STEPS = (Continue, Decline, SendError, Close)
