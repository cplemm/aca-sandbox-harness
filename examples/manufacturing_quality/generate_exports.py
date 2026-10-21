"""Generate deterministic, entirely synthetic manufacturing quality exports."""

from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 20261020
START_DATE = date(2026, 8, 24)
DAYS = 56
LINES = ("L1", "L2", "L3")
SHIFTS = ("Day", "Evening", "Night")
PRODUCTS = {
    "P100": ("Bearing Housing", "Machined aluminum housing"),
    "P200": ("Drive Coupling", "Precision drive coupling"),
    "P300": ("Seal Carrier", "Polymer seal carrier"),
}
SPIKE_LINE = "L3"
SPIKE_SHIFT = "Night"
SPIKE_PRODUCT = "P300"
SPIKE_START = START_DATE + timedelta(days=42)


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def generate_exports(output_dir: Path, seed: int = SEED) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    production: list[dict[str, object]] = []
    inspections: list[dict[str, object]] = []

    for day_number in range(DAYS):
        production_date = START_DATE + timedelta(days=day_number)
        for line in LINES:
            for shift in SHIFTS:
                for product_code in PRODUCTS:
                    lot_id = f"LOT-{production_date:%Y%m%d}-{line}-{shift.upper()}-{product_code}"
                    units = 100
                    spike = (
                        production_date >= SPIKE_START
                        and line == SPIKE_LINE
                        and shift == SPIKE_SHIFT
                        and product_code == SPIKE_PRODUCT
                    )
                    defect_probability = 0.105 if spike else 0.015
                    defects = sum(rng.random() < defect_probability for _ in range(units))
                    production.append(
                        {
                            "lot_id": lot_id,
                            "production_date": production_date.isoformat(),
                            "line_id": line,
                            "shift": shift,
                            "product_code": product_code,
                            "produced_units": units,
                        }
                    )
                    inspections.append(
                        {
                            "lot_id": lot_id,
                            "inspected_units": units,
                            "defect_count": defects,
                        }
                    )

    write_csv(
        output_dir / "production.csv",
        ("lot_id", "production_date", "line_id", "shift", "product_code", "produced_units"),
        production,
    )
    write_csv(
        output_dir / "inspections.csv",
        ("lot_id", "inspected_units", "defect_count"),
        inspections,
    )
    write_csv(
        output_dir / "product_reference.csv",
        ("product_code", "product_family", "description"),
        [
            {
                "product_code": code,
                "product_family": family,
                "description": description,
            }
            for code, (family, description) in PRODUCTS.items()
        ],
    )
    write_csv(
        output_dir / "maintenance_events.csv",
        ("event_id", "event_date", "line_id", "event_type", "description"),
        [
            {
                "event_id": "MNT-001",
                "event_date": (SPIKE_START - timedelta(days=1)).isoformat(),
                "line_id": SPIKE_LINE,
                "event_type": "Scheduled maintenance",
                "description": "Replaced guide bushing during scheduled service",
            }
        ],
    )
    (output_dir / "scenario_metadata.json").write_text(
        json.dumps(
            {
                "factory": "Northstar Components",
                "synthetic": True,
                "seed": seed,
                "first_production_date": START_DATE.isoformat(),
                "last_production_date": (START_DATE + timedelta(days=DAYS - 1)).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    generate_exports(args.output_dir, args.seed)
    print(f"Synthetic exports written to {args.output_dir.resolve()} (seed={args.seed}).")


if __name__ == "__main__":
    main()
