"""Bounded command execution inside the trusted sandbox."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

OUTPUT_LIMIT = 16_384
COMMAND_TIMEOUT_SECONDS = 30


class CommandExecutor:
    def __init__(self, workspace: Path, deadline: float, secret: str) -> None:
        self.workspace = workspace
        self.deadline = deadline
        self.secret = secret

    async def execute(self, command: str) -> dict[str, str | int | bool | None]:
        """Execute shell/Python commands in the workspace; output is capped."""
        if not command.strip() or len(command) > 65_536:
            raise ValueError("Command must contain between 1 and 65536 characters.")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Run deadline reached.")
        allowed_env = {
            "PATH",
            "HOME",
            "TMP",
            "TEMP",
            "TMPDIR",
            "SYSTEMROOT",
            "COMSPEC",
            "USERPROFILE",
            "VIRTUAL_ENV",
            "LANG",
            "LC_ALL",
        }
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed_env}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        options = {
            "cwd": self.workspace,
            "env": env,
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "nt":
            process = await asyncio.create_subprocess_shell(command, **options)
        else:
            process = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                command,
                start_new_session=True,
                **options,
            )
        stdout = bytearray()
        stderr = bytearray()
        truncated = [False]

        async def drain(reader: asyncio.StreamReader | None, target: bytearray) -> None:
            assert reader is not None
            while chunk := await reader.read(4096):
                space = OUTPUT_LIMIT - len(target)
                target.extend(chunk[:space])
                if len(chunk) > space:
                    truncated[0] = True

        readers = [
            asyncio.create_task(drain(process.stdout, stdout)),
            asyncio.create_task(drain(process.stderr, stderr)),
        ]
        timed_out = False
        try:
            async with asyncio.timeout(min(COMMAND_TIMEOUT_SECONDS, remaining)):
                await process.wait()
                await asyncio.gather(*readers)
        except TimeoutError:
            timed_out = True
        finally:
            # Kill the group even if the shell exited but left background children.
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.returncode is None:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill.exe",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                await killer.wait()
            await process.wait()
            await asyncio.gather(*readers, return_exceptions=True)

        def clean(data: bytearray) -> str:
            return data.decode("utf-8", errors="replace").replace(self.secret, "[REDACTED]")

        return {
            "stdout": clean(stdout),
            "stderr": clean(stderr),
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "output_truncated": truncated[0],
        }
