from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import TOKEN

from aca_sandbox_harness.tools import OUTPUT_LIMIT, CommandExecutor


def command(script: str) -> str:
    return f'"{sys.executable}" -c "{script}"'


@pytest.fixture
async def exited_process(monkeypatch):
    process = SimpleNamespace(
        pid=12345,
        returncode=0,
        stdout=asyncio.StreamReader(),
        stderr=asyncio.StreamReader(),
        wait=AsyncMock(return_value=0),
    )
    process.stderr.feed_eof()
    factory = "create_subprocess_shell" if os.name == "nt" else "create_subprocess_exec"
    monkeypatch.setattr(
        f"aca_sandbox_harness.tools.asyncio.{factory}", AsyncMock(return_value=process)
    )
    monkeypatch.setattr("aca_sandbox_harness.tools.os.killpg", lambda *_: None, raising=False)
    return process


async def test_timeout_while_draining_cancelled_readers_returns_result(
    tmp_path: Path,
    monkeypatch,
    exited_process,
) -> None:
    monkeypatch.setattr("aca_sandbox_harness.tools.COMMAND_TIMEOUT_SECONDS", 0.02)
    tool = CommandExecutor(tmp_path, time.monotonic() + 60, TOKEN)
    result = await tool.execute("ignored")
    assert result["timed_out"] is True


async def test_reader_error_is_not_swallowed(tmp_path: Path, exited_process) -> None:
    exited_process.stdout.set_exception(OSError("cannot read stdout"))
    tool = CommandExecutor(tmp_path, time.monotonic() + 60, TOKEN)
    with pytest.raises(OSError, match="cannot read stdout"):
        await tool.execute("ignored")


async def test_command_nonzero_exit_and_redaction(tmp_path: Path) -> None:
    tool = CommandExecutor(tmp_path, time.monotonic() + 60, TOKEN)
    result = await tool.execute(command(f"import sys; print('{TOKEN}'); sys.exit(7)"))
    assert result["exit_code"] == 7
    assert str(result["stdout"]).strip() == "[REDACTED]"
    assert result["timed_out"] is False


async def test_command_output_is_bounded(tmp_path: Path) -> None:
    tool = CommandExecutor(tmp_path, time.monotonic() + 60, TOKEN)
    result = await tool.execute(command(f"print('x' * {OUTPUT_LIMIT * 4})"))
    assert len(str(result["stdout"]).encode()) == OUTPUT_LIMIT
    assert result["output_truncated"] is True


async def test_sensitive_environment_not_forwarded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", TOKEN)
    monkeypatch.setenv("AZURE_CLIENT_SECRET", TOKEN)
    tool = CommandExecutor(tmp_path, time.monotonic() + 60, TOKEN)
    result = await tool.execute(
        command(
            "import os; print(os.environ.get('AZURE_OPENAI_API_KEY', 'absent')); "
            "print(os.environ.get('AZURE_CLIENT_SECRET', 'absent'))"
        )
    )
    assert str(result["stdout"]).splitlines() == ["absent", "absent"]


async def test_commands_use_the_runtime_python_environment(tmp_path: Path) -> None:
    tool = CommandExecutor(tmp_path, time.monotonic() + 60, TOKEN)
    result = await tool.execute('python -c "import sys; print(sys.prefix)"')
    assert result["exit_code"] == 0
    actual = Path(str(result["stdout"]).strip())
    assert actual == Path(sys.prefix)


async def test_command_timeout_terminates_process(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("aca_sandbox_harness.tools.COMMAND_TIMEOUT_SECONDS", 0.2)
    tool = CommandExecutor(tmp_path, time.monotonic() + 60, TOKEN)
    start = time.monotonic()
    result = await tool.execute(command("import time; time.sleep(30)"))
    assert result["timed_out"] is True
    assert time.monotonic() - start < 5


async def test_command_cancellation_cleans_up(tmp_path: Path) -> None:
    tool = CommandExecutor(tmp_path, time.monotonic() + 60, TOKEN)
    task = asyncio.create_task(tool.execute(command("import time; time.sleep(30)")))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup in ACA")
async def test_shell_background_child_is_terminated(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("aca_sandbox_harness.tools.COMMAND_TIMEOUT_SECONDS", 0.2)
    tool = CommandExecutor(tmp_path, time.monotonic() + 60, TOKEN)
    result = await tool.execute("sleep 30 & echo $! > child.pid; wait")
    assert result["timed_out"] is True
    pid = int((tmp_path / "child.pid").read_text())
    # A killed child can remain a zombie briefly; it must not still be running.
    status = Path(f"/proc/{pid}/stat")

    def child_stopped() -> bool:
        return not status.exists() or status.read_text().split()[2] == "Z"

    assert await asyncio.to_thread(child_stopped)


@pytest.mark.skipif(os.name == "nt", reason="POSIX background-child pipe behavior")
async def test_exited_shell_with_open_child_pipes_still_returns_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("aca_sandbox_harness.tools.COMMAND_TIMEOUT_SECONDS", 0.2)
    tool = CommandExecutor(tmp_path, time.monotonic() + 60, TOKEN)
    result = await tool.execute("sleep 30 &")
    assert result["timed_out"] is True
