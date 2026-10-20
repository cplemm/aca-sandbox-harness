"""Validated wire protocol shared by the client and sandbox."""

from __future__ import annotations

import os
import re
from pathlib import PurePosixPath
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

ROOT = "/tmp/aca-harness"
WORKSPACE = f"{ROOT}/workspace"
FEED = "https://packagefeedproxy.microsoft.io/pypi/simple"
PROTOCOL_VERSION = 1
TOKEN_MARGIN_SECONDS = 120
MAX_FILE_BYTES = 16 * 1024 * 1024


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


def validate_id(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value):
        raise ValueError("ID must contain only letters, digits, underscores, and hyphens.")
    return value


def validate_request_id(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Request ID must be a lowercase UUID hex string.")
    return value


def workspace_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value != str(path)
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or "\x00" in value
        or str(path) == "."
        or ":" in value
    ):
        raise ValueError("Use a normalized relative workspace path without '..'.")
    return f"{WORKSPACE}/{path}"


class ModelSettings(WireModel):
    endpoint: str
    deployment: str = Field(min_length=1, max_length=200)
    token_scope: str = "https://ai.azure.com/.default"

    @field_validator("endpoint")
    @classmethod
    def endpoint_is_azure_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".openai.azure.com")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
            or parsed.path not in ("", "/")
        ):
            raise ValueError("Use an Azure OpenAI resource HTTPS endpoint, without an API path.")
        return value.rstrip("/")

    @field_validator("deployment", "token_scope")
    @classmethod
    def not_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must not be blank.")
        return value


class SandboxSettings(WireModel):
    subscription_id: str
    resource_group: str
    sandbox_group: str
    region: str = Field(pattern=r"^[a-z0-9]+$")
    model: ModelSettings
    public_image: str = "python-3.12"

    @field_validator("subscription_id")
    @classmethod
    def valid_subscription(cls, value: str) -> str:
        return str(UUID(value))

    @field_validator("resource_group", "sandbox_group")
    @classmethod
    def valid_segment(cls, value: str) -> str:
        return validate_id(value)

    @classmethod
    def from_env(cls) -> Self:
        names = [
            "AZURE_SUBSCRIPTION_ID",
            "AZURE_RESOURCE_GROUP",
            "AZURE_SANDBOX_GROUP",
            "AZURE_REGION",
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_DEPLOYMENT",
        ]
        missing = [name for name in names if not os.environ.get(name)]
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))
        return cls(
            subscription_id=os.environ[names[0]],
            resource_group=os.environ[names[1]],
            sandbox_group=os.environ[names[2]],
            region=os.environ[names[3]],
            model=ModelSettings(
                endpoint=os.environ[names[4]],
                deployment=os.environ[names[5]],
            ),
        )


class RunRequest(WireModel):
    protocol: Literal[1] = PROTOCOL_VERSION
    request_id: str
    sandbox_id: str
    prompt: str = Field(min_length=1, max_length=65_536)
    timeout_seconds: int = Field(default=300, ge=1, le=300)
    model: ModelSettings

    _request_id = field_validator("request_id")(validate_request_id)
    _sandbox_id = field_validator("sandbox_id")(validate_id)

    @field_validator("prompt")
    @classmethod
    def nonempty_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Prompt must not be blank.")
        return value


class AuthInput(WireModel):
    token: SecretStr
    expires_on: int

    def require_lifetime(self, timeout_seconds: int, now: float) -> None:
        if not self.token.get_secret_value() or (
            self.expires_on - now < timeout_seconds + TOKEN_MARGIN_SECONDS
        ):
            raise ValueError("Inference token expires too soon for this run. Reauthenticate/retry.")


class Artifact(WireModel):
    path: str
    size_bytes: int = Field(ge=0)


class RunError(WireModel):
    code: Literal[
        "busy",
        "timeout",
        "authentication",
        "invalid_state",
        "invalid_request",
        "approval_required",
        "execution_failed",
        "tool_limit",
    ]
    message: str


class RunResult(WireModel):
    protocol: Literal[1] = PROTOCOL_VERSION
    request_id: str
    sandbox_id: str
    conversation_id: str | None = None
    status: Literal["succeeded", "failed"]
    text: str = ""
    artifacts: list[Artifact] = Field(default_factory=list)
    artifacts_truncated: bool = False
    error: RunError | None = None

    @model_validator(mode="after")
    def consistent_status(self) -> Self:
        if (self.status == "failed") != (self.error is not None):
            raise ValueError(
                "Failed results require an error; successful results must not have one."
            )
        return self


class Installation(WireModel):
    protocol: Literal[1] = PROTOCOL_VERSION
    package_version: Literal["0.1.0"] = "0.1.0"
    model: ModelSettings


class FileRequest(WireModel):
    request_id: str
    direction: Literal["upload", "download"]
    path: str

    _id = field_validator("request_id")(validate_request_id)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        workspace_path(value)
        return value


class HarnessError(RuntimeError):
    """An actionable client-side failure."""


class BootstrapError(HarnessError):
    def __init__(self, sandbox_id: str) -> None:
        self.sandbox_id = sandbox_id
        super().__init__(
            f"Bootstrap failed for sandbox {sandbox_id}; inspect {ROOT}/bootstrap.log "
            "using the Sandbox SDK, or explicitly delete this ID."
        )


class ExecutionUncertain(HarnessError):
    def __init__(self, sandbox_id: str, request_id: str) -> None:
        self.sandbox_id = sandbox_id
        self.request_id = request_id
        super().__init__(
            f"Execution outcome unknown: sandbox={sandbox_id}, request={request_id}. "
            "Use get_result(request_id); do not blindly resubmit the prompt."
        )
