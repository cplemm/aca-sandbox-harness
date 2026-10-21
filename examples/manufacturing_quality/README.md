# Persistent manufacturing quality investigation

This demo uses **only deterministic synthetic data** for the fictional Northstar
Components factory. It creates a persistent volume, mounts it at
`/tmp/aca-harness/workspace`, and runs an agent investigation in one sandbox. It
then creates a **different** sandbox, mounts the same volume, and asks the agent
to continue from the saved files.

## Run it

Complete the project setup in the root README first: configure `.env`, sign in
with Azure CLI, and ensure the identity has `Container Apps SandboxGroup Data
Owner` on the configured sandbox group and `Cognitive Services OpenAI User` on
the existing model resource.

```powershell
uv run --env-file .env python examples\manufacturing_quality\run_investigation.py
```

The default flow creates a uniquely named 10-GiB Data Disk, prints its name and
both sandbox IDs, deletes the first sandbox to release the exclusive mount, and
deletes both sandboxes after the follow-up. The volume and its files are retained.
The source scripts, generated data, agent-written analysis code, report, chart,
and follow-up files remain on that volume.

Pass `--suspend` to retain the sandboxes instead:

```powershell
uv run --env-file .env python examples\manufacturing_quality\run_investigation.py --suspend
```

This mode creates or expects an Azure Blob volume, suspends the first sandbox
after its writes complete, then creates the second sandbox for follow-up. It
suspends the second sandbox at the end (also on failure where possible) and
prints both sandbox IDs for reconnecting with `client.connect(id)`. Azure Blob
supports mounting into multiple sandboxes; this example uses one writer at a
time and does not run the investigations concurrently. Data Disk volumes are
exclusive to a single sandbox and therefore cannot be used with `--suspend`.

Run again against the same data and artifacts with:

```powershell
uv run --env-file .env python examples\manufacturing_quality\run_investigation.py `
  --volume-name THE_PRINTED_VOLUME_NAME
```

Add `--suspend` when the saved volume is an Azure Blob volume.
When reusing a volume, the script checks that the expected source files and
initial investigation artifacts exist before asking the agent to continue. It
does not regenerate or overwrite them.

Verification also runs in the second sandbox before its follow-up. Metadata is
stored at `manufacturing-quality/exports/scenario_metadata.json`, alongside the
source CSVs. Transfer failures identify the sandbox and workspace-relative path.
A failure while verifying the first sandbox happens before the second sandbox
is created; with `--suspend`, the first sandbox and volume are retained.

## What the two sandboxes do

Both sandboxes mount the same volume at `/tmp/aca-harness/workspace`. The
workflow is sequential: the first sandbox finishes its writes and releases or
suspends before the second starts, so the two agents do not write concurrently.

1. **First sandbox — build the initial investigation.** For a new volume, the
   demo uploads the generator and reference analyzer, then asks the agent to run
   them from `manufacturing-quality/`. The generator creates the synthetic
   exports; the analyzer joins and validates them and saves
   `artifacts/investigation.md`, `artifacts/weekly_defect_rate.svg`, and
   `artifacts/summary.json`. The agent also writes and runs
   `artifacts/lot_review.py`, saving a separate
   `artifacts/segment_breakdown.csv` with the recent defect counts and rates
   grouped by line, shift, and product. For a reused volume, the demo checks the
   saved files and asks the agent to write a `resume_review.md` after verifying
   the prior findings; it does not regenerate the source exports.
2. **Second sandbox — continue with a sensitivity analysis.** The demo creates
   a different sandbox, mounts the same volume, and checks that the source
   exports and first-sandbox artifacts are readable there. The agent reads the
   saved investigation and writes and runs `artifacts/followup.py`. It compares
   recent defect rates with the earlier period after excluding Night shift and,
   separately, product P300, then saves its calculations and interpretation in
   `artifacts/followup.md`. It is instructed not to regenerate the exports or
   claim maintenance as a proven cause.

By default each sandbox is deleted after its part of the workflow. With
`--suspend`, the first sandbox is suspended before the second starts, and the
second is suspended after the follow-up; their IDs are printed so they can be
reconnected with `client.connect(id)`. The volume remains independently
persistent unless it is explicitly deleted.

## Agent-generated code

There is code generation in the demo: the LLM-driven agent writes and runs
additional Python scripts inside the sandbox. On a new volume, the first
sandbox's agent creates `artifacts/lot_review.py` and runs it to produce
`artifacts/segment_breakdown.csv`. The second sandbox's agent creates and runs
`artifacts/followup.py`, saving its result in `artifacts/followup.md`. If the
demo is run with an existing volume, the first sandbox also creates and runs
`artifacts/resume_review.py`, saving `artifacts/resume_review.md`.

These agent-authored scripts are distinct from the checked-in
`generate_exports.py` and `analyze_quality.py`: the demo uploads those existing
scripts to the sandbox, and the agent runs them; the LLM does not generate them.
`generate_exports.py` generates synthetic CSV/JSON data, not code. Agent-created
scripts and outputs are saved on the mounted volume alongside the input exports.

To permanently delete a volume and all of its data after the sandboxes are
deleted, explicitly add `--delete-volume`. This also applies when reusing a
named volume. `--suspend` and `--delete-volume` are mutually exclusive because
suspended sandboxes retain their mounts. Do not use `--delete-volume` if the
artifacts must be kept. If cleanup fails, the error is surfaced; inspect the
volume and sandbox mounts before retrying rather than assuming cleanup succeeded.

## Synthetic exports

`generate_exports.py` uses Python's standard library and seed **20261020**.
It generates eight weeks of data, 2026-08-24 through 2026-10-18, with 100
produced and inspected units in each lot. The planted increase begins on
2026-10-05 and is concentrated in **Line L3 / Night / product P300**. The
nominal defect probability for that segment is 10.5% during the final 14 days,
versus 1.5% for other lots. Counts are sampled deterministically, so observed
rates are close to, but not necessarily exactly, those probabilities.

| File | Grain and fields |
| --- | --- |
| `production.csv` | One row per lot: `lot_id`, `production_date`, `line_id`, `shift`, `product_code`, `produced_units` |
| `inspections.csv` | One row per lot: `lot_id`, `inspected_units`, `defect_count` |
| `product_reference.csv` | One row per product: `product_code`, `product_family`, `description` |
| `maintenance_events.csv` | One row per event: `event_id`, `event_date`, `line_id`, `event_type`, `description` |
| `scenario_metadata.json` | Fictional factory name, seed, and data-window dates |

In business terms, `production.csv` says **what the factory planned and
produced** for each lot (which line and shift made which product, and how many
units). `inspections.csv` says **what quality inspection observed** for that
same lot: the number inspected and how many were defective. `lot_id` is the
join key; it links the production context to the observed quality result.
`product_reference.csv` translates product codes into readable product-family
names and descriptions. `maintenance_events.csv` records a dated event on a
line so investigators can compare timing with quality trends; it is not proof
that maintenance caused a change. `scenario_metadata.json` records the
synthetic scenario identity and generation parameters so a reviewer can
recognize the data window and reproduce it with the same seed.

The demo defines defect rate as `defect_count / inspected_units`; it is not a
scrap, rework, or yield measure. All lots have 100 produced and inspected units
in this scenario, but the analysis still uses inspected units as the rate
denominator and validates the relationship between production and inspection
counts.

## Checked-in volume snapshot

`sample_data/manufacturing-quality/` contains a copy of all 16 files from the
retained demo volume at the time it was exported. It includes the source
generator and analyzer, synthetic exports, agent-generated analysis scripts,
reports, chart, and machine-readable results. This is a snapshot of that run,
not output regenerated by the test suite; use it to inspect or check in the
actual files produced during the investigation.

`analyze_quality.py` validates unique lot IDs, joins the production and
inspection exports, checks unit counts and product references, and produces:

- `artifacts/investigation.md` — segment contribution, before/after comparison,
  simple two-proportion test, sensitivity checks, and alternative explanations.
- `artifacts/weekly_defect_rate.svg` — a standard-library SVG trend chart.
- `artifacts/summary.json` — machine-readable result values.

The agent is asked to write and run an additional lot-level investigation script
and a later follow-up script. Those scripts and their CSV/Markdown outputs are
kept alongside the reference analysis on the mounted volume. The comparison's
two-sided normal-approximation p-value is not a causal test and does not model
lot clustering or multiple comparisons.

## Storage and lifecycle boundaries

- The default flow uses a **Data Disk** volume for full POSIX behavior and
  durable working files. It is mounted at the existing workspace path, so source
  exports and analysis artifacts go directly to persistent storage rather than
  an ephemeral sandbox directory.
- A Data Disk can be mounted by only one sandbox at a time. The default flow
  deletes the first sandbox before creating the second, then mounts the same
  volume in that distinct sandbox. The service rejects concurrent mounts.
- `--suspend` uses **Azure Blob** storage so the first sandbox can remain
  suspended while a second sandbox mounts the same volume. Blob volumes have
  partial POSIX semantics and are intended for shared, read-heavy content.
  The demo suspends the first sandbox before follow-up writes and never has
  concurrent agents writing to the volume.
- The configured sandbox group owns the volume. Creating a volume and mounting
  it are explicit in the example; normal `SandboxHarness.create()`, sandbox
  deletion, and client close do not create or delete volumes.
- Deleting a sandbox removes its sandbox-local conversation/control state;
  suspending it stops compute but retains that state for `client.connect(id)`.
  Neither action deletes the group-scoped volume. Only `--delete-volume` removes
  the persistent data; the service rejects deleting a volume while it is mounted.
- A retained volume can continue to incur Azure storage charges even when no
  sandbox is running. Charges depend on the volume/storage usage and region;
  consult current Azure pricing for the chosen service and region. Delete
  retained volumes when their data is no longer needed.

## Example prompts

- “Investigate the defect-rate increase over the last four weeks. Which line,
  shift, and product contribute most? Show the calculation and a chart.”
- “Compare Line L3 / Night / P300 before and after the increase. Is the observed
  difference statistically distinguishable under a simple two-proportion
  approximation, and what else could explain it?”
- “Does the spike remain if you exclude the night shift or product P300? Show
  the sensitivity analysis.”
- “Summarize the strongest evidence, alternative explanations, and next checks
  for a quality engineer. Do not state correlation as confirmed root cause.”
- On a later run using the printed volume name: “Continue the prior
  investigation, inspect the saved analysis and report, and compare the new
  shift breakdown with the earlier result.”

No production systems are connected or changed. The findings are illustrative
evidence and hypotheses for human validation, not an autonomous quality
disposition or definitive root-cause determination.
