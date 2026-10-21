# Synthetic manufacturing quality investigation

> This is synthetic evidence for a demo, not a production quality disposition or a proven root cause.

- Data window: 2026-08-24 through 2026-10-18 (1,512 joined lots).
- Last-14-day overall defect rate: 1.78% (674/37,800).
- Maintenance record: 2026-10-04 on L3: Replaced guide bushing during scheduled service.

## Strongest observed segment

| Line | Shift | Product | Defects / inspected | Rate |
|---|---|---|---:|---:|
| L3 | Night | P300 | 149 / 1,400 | 10.64% |
| L2 | Evening | P300 | 27 / 1,400 | 1.93% |
| L3 | Night | P200 | 25 / 1,400 | 1.79% |
| L1 | Evening | P200 | 24 / 1,400 | 1.71% |
| L2 | Evening | P200 | 24 / 1,400 | 1.71% |

## Before/after check for L3 / Night / P300

- Before last 14 days: 1.74% (73/4,200).
- Last 14 days: 10.64% (149/1,400).
- Two-proportion z-test, two-sided p-value: 1.74e-49; statistically distinguishable at 5% under this approximation.

## Sensitivity and interpretation

- Excluding Night shift, last-14-day rate: 1.44% (363/25,200).
- Excluding product P300, last-14-day rate: 1.45% (366/25,200).
- Weekly trend chart: `weekly_defect_rate.svg`.
- The scheduled guide-bushing replacement precedes the elevated period, but timing and segment association do not establish causation.
- Check lot-level records, inspection method/coverage changes, product mix, supplier/material batches, tooling condition, and maintenance work details with a quality engineer.
- The z-test is a simple large-sample comparison of independent inspected units; clustering by lot, repeated conditions, and multiple comparisons are not modeled.
