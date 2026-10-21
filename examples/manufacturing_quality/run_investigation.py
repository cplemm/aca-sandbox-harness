"""Run a synthetic quality investigation across two sandboxes reusing one volume."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from uuid import uuid4

from azure.containerapps.sandbox import SandboxVolume
from azure.identity import DefaultAzureCredential

from aca_sandbox_harness import HarnessSandbox, RunResult, SandboxHarness, SandboxSettings
from aca_sandbox_harness.models import WORKSPACE

DEMO_DIR = Path(__file__).parent
REMOTE_DIR = "manufacturing-quality"
MOUNT_PATH = WORKSPACE

INITIAL_PROMPT = f"""Work only with the synthetic exports in {MOUNT_PATH}/{REMOTE_DIR}.
First change directory to `{MOUNT_PATH}/{REMOTE_DIR}`. Run
`python generate_exports.py exports --seed 20261020`, then run
`python analyze_quality.py --exports exports --output artifacts`.
Inspect the code and CSV schemas. Write `artifacts/lot_review.py` to independently
join production.csv to inspections.csv by lot_id, validate row/count consistency,
and calculate last-14-day defect counts and rates for each line, shift, and product.
Run that script and save its results to `artifacts/segment_breakdown.csv`.
Summarize the strongest segment with its numerator, denominator, and rate. Treat
the maintenance entry as context only: do not claim correlation proves causation.
Do not modify source exports. Keep all code and outputs under this directory."""

RESUME_PROMPT = f"""Continue the saved Northstar Components synthetic investigation in
`{MOUNT_PATH}/{REMOTE_DIR}`. Do not regenerate or modify source exports. Review the
saved report, chart, summary, and source data; write and run `artifacts/resume_review.py`
to verify the top segment and its before/after rates from the CSVs. Save your findings
in `artifacts/resume_review.md`. Do not state correlation as proven cause."""

FOLLOWUP_PROMPT = f"""Continue the prior investigation from the mounted volume at
{MOUNT_PATH}/{REMOTE_DIR}. First change directory to `{MOUNT_PATH}/{REMOTE_DIR}`.
Do not regenerate the exports. Read the saved `artifacts/investigation.md`,
`artifacts/weekly_defect_rate.svg`, `artifacts/segment_breakdown.csv`, and source
exports. Write and run `artifacts/followup.py` to compare last-14-day defect rates
with the earlier period after excluding Night shift and after excluding product
P300. Save a concise result to `artifacts/followup.md`. Explain what changes,
mention the maintenance timing without asserting root cause, and list two checks
for a quality engineer. Cite calculations from the files rather than guessing."""


def show(result: RunResult, label: str) -> None:
    print(f"{label}: request={result.request_id} conversation={result.conversation_id}")
    if result.error:
        raise RuntimeError(f"{result.error.code}: {result.error.message}")
    print(result.text)


def upload_demo_code(sandbox: HarnessSandbox) -> None:
    for filename in ("generate_exports.py", "analyze_quality.py"):
        sandbox.upload_file(DEMO_DIR / filename, f"{REMOTE_DIR}/{filename}")


def verify_existing_volume(sandbox: HarnessSandbox) -> None:
    required_files = (
        f"{REMOTE_DIR}/generate_exports.py",
        f"{REMOTE_DIR}/analyze_quality.py",
        f"{REMOTE_DIR}/exports/scenario_metadata.json",
        f"{REMOTE_DIR}/exports/production.csv",
        f"{REMOTE_DIR}/exports/inspections.csv",
        f"{REMOTE_DIR}/exports/product_reference.csv",
        f"{REMOTE_DIR}/exports/maintenance_events.csv",
        f"{REMOTE_DIR}/artifacts/investigation.md",
        f"{REMOTE_DIR}/artifacts/summary.json",
        f"{REMOTE_DIR}/artifacts/weekly_defect_rate.svg",
        f"{REMOTE_DIR}/artifacts/lot_review.py",
        f"{REMOTE_DIR}/artifacts/segment_breakdown.csv",
    )
    with tempfile.TemporaryDirectory(prefix="aca-quality-volume-") as temporary:
        for index, remote_path in enumerate(required_files):
            sandbox.download_file(remote_path, Path(temporary) / f"required-{index}")


def verify_followup_artifact(sandbox: HarnessSandbox) -> None:
    with tempfile.TemporaryDirectory(prefix="aca-quality-followup-") as temporary:
        sandbox.download_file(
            f"{REMOTE_DIR}/artifacts/followup.md",
            Path(temporary) / "followup.md",
        )


def finish_sandboxes(sandboxes: list[HarnessSandbox], *, suspend: bool) -> None:
    for sandbox in reversed(sandboxes):
        if suspend:
            sandbox.suspend()
            print(f"Suspended sandbox: {sandbox.sandbox_id}")
        else:
            sandbox.delete()


def run_demo(*, volume_name: str | None, delete_volume: bool, suspend_sandboxes: bool) -> None:
    if volume_name is not None and not volume_name.strip():
        raise ValueError("--volume-name must not be blank.")
    if suspend_sandboxes and delete_volume:
        raise ValueError("--suspend cannot be combined with --delete-volume.")
    created_volume = volume_name is None
    volume_name = volume_name or f"quality-demo-{uuid4().hex[:12]}"
    sandboxes: list[HarnessSandbox] = []
    suspended_ids: list[str] = []
    with DefaultAzureCredential() as credential:
        with SandboxHarness(SandboxSettings.from_env(), credential=credential) as client:
            if created_volume:
                if suspend_sandboxes:
                    volume = client.create_volume(volume_name, type="AzureBlob")
                else:
                    volume = client.create_volume(volume_name, type="DataDisk", size="10Gi")
            else:
                volume = client.get_volume(volume_name)
            required_type = "AzureBlob" if suspend_sandboxes else "DataDisk"
            if volume.type != required_type:
                raise ValueError(
                    f"Volume {volume_name!r} has type {volume.type!r}; "
                    f"--suspend requires {required_type}, while the default flow "
                    "requires a Data Disk volume."
                )
            print(f"Using {required_type} volume: {volume_name}")
            print("The volume is retained unless --delete-volume is specified.")
            mount = SandboxVolume(volume_name=volume_name, mountpoint=MOUNT_PATH)
            try:
                first = client.create(volumes=[mount])
                sandboxes.append(first)
                print(f"First sandbox: {first.sandbox_id}")
                first_sandbox_id = first.sandbox_id
                if created_volume:
                    upload_demo_code(first)
                    show(first.run(INITIAL_PROMPT), "Initial investigation")
                else:
                    verify_existing_volume(first)
                    show(first.run(RESUME_PROMPT), "Resumed investigation")
                verify_existing_volume(first)

                if suspend_sandboxes:
                    first.suspend()
                    suspended_ids.append(first.sandbox_id)
                    sandboxes.remove(first)
                    print(f"Suspended first sandbox: {first.sandbox_id}")
                else:
                    # Release the exclusive mount before the next sandbox attaches it.
                    first.delete()
                    sandboxes.remove(first)

                second = client.create(volumes=[mount])
                sandboxes.append(second)
                if second.sandbox_id == first_sandbox_id:
                    raise RuntimeError("The follow-up must use a distinct sandbox ID.")
                print(f"Distinct follow-up sandbox: {second.sandbox_id}")
                verify_existing_volume(second)
                show(second.run(FOLLOWUP_PROMPT), "Follow-up investigation")
                verify_followup_artifact(second)
            finally:
                finish_sandboxes(sandboxes, suspend=suspend_sandboxes)
                if suspend_sandboxes:
                    suspended_ids.extend(sandbox.sandbox_id for sandbox in sandboxes)
                if delete_volume:
                    client.delete_volume(volume_name)
                    print(f"Deleted volume and its data: {volume_name}")
                elif created_volume:
                    print(f"Persistent volume retained for later use: {volume_name}")
                if suspended_ids:
                    print("Reconnect to suspended sandboxes with client.connect(id):")
                    for sandbox_id in suspended_ids:
                        print(f"  {sandbox_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--volume-name",
        help="Reuse a Data Disk volume, or an AzureBlob volume with --suspend.",
    )
    lifecycle = parser.add_mutually_exclusive_group()
    lifecycle.add_argument(
        "--delete-volume",
        action="store_true",
        help="Permanently delete the selected volume and its contents after "
        "both sandboxes are deleted.",
    )
    lifecycle.add_argument(
        "--suspend",
        dest="suspend_sandboxes",
        action="store_true",
        help="Suspend rather than delete sandboxes; uses a reusable AzureBlob volume.",
    )
    args = parser.parse_args()
    run_demo(
        volume_name=args.volume_name,
        delete_volume=args.delete_volume,
        suspend_sandboxes=args.suspend_sandboxes,
    )


if __name__ == "__main__":
    main()
