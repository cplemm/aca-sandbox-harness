"""Fixed sandbox entry point; requests and credentials arrive as files, not shell text."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

from .models import (
    MAX_FILE_BYTES,
    ROOT,
    Artifact,
    AuthInput,
    FileRequest,
    Installation,
    ModelSettings,
    RunError,
    RunRequest,
    RunResult,
    validate_request_id,
)
from .tools import CommandExecutor

if TYPE_CHECKING:
    from agent_framework import Agent
    from agent_framework.openai import OpenAIChatCompletionClient


class BusyError(RuntimeError):
    pass


class ResultContext(TypedDict):
    request_id: str
    sandbox_id: str
    conversation_id: NotRequired[str]


@contextlib.contextmanager
def wall_deadline(seconds: int) -> Iterator[None]:
    """Also interrupt synchronous work on the Linux sandbox's main thread."""
    if os.name == "nt" or threading.current_thread() is not threading.main_thread():
        yield
        return

    def expired(signum: int, frame: object) -> None:
        raise TimeoutError("Prompt deadline reached.")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@contextlib.contextmanager
def conversation_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "conversation.lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BusyError("Another prompt is active in this sandbox.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def compose_agent(
    client: OpenAIChatCompletionClient,
    workspace: Path,
    memory: Path,
    executor: CommandExecutor,
) -> Agent:
    from agent_framework import (
        AgentModeProvider,
        FileSystemAgentFileStore,
        create_harness_agent,
        tool,
    )

    command_tool = tool(
        executor.execute,
        name="execute_command",
        approval_mode="never_require",
        description="Run a shell command in the sandbox workspace, including Python scripts.",
    )
    return create_harness_agent(
        client=client,
        name="sandbox-harness",
        agent_instructions=(
            "Execute the user's task autonomously inside this disposable sandbox. "
            "Use execute_command to run Python or shell commands. "
            f"Your working directory is {workspace}; keep user artifacts there. "
            "Treat command nonzero exits and timeouts as failures; "
            "inspect and report them honestly. "
            "Do not ask for interactive approvals. "
            "Do not read or disclose authentication material."
        ),
        tools=[command_tool],
        file_access_store=FileSystemAgentFileStore(workspace),
        file_memory_store=FileSystemAgentFileStore(memory),
        file_access_disable_readonly_tool_approval=True,
        file_access_disable_write_tool_approval=True,
        mode_provider=AgentModeProvider(default_mode="execute", expose_mode_set=False),
        disable_tool_auto_approval=True,
        disable_web_search=True,
        max_context_window_tokens=32_768,
        max_output_tokens=4096,
    )


def new_chat_client(model: ModelSettings, token: str, timeout: int) -> OpenAIChatCompletionClient:
    from agent_framework.openai import OpenAIChatCompletionClient
    from openai import AsyncOpenAI

    return OpenAIChatCompletionClient(
        model=model.deployment,
        async_client=AsyncOpenAI(
            base_url=f"{model.endpoint}/openai/v1/",
            api_key=token,
            max_retries=0,
            timeout=timeout,
        ),
        function_invocation_configuration={
            "max_iterations": 16,
            "max_function_calls": 32,
            "max_duration_seconds": timeout,
            "allow_concurrent_invocation": False,
            "include_detailed_errors": False,
        },
    )


def collect_artifacts(workspace: Path) -> tuple[list[Artifact], bool]:
    artifacts: list[Artifact] = []
    for path in sorted(workspace.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if not path.resolve().is_relative_to(workspace.resolve()):
            continue
        if len(artifacts) == 1000:
            return artifacts, True
        artifacts.append(
            Artifact(path=path.relative_to(workspace).as_posix(), size_bytes=path.stat().st_size)
        )
    return artifacts, False


def classify_error(exc: Exception) -> RunError:
    if isinstance(exc, BusyError):
        return RunError(code="busy", message=str(exc))
    if isinstance(exc, TimeoutError):
        return RunError(code="timeout", message="The prompt exceeded its execution deadline.")
    cause: BaseException | None = exc
    for _ in range(10):
        status_code = getattr(cause, "status_code", None)
        if status_code in (401, 403):
            return RunError(
                code="authentication",
                message=f"Azure OpenAI returned {status_code}. Check token audience, tenant, and "
                "Cognitive Services OpenAI User access on the configured resource.",
            )
        cause = cause.__cause__ if cause is not None else None
        if cause is None:
            break
    return RunError(
        code="execution_failed",
        message=f"Harness execution failed ({type(exc).__name__}); no prompt was retried.",
    )


async def execute_request(root: Path, request: RunRequest, auth: AuthInput) -> RunResult:
    from agent_framework import AgentSession

    base: ResultContext = {"request_id": request.request_id, "sandbox_id": request.sandbox_id}
    try:
        auth.require_lifetime(request.timeout_seconds, time.time())
    except ValueError as exc:
        return RunResult(
            **base,
            status="failed",
            error=RunError(
                code="authentication",
                message=str(exc),
            ),
        )
    token = auth.token.get_secret_value()
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    checkpoint = root / "session.json"
    if checkpoint.exists():
        try:
            serialized = json.loads(checkpoint.read_text(encoding="utf-8"))
            if (
                not isinstance(serialized, dict)
                or serialized.get("type") != "session"
                or not isinstance(serialized.get("session_id"), str)
                or not serialized["session_id"]
                or not isinstance(serialized.get("state"), dict)
            ):
                raise ValueError("Invalid checkpoint envelope.")
            session = AgentSession.from_dict(serialized)
        except (ValueError, TypeError, KeyError) as exc:
            return RunResult(
                **base,
                status="failed",
                error=RunError(
                    code="invalid_state",
                    message=f"Cannot restore conversation checkpoint ({type(exc).__name__}).",
                ),
            )
    else:
        session = AgentSession()
    base["conversation_id"] = session.session_id
    client = new_chat_client(request.model, token, request.timeout_seconds)
    executor = CommandExecutor(
        workspace,
        time.monotonic() + request.timeout_seconds,
        token,
    )
    try:
        agent = compose_agent(client, workspace, root / "memory", executor)
        async with asyncio.timeout(request.timeout_seconds):
            response = await agent.run(request.prompt, session=session)
        if response.user_input_requests:
            return RunResult(
                **base,
                status="failed",
                error=RunError(
                    code="approval_required",
                    message="Unexpected interactive input request.",
                ),
            )
        artifacts, truncated = collect_artifacts(workspace)
        result = RunResult(
            **base,
            status="succeeded",
            text=response.text,
            artifacts=artifacts,
            artifacts_truncated=truncated,
        )
        # A trusted prompt could still echo a token; do not persist its literal value.
        state_json = json.dumps(session.to_dict(), ensure_ascii=False).replace(token, "[REDACTED]")
        atomic_write(checkpoint, state_json)
        return RunResult.model_validate_json(result.model_dump_json().replace(token, "[REDACTED]"))
    except Exception as exc:
        return RunResult(**base, status="failed", error=classify_error(exc))
    finally:
        await client.client.close()


def process_request(root: Path, request_id: str) -> RunResult:
    validate_request_id(request_id)
    directory = root / "requests" / request_id
    result_path = directory / "result.json"
    auth_path = directory / "auth.json"
    try:
        request = RunRequest.model_validate_json((directory / "request.json").read_bytes())
    except (OSError, ValueError):
        auth_path.unlink(missing_ok=True)
        raise
    base: ResultContext = {"request_id": request_id, "sandbox_id": request.sandbox_id}
    try:
        with conversation_lock(root):
            if result_path.exists():
                return RunResult.model_validate_json(result_path.read_bytes())
            if request.request_id != request_id:
                raise ValueError("Mismatched request ID.")
            installed = Installation.model_validate_json((root / "installation.json").read_bytes())
            if installed.model != request.model:
                raise ValueError("Request model differs from sandbox installation.")
            if (directory / "started").exists():
                raise RuntimeError("Request was already started; automatic replay is unsafe.")
            auth = AuthInput.model_validate_json(auth_path.read_bytes())
            auth_path.unlink()
            atomic_write(directory / "started", "started")
            try:
                with wall_deadline(request.timeout_seconds):
                    result = asyncio.run(execute_request(root, request, auth))
            except TimeoutError as exc:
                result = RunResult(**base, status="failed", error=classify_error(exc))
            atomic_write(result_path, result.model_dump_json())
            return result
    except BusyError as exc:
        result = RunResult(**base, status="failed", error=classify_error(exc))
        atomic_write(result_path, result.model_dump_json())
        return result
    finally:
        auth_path.unlink(missing_ok=True)


def transfer_file(root: Path, request_id: str) -> None:
    validate_request_id(request_id)
    directory = root / "transfers" / request_id
    request = FileRequest.model_validate_json((directory / "request.json").read_bytes())
    if request.request_id != request_id:
        raise ValueError("Mismatched file-transfer ID.")
    with conversation_lock(root):
        workspace = (root / "workspace").resolve()
        path = (workspace / request.path).resolve()
        if not path.is_relative_to(workspace):
            raise ValueError("File path escapes the workspace through a symlink.")
        if request.direction == "upload":
            content = (directory / "payload").read_bytes()
            if len(content) > MAX_FILE_BYTES:
                raise ValueError("Upload exceeds the 16 MiB limit.")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".upload")
            temporary.write_bytes(content)
            temporary.replace(path)
        else:
            with path.open("rb") as source:
                content = source.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES:
                raise ValueError("Download exceeds the 16 MiB limit.")
            (directory / "payload").write_bytes(content)
        atomic_write(directory / "complete", "complete")


def initialize(root: Path, model: ModelSettings) -> None:
    with conversation_lock(root):
        path = root / "installation.json"
        if path.exists():
            previous = Installation.model_validate_json(path.read_bytes())
            if previous.model != model:
                raise ValueError("Refusing to reinitialize a sandbox with another model.")
            return
        (root / "workspace").mkdir(parents=True, exist_ok=True)
        atomic_write(path, Installation(model=model).model_dump_json())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init", "run", "transfer"])
    parser.add_argument("request_id", nargs="?")
    args = parser.parse_args(argv)
    root = Path(ROOT)
    try:
        if args.action == "init":
            initialize(root, ModelSettings.model_validate_json((root / "model.json").read_bytes()))
        elif args.action == "transfer":
            if not args.request_id:
                parser.error("transfer requires a request ID")
            transfer_file(root, args.request_id)
        else:
            if not args.request_id:
                parser.error("run requires a request ID")
            result = process_request(root, args.request_id)
            return 0 if result.status == "succeeded" else 1
    except Exception as exc:
        # Do not print exception inputs, headers, or traceback locals containing tokens.
        print(f"Runner failed: {type(exc).__name__}. Inspect request status.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
