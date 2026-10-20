from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import TOKEN
from pydantic import SecretStr, ValidationError

from aca_sandbox_harness import ModelSettings, SandboxSettings
from aca_sandbox_harness.models import AuthInput, FileRequest, RunRequest, RunResult
from aca_sandbox_harness.runner import transfer_file


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://test.openai.azure.com",
        "https://example.com",
        "https://test.openai.azure.com/openai/v1/",
        "https://test.openai.azure.com/?token=secret",
        "https://user:password@test.openai.azure.com",
    ],
)
def test_model_endpoint_validation(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        ModelSettings(endpoint=endpoint, deployment="test")


def test_missing_configuration_is_explicit(monkeypatch) -> None:
    monkeypatch.delenv("AZURE_SANDBOX_GROUP", raising=False)
    with pytest.raises(ValueError, match="AZURE_SANDBOX_GROUP"):
        SandboxSettings.from_env()


@pytest.mark.parametrize("prompt, timeout", [("", 300), ("  ", 300), ("ok", 0), ("ok", 301)])
def test_invalid_prompt_or_limit(settings: SandboxSettings, prompt: str, timeout: int) -> None:
    with pytest.raises(ValidationError):
        RunRequest(
            request_id="0" * 32,
            sandbox_id="test-sandbox",
            prompt=prompt,
            timeout_seconds=timeout,
            model=settings.model,
        )


def test_five_minute_default_and_exact_token_margin(settings: SandboxSettings) -> None:
    request = RunRequest(
        request_id="0" * 32,
        sandbox_id="test-sandbox",
        prompt="hello",
        model=settings.model,
    )
    assert request.timeout_seconds == 300
    now = int(time.time())
    AuthInput(token=SecretStr(TOKEN), expires_on=now + 420).require_lifetime(300, now)
    with pytest.raises(ValueError, match="expires too soon"):
        AuthInput(token=SecretStr(TOKEN), expires_on=now + 419).require_lifetime(300, now)


def test_auth_repr_and_normal_serialization_hide_token() -> None:
    auth = AuthInput(token=SecretStr(TOKEN), expires_on=3600)
    assert TOKEN not in repr(auth)
    assert TOKEN not in auth.model_dump_json()


def test_success_shaped_result_cannot_hide_error() -> None:
    with pytest.raises(ValidationError):
        RunResult(request_id="a" * 32, sandbox_id="sandbox", status="failed")


def test_upload_size_limit_is_enforced_on_runner(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("aca_sandbox_harness.runner.MAX_FILE_BYTES", 4)
    directory = tmp_path / "transfers" / ("a" * 32)
    directory.mkdir(parents=True)
    (directory / "request.json").write_text(
        FileRequest(
            request_id="a" * 32,
            direction="upload",
            path="output.bin",
        ).model_dump_json()
    )
    (directory / "payload").write_bytes(b"12345")
    with pytest.raises(ValueError, match="16 MiB"):
        transfer_file(tmp_path, "a" * 32)
    assert not (tmp_path / "workspace" / "output.bin").exists()
