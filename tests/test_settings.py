from datetime import timedelta

import pytest
from pydantic import ValidationError

from reparo_playwright_bridge import ReparoSettings


def test_defaults() -> None:
    settings = ReparoSettings(base_url="https://reparo.example.com", robot_key="k")

    assert settings.model_format == "bpmn"
    assert settings.future_contract is False
    assert settings.silence_timeout == timedelta(minutes=2)
    assert settings.recovery_deadline == timedelta(minutes=10)
    assert settings.connect_attempts == 3


def test_is_frozen() -> None:
    settings = ReparoSettings(base_url="https://reparo.example.com", robot_key="k")

    with pytest.raises(ValidationError):
        settings.connect_attempts = 5


def test_robot_key_comes_from_the_argument_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPARO_ROBOT_KEY", "from-env")

    settings = ReparoSettings(
        base_url="https://reparo.example.com", robot_key="from-arg"
    )

    assert settings.robot_key.get_secret_value() == "from-arg"


def test_robot_key_falls_back_to_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPARO_ROBOT_KEY", "from-env")

    settings = ReparoSettings(base_url="https://reparo.example.com")

    assert settings.robot_key.get_secret_value() == "from-env"


@pytest.mark.parametrize("env_value", [None, ""])
def test_a_missing_robot_key_fails_construction(
    monkeypatch: pytest.MonkeyPatch, env_value: str | None
) -> None:
    if env_value is None:
        monkeypatch.delenv("REPARO_ROBOT_KEY", raising=False)
    else:
        monkeypatch.setenv("REPARO_ROBOT_KEY", env_value)

    with pytest.raises(ValidationError, match="REPARO_ROBOT_KEY"):
        _ = ReparoSettings(base_url="https://reparo.example.com")


def test_the_robot_key_is_never_shown() -> None:
    settings = ReparoSettings(
        base_url="https://reparo.example.com", robot_key="s3cret-key"
    )

    assert "s3cret-key" not in repr(settings)
    assert "s3cret-key" not in str(settings)
    assert "s3cret-key" not in settings.model_dump_json()


@pytest.mark.parametrize(
    ("base_url", "ws_url", "report_url"),
    [
        (
            "https://reparo.example.com",
            "wss://reparo.example.com/recovery/robot_exception/ws",
            "https://reparo.example.com/recovery/report_result/abc-123",
        ),
        (
            "http://localhost:8000/",
            "ws://localhost:8000/recovery/robot_exception/ws",
            "http://localhost:8000/recovery/report_result/abc-123",
        ),
        (
            "https://example.com/reparo/api",
            "wss://example.com/reparo/api/recovery/robot_exception/ws",
            "https://example.com/reparo/api/recovery/report_result/abc-123",
        ),
    ],
)
def test_endpoints_are_derived_from_the_base_url(
    base_url: str, ws_url: str, report_url: str
) -> None:
    settings = ReparoSettings(base_url=base_url, robot_key="k")

    assert settings.websocket_url == ws_url
    assert settings.report_result_url("abc-123") == report_url


def test_base_url_must_be_http_or_https() -> None:
    with pytest.raises(ValidationError, match="base_url"):
        _ = ReparoSettings(base_url="ftp://reparo.example.com", robot_key="k")


def test_overrides() -> None:
    settings = ReparoSettings(
        base_url="https://reparo.example.com",
        robot_key="k",
        model_format="compact",
        future_contract=True,
        silence_timeout=timedelta(seconds=1),
        recovery_deadline=timedelta(seconds=2),
        connect_attempts=1,
    )

    assert (
        settings.model_format,
        settings.future_contract,
        settings.connect_attempts,
    ) == ("compact", True, 1)
    assert (settings.silence_timeout, settings.recovery_deadline) == (
        timedelta(seconds=1),
        timedelta(seconds=2),
    )
