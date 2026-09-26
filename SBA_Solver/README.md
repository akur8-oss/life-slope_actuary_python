# SBA Solver Runbook

## 1. Overview
The SBA solver computes the **Best Estimate Liability (BEL)** for Bermuda SBA
(Scenarios-Based Approach) approach under Bermuda EBS reporting. For one or more "pivot" time points along a
base projection's liability cash flows, it iteratively runs SLOPE projections —
using a secant goal seek method — to find the starting asset value that produces
a zero ending asset balance. The solved starting asset value at each pivot point is
the BEL.

It relies on the repo-root `Shared/` folder for SLOPE API access and Sigma report
retrieval — make sure `Shared/` is present alongside `SBA_Solver/` when running.

## 2. Environment Setup
For installing `uv` and general dependency management (`uv add <package>`), see the
repo-root [`README.md`](../README.md). This section only covers what's specific to
SBA_Solver.

### Dependencies
Installed via `uv sync` from within `SBA_Solver/` (see `pyproject.toml`):
- `requests` — SLOPE API calls
- `pandas` — report and cash-flow data handling
- `python-dateutil` — token expiry parsing
- `openpyxl` — reads/writes the SBA Scenario Generator Excel template
- `xlwings` — drives a local MS Excel instance to recalculate that template

### Special requirement: Microsoft Excel must be installed locally
This solver requires the use of Microsoft Excel. When solving at
any time point other than 0, it uses `xlwings` to open `SBA Scenario Generator.xlsx`,
recalculate its formulas, and save it — `openpyxl` alone can write values but cannot
recalculate formulas. `xlwings` drives Excel via AppleScript on macOS or COM on
Windows, so Excel must be installed and licensed on the machine running the solver.

### Quick Start
1. Install dependencies:
   ```
   cd SBA_Solver
   uv sync
   ```
2. Create `Shared/keys.py` locally with your SLOPE API credentials (obtain these
   from your SLOPE admin/tenant):
   ```python
   api_key = "..."
   api_secret = "..."
   ```
   This file is git-ignored — every user/environment needs their own local copy.
   **Never commit it, and never paste real key values into docs, tickets, or chat.**
3. Edit the run parameters at the top of `SBA_Solver/main.py`:
   - `base_projection_id`
   - `solver_time_points`
4. Run from the repo root:
   ```
   python SBA_Solver/main.py
   ```
   (Or use the "SBA Solver Module" launch config in `.vscode/launch.json`.)

### Folder structure
| File | Purpose |
|---|---|
| `main.py` | Entry point — sets run parameters, logging, and kicks off the solve |
| `sba_solver.py` | `SbaSolver` class — all solver logic |
| `settings.py` | Environment-level configuration (credentials, folders, template/table names, tolerances) |
| `reports.json` | Sigma report workbook/element IDs and filter keys |
| `SBA Scenario Generator.xlsx` | Excel template used to build SBA scenario rate tables from a spot curve |
| `pyproject.toml`, `uv.lock`, `.python-version` | Dependency/version management (Python 3.13) |
| `../Shared/slope_api.py` | SLOPE REST API client |
| `../Shared/sigma_report.py` | Sigma report retrieval helper |
| `../Shared/keys.py` | Local, git-ignored credentials file (you create this) |

## 3. Script Architecture
- **`main.py`** — Sets up logging, loads `reports.json` into `SigmaReportParams`
  objects, instantiates `SbaSolver`, calls `calculate_bel()`, then
  `print_results()`.
- **`sba_solver.py` — `SbaSolver` class** — Owns the whole workflow:
  - `__create_liability_cash_flows` — pulls liability cash flows from the base
    projection and uploads them as an EPL data table.
  - `__solve_at_time` — for a given pivot point, gets market value of liabilities,
    optionally generates asset MPFs and a scenario file, brackets an initial guess,
    and iterates the secant method via `__start_run` / `__get_solver_results` /
    `__solve_next_guess` until convergence or `solver_max_iterations` is reached.
  - `__create_scenario_file` — writes a spot curve into `SBA Scenario Generator.xlsx`
    and drives Excel (via `xlwings`) to recalculate it before uploading to SLOPE.
  - `calculate_bel(time_indexes)` — the main public entry point, solves BEL at every
    requested pivot point.
  - `calculate_bel_at_zero()` — a convenience method for solving only at time 0
    (uses the base projection's own assets/scenario rather than generating new ones).
  - `print_results()` — prints the collected `final_bel` list to console.
- **`settings.py`** — Environment-level constants: credentials (imported from
  `Shared/keys.py`), working folder, template/table/virtual-folder names, and
  solver tuning constants.
- **`reports.json`** — Maps logical report names (Asset MPF, Asset Products,
  Liability Cash Flows, Market Value Liability, Scenario Data, Solver Results) to
  Sigma workbook/element IDs and filter key names. These IDs are tenant-specific.

## 4. Input Parameters

### Dynamic — edit per run, in `main.py`
- `base_projection_id` (int) — the base/source SLOPE projection to solve against.
  It must already be `Completed` or `CompletedWithErrors`, and it must contain the
  liability cash flows and portfolios the solver reads from.
- `solver_time_points` (`list[int]`) — the pivot points, in months, at which to
  solve for BEL (e.g. `[0]`, or `[0, 12, 24, 36, 48, 60, 120, 240]`).
- `logging_level` — set inside `setup_logging()` in `main.py` (currently hardcoded
  to `logging.INFO`); edit that line directly to change verbosity (e.g. `DEBUG`).

### Static — configure once per environment, in `settings.py`
- `solver_folder` — local scratch/working directory for intermediate files.
  Defaults to `~/Slope API/SBA Solver`; override with the `SLOPE_WORKING_DIR`
  environment variable.
- `sba_scenario_generator` — path to the Excel template; resolved automatically
  relative to `settings.py`, no manual copying needed.
- `sba_template_name` — name of the SLOPE projection template used when a new
  EPL-based projection is created for a pivot point.
- `starting_asset_table_name` / `epl_table_name` — table structure names the
  solver uploads starting-asset guesses and liability cash flows to.
- `virtual_folder_name` — SLOPE virtual folder where generated projections are
  filed.
- `solver_final_asset_tolerance` — convergence tolerance on ending assets; higher
  values converge faster but less precisely.
- `solver_max_iterations` — max number of iteration rounds before giving up and
  returning the best guess found so far.
- `next_guess_range` — the weighted-average range (default 0.20 = 20%) around the
  secant method's best guess used to bracket the next round of trial runs.

### One-time SLOPE-side setup
The projection template referenced by `sba_template_name` must be configured with:
1. A single portfolio named `Inforce Portfolio`.
2. The correct reinvestment strategy for your reporting basis.
3. The asset scaling variable set on the Projection Portfolio.
4. Initial Asset Scaling Type `4` in the Portfolio Parameters table.
5. Company Properties set to Capital Method `None`, Distribute Earnings `False`,
   Tax Rate `0`.

Also required: table structures matching `starting_asset_table_name` and
`epl_table_name`, and Sigma reports published matching the workbook/element IDs in
`reports.json`. If reports are moved or republished, or this solver is pointed at a
different SLOPE tenant, `reports.json` must be updated to match.

Other environment variables: `SLOPE_REPORTS_DIR` (in `Shared/sigma_report.py`)
overrides where downloaded Sigma report CSVs are saved (default `~/Slope API`).

## 5. Execution Flow
1. `main.py` sets up logging and loads `reports.json`.
2. `SbaSolver.__init__` authorizes against the SLOPE API, confirms the base
   projection is completed, and resolves table/template IDs by name.
3. `calculate_bel(solver_time_points)`:
   - Pulls the base projection's liability cash flows and uploads them as an EPL
     data table (`__create_liability_cash_flows`).
   - For each pivot point (`__solve_at_time`):
     - Looks up market value of liabilities at that point.
     - Generates asset MPFs and a scenario file if solving at a non-zero time
       point (skipped at time 0, which reuses the base projection's own assets).
     - Brackets three initial starting-asset guesses (95%, 99.5%, 110% of the
       starting guess) and runs a projection for each.
     - Iterates: checks each result against `solver_final_asset_tolerance`; if not
       converged and iterations remain, computes a new bracket of guesses via the
       secant method (`__solve_next_guess`) and reruns, up to
       `solver_max_iterations`.
4. `print_results()` prints the collected BEL per time point to console.

## 6. API Integration Points
- **Auth**: `SlopeApi.authorize(api_key, api_secret)`.
- **Report retrieval**: `SigmaReport` / `SigmaReportParams`, used for all six
  reports listed in `reports.json`.
- **Table upload**: `create_or_update_data_table` for EPL cash flows and each
  round of starting-asset guesses; `create_or_update_scenario_table` for
  generated scenario files.
- **Projection lifecycle**: `copy_projection` (time 0) or
  `create_projection_from_template` (non-zero time points), `update_projection`,
  `update_projection_table`, `run_projection`, and polling via
  `is_projection_running` / `get_projection_status`.

## 7. Outputs Produced
- **Local**, under `solver_folder/<base_projection_id>/`:
  - `sba_scenarios_time_<time_index>.xlsx` — generated scenario file per pivot
    point.
  - `sba_assets.csv` — starting-asset guesses uploaded to SLOPE per iteration
    (overwritten each iteration).
  - Downloaded Sigma report CSVs land under `~/Slope API/Reports/` (or
    `SLOPE_REPORTS_DIR`).
- **SLOPE-side**: uploaded EPL/starting-asset/scenario data tables, and new
  projections filed under the `virtual_folder_name` virtual folder.
- **Final BEL results**: collected in `SbaSolver.final_bel` and printed to console
  via `print_results()`. **There is currently no automatic export to CSV/Excel** —
  if you need a persisted results file, capture the console output or add an
  export step.

## 8. Error Handling and Logging
- The solve loop in `calculate_bel()` is not currently wrapped in exception
  handling (a previous try/except is commented out at the bottom of the method) —
  a failure partway through a multi-time-point run will abort without any
  restart/checkpoint context, and any projections/tables already created in SLOPE
  will remain there.
- When convergence isn't reached within `solver_max_iterations`, an estimate of
  maximum potential BEL error is computed (`__calculate_max_error`) but only
  surfaced via a log message — it is not included in `final_bel` or
  `print_results()` output.
- Logging defaults to `INFO`; raise to `DEBUG` in `setup_logging()` for more detail
  on API calls and iteration progress.

## 9. Maintenance Guidelines
- **Sigma reports moved/republished, or new SLOPE tenant**: update the
  workbook/element IDs and filter keys in `reports.json`.
- **SLOPE-side naming changes**: update `sba_template_name`,
  `starting_asset_table_name`, `epl_table_name`, or `virtual_folder_name` in
  `settings.py` to match.
- **Convergence speed/precision trade-offs**: tune
  `solver_final_asset_tolerance`, `solver_max_iterations`, and
  `next_guess_range` in `settings.py`.
- **Credentials**: each user/environment needs their own local `Shared/keys.py`
  with `api_key`/`api_secret`; never commit this file.

## 10. Troubleshooting Tips
- **Excel hangs during a run**: this is usually a stuck dialog box left open in a
  background Excel instance — close all Excel windows and rerun. This only occurs
  during scenario file generation (`__create_scenario_file`), so it will only
  happen when solving at non-zero time points.
- **No automated tests exist** for this project — validate changes manually
  against a known SLOPE projection with an expected BEL result before relying on
  them.
- **Convergence not reached within `solver_max_iterations`**: check the logged
  "Maximum Potential Error in BEL" message; consider raising
  `solver_max_iterations` or relaxing `solver_final_asset_tolerance`.
- **API errors (401/403/409, etc.)**: verify `Shared/keys.py` credentials are
  current, and confirm the API user has access to the referenced projection,
  template, and table structures in SLOPE.
- **Table upload / column mismatches**: if EPL or starting-asset uploads fail,
  confirm the table structure column names in SLOPE match what
  `__create_liability_cash_flows` / `__start_run` write.

## 11. Known Gaps / Limitations
- No automated tests.
- No results export file — final BEL output is console-only.
- Exception handling in `calculate_bel()` is currently disabled (commented out).
- Guess-bracket percentages (95% / 99.5% / 110%) and the fallback $10,000,000
  starting guess (used when market value of liabilities and asset market value
  are both zero or negative) are hardcoded in `sba_solver.py` rather than exposed
  as configurable settings.
