# Resume review of saved Northstar Components investigation

I reviewed the saved investigation artifacts and source exports without modifying the source export files:

- Saved report: `artifacts/investigation.md`
- Saved summary: `artifacts/summary.json`
- Saved chart: `artifacts/weekly_defect_rate.svg`
- Saved segment table: `artifacts/segment_breakdown.csv`
- Source data: `exports/production.csv`, `exports/inspections.csv`, `exports/maintenance_events.csv`, `exports/product_reference.csv`, `exports/scenario_metadata.json`

I then wrote and ran `artifacts/resume_review.py` to independently verify the top segment and its before/after rates directly from the CSVs.

## Verification result

The script confirmed that the top segment in the last 14 days is:

- **Line:** L3
- **Shift:** Night
- **Product:** P300

And it confirmed the same before/after values stated in the saved report and summary:

- **Before last 14 days:** 73 defects / 4,200 inspected = **1.7381%**
- **Last 14 days:** 149 defects / 1,400 inspected = **10.6429%**

The script also reproduced the saved two-sided two-proportion z-test p-value:

- **p-value:** 1.7431396055889067e-49

## Cross-check against saved outputs

All key checks passed:

- Top segment from CSVs matches `artifacts/investigation.md`
- Top segment from CSVs matches `artifacts/summary.json`
- Before-period rate matches `artifacts/summary.json`
- Last-14-day rate matches `artifacts/summary.json`
- Recomputed p-value matches `artifacts/summary.json`

## Notes

- The saved chart and summary are consistent with the verified data window of **2026-08-24 through 2026-10-18** and the last-14-day window of **2026-10-05 through 2026-10-18**.
- The maintenance log contains a scheduled maintenance event on **2026-10-04** for **L3**: “Replaced guide bushing during scheduled service.”
- This timing is consistent with the narrative in the saved report, but the data review only confirms an association in timing and segment. It does **not** prove that the maintenance event caused the higher defect rate.

## Output produced

- Verification script: `artifacts/resume_review.py`
- This review note: `artifacts/resume_review.md`
