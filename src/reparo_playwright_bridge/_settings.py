"""ReparoSettings: how to reach and talk to one Reparo setup."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import ClassVar
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from reparo_playwright_bridge._model_export import ModelFormat

ROBOT_KEY_ENV = "REPARO_ROBOT_KEY"
DEFAULT_SILENCE_TIMEOUT = timedelta(minutes=2)
DEFAULT_RECOVERY_DEADLINE = timedelta(minutes=10)


class ReparoSettings(BaseModel):
    """All Reparo settings for a run, configured once and reused.

    `robot_key` comes from the argument, else from the `REPARO_ROBOT_KEY` environment variable;
    with neither, construction fails. It is held as a secret and never shown.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, protected_namespaces=()
    )

    base_url: str
    robot_key: SecretStr
    model_format: ModelFormat = "bpmn"
    silence_timeout: timedelta = Field(default=DEFAULT_SILENCE_TIMEOUT, gt=timedelta(0))
    recovery_deadline: timedelta = Field(
        default=DEFAULT_RECOVERY_DEADLINE, gt=timedelta(0)
    )
    connect_attempts: int = Field(default=3, ge=1)

    def __init__(
        self,
        *,
        base_url: str,
        robot_key: str | SecretStr | None = None,
        model_format: ModelFormat = "bpmn",
        silence_timeout: timedelta = DEFAULT_SILENCE_TIMEOUT,
        recovery_deadline: timedelta = DEFAULT_RECOVERY_DEADLINE,
        connect_attempts: int = 3,
    ) -> None:
        if robot_key is None or robot_key == "":
            robot_key = os.environ.get(ROBOT_KEY_ENV) or None
        super().__init__(
            base_url=base_url,
            robot_key=robot_key,  # None fails validation
            model_format=model_format,
            silence_timeout=silence_timeout,
            recovery_deadline=recovery_deadline,
            connect_attempts=connect_attempts,
        )

    @field_validator("robot_key", mode="before")
    @classmethod
    def _require_robot_key(cls, value: object) -> object:
        if value is None:
            raise ValueError(f"no robot key: pass robot_key or set {ROBOT_KEY_ENV}")
        return value

    @field_validator("base_url")
    @classmethod
    def _http_base_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("must be an http:// or https:// URL")
        return value.rstrip("/")

    @property
    def websocket_url(self) -> str:
        """Reparo's recovery WebSocket endpoint (http becomes ws, https becomes wss)."""
        ws_base = "ws" + self.base_url.removeprefix("http")
        return f"{ws_base}/recovery/robot_exception/ws"

    def report_result_url(self, recovery_id: str) -> str:
        """Reparo's `report_result` endpoint for one Recovery id."""
        return f"{self.base_url}/recovery/report_result/{quote(recovery_id, safe='')}"
