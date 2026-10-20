from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from conftest import TOKEN, auth_file
from test_runner import new_request

from aca_sandbox_harness import SandboxSettings
from aca_sandbox_harness.client import runtime_bundle
from aca_sandbox_harness.models import RunResult
from aca_sandbox_harness.runner import initialize


def test_bundled_runner_round_trip_in_fresh_python_process(
    tmp_path: Path,
    settings: SandboxSettings,
) -> None:
    bundle_path = tmp_path / "runtime.zip"
    bundle_path.write_bytes(runtime_bundle())
    source = tmp_path / "source"
    with zipfile.ZipFile(bundle_path) as bundle:
        bundle.extractall(source)
    root = tmp_path / "sandbox"
    initialize(root, settings.model)
    request = new_request(settings, "write a file")
    directory = auth_file(root, request)
    tests = Path(__file__).parent
    script = (
        "import sys; from types import SimpleNamespace; "
        "from test_runner import install_model_stub; "
        "from aca_sandbox_harness import runner; "
        "assert sys.argv[3] in runner.__file__; "
        "install_model_stub(SimpleNamespace(setattr=setattr)); "
        "runner.ROOT=sys.argv[1]; "
        "raise SystemExit(runner.main(['run',sys.argv[2]]))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(source), str(tests)))
    response = subprocess.run(
        [sys.executable, "-c", script, str(root), request.request_id, str(source)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert response.returncode == 0, response.stderr
    result = RunResult.model_validate_json((directory / "result.json").read_bytes())
    assert result.status == "succeeded", result.error
    assert (root / "workspace" / "answer.txt").read_text() == "42"
    assert not (directory / "auth.json").exists()
    assert TOKEN not in response.stdout + response.stderr
    state = json.loads((root / "session.json").read_bytes())
    assert state["session_id"] == result.conversation_id
