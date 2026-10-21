import csv
from datetime import datetime, date
from math import erf, sqrt
from pathlib import Path

ROOT = Path('.')
ART = ROOT / 'artifacts'
EXP = ROOT / 'exports'


def parse_date(s):
    return datetime.strptime(s, '%Y-%m-%d').date()


def load_joined_rows():
    prod = {}
    with open(EXP / 'production.csv', newline='') as fh:
        for row in csv.DictReader(fh):
            prod[row['lot_id']] = row

    rows = []
    with open(EXP / 'inspections.csv', newline='') as fh:
        for row in csv.DictReader(fh):
            p = prod[row['lot_id']]
            d = parse_date(p['production_date'])
            rows.append({
                'lot_id': row['lot_id'],
                'date': d,
                'line': p['line_id'],
                'shift': p['shift'],
                'product_code': p['product_code'],
                'inspected_units': int(row['inspected_units']),
                'defect_count': int(row['defect_count']),
            })
    return rows


def two_prop_pvalue(x1, n1, x2, n2):
    p_pool = (x1 + x2) / (n1 + n2)
    se = sqrt(p_pool * (1 - p_pool) * (1/n1 + 1/n2))
    if se == 0:
        return 1.0, 0.0
    z = ((x1 / n1) - (x2 / n2)) / se
    phi = 0.5 * (1 + erf(abs(z) / sqrt(2)))
    p = 2 * (1 - phi)
    return p, z


def summarize(rows, predicate):
    kept = [r for r in rows if predicate(r)]
    max_date = max(r['date'] for r in kept)
    last14_start = max_date.fromordinal(max_date.toordinal() - 13)
    earlier = [r for r in kept if r['date'] < last14_start]
    recent = [r for r in kept if r['date'] >= last14_start]

    def agg(subset):
        defects = sum(r['defect_count'] for r in subset)
        inspected = sum(r['inspected_units'] for r in subset)
        rate = defects / inspected if inspected else 0.0
        return defects, inspected, rate

    e = agg(earlier)
    r = agg(recent)
    p, z = two_prop_pvalue(r[0], r[1], e[0], e[1])
    return {
        'last14_start': last14_start,
        'max_date': max_date,
        'earlier': e,
        'recent': r,
        'pvalue': p,
        'z': z,
    }


def fmt_pct(x):
    return f'{x*100:.2f}%'

rows = load_joined_rows()

with open(EXP / 'maintenance_events.csv', newline='') as fh:
    maint = list(csv.DictReader(fh))

night_excl = summarize(rows, lambda r: r['shift'] != 'Night')
p300_excl = summarize(rows, lambda r: r['product_code'] != 'P300')

lines = []
lines.append('# Follow-up sensitivity checks')
lines.append('')
lines.append(f"Window split from source exports: earlier period {min(r['date'] for r in rows)} to {(night_excl['last14_start']).fromordinal(night_excl['last14_start'].toordinal()-1)}, last 14 days {night_excl['last14_start']} to {night_excl['max_date']}.")
lines.append('')
lines.append('## Excluding Night shift')
lines.append(f"- Earlier period: {fmt_pct(night_excl['earlier'][2])} ({night_excl['earlier'][0]}/{night_excl['earlier'][1]}).")
lines.append(f"- Last 14 days: {fmt_pct(night_excl['recent'][2])} ({night_excl['recent'][0]}/{night_excl['recent'][1]}).")
lines.append(f"- Change: +{(night_excl['recent'][2]-night_excl['earlier'][2])*100:.2f} percentage points; ratio {(night_excl['recent'][2]/night_excl['earlier'][2]):.2f}x.")
lines.append(f"- Two-proportion z-test p-value: {night_excl['pvalue']:.3g}.")
lines.append('')
lines.append('## Excluding product P300')
lines.append(f"- Earlier period: {fmt_pct(p300_excl['earlier'][2])} ({p300_excl['earlier'][0]}/{p300_excl['earlier'][1]}).")
lines.append(f"- Last 14 days: {fmt_pct(p300_excl['recent'][2])} ({p300_excl['recent'][0]}/{p300_excl['recent'][1]}).")
lines.append(f"- Change: +{(p300_excl['recent'][2]-p300_excl['earlier'][2])*100:.2f} percentage points; ratio {(p300_excl['recent'][2]/p300_excl['earlier'][2]):.2f}x.")
lines.append(f"- Two-proportion z-test p-value: {p300_excl['pvalue']:.3g}.")
lines.append('')
lines.append('## Interpretation note')
for row in maint:
    lines.append(f"- Maintenance log: {row['event_date']} on {row['line_id']} — {row['event_type']}: {row['description']}")
lines.append('- The elevated period in the prior investigation begins on 2026-10-05, so the scheduled maintenance entry falls one day before that window; this timing is worth checking but does not by itself establish root cause.')
lines.append('- Compared with the prior overall last-14-day rate of 1.78% (674/37,800) from investigation.md, both exclusions reduce the recent rate to about 1.45%, but each still remains above its own earlier-period baseline.')
lines.append('')
lines.append('## Two checks for a quality engineer')
lines.append('- Verify whether inspection method, sampling coverage, or defect-coding practice changed around 2026-10-04 to 2026-10-05, especially outside Night shift and outside P300, since the increase is reduced but not eliminated by either exclusion.')
lines.append('- Review lots around the maintenance date for shared materials, tooling settings, setup changes, and first-article/containment records to see whether the post-2026-10-04 increase clusters by line, component source, or defect mode rather than assuming the maintenance event caused it.')

(ART / 'followup.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
print((ART / 'followup.md').read_text())
