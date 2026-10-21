# Follow-up sensitivity checks

Window split from source exports: earlier period 2026-08-24 to 2026-10-04, last 14 days 2026-10-05 to 2026-10-18.

## Excluding Night shift
- Earlier period: 1.50% (1136/75600).
- Last 14 days: 1.44% (363/25200).
- Change: +-0.06 percentage points; ratio 0.96x.
- Two-proportion z-test p-value: 0.48.

## Excluding product P300
- Earlier period: 1.53% (1160/75600).
- Last 14 days: 1.45% (366/25200).
- Change: +-0.08 percentage points; ratio 0.95x.
- Two-proportion z-test p-value: 0.356.

## Interpretation note
- Maintenance log: 2026-10-04 on L3 — Scheduled maintenance: Replaced guide bushing during scheduled service
- The elevated period in the prior investigation begins on 2026-10-05, so the scheduled maintenance entry falls one day before that window; this timing is worth checking but does not by itself establish root cause.
- Compared with the prior overall last-14-day rate of 1.78% (674/37,800) from investigation.md, both exclusions reduce the recent rate to about 1.45%, but each still remains above its own earlier-period baseline.

## Two checks for a quality engineer
- Verify whether inspection method, sampling coverage, or defect-coding practice changed around 2026-10-04 to 2026-10-05, especially outside Night shift and outside P300, since the increase is reduced but not eliminated by either exclusion.
- Review lots around the maintenance date for shared materials, tooling settings, setup changes, and first-article/containment records to see whether the post-2026-10-04 increase clusters by line, component source, or defect mode rather than assuming the maintenance event caused it.
