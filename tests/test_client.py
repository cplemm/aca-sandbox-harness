from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError
from conftest import TOKEN

from aca_sandbox_harness import BootstrapError, ExecutionUncertain, HarnessError
from aca_sandbox_harness.client import runtime_bundle
from aca_sandbox_harness.models import ROOT, Installation, workspace_path


def test_create_connect_run_and_explicit_delete(fake_stack) -> None:
    client, remote, credential, group = fake_stack
    handle = client.create()
    assert handle.sandbox_id == remote.sandbox_id
    assert group.created[0]["disk"] == "python-3.12"
    assert group.created[0]["auto_suspend_mode"] == "Disk"
    assert TOKEN not in repr(group.created)
    first = handle.run("First instruction")
    second = client.connect(handle.sandbox_id).run("Follow-up")
    assert first.text == "First instruction"
    assert second.text == "Follow-up"
    assert credential.scopes == ["https://ai.azure.com/.default"] * 2
    assert sum("bootstrap.py" in command for command in remote.commands) == 1
    assert not list(remote.root.rglob("auth.json"))
    assert not remote.deleted
    client.close()
    assert not remote.deleted
    handle.delete()
    handle.delete()
    assert remote.deleted


def test_suspend_then_connect_preserves_sandbox_state(fake_stack, tmp_path: Path) -> None:
    client, remote, _, _ = fake_stack
    handle = client.create()
    first = handle.run("Before suspension")
    source = tmp_path / "workspace.txt"
    source.write_text("preserved", encoding="utf-8")
    handle.upload_file(source, "workspace.txt")
    installation = remote.path(f"{ROOT}/installation.json").read_bytes()

    handle.suspend()

    assert remote.stopped
    assert not remote.deleted
    assert not list(remote.root.rglob("auth.json"))
    assert remote.path("workspace/workspace.txt").read_text(encoding="utf-8") == "preserved"
    assert remote.path(f"{ROOT}/installation.json").read_bytes() == installation

    resumed = client.connect(handle.sandbox_id)
    second = resumed.run("After resuming")
    assert second.conversation_id == first.conversation_id
    assert not remote.stopped
    assert not remote.deleted


def test_suspend_failure_does_not_delete_sandbox(fake_stack) -> None:
    client, remote, _, _ = fake_stack
    handle = client.create()
    remote.stop_failure = ServiceRequestError("stop failed")

    with pytest.raises(HarnessError, match="ACA operation failed"):
        handle.suspend()

    assert not remote.deleted


@pytest.mark.parametrize(
    "prompt",
    [
        'single \' quotes and "double" quotes\nnew line',
        "$(touch /tmp/never); `whoami` && echo boom",
        "Unicode: \u00fc\u00df \u4f60\u597d",
    ],
)
def test_prompt_and_auth_are_data_not_command_arguments(fake_stack, prompt: str) -> None:
    client, remote, _, _ = fake_stack
    handle = client.create()
    result = handle.run(prompt)
    assert result.text == prompt
    assert all(prompt not in command and TOKEN not in command for command in remote.commands)
    auth_inputs = [(data, mode) for path, data, mode in remote.writes if path.endswith("auth.json")]
    assert len(auth_inputs) == 1
    assert json.loads(auth_inputs[0][0])["token"] == TOKEN
    assert auth_inputs[0][1] == "0600"
    assert TOKEN not in result.model_dump_json()


def test_expiring_token_rejected_before_transfer(fake_stack) -> None:
    client, remote, credential, _ = fake_stack
    handle = client.create()
    credential.expires_on = 0
    with pytest.raises(ValueError, match="expires too soon"):
        handle.run("hello")
    assert not (remote.root / "requests").exists()


def test_bootstrap_failure_retains_id_for_cleanup(fake_stack) -> None:
    client, remote, _, _ = fake_stack
    remote.bootstrap_exit = 1
    with pytest.raises(BootstrapError) as error:
        client.create()
    assert error.value.sandbox_id == remote.sandbox_id
    assert not remote.deleted


def test_network_error_after_completed_execution_does_not_replay(fake_stack) -> None:
    client, remote, _, _ = fake_stack
    handle = client.create()
    remote.exec_failure = ServiceRequestError("response lost")
    result = handle.run("Execute exactly once.")
    assert result.status == "succeeded"
    count = len(remote.commands)
    assert handle.get_result(result.request_id) == result
    assert len(remote.commands) == count


def test_missing_result_is_uncertain_and_has_recovery_id(fake_stack) -> None:
    client, remote, _, _ = fake_stack
    handle = client.create()
    remote.omit_result = True
    with pytest.raises(ExecutionUncertain) as error:
        handle.run("hello")
    assert error.value.sandbox_id == remote.sandbox_id
    assert handle.get_result(error.value.request_id) is None
    assert not list(remote.root.rglob("auth.json"))
    assert len(remote.commands) == 2


def test_unknown_execution_keeps_recovery_ids_when_result_read_also_fails(
    fake_stack,
    monkeypatch,
) -> None:
    client, remote, _, _ = fake_stack
    handle = client.create()
    remote.exec_failure = ServiceRequestError("response lost")

    def fail_read(path: str) -> bytes:
        raise ServiceRequestError("cannot inspect result")

    monkeypatch.setattr(remote, "read_file", fail_read)
    with pytest.raises(ExecutionUncertain) as error:
        handle.run("Do not execute twice")
    assert len(error.value.request_id) == 32
    assert error.value.sandbox_id == remote.sandbox_id
    assert not list(remote.root.rglob("auth.json"))


def test_malformed_result_does_not_look_successful(fake_stack) -> None:
    client, remote, _, _ = fake_stack
    handle = client.create()
    remote.on_run = lambda rid: (remote.root / "requests" / rid / "result.json").write_text("{}")
    with pytest.raises(HarnessError, match="Malformed"):
        handle.run("hello")
    assert not list(remote.root.rglob("auth.json"))


def test_rejects_mismatched_result_ids(fake_stack) -> None:
    client, remote, _, _ = fake_stack
    handle = client.create()
    result = handle.run("hello")
    path = remote.root / "requests" / result.request_id / "result.json"
    data = json.loads(path.read_bytes())
    data["sandbox_id"] = "other-sandbox"
    path.write_text(json.dumps(data))
    with pytest.raises(HarnessError, match="IDs"):
        handle.get_result(result.request_id)


def test_result_read_access_failure_is_actionable(fake_stack, monkeypatch) -> None:
    client, remote, _, _ = fake_stack
    handle = client.create()

    def fail(path: str) -> bytes:
        raise ClientAuthenticationError("denied")

    monkeypatch.setattr(remote, "read_file", fail)
    with pytest.raises(HarnessError, match="Data Owner"):
        handle.get_result(uuid4().hex)


def test_reconnect_refuses_changed_model(fake_stack) -> None:
    client, remote, _, _ = fake_stack
    client.create()
    path = remote.root / "installation.json"
    installation = Installation.model_validate_json(path.read_bytes())
    other = installation.model_copy(
        update={
            "model": installation.model.model_copy(update={"deployment": "another-deployment"}),
        }
    )
    path.write_text(other.model_dump_json())
    with pytest.raises(HarnessError, match="different"):
        client.connect(remote.sandbox_id)


def test_artifact_round_trip_and_no_overwrite(fake_stack, tmp_path: Path) -> None:
    client, remote, _, _ = fake_stack
    handle = client.create()
    source = tmp_path / "source.bin"
    source.write_bytes(b"\x00\xffartifact")
    handle.upload_file(source, "inputs/source.bin")
    target = tmp_path / "download.bin"
    handle.download_file("inputs/source.bin", target)
    assert target.read_bytes() == source.read_bytes()
    with pytest.raises(FileExistsError):
        handle.download_file("inputs/source.bin", target)
    assert not list((remote.root / "transfers").rglob("payload"))


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../b", "a\\b", ".", "a//b"])
def test_unsafe_workspace_paths_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        workspace_path(path)


def test_bundle_has_runner_and_locked_runtime_dependencies() -> None:
    with zipfile.ZipFile(io.BytesIO(runtime_bundle())) as bundle:
        names = bundle.namelist()
        assert "aca_sandbox_harness/runner.py" in names
        assert "aca_sandbox_harness/bootstrap.py" in names
        requirements = bundle.read("aca_sandbox_harness/runtime-requirements.txt").decode()
        assert "agent-framework-core==1.21.0" in requirements
        assert "--hash=sha256:" in requirements
        assert all(not name.startswith("/") and ".." not in name for name in names)


def test_auth_cleanup_failure_is_explicit(fake_stack) -> None:
    client, remote, _, _ = fake_stack
    handle = client.create()
    remote.cleanup_failure = ServiceRequestError("disconnected")
    with pytest.raises(HarnessError, match="transient auth"):
        handle.run("hello")


def test_get_result_validates_request_id_before_network(fake_stack) -> None:
    client, remote, _, _ = fake_stack
    handle = client.create()
    calls = remote.ready_calls
    with pytest.raises(ValueError):
        handle.get_result("../wrong")
    assert remote.ready_calls == calls
    assert not remote.path(f"{ROOT}/requests").exists()
