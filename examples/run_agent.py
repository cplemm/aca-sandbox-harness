"""Create a sandbox, run prompts, download an artifact, and clean up its lifecycle."""

import argparse
from pathlib import Path

from azure.identity import DefaultAzureCredential

from aca_sandbox_harness import RunResult, SandboxHarness, SandboxSettings


def show(result: RunResult) -> None:
    print(f"request={result.request_id} conversation={result.conversation_id}")
    if result.error:
        raise RuntimeError(f"{result.error.code}: {result.error.message}")
    print(result.text)


def main(*, suspend: bool = False) -> None:
    target = Path("analysis.txt")
    if target.exists():
        raise FileExistsError("Move analysis.txt before running; downloads never overwrite files.")
    with DefaultAzureCredential() as credential:
        with SandboxHarness(SandboxSettings.from_env(), credential=credential) as client:
            sandbox = client.create()
            print(f"Created sandbox: {sandbox.sandbox_id}")
            try:
                show(
                    sandbox.run(
                        "Create a Python script that calculates the mean of [10, 20, 30], "
                        "run it, and save the result to analysis.txt in the workspace. "
                        "Report the actual command output."
                    )
                )
                # Reconnecting preserves both conversation and workspace files.
                sandbox = client.connect(sandbox.sandbox_id)
                show(
                    sandbox.run(
                        "Using the script and data from the previous turn, also calculate "
                        "the sum and append it to analysis.txt. Run the script to verify."
                    )
                )
                sandbox.download_file("analysis.txt", target)
                print(f"Downloaded: {target.resolve()}")
            finally:
                if suspend:
                    sandbox.suspend()
                    print(f"Suspended sandbox: {sandbox.sandbox_id}")
                else:
                    sandbox.delete()
                    print(f"Deleted sandbox: {sandbox.sandbox_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the ACA sandbox harness example.")
    parser.add_argument(
        "--suspend",
        action="store_true",
        help="Suspend the sandbox after execution instead of deleting it.",
    )
    main(suspend=parser.parse_args().suspend)
