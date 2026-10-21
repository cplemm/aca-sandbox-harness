from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

DEMO_DIR = Path(__file__).parents[1] / "examples" / "manufacturing_quality"


def run_script(script: str, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, str(DEMO_DIR / script), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_synthetic_exports_are_deterministic_and_analysis_finds_planted_spike(
    tmp_path: Path,
) -> None:
    exports = tmp_path / "exports"
    output = tmp_path / "artifacts"
    run_script("generate_exports.py", str(exports), "--seed", "20261020")
    first_generation = {path.name: path.read_bytes() for path in exports.iterdir()}
    run_script("generate_exports.py", str(exports), "--seed", "20261020")
    assert {path.name: path.read_bytes() for path in exports.iterdir()} == first_generation

    run_script("analyze_quality.py", "--exports", str(exports), "--output", str(output))
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    target = summary["target_segment"]

    assert summary["synthetic"] is True
    assert summary["joined_lots"] == 56 * 3 * 3 * 3
    assert target["line"] == "L3"
    assert target["shift"] == "Night"
    assert target["product_code"] == "P300"
    assert target["during_rate"] > target["before_rate"] * 4
    assert target["two_sided_p_value"] < 0.05
    assert summary["top_segments_last_14_days"][0]["line"] == "L3"
    assert summary["top_segments_last_14_days"][0]["shift"] == "Night"
    assert summary["top_segments_last_14_days"][0]["product_code"] == "P300"
    assert (output / "investigation.md").is_file()
    assert "<svg" in (output / "weekly_defect_rate.svg").read_text(encoding="utf-8")


def test_volume_verification_reads_generated_export_layout_without_modifying_files(
    fake_stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(DEMO_DIR))
    demo = importlib.import_module("run_investigation")
    client, remote, _, _ = fake_stack
    sandbox = client.create()
    demo.upload_demo_code(sandbox)
    workspace = remote.root / "workspace"
    directory = workspace / "manufacturing-quality"
    run_script("generate_exports.py", str(directory / "exports"))
    run_script(
        "analyze_quality.py",
        "--exports",
        str(directory / "exports"),
        "--output",
        str(directory / "artifacts"),
    )
    (directory / "artifacts" / "lot_review.py").write_text("# Saved agent script\n")
    (directory / "artifacts" / "segment_breakdown.csv").write_text(
        "line,shift,product,defects,inspected\nL3,Night,P300,149,1400\n"
    )
    original = {
        path.relative_to(workspace): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    assert not (directory / "scenario_metadata.json").exists()

    demo.verify_existing_volume(sandbox)

    assert {
        path.relative_to(workspace): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    } == original

    (directory / "exports" / "production.csv").unlink()
    with pytest.raises(FileNotFoundError, match="production.csv"):
        demo.verify_existing_volume(sandbox)
