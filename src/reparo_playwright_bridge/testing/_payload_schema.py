"""A copy of Reparo's `RecoveryPayload` (backend `templates/recovery_payload.py`), kept in step by hand."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


def _required_id(value: object) -> str:
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("id fields must be non-empty")
        return value
    raise TypeError("id fields must be str or int")


def _optional_id(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value if value.strip() else None
    raise TypeError("activity_id must be str, int, or null")


class _Strict(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", protected_namespaces=()
    )


class RecoveryUiLogEntry(_Strict):
    case_id: str
    activity_id: str | None
    activity_name: str | None
    event_id: str
    event_name: str | None
    model_event_id: str | None = None
    action_type: str
    application: str | None
    input: str | None
    ui_element_target: str | None
    ui_group: str | None = None
    timestamp: str
    previous_state: str | None
    current_state: str | None

    @field_validator("case_id", "event_id", mode="before")
    @classmethod
    def _normalize_required_id(cls, value: object) -> str:
        return _required_id(value)

    @field_validator("activity_id", mode="before")
    @classmethod
    def _normalize_optional_id(cls, value: object) -> str | None:
        return _optional_id(value)

    @field_validator("timestamp")
    @classmethod
    def _timestamp_iso8601(cls, value: str) -> str:
        try:
            _ = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                "timestamp must be a valid ISO-8601 datetime string"
            ) from exc
        return value


class RecoveryErroredAct(_Strict):
    case_id: str
    activity_id: str | None
    activity_name: str | None
    event_id: str
    event_name: str | None
    model_event_id: str | None = None
    action_type: str
    application: str | None
    input: str | None
    error_code: str
    error_description: str

    @field_validator("case_id", "event_id", mode="before")
    @classmethod
    def _normalize_required_id(cls, value: object) -> str:
        return _required_id(value)

    @field_validator("activity_id", mode="before")
    @classmethod
    def _normalize_optional_id(cls, value: object) -> str | None:
        return _optional_id(value)


class RobotCapabilities(_Strict):
    """The robot's Modalities, most preferred first, and whether it offers Text targeting (`ocr`).

    `details` only tells the agent how to write targets (e.g. `{"dom": {"selector_syntax": "css"}}`).
    """

    modalities: list[Literal["vision", "dom", "a11y"]] = Field(min_length=1)
    ocr: bool = False
    details: dict[str, object] | None = None

    @field_validator("modalities")
    @classmethod
    def _no_duplicates(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("modalities must not repeat")
        return value


class RecoveryPayload(_Strict):
    contract_version: StrictInt
    task_name: str
    platform: str
    os: str
    variables: dict[str, object] = Field(default_factory=dict)
    ui_log: list[RecoveryUiLogEntry]
    errored_act: RecoveryErroredAct
    model: str
    recovery_task: str | None = None
    capabilities: RobotCapabilities = Field(
        default_factory=lambda: RobotCapabilities(modalities=["vision"])
    )

    @field_validator("model")
    @classmethod
    def _model_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must be a non-empty string")
        return value
