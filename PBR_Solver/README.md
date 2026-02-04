# PBR Solver scripts

This folder contains a lightweight driver for solving VM-20 Principle-Based Reserve (PBR) starting assets using the SLOPE API. The scripts orchestrate a stochastic run, pick representative scenarios, and iterate on starting assets until the calculated reserve is within tolerance.

## Contents
- `main.py` – quick-start script that wires together credentials, report definitions, and solver parameters before running the VM-20 solver.
- `vm20.py` – `VM20` class that performs the asset collar solve: pulls base projection details, samples scenarios, uploads liability cash flows, iterates on starting assets, and validates results against the full stochastic set.
- `vm20_params.py` – dataclasses for user-supplied runtime settings (`VM20Params`) and optional restart settings (`VM20RestartParams`).
- `guess_iteration.py` – helper dataclass for tracking prior/current guesses when the solver is restarted mid-iteration.
- `reports.json` – Sigma report workbook/element IDs used to pull starting assets, scenario reserves, and liability cash flows.

## Prerequisites
1. Python 3.13 or later.
2. Access to the SLOPE API with a VM-20 model configured with the following table structures:
   - **EPL Inputs** (for liability cash flows)
   - **Initial Asset Scaling** (for starting asset values)
3. Valid API credentials stored in `Shared/keys.py`:
   ```python
   api_key = "<your API key>"
   api_secret = "<your API secret>"
   ```
4. A base stochastic projection ID that has already been run with starting assets set to NPR.

## Configuration
Update `main.py` before running:
- `projection_id` – the base stochastic projection to copy and iterate against.
- `logging_level` – desired verbosity.
- `VM20Params` values passed to `params`, including:
  - `scenario_sample_size`, `min_scenarios`, and `max_iterations` to control solver breadth and depth.
  - `pbr_projection_template_name`, `epl_table_structure_name`, and `starting_assets_table_structure_name` to match your model.
  - `reports` – loaded from `reports.json` via `parse_reports_json()`.

Optional restart support:
- Populate `VM20RestartParams` in `main.py` if you want to resume from a previous run with known starting assets, selected scenarios, existing EPL table ID, or `GuessIteration` values.
- If restart values are omitted, the solver derives starting assets from the "Starting Assets" report, identifies CTE(70) scenarios, samples scenarios based on `scenario_sample_size`, and builds a fresh EPL table from the "Liability Cash Flows" report.

## Running the solver
From the repository root:
```bash
python -m PBR_Solver.main
```
The script will:
1. Authorize with the SLOPE API using the keys from `Shared/keys.py`.
2. Pull base projection details and create a working directory under `c:\\Slope API\\VM20\\Projection-<id>`.
3. Determine starting assets (either from restart parameters or the "Starting Assets" Sigma report).
4. Select sample scenarios (restart-provided or CTE(70)-based sample).
5. Generate liability cash flows for the sample scenarios and upload them as an EPL table.
6. Iterate on starting assets using a secant method until the asset collar tolerance (±2%) is met or `max_iterations` is reached.
7. Rerun the full stochastic scenario set with the solved starting assets for a final tolerance check.

Solver progress and restart hints are written to the console via the configured logging level. Final assets and projection IDs are printed when the solver completes.

## Customizing reports
The solver expects report metadata in `reports.json`, keyed by friendly names used in the code. Each entry defines the Sigma workbook ID, element ID, and filter mapping. Adjust these IDs to match your environment if you use different reports for:
- `Scenario Reserves`
- `Liability Cash Flows`
- `Starting Assets`
- `Deterministic Reserves` (available for completeness even though it is not referenced directly in the solver)

## Troubleshooting
- **Empty or missing report data:** Ensure the Sigma report IDs and filter names in `reports.json` match your environment and that the projection has completed before rerunning.
- **Convergence issues:** Increase `max_iterations`, adjust `scenario_sample_size`, or provide `initial_guesses` in `VM20RestartParams` based on prior runs.
- **File path errors on upload:** Confirm the table structure names in `VM20Params` match the model and that the working directory path is accessible from the machine running the script.
