from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
EXPORTS = BASE / 'exports'
ARTIFACTS = BASE / 'artifacts'


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def load_joined() -> list[dict[str, object]]:
    production = {row['lot_id']: row for row in read_csv(EXPORTS / 'production.csv')}
    inspections = {row['lot_id']: row for row in read_csv(EXPORTS / 'inspections.csv')}
    if production.keys() != inspections.keys():
        raise ValueError('lot_id mismatch between production and inspections')
    joined = []
    for lot_id in sorted(production):
        prod = production[lot_id]
        insp = inspections[lot_id]
        joined.append({
            'lot_id': lot_id,
            'production_date': date.fromisoformat(prod['production_date']),
            'line': prod['line_id'],
            'shift': prod['shift'],
            'product_code': prod['product_code'],
            'inspected_units': int(insp['inspected_units']),
            'defect_count': int(insp['defect_count']),
        })
    return joined


def rate(defects: int, inspected: int) -> float:
    return defects / inspected if inspected else 0.0


def norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def two_prop_p_value(x1: int, n1: int, x2: int, n2: int) -> float:
    p_pool = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    z = (x1 / n1 - x2 / n2) / se
    return 2 * norm_sf(abs(z))


def main() -> None:
    rows = load_joined()
    latest = max(r['production_date'] for r in rows)
    window_start = latest - timedelta(days=13)
    during = [r for r in rows if r['production_date'] >= window_start]
    before = [r for r in rows if r['production_date'] < window_start]

    seg = defaultdict(lambda: [0, 0])
    for r in during:
        key = (r['line'], r['shift'], r['product_code'])
        seg[key][0] += int(r['defect_count'])
        seg[key][1] += int(r['inspected_units'])
    ranked = sorted(
        (
            {
                'line': k[0], 'shift': k[1], 'product_code': k[2],
                'defects': v[0], 'inspected_units': v[1], 'rate': rate(v[0], v[1])
            }
            for k, v in seg.items()
        ),
        key=lambda x: (-x['rate'], -x['defects'], x['line'], x['shift'], x['product_code'])
    )
    top = ranked[0]

    def summarize(period_rows: list[dict[str, object]], target: dict[str, object]) -> tuple[int, int, float]:
        matched = [r for r in period_rows if r['line'] == target['line'] and r['shift'] == target['shift'] and r['product_code'] == target['product_code']]
        defects = sum(int(r['defect_count']) for r in matched)
        inspected = sum(int(r['inspected_units']) for r in matched)
        return defects, inspected, rate(defects, inspected)

    b_def, b_insp, b_rate = summarize(before, top)
    d_def, d_insp, d_rate = summarize(during, top)
    p_value = two_prop_p_value(b_def, b_insp, d_def, d_insp)

    saved_summary = json.loads((ARTIFACTS / 'summary.json').read_text(encoding='utf-8'))
    target = saved_summary['target_segment']
    top_segments = saved_summary['top_segments_last_14_days']

    checks = {
        'top_matches_saved_target': top['line'] == target['line'] and top['shift'] == target['shift'] and top['product_code'] == target['product_code'],
        'top_matches_saved_top_row': top['line'] == top_segments[0]['line'] and top['shift'] == top_segments[0]['shift'] and top['product_code'] == top_segments[0]['product_code'],
        'before_rate_matches_summary': abs(b_rate - float(target['before_rate'])) < 1e-12,
        'during_rate_matches_summary': abs(d_rate - float(target['during_rate'])) < 1e-12,
        'p_value_matches_summary': abs(p_value - float(target['two_sided_p_value'])) < 1e-60,
    }

    result = {
        'data_window': {'start': min(r['production_date'] for r in rows).isoformat(), 'end': latest.isoformat()},
        'last_14_day_window': {'start': window_start.isoformat(), 'end': latest.isoformat()},
        'top_segment_from_csvs': top,
        'before_period_for_top_segment': {'defects': b_def, 'inspected_units': b_insp, 'rate': b_rate},
        'last_14_days_for_top_segment': {'defects': d_def, 'inspected_units': d_insp, 'rate': d_rate},
        'two_sided_p_value': p_value,
        'checks': checks,
    }
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
