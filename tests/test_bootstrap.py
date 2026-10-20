from __future__ import annotations

import io
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

from aca_sandbox_harness import bootstrap
from aca_sandbox_harness.models import FEED


def test_bootstrap_uses_only_the_microsoft_feed_and_locked_requirements(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("aca_sandbox_harness/runtime-requirements.txt", "example==1.0")
    (tmp_path / "bundle.zip").write_bytes(output.getvalue())
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def capture(command: list[str], **kwargs: Any) -> None:
        calls.append((command, kwargs))

    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap.subprocess, "run", capture)
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://must-not-be-used.invalid/simple")
    bootstrap.main()
    pip_command, pip_options = calls[1]
    assert pip_command[pip_command.index("--index-url") + 1] == FEED
    assert "--require-hashes" in pip_command
    assert "--only-binary=:all:" in pip_command
    assert "PIP_EXTRA_INDEX_URL" not in pip_options["env"]
    assert pip_options["env"]["PIP_CONFIG_FILE"] == os.devnull
    assert calls[2][0][-1] == "init"
    assert calls[2][1]["env"]["PYTHONPATH"] == str(tmp_path / "source")


def test_setup_helper_preview_never_authenticates_or_provisions() -> None:
    script = Path(__file__).parents[1] / "scripts" / "setup_sandbox_group.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--subscription",
            "00000000-0000-0000-0000-000000000001",
            "--resource-group",
            "preview-rg",
            "--group",
            "preview-group",
            "--region",
            "eastus2",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "No changes made" in result.stdout
    assert "preview-group" in result.stdout
    assert "DefaultAzureCredential" not in result.stderr
