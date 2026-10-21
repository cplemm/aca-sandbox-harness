"""Validate and analyze the synthetic manufacturing exports using only Python's stdlib."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
from collections import defaultdict
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import TypedDict


class JoinedRow(TypedDict):
    lot_id: str
    production_date: date
    line_id: str
    shift: str
    product_code: str
    produced_units: int
    inspected_units: int
    defect_count: int


def _include_row(_row: JoinedRow) -> bool:
    return True


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def rate(defects: int, inspected: int) -> float:
    return defects / inspected if inspected else 0.0


def p_value_two_proportions(
    defects_a: int, inspected_a: int, defects_b: int, inspected_b: int
) -> float:
    if not inspected_a or not inspected_b:
        return 1.0
    pooled = (defects_a + defects_b) / (inspected_a + inspected_b)
    variance = pooled * (1 - pooled) * (1 / inspected_a + 1 / inspected_b)
    if variance <= 0:
        return 1.0
    z_score = abs(rate(defects_a, inspected_a) - rate(defects_b, inspected_b)) / math.sqrt(variance)
    return math.erfc(z_score / math.sqrt(2))


def validate_and_join(exports_dir: Path) -> list[JoinedRow]:
    production = read_csv(exports_dir / "production.csv")
    inspections = read_csv(exports_dir / "inspections.csv")
    products = read_csv(exports_dir / "product_reference.csv")
    product_codes = {row["product_code"] for row in products}
    if len(product_codes) != len(products):
        raise ValueError("product_reference.csv contains duplicate product_code values.")

    production_by_lot = {row["lot_id"]: row for row in production}
    inspection_by_lot = {row["lot_id"]: row for row in inspections}
    if len(production_by_lot) != len(production) or len(inspection_by_lot) != len(inspections):
        raise ValueError("Production and inspection lot_id values must be unique.")
    if production_by_lot.keys() != inspection_by_lot.keys():
        raise ValueError("Production and inspection exports must contain exactly the same lots.")

    joined: list[JoinedRow] = []
    for lot_id, produced in production_by_lot.items():
        inspected = inspection_by_lot[lot_id]
        produced_units = int(produced["produced_units"])
        inspected_units = int(inspected["inspected_units"])
        defects = int(inspected["defect_count"])
        if (
            produced_units <= 0
            or inspected_units <= 0
            or inspected_units > produced_units
            or defects < 0
            or defects > inspected_units
        ):
            raise ValueError(f"Invalid production/inspection counts for lot {lot_id}.")
        if produced["product_code"] not in product_codes:
            raise ValueError(f"Unknown product code for lot {lot_id}.")
        joined.append(
            {
                "lot_id": lot_id,
                "production_date": date.fromisoformat(produced["production_date"]),
                "line_id": produced["line_id"],
                "shift": produced["shift"],
                "product_code": produced["product_code"],
                "produced_units": produced_units,
                "inspected_units": inspected_units,
                "defect_count": defects,
            }
        )
    if not joined:
        raise ValueError("No production lots found.")
    return joined


def aggregate(
    rows: list[JoinedRow],
    predicate: Callable[[JoinedRow], bool] | None = None,
) -> tuple[int, int]:
    if predicate is None:
        predicate = _include_row
    defects = sum(int(row["defect_count"]) for row in rows if predicate(row))
    inspected = sum(int(row["inspected_units"]) for row in rows if predicate(row))
    return defects, inspected


def make_chart(weekly: list[tuple[str, float, int]], target: Path) -> None:
    width, height = 840, 440
    left, right, top, bottom = 76, 24, 36, 76
    chart_width, chart_height = width - left - right, height - top - bottom
    max_rate = max((item[1] for item in weekly), default=0.01)
    y_max = max(0.02, math.ceil(max_rate * 100 / 2) * 2 / 100)
    points = []
    for index, (_, weekly_rate, _) in enumerate(weekly):
        x = left + chart_width * index / max(1, len(weekly) - 1)
        y = top + chart_height * (1 - weekly_rate / y_max)
        points.append((x, y))
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    grid = []
    for tick in range(5):
        value = y_max * tick / 4
        y = top + chart_height * (1 - tick / 4)
        grid.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            'stroke="#d9e2ec"/><text x="12" y="'
            f'{y + 4:.1f}" font-size="12">{value:.0%}</text>'
        )
    labels = []
    for index, (label, _, _) in enumerate(weekly):
        x = left + chart_width * index / max(1, len(weekly) - 1)
        labels.append(
            f'<text x="{x:.1f}" y="{height - 36}" text-anchor="middle" '
            f'font-size="12">{html.escape(label)}</text>'
        )
    circles = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#1677b8"/>' for x, y in points
    )
    svg = "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="white"/>',
            f'<text x="{width / 2}" y="24" text-anchor="middle" font-size="18" '
            'font-family="sans-serif">Northstar Components weekly defect rate '
            "(synthetic data)</text>",
            "".join(grid),
            f'<polyline points="{polyline}" fill="none" stroke="#1677b8" stroke-width="3"/>',
            f"{circles}{''.join(labels)}",
            f'<text x="18" y="{height / 2}" transform="rotate(-90 18 {height / 2})" '
            'text-anchor="middle" font-size="13">Defects / inspected units</text>',
            "</svg>",
        ]
    )
    target.write_text(svg, encoding="utf-8")


def analyze(exports_dir: Path, output_dir: Path) -> dict[str, object]:
    rows = validate_and_join(exports_dir)
    maintenance = read_csv(exports_dir / "maintenance_events.csv")
    latest = max(row["production_date"] for row in rows)
    spike_start = latest - timedelta(days=13)

    def before(row: JoinedRow) -> bool:
        return row["production_date"] < spike_start

    def during(row: JoinedRow) -> bool:
        return row["production_date"] >= spike_start

    def target_group(row: JoinedRow) -> bool:
        return row["line_id"] == "L3" and row["shift"] == "Night" and row["product_code"] == "P300"

    baseline_defects, baseline_inspected = aggregate(
        rows, lambda row: before(row) and target_group(row)
    )
    affected_defects, affected_inspected = aggregate(
        rows, lambda row: during(row) and target_group(row)
    )
    significance = p_value_two_proportions(
        baseline_defects, baseline_inspected, affected_defects, affected_inspected
    )

    contributors: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    weekly: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    start = min(row["production_date"] for row in rows)
    for row in rows:
        if during(row):
            key = (str(row["line_id"]), str(row["shift"]), str(row["product_code"]))
            contributors[key][0] += int(row["defect_count"])
            contributors[key][1] += int(row["inspected_units"])
        week_number = (row["production_date"] - start).days // 7
        week_start = start + timedelta(days=week_number * 7)
        bucket = weekly[week_start.isoformat()]
        bucket[0] += int(row["defect_count"])
        bucket[1] += int(row["inspected_units"])

    top = sorted(contributors.items(), key=lambda item: item[1][0], reverse=True)
    weekly_values = [
        (label, rate(values[0], values[1]), values[1]) for label, values in sorted(weekly.items())
    ]
    after_defects, after_inspected = aggregate(rows, during)
    without_nights = aggregate(
        rows,
        lambda row: during(row) and row["shift"] != "Night",
    )
    without_p300 = aggregate(
        rows,
        lambda row: during(row) and row["product_code"] != "P300",
    )
    maintenance_summary = "; ".join(
        f"{event['event_date']} on {event['line_id']}: {event['description']}"
        for event in maintenance
    )
    significance_label = (
        "statistically distinguishable"
        if significance < 0.05
        else "not statistically distinguishable"
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    chart_path = output_dir / "weekly_defect_rate.svg"
    make_chart(weekly_values, chart_path)
    report = [
        "# Synthetic manufacturing quality investigation",
        "",
        "> This is synthetic evidence for a demo, not a production quality "
        "disposition or a proven root cause.",
        "",
        f"- Data window: {start.isoformat()} through {latest.isoformat()} "
        f"({len(rows):,} joined lots).",
        f"- Last-14-day overall defect rate: {rate(after_defects, after_inspected):.2%} "
        f"({after_defects:,}/{after_inspected:,}).",
        f"- Maintenance record: {maintenance_summary or 'none recorded'}.",
        "",
        "## Strongest observed segment",
        "",
        "| Line | Shift | Product | Defects / inspected | Rate |",
        "|---|---|---|---:|---:|",
    ]
    for (line, shift, product), (defects, inspected) in top[:5]:
        report.append(
            f"| {line} | {shift} | {product} | {defects:,} / {inspected:,} | "
            f"{rate(defects, inspected):.2%} |"
        )
    report.extend(
        [
            "",
            "## Before/after check for L3 / Night / P300",
            "",
            f"- Before last 14 days: {rate(baseline_defects, baseline_inspected):.2%} "
            f"({baseline_defects:,}/{baseline_inspected:,}).",
            f"- Last 14 days: {rate(affected_defects, affected_inspected):.2%} "
            f"({affected_defects:,}/{affected_inspected:,}).",
            f"- Two-proportion z-test, two-sided p-value: {significance:.3g}; "
            f"{significance_label} "
            "at 5% under this approximation.",
            "",
            "## Sensitivity and interpretation",
            "",
            f"- Excluding Night shift, last-14-day rate: {rate(*without_nights):.2%} "
            f"({without_nights[0]:,}/{without_nights[1]:,}).",
            f"- Excluding product P300, last-14-day rate: {rate(*without_p300):.2%} "
            f"({without_p300[0]:,}/{without_p300[1]:,}).",
            f"- Weekly trend chart: `{chart_path.name}`.",
            "- The scheduled guide-bushing replacement precedes the elevated period, "
            "but timing and segment association do not establish causation.",
            "- Check lot-level records, inspection method/coverage changes, product "
            "mix, supplier/material batches, tooling condition, and maintenance "
            "work details with a quality engineer.",
            "- The z-test is a simple large-sample comparison of independent "
            "inspected units; clustering by lot, repeated conditions, and multiple "
            "comparisons are not modeled.",
            "",
        ]
    )
    report_path = output_dir / "investigation.md"
    report_path.write_text("\n".join(report), encoding="utf-8")
    summary = {
        "synthetic": True,
        "data_window": [start.isoformat(), latest.isoformat()],
        "joined_lots": len(rows),
        "target_segment": {
            "line": "L3",
            "shift": "Night",
            "product_code": "P300",
            "before_rate": rate(baseline_defects, baseline_inspected),
            "during_rate": rate(affected_defects, affected_inspected),
            "two_sided_p_value": significance,
        },
        "top_segments_last_14_days": [
            {
                "line": key[0],
                "shift": key[1],
                "product_code": key[2],
                "defects": values[0],
                "inspected_units": values[1],
                "rate": rate(values[0], values[1]),
            }
            for key, values in top[:5]
        ],
        "sensitivity_last_14_days": {
            "excluding_night_rate": rate(*without_nights),
            "excluding_p300_rate": rate(*without_p300),
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exports", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = analyze(args.exports, args.output)
    print(
        "Analysis complete: "
        f"{summary['joined_lots']} joined lots; report and chart in {args.output}."
    )


if __name__ == "__main__":
    main()
