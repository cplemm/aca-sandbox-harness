from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def rate(defects: int, inspected: int) -> float:
    return defects / inspected if inspected else 0.0


def validate_and_join(exports_dir: Path) -> list[dict[str, object]]:
    production = read_csv(exports_dir / 'production.csv')
    inspections = read_csv(exports_dir / 'inspections.csv')

    production_by_lot = {row['lot_id']: row for row in production}
    inspections_by_lot = {row['lot_id']: row for row in inspections}

    if len(production_by_lot) != len(production):
        raise ValueError('Duplicate lot_id found in production.csv')
    if len(inspections_by_lot) != len(inspections):
        raise ValueError('Duplicate lot_id found in inspections.csv')
    if production_by_lot.keys() != inspections_by_lot.keys():
        missing_in_inspections = sorted(production_by_lot.keys() - inspections_by_lot.keys())
        missing_in_production = sorted(inspections_by_lot.keys() - production_by_lot.keys())
        raise ValueError(
            'lot_id mismatch between production.csv and inspections.csv; '
            f'missing_in_inspections={missing_in_inspections[:5]}, '
            f'missing_in_production={missing_in_production[:5]}'
        )

    joined: list[dict[str, object]] = []
    for lot_id in sorted(production_by_lot):
        prod = production_by_lot[lot_id]
        insp = inspections_by_lot[lot_id]
        produced_units = int(prod['produced_units'])
        inspected_units = int(insp['inspected_units'])
        defect_count = int(insp['defect_count'])
        if produced_units <= 0 or inspected_units <= 0:
            raise ValueError(f'Non-positive units for {lot_id}')
        if inspected_units > produced_units:
            raise ValueError(f'Inspected units exceed produced units for {lot_id}')
        if defect_count < 0 or defect_count > inspected_units:
            raise ValueError(f'Invalid defect count for {lot_id}')
        joined.append({
            'lot_id': lot_id,
            'production_date': date.fromisoformat(prod['production_date']),
            'line_id': prod['line_id'],
            'shift': prod['shift'],
            'product_code': prod['product_code'],
            'produced_units': produced_units,
            'inspected_units': inspected_units,
            'defect_count': defect_count,
        })

    if len(joined) != len(production) or len(joined) != len(inspections):
        raise ValueError('Joined row count does not match source row counts')
    return joined


def summarize_segments(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    latest_date = max(row['production_date'] for row in rows)
    window_start = latest_date - timedelta(days=13)
    filtered = [row for row in rows if row['production_date'] >= window_start]

    aggregates: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for row in filtered:
        for segment_type, segment_value in [
            ('line', str(row['line_id'])),
            ('shift', str(row['shift'])),
            ('product', str(row['product_code'])),
        ]:
            bucket = aggregates[(segment_type, segment_value)]
            bucket[0] += int(row['defect_count'])
            bucket[1] += int(row['inspected_units'])

    summary = []
    for (segment_type, segment_value), (defects, inspected) in sorted(aggregates.items()):
        summary.append({
            'segment_type': segment_type,
            'segment_value': segment_value,
            'window_start': window_start.isoformat(),
            'window_end': latest_date.isoformat(),
            'defect_count': defects,
            'inspected_units': inspected,
            'defect_rate': f'{rate(defects, inspected):.6f}',
        })
    return summary


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError('No rows to write')
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description='Independent lot review for synthetic manufacturing exports')
    parser.add_argument('--exports', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()

    rows = validate_and_join(args.exports)
    output_rows = summarize_segments(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.output, output_rows)
    print(f'Validated {len(rows)} joined lots and wrote {len(output_rows)} segment rows to {args.output}')


if __name__ == '__main__':
    main()
