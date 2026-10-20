from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from agent_framework.openai import OpenAIChatCompletionClient
from conftest import TOKEN, auth_file
from openai import AsyncOpenAI
from pydantic import SecretStr

from aca_sandbox_harness import SandboxSettings, runner
from aca_sandbox_harness.models import AuthInput, FileRequest, RunRequest


def install_model_stub(
    monkeypatch,
    *,
    failure: int | None = None,
    delay: float = 0,
    repeat_tools: bool = False,
):
    requests: list[dict] = []

    async def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert "/openai/v1/chat/completions" in str(request.url)
        data = json.loads(request.content)
        requests.append(data)
        if delay:
            await asyncio.sleep(delay)
        if failure:
            return httpx2.Response(failure, json={"error": {"message": "Access denied"}})
        messages = data["messages"]
        latest = messages[-1]
        if repeat_tools and data.get("tool_choice") != "none":
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{len(requests)}_{index}",
                        "type": "function",
                        "function": {"name": "mode_get", "arguments": "{}"},
                    }
                    for index in range(8)
                ],
            }
        elif latest["role"] == "user" and latest["content"] == "write a file":
            script = "from pathlib import Path; Path('answer.txt').write_text('42'); print(42)"
            command = f'"{sys.executable}" -c "{script}"'
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "execute_command",
                            "arguments": json.dumps({"command": command}),
                        },
                    }
                ],
            }
        elif latest["role"] == "user" and latest["content"] == "follow up":
            assert any(message.get("content") == "write a file" for message in messages)
            message = {"role": "assistant", "content": "I remember the file and the result 42."}
        else:
            message = {"role": "assistant", "content": f"The verified answer is 42. {TOKEN}"}
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "test-deployment",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if "tool_calls" in message else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    def new_client(model, token, timeout):
        return OpenAIChatCompletionClient(
            model=model.deployment,
            async_client=AsyncOpenAI(
                base_url=f"{model.endpoint}/openai/v1/",
                api_key=token,
                max_retries=0,
                http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
            ),
            function_invocation_configuration={
                "max_iterations": 16,
                "max_function_calls": 32,
                "allow_concurrent_invocation": False,
                "include_detailed_errors": False,
            },
        )

    monkeypatch.setattr(runner, "new_chat_client", new_client)
    return requests


def new_request(settings: SandboxSettings, prompt: str, timeout: int = 300) -> RunRequest:
    return RunRequest(
        request_id=uuid4().hex,
        sandbox_id="test-sandbox",
        prompt=prompt,
        timeout_seconds=timeout,
        model=settings.model,
    )


def test_real_harness_tools_state_followup_and_no_token_persistence(
    tmp_path: Path,
    settings: SandboxSettings,
    monkeypatch,
) -> None:
    requests = install_model_stub(monkeypatch)
    runner.initialize(tmp_path, settings.model)
    first = new_request(settings, "write a file")
    directory = auth_file(tmp_path, first)
    result = runner.process_request(tmp_path, first.request_id)
    assert result.status == "succeeded", result.error
    assert (tmp_path / "workspace" / "answer.txt").read_text() == "42"
    assert any(artifact.path == "answer.txt" for artifact in result.artifacts)
    assert TOKEN not in result.model_dump_json()
    assert not (directory / "auth.json").exists()
    assert TOKEN not in (tmp_path / "session.json").read_text()
    assert "[REDACTED]" in result.text
    assert len(requests) == 2
    tools = requests[0]["tools"]
    names = {entry["function"]["name"] for entry in tools}
    assert {"execute_command", "file_access_write", "todos_add", "mode_get"} <= names
    assert "mode_set" not in names
    again = runner.process_request(tmp_path, first.request_id)
    assert again == result
    assert len(requests) == 2
    second = new_request(settings, "follow up")
    auth_file(tmp_path, second)
    followup = runner.process_request(tmp_path, second.request_id)
    assert followup.status == "succeeded", followup.error
    assert followup.conversation_id == result.conversation_id
    assert "remember" in followup.text


@pytest.mark.parametrize("status", [401, 403])
def test_model_auth_errors_are_explicit(
    tmp_path: Path,
    settings: SandboxSettings,
    monkeypatch,
    status: int,
) -> None:
    install_model_stub(monkeypatch, failure=status)
    runner.initialize(tmp_path, settings.model)
    request = new_request(settings, "hello")
    directory = auth_file(tmp_path, request)
    result = runner.process_request(tmp_path, request.request_id)
    assert result.error is not None
    assert result.error.code == "authentication"
    assert str(status) in result.error.message
    assert not (directory / "auth.json").exists()
    assert not (tmp_path / "session.json").exists()


def test_prompt_deadline_and_checkpoint_preservation(
    tmp_path: Path,
    settings: SandboxSettings,
    monkeypatch,
) -> None:
    install_model_stub(monkeypatch, delay=2)
    runner.initialize(tmp_path, settings.model)
    old_state = json.dumps(
        {
            "type": "session",
            "session_id": "previous",
            "state": {},
        }
    )
    (tmp_path / "session.json").write_text(old_state)
    request = new_request(settings, "hello", timeout=1)
    auth_file(tmp_path, request)
    started = time.monotonic()
    result = runner.process_request(tmp_path, request.request_id)
    assert time.monotonic() - started < 2
    assert result.error is not None and result.error.code == "timeout"
    assert (tmp_path / "session.json").read_text() == old_state


def test_corrupt_state_is_not_silently_reset(
    tmp_path: Path,
    settings: SandboxSettings,
    monkeypatch,
) -> None:
    requests = install_model_stub(monkeypatch)
    runner.initialize(tmp_path, settings.model)
    (tmp_path / "session.json").write_text("{}")
    request = new_request(settings, "hello")
    auth_file(tmp_path, request)
    result = runner.process_request(tmp_path, request.request_id)
    assert result.error is not None and result.error.code == "invalid_state"
    assert requests == []


def test_total_function_calls_are_capped_at_32(
    tmp_path: Path,
    settings: SandboxSettings,
    monkeypatch,
) -> None:
    requests = install_model_stub(monkeypatch, repeat_tools=True)
    runner.initialize(tmp_path, settings.model)
    request = new_request(settings, "keep invoking tools")
    auth_file(tmp_path, request)
    result = runner.process_request(tmp_path, request.request_id)
    assert result.status == "succeeded", result.error
    assert len(requests) == 5
    assert requests[-1]["tool_choice"] == "none"
    tool_results = [message for message in requests[-1]["messages"] if message["role"] == "tool"]
    assert len(tool_results) == 32


def test_already_started_request_is_not_replayed(
    tmp_path: Path,
    settings: SandboxSettings,
    monkeypatch,
) -> None:
    requests = install_model_stub(monkeypatch)
    runner.initialize(tmp_path, settings.model)
    request = new_request(settings, "hello")
    directory = auth_file(tmp_path, request)
    (directory / "started").write_text("started")
    with pytest.raises(RuntimeError, match="already started"):
        runner.process_request(tmp_path, request.request_id)
    assert requests == []
    assert not (directory / "auth.json").exists()


def test_busy_runner_reports_failure_and_cleans_auth(
    tmp_path: Path,
    settings: SandboxSettings,
) -> None:
    runner.initialize(tmp_path, settings.model)
    request = new_request(settings, "hello")
    directory = auth_file(tmp_path, request)
    with runner.conversation_lock(tmp_path):
        result = runner.process_request(tmp_path, request.request_id)
    assert result.error is not None and result.error.code == "busy"
    assert not (directory / "auth.json").exists()


def test_insufficient_token_lifetime_never_calls_model(
    tmp_path: Path,
    settings: SandboxSettings,
    monkeypatch,
) -> None:
    requests = install_model_stub(monkeypatch)
    request = new_request(settings, "hello")
    auth = AuthInput(token=SecretStr(TOKEN), expires_on=0)
    result = asyncio.run(runner.execute_request(tmp_path, request, auth))
    assert result.error is not None and result.error.code == "authentication"
    assert requests == []


@pytest.mark.skipif(os.name == "nt", reason="Linux main-thread wall-clock alarm")
def test_wall_deadline_also_interrupts_synchronous_work() -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        with runner.wall_deadline(1):
            time.sleep(10)
    assert time.monotonic() - started < 2


@pytest.mark.skipif(os.name == "nt", reason="Linux sandbox symlink behavior")
def test_file_transfer_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("not an artifact")
    (workspace / "link").symlink_to(outside)
    rid = uuid4().hex
    directory = tmp_path / "transfers" / rid
    directory.mkdir(parents=True)
    (directory / "request.json").write_text(
        FileRequest(
            request_id=rid,
            direction="download",
            path="link",
        ).model_dump_json()
    )
    with pytest.raises(ValueError, match="symlink"):
        runner.transfer_file(tmp_path, rid)
