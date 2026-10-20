from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ResourceNotFoundError

from aca_sandbox_harness import ModelSettings, SandboxHarness, SandboxSettings
from aca_sandbox_harness.models import ROOT, RunRequest, RunResult
from aca_sandbox_harness.runner import initialize, transfer_file

TOKEN = "test-only-access-token-not-a-real-credential"


@pytest.fixture
def settings() -> SandboxSettings:
    return SandboxSettings(
        subscription_id="00000000-0000-0000-0000-000000000001",
        resource_group="test-rg",
        sandbox_group="test-group",
        region="eastus2",
        model=ModelSettings(endpoint="https://test.openai.azure.com", deployment="test-deployment"),
    )


class FakeCredential:
    def __init__(self) -> None:
        self.scopes: list[str] = []
        self.expires_on = int(time.time()) + 3600

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.scopes.extend(scopes)
        return AccessToken(TOKEN, self.expires_on)


@dataclass(frozen=True)
class Output:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


class Poller:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    def result(self, timeout: float | None = None) -> Any:
        return self.value


class FakeSandbox:
    sandbox_id = "test-sandbox"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.commands: list[str] = []
        self.writes: list[tuple[str, bytes, str | None]] = []
        self.on_run: Callable[[str], None] | None = None
        self.bootstrap_exit = 0
        self.deleted = False
        self.stopped = False
        self.stop_failure: Exception | None = None
        self.ready_calls = 0
        self.exec_failure: Exception | None = None
        self.cleanup_failure: Exception | None = None
        self.omit_result = False

    def path(self, remote: str) -> Path:
        return self.root / Path(remote.removeprefix(ROOT + "/"))

    def write_file(self, path: str, content: str | bytes, *, mode: str | None = None) -> None:
        data = content.encode() if isinstance(content, str) else content
        self.writes.append((path, data, mode))
        local = self.path(path)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)

    def read_file(self, path: str) -> bytes:
        try:
            return self.path(path).read_bytes()
        except FileNotFoundError:
            raise ResourceNotFoundError("File missing") from None

    def delete_file(self, path: str) -> None:
        if self.cleanup_failure is not None:
            raise self.cleanup_failure
        try:
            self.path(path).unlink()
        except FileNotFoundError:
            raise ResourceNotFoundError("File missing") from None

    def exec(self, command: str) -> Output:
        self.commands.append(command)
        if f"{ROOT}/bootstrap.py" in command:
            if not self.bootstrap_exit:
                initialize(
                    self.root,
                    ModelSettings.model_validate_json((self.root / "model.json").read_bytes()),
                )
            return Output(exit_code=self.bootstrap_exit)
        request_id = command.split()[-1]
        if " transfer " in command:
            transfer_file(self.root, request_id)
            return Output()
        if self.on_run:
            self.on_run(request_id)
        elif not self.omit_result:
            request = RunRequest.model_validate_json(
                (self.root / "requests" / request_id / "request.json").read_bytes()
            )
            result = RunResult(
                request_id=request_id,
                sandbox_id=self.sandbox_id,
                conversation_id="test-conversation",
                status="succeeded",
                text=request.prompt,
            )
            (self.root / "requests" / request_id / "result.json").write_text(
                result.model_dump_json(),
                encoding="utf-8",
            )
        if self.exec_failure is not None:
            raise self.exec_failure
        return Output()

    def ensure_running(self, *, timeout: int = 300) -> None:
        self.ready_calls += 1
        self.stopped = False

    def begin_stop(self, *, polling_timeout: int = 180) -> Poller:
        if self.stop_failure is not None:
            raise self.stop_failure
        self.stopped = True
        return Poller()

    def begin_delete(self, *, polling_timeout: int = 300) -> Poller:
        if self.deleted:
            raise ResourceNotFoundError("Sandbox already deleted")
        self.deleted = True
        return Poller()


class FakeGroup:
    def __init__(self, sandbox: FakeSandbox) -> None:
        self.sandbox = sandbox
        self.created: list[dict[str, Any]] = []

    def begin_create_sandbox(self, **kwargs: Any) -> Poller:
        self.created.append(kwargs)
        return Poller(self.sandbox)

    def get_sandbox_client(self, sandbox_id: str) -> FakeSandbox:
        assert sandbox_id == self.sandbox.sandbox_id
        return self.sandbox

    def close(self) -> None:
        pass


@pytest.fixture
def fake_stack(tmp_path: Path, settings: SandboxSettings):
    sandbox = FakeSandbox(tmp_path / "sandbox")
    credential = FakeCredential()
    group = FakeGroup(sandbox)
    client = SandboxHarness(settings, credential=credential, group_client=group)
    return client, sandbox, credential, group


def auth_file(root: Path, request: RunRequest, token: str = TOKEN) -> Path:
    directory = root / "requests" / request.request_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "request.json").write_text(request.model_dump_json(), encoding="utf-8")
    (directory / "auth.json").write_text(
        json.dumps({"token": token, "expires_on": int(time.time()) + 3600}),
        encoding="utf-8",
    )
    return directory
