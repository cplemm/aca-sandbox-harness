"""Synchronous client using only ACA Sandbox SDK execution and file transport."""

from __future__ import annotations

import io
import json
import time
import zipfile
from importlib.resources import files
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region
from azure.core.credentials import TokenCredential
from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
)
from azure.identity import DefaultAzureCredential
from pydantic import SecretStr, ValidationError

from .models import (
    MAX_FILE_BYTES,
    ROOT,
    AuthInput,
    BootstrapError,
    ExecutionUncertain,
    FileRequest,
    HarnessError,
    Installation,
    RunRequest,
    RunResult,
    SandboxSettings,
    validate_id,
    validate_request_id,
    workspace_path,
)

RUNNER = f"PYTHONPATH={ROOT}/source {ROOT}/venv/bin/python -m aca_sandbox_harness.runner"


class ExecOutput(Protocol):
    @property
    def exit_code(self) -> int: ...
    @property
    def stdout(self) -> str: ...
    @property
    def stderr(self) -> str: ...


class Waiter(Protocol):
    def result(self, timeout: float | None = None) -> Any: ...


class SandboxIO(Protocol):
    @property
    def sandbox_id(self) -> str: ...
    def exec(self, command: str) -> ExecOutput: ...
    def ensure_running(self, *, timeout: int = 300) -> None: ...
    def write_file(self, path: str, content: str | bytes, *, mode: str | None = None) -> None: ...
    def read_file(self, path: str) -> bytes: ...
    def delete_file(self, path: str) -> None: ...
    def begin_stop(self, *, polling_timeout: int = 180) -> Waiter: ...
    def begin_delete(self, *, polling_timeout: int = 300) -> Waiter: ...


class GroupIO(Protocol):
    def begin_create_sandbox(self, **kwargs: Any) -> Waiter: ...
    def get_sandbox_client(self, sandbox_id: str) -> SandboxIO: ...
    def close(self) -> None: ...


def runtime_bundle() -> bytes:
    output = io.BytesIO()
    package = files("aca_sandbox_harness")
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for entry in package.iterdir():
            if entry.name.endswith(".py") or entry.name == "runtime-requirements.txt":
                bundle.writestr(f"aca_sandbox_harness/{entry.name}", entry.read_bytes())
    return output.getvalue()


def service_error(exc: AzureError) -> HarnessError:
    if isinstance(exc, ClientAuthenticationError) or (
        isinstance(exc, HttpResponseError) and exc.status_code in (401, 403)
    ):
        return HarnessError(
            "ACA authentication/access failed. Check DefaultAzureCredential, the tenant, "
            "and Container Apps SandboxGroup Data Owner on the configured sandbox group."
        )
    return HarnessError(f"ACA operation failed ({type(exc).__name__}). No action was replayed.")


class SandboxHarness:
    """Factory for sandbox handles. Closing it does not delete any sandbox."""

    def __init__(
        self,
        settings: SandboxSettings,
        *,
        credential: TokenCredential | None = None,
        group_client: GroupIO | None = None,
    ) -> None:
        self.settings = settings
        self._owns_credential = credential is None
        self.credential = credential if credential is not None else DefaultAzureCredential()
        self.group: GroupIO = (
            group_client
            if group_client is not None
            else SandboxGroupClient(
                endpoint_for_region(settings.region),
                self.credential,
                subscription_id=settings.subscription_id,
                resource_group=settings.resource_group,
                sandbox_group=settings.sandbox_group,
                connection_timeout=10,
                read_timeout=660,
                retry_total=0,
                logging_enable=False,
            )
        )

    def create(self) -> HarnessSandbox:
        try:
            sandbox = self.group.begin_create_sandbox(
                disk=self.settings.public_image,
                auto_suspend_seconds=600,
                auto_suspend_mode="Disk",
                polling_timeout=300,
                entrypoint=["/bin/sh"],
                cmd=["-c", "sleep infinity"],
                labels={"application": "aca-sandbox-harness"},
            ).result(timeout=310)
        except AzureError as exc:
            raise service_error(exc) from None
        handle = HarnessSandbox(self, sandbox)
        try:
            sandbox.write_file(f"{ROOT}/bundle.zip", runtime_bundle())
            sandbox.write_file(
                f"{ROOT}/bootstrap.py",
                files("aca_sandbox_harness").joinpath("bootstrap.py").read_bytes(),
            )
            sandbox.write_file(f"{ROOT}/model.json", self.settings.model.model_dump_json())
            result = sandbox.exec(f"python3 {ROOT}/bootstrap.py > {ROOT}/bootstrap.log 2>&1")
            if result.exit_code != 0:
                raise BootstrapError(sandbox.sandbox_id)
            handle._check_installation()
        except (AzureError, TimeoutError, HarnessError) as exc:
            if isinstance(exc, BootstrapError):
                raise
            raise BootstrapError(sandbox.sandbox_id) from None
        return handle

    def connect(self, sandbox_id: str) -> HarnessSandbox:
        validate_id(sandbox_id)
        handle = HarnessSandbox(self, self.group.get_sandbox_client(sandbox_id))
        handle._ready()
        handle._check_installation()
        return handle

    def close(self) -> None:
        self.group.close()
        if self._owns_credential:
            close = getattr(self.credential, "close", None)
            if close:
                close()

    def __enter__(self) -> SandboxHarness:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class HarnessSandbox:
    def __init__(self, owner: SandboxHarness, sandbox: SandboxIO) -> None:
        self._owner = owner
        self._sandbox = sandbox
        self.sandbox_id = validate_id(sandbox.sandbox_id)

    def _ready(self) -> None:
        try:
            self._sandbox.ensure_running(timeout=300)
        except AzureError as exc:
            raise service_error(exc) from None

    def _check_installation(self) -> None:
        try:
            data = self._sandbox.read_file(f"{ROOT}/installation.json")
            installation = Installation.model_validate_json(data)
        except ResourceNotFoundError:
            raise HarnessError(
                "Sandbox has no harness installation; bootstrap did not finish."
            ) from None
        except ValidationError:
            raise HarnessError(
                "Sandbox installation metadata is invalid or incompatible."
            ) from None
        if installation.model != self._owner.settings.model:
            raise HarnessError("Sandbox uses a different Azure OpenAI endpoint or deployment.")

    def get_result(self, request_id: str) -> RunResult | None:
        """Retrieve without rerunning; None means no final result is recorded yet."""
        validate_request_id(request_id)
        self._ready()
        try:
            data = self._sandbox.read_file(f"{ROOT}/requests/{request_id}/result.json")
        except ResourceNotFoundError:
            return None
        except AzureError as exc:
            raise service_error(exc) from None
        try:
            result = RunResult.model_validate_json(data)
        except ValidationError:
            raise HarnessError(
                "Malformed harness result; execution must not be blindly retried."
            ) from None
        if result.request_id != request_id or result.sandbox_id != self.sandbox_id:
            raise HarnessError("Harness result IDs do not match the requested execution.")
        return result

    def run(self, prompt: str, *, timeout_seconds: int = 300) -> RunResult:
        request = RunRequest(
            request_id=uuid4().hex,
            sandbox_id=self.sandbox_id,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            model=self._owner.settings.model,
        )
        self._ready()
        try:
            access_token = self._owner.credential.get_token(self._owner.settings.model.token_scope)
        except ClientAuthenticationError:
            raise HarnessError(
                "DefaultAzureCredential could not obtain an inference token."
            ) from None
        auth = AuthInput(token=SecretStr(access_token.token), expires_on=access_token.expires_on)
        auth.require_lifetime(timeout_seconds, time.time())
        directory = f"{ROOT}/requests/{request.request_id}"
        auth_path = f"{directory}/auth.json"
        # SecretStr deliberately redacts normal serialization; reveal only for this transport.
        auth_json = json.dumps(
            {
                "token": auth.token.get_secret_value(),
                "expires_on": auth.expires_on,
            }
        )
        try:
            self._sandbox.write_file(
                f"{directory}/request.json", request.model_dump_json(), mode="0600"
            )
            self._sandbox.write_file(auth_path, auth_json, mode="0600")
            try:
                self._sandbox.exec(f"{RUNNER} run {request.request_id}")
            except (AzureError, TimeoutError):
                try:
                    result = self.get_result(request.request_id)
                except (HarnessError, TimeoutError):
                    raise ExecutionUncertain(self.sandbox_id, request.request_id) from None
                if result is not None:
                    return result
                raise ExecutionUncertain(self.sandbox_id, request.request_id) from None
            result = self.get_result(request.request_id)
            if result is None:
                raise ExecutionUncertain(self.sandbox_id, request.request_id)
            return result
        except AzureError as exc:
            raise service_error(exc) from None
        finally:
            try:
                self._sandbox.delete_file(auth_path)
            except ResourceNotFoundError:
                pass
            except AzureError:
                raise HarnessError(
                    f"Could not remove transient auth input from sandbox {self.sandbox_id}. "
                    f"Request={request.request_id}. It contains a short-lived token; "
                    "inspect or explicitly delete the sandbox."
                ) from None

    def _transfer(self, path: str, payload: bytes | None) -> bytes | None:
        workspace_path(path)
        self._ready()
        request = FileRequest(
            request_id=uuid4().hex,
            direction="download" if payload is None else "upload",
            path=path,
        )
        directory = f"{ROOT}/transfers/{request.request_id}"
        try:
            self._sandbox.write_file(f"{directory}/request.json", request.model_dump_json())
            if payload is not None:
                self._sandbox.write_file(f"{directory}/payload", payload)
            result = self._sandbox.exec(f"{RUNNER} transfer {request.request_id}")
            if result.exit_code != 0:
                raise HarnessError(
                    "File transfer failed. Check the path, symlinks, file size, and active prompt."
                )
            if self._sandbox.read_file(f"{directory}/complete") != b"complete":
                raise HarnessError("File-transfer completion marker is invalid.")
            return self._sandbox.read_file(f"{directory}/payload") if payload is None else None
        except AzureError as exc:
            raise service_error(exc) from None
        finally:
            for name in ("payload", "request.json", "complete"):
                try:
                    self._sandbox.delete_file(f"{directory}/{name}")
                except ResourceNotFoundError:
                    pass
                except AzureError:
                    raise HarnessError(
                        f"Could not clean file-transfer staging in sandbox {self.sandbox_id}."
                    ) from None

    def upload_file(self, local_path: str | Path, remote_path: str) -> None:
        with Path(local_path).open("rb") as source:
            content = source.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("Upload exceeds the 16 MiB limit.")
        self._transfer(remote_path, content)

    def download_file(self, remote_path: str, local_path: str | Path) -> None:
        content = self._transfer(remote_path, None)
        if content is None or len(content) > MAX_FILE_BYTES:
            raise HarnessError("Invalid or oversized download.")
        path = Path(local_path)
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite local file: {path}")
        with path.open("xb") as target:
            target.write(content)

    def delete(self) -> None:
        try:
            self._sandbox.begin_delete(polling_timeout=300).result(timeout=310)
        except ResourceNotFoundError:
            return
        except AzureError as exc:
            raise service_error(exc) from None

    def suspend(self) -> None:
        """Stop this sandbox while retaining its disk state for a later reconnect."""
        try:
            self._sandbox.begin_stop(polling_timeout=300).result(timeout=310)
        except AzureError as exc:
            raise service_error(exc) from None
