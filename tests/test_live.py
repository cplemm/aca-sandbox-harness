import os
from pathlib import Path

import pytest

from aca_sandbox_harness import SandboxHarness, SandboxSettings

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("ACA_HARNESS_LIVE_TEST") != "1",
        reason="Requires explicit opt-in and an existing sandbox group/model deployment.",
    ),
]


def test_live_prompt_followup_and_artifact(tmp_path: Path) -> None:
    with SandboxHarness(SandboxSettings.from_env()) as client:
        sandbox = client.create()
        print(f"Live-test sandbox ID: {sandbox.sandbox_id}")
        try:
            first = sandbox.run(
                "Use a shell tool to execute Python that writes 42 to answer.txt. "
                "Remember the marker cobalt-otter for my next message."
            )
            assert first.status == "succeeded", first.error
            followup = client.connect(sandbox.sandbox_id).run(
                "What marker did I ask you to remember? Check answer.txt with a tool."
            )
            assert followup.status == "succeeded", followup.error
            assert followup.conversation_id == first.conversation_id
            assert "cobalt-otter" in followup.text
            target = tmp_path / "answer.txt"
            sandbox.download_file("answer.txt", target)
            assert target.read_text().strip() == "42"
        finally:
            sandbox.delete()
