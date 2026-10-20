"""Uploaded standalone bootstrap script; requires only the image's Python."""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path("/tmp/aca-harness")
FEED = "https://packagefeedproxy.microsoft.io/pypi/simple"


def main() -> None:
    with zipfile.ZipFile(ROOT / "bundle.zip") as bundle:
        bundle.extractall(ROOT / "source")
    subprocess.run(
        [sys.executable, "-m", "venv", str(ROOT / "venv")],
        check=True,
        timeout=60,
    )
    python = ROOT / "venv" / "bin" / "python"
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PIP_", "UV_"))}
    env["PIP_CONFIG_FILE"] = os.devnull
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--index-url",
            FEED,
            "--disable-pip-version-check",
            "--no-input",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(ROOT / "source" / "aca_sandbox_harness" / "runtime-requirements.txt"),
        ],
        env=env,
        check=True,
        timeout=540,
    )
    env["PYTHONPATH"] = str(ROOT / "source")
    subprocess.run(
        [str(python), "-m", "aca_sandbox_harness.runner", "init"],
        env=env,
        check=True,
        timeout=30,
    )


if __name__ == "__main__":
    main()
