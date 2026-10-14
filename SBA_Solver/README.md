# SBA Solver Runbook

## 1. Overview
The SBA solver computes the **Best Estimate Liability (BEL)** for Bermuda SBA
(Scenarios-Based Approach) approach under Bermuda EBS reporting. For one or more "pivot" time points along a
base projection's liability cash flows, it iteratively runs SLOPE projections —
using a secant goal seek method — to find the starting asset value that produces
a zero ending asset balance. The solved starting asset value at each pivot point is
the BEL.

All you need to provide is a completed **base projection ID** and the **time points** to
solve at. The solver builds everything else it needs in SLOPE automatically, including
the solver projection (no hand-maintained projection template is required).

It relies on the repo-root `Shared/` folder for SLOPE API access, credentials and Sigma
report retrieval — make sure `Shared/` is present alongside `SBA_Solver/` when running.

## 2. Environment Setup
For installing `uv` and general dependency management (`uv add <package>`), see the
repo-root [`README.md`](../README.md). This section only covers what's specific to
SBA_Solver.

### Dependencies
Installed via `uv sync` from within `SBA_Solver/` (see `pyproject.toml`):
- `requests` — SLOPE API calls
- `pandas` — report and cash-flow data handling
- `python-dateutil` — token expiry parsing
- `python-dotenv` — loads SLOPE API credentials from the repo-root `.env` file
- `openpyxl` — reads/writes the SBA Scenario Generator Excel template
- `xlwings` — drives a local MS Excel instance to recalculate that template

### Special requirement: Microsoft Excel must be installed locally
This solver requires the use of Microsoft Excel. It uses `xlwings` to open
`SBA Scenario Generator.xlsx`, recalculate its formulas, and save it — `openpyxl` alone
can write values but cannot recalculate formulas. `xlwings` drives Excel via AppleScript
on macOS or COM on Windows, so Excel must be installed and licensed on the machine
running the solver.

On macOS, Excel is sandboxed and asks to "Grant File Access" for every new file outside
its own container. To avoid this prompt, the solver recalculates the scenario workbook in
`~/Library/Containers/com.microsoft.Excel/Data/SBA Solver/`, then moves it to the
solver working folder.

### Quick Start
1. Install dependencies:
   ```
   cd SBA_Solver
   uv sync
   ```
2. Set up your SLOPE API credentials (obtain these from your SLOPE admin/tenant). Copy
   `env.example` in the repo root to a new file named `.env` in the repo root, and fill in:
   ```
   SLOPE_API_KEY=...
   SLOPE_API_SECRET=...
   ```
   `.env` is git-ignored — every user/environment needs their own local copy.
   **Never commit it, and never paste real key values into docs, tickets, or chat.**
   To switch between tenants, either edit `.env` or keep one file per tenant and set the
   `SLOPE_ENV_FILE` environment variable to the path of the one you want to use.
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
| `settings.py` | Environment-level configuration (folders, table/product names, table overrides, tolerances) |
| `reports.json` | Sigma report workbook/element IDs and filter keys |
| `SBA Scenario Generator.xlsx` | Excel template used to build SBA scenario rate tables from a spot curve |
| `pyproject.toml`, `uv.lock`, `.python-version` | Dependency/version management (Python 3.13) |
| `../Shared/slope_api.py` | SLOPE REST API client |
| `../Shared/sigma_report.py` | Sigma report retrieval helper |
| `../Shared/credentials.py` | Loads SLOPE API credentials from the repo-root `.env` file |
| `../env.example` | Template for the `.env` file |
| `../.env` | Local, git-ignored credentials file (you create this) |

## 3. Script Architecture
- **`main.py`** — Sets up logging, loads `reports.json` into `SigmaReportParams`
  objects, instantiates `SbaSolver`, calls `calculate_bel()`, then
  `print_results()`.
- **`sba_solver.py` — `SbaSolver` class** — Owns the whole workflow:
  - `__create_liability_cash_flows` — pulls liability cash flows from the base
    projection and sends them to SLOPE as the EPL Inputs data table (as row data —
    no file upload).
  - `__setup_first_solver_projection` — turns a copy of the base projection into the
    first solver run (see [Solver projections](#solver-projections)). Every later solver
    run is a copy of this first one.
  - `__solve_at_time` — for a given pivot point, gets market value of liabilities,
    optionally generates asset MPFs and a scenario file, brackets an initial guess,
    and iterates the secant method via `__start_run` / `__get_solver_results` /
    `__solve_next_guess` until convergence or `solver_max_iterations` is reached.
  - `__create_scenario_file` — writes a spot curve into `SBA Scenario Generator.xlsx`
    and drives Excel (via `xlwings`) to recalculate it before uploading to SLOPE.
  - `calculate_bel(time_indexes)` — the main public entry point, solves BEL at every
    requested pivot point.
  - `calculate_bel_at_zero()` — a convenience method for solving only at time 0
    (copies the base projection with its own liabilities, assets and scenario rather than
    using the EPL product).
  - `print_results()` — prints the collected `final_bel` list to console.
- **`settings.py`** — Environment-level constants: credentials (loaded by
  `Shared/credentials.py`), working folder, table/product/virtual-folder names,
  solver table overrides, and solver tuning constants.
- **`reports.json`** — Maps logical report names (Asset MPF, Asset Products,
  Liability Cash Flows, Market Value Liability, Scenario Data, Solver Results) to
  Sigma workbook/element IDs and filter key names. These IDs are tenant-specific.

### Solver projections
The first solver run (the first guess at the first time point) is built from a copy of the
base projection. Before it is started, the solver:
1. Sets the run settings used for every solver run (Output All Results is left as set on
   the base projection): any scenario
   filter (`scenarioSubset`) on the base projection removed so every guess runs all
   scenarios, the `virtual_folder_name` virtual folder, and the EPL Inputs table holding the base
   projection's liability cash flows. If the base projection already uses the EPL product,
   its own EPL Inputs table is kept instead.
2. Applies `solver_table_overrides`: for each listed table structure (by default Portfolio
   Parameters and Company Properties), it takes the data table used on the base projection,
   overrides the listed column values on every row, saves the result as a new data table
   named `<base_projection_id> SBA <table structure>`, and sets it on the projection.
   Tables listed in `shared_solver_tables` (by default Company Properties, as
   `SBA Solver Company Properties`) are shared by every solve instead. The table is created
   once from the base projection's table if it doesn't exist yet. Afterwards, the existing
   table is reused; its overridden column values are checked and, if any are wrong, they are
   corrected in place.
3. Replaces all liability products with a single **Externally Projected Liability** product
   (for runtime performance). If the base projection already uses the EPL product, it is
   kept as is, along with its model point file. Otherwise, the EPL product's model point file (`epl_mpf_path`) is the same for every
   run, so it is reused if it already exists in the File Manager. Otherwise it is created
   with a single model point: `epl_liability_id_field` set to `BEL` (to link to the EPL
   Inputs rows), every date field (e.g. Issue Date) set to the base projection's start date,
   and any other required fields set to their default value.

Every later solver run, including those at later time points, is a copy of that first solver
run — it can be copied while it is still running. Each copy then gets its own pivot-point
settings (start date, period, scenario table, asset model points) and starting-asset guess
table before it is started.

## 4. Input Parameters

### Dynamic — edit per run, in `main.py`
- `base_projection_id` (int) — the base/source SLOPE projection to solve against.
  It must already be `Completed` or `CompletedWithErrors`, and it must contain the
  liability cash flows and portfolio the solver reads from.
- `solver_time_points` (`list[int]`) — the pivot points, in months, at which to
  solve for BEL (e.g. `[0]`, or `[0, 12, 24, 36, 48, 60, 120, 240]`).
- `initial_guess` (optional) — the starting-asset value the solver starts from, in place of
  the value it derives. It can be:
  - `None` (default): derive the guess at every time point from the market value of
    liabilities, falling back to the asset market value, then $10,000,000.
  - A single number, e.g. `1500000000`: used at every time point.
  - A dict keyed by time point, e.g. `{0: 1500000000, 12: 1450000000}`: time points not
    in the dict derive their own guess.

  A provided guess is bracketed tightly in the first round (`provided_guess_brackets`,
  default 98%, 100%, 102%). A derived guess is likely to be further off, so it is
  bracketed more widely (`derived_guess_brackets`, default 90%, 99.5%, 105%). A guess
  close to the answer (e.g. the BEL from a previous solve) usually converges in fewer
  iterations. The log shows whether each time point used a provided or derived guess.
- `logging_level` — set inside `setup_logging()` in `main.py` (currently hardcoded
  to `logging.INFO`); edit that line directly to change verbosity (e.g. `DEBUG`).

### Static — configure once per environment, in `settings.py`
- `solver_folder` — local scratch/working directory for intermediate files.
  Defaults to `~/Slope API/SBA Solver`; override with the `SLOPE_WORKING_DIR`
  environment variable (which can also be set in `.env`).
- `sba_scenario_generator` — path to the Excel template; resolved automatically
  relative to `settings.py`, no manual copying needed.
- `starting_asset_table_name` / `epl_table_name` — table structure names the
  solver writes starting-asset guesses and liability cash flows to.
- `virtual_folder_name` — SLOPE virtual folder where generated projections are
  filed.
- `epl_product_name` — name of the Externally Projected Liability product on the model.
- `epl_liability_id_field` — the EPL model point field (default `ID`) that is set to `BEL`
  to link the model point to the `Liability ID` rows of the EPL Inputs table. Only used when
  the EPL model point file has to be created.
- `epl_mpf_path` — SLOPE File Manager path of the EPL model point file.
- `solver_table_overrides` — column values to override on the base projection's tables
  for the solver runs, keyed by table structure name. Defaults:
  - Portfolio Parameters: Initial Asset Scaling `Use Asset Scaling Table`
  - Company Properties: Required Capital Method `None`, Distribute Earnings? `false`, Tax Rate `0`
- `shared_solver_tables` — override tables shared by every solve, keyed by table structure
  name, with the data table name to use (default: Company Properties →
  `SBA Solver Company Properties`). Created once, then reused. Only the overridden columns
  are kept in line with `solver_table_overrides`; the other columns are copied from the base
  projection that created the table. To rebuild it from a different base, delete or rename
  it in SLOPE.
- `solver_final_asset_tolerance` — convergence tolerance on ending assets; higher
  values converge faster but less precisely.
- `provided_guess_brackets` / `derived_guess_brackets` — the multiples of the starting
  guess run in the first round. `provided_guess_brackets` (default `[0.98, 1.00, 1.02]`)
  applies when `initial_guess` is given in `main.py`. `derived_guess_brackets` (default
  `[0.90, 0.995, 1.05]`) applies when the guess is derived from the market value of
  liabilities or asset market value.
- `solver_max_iterations` — max number of iteration rounds before giving up and
  returning the best guess found so far.

### One-time SLOPE-side setup
No projection template is needed. The model used by the base projection must have:
1. Table structures matching `starting_asset_table_name` and `epl_table_name`.
2. A Liability product matching `epl_product_name`. If the EPL model point file doesn't
   exist yet, the product also needs a model point field matching `epl_liability_id_field`.
3. Table structures and columns matching `solver_table_overrides`, with a data table set
   for each of them on the base projection. A shared table that already exists doesn't need
   one.
4. A base projection set up as described in [Base projection requirements](#base-projection-requirements).

### Base projection requirements
Every solver run is a copy of the base projection, so the runs inherit its configuration.
The solver changes only the settings listed under [Solver projections](#solver-projections):
start date, period, scenario table, asset model points, starting-asset table, the override
tables, the liability products and the scenario filter. Everything else must already be
correct on the base projection before you run the solver.

- **SBA reinvestment strategy rules.** The base projection's portfolio must be configured
  with the Bermuda SBA reinvestment strategy rules. The solver doesn't set or check them.
  If the portfolio uses a different strategy (e.g. a GAAP or statutory basis), every guess
  is projected on that basis, and the solved BEL is not an SBA BEL even though the solver
  converges normally.
- **Initial Asset Scaling variable on the portfolio.** The portfolio must have the Initial
  Asset Scaling variable set. That is how each run picks up the starting-asset guess the
  solver writes to the `starting_asset_table_name` table. The solver also sets the
  Portfolio Parameters column `Initial Asset Scaling` to `Use Asset Scaling Table`
  (`solver_table_overrides`), but that only takes effect if the variable is set on the
  portfolio. Without it, the guesses aren't applied, so every run ends with the same
  remaining assets. The solve then can't converge, and the secant step fails because the
  results don't change between guesses.
- **Completed run.** The base projection must have status `Completed` or
  `CompletedWithErrors`, since the solver reads its liability cash flows, market value of
  liabilities and scenario data from the Sigma reports for this run (scenario 1).
- **A single portfolio.** The solver uses the first portfolio for asset model points and the
  EPL product.
- **Data tables for the override tables.** Each table structure in `solver_table_overrides`
  (Portfolio Parameters, Company Properties) must have a data table set on the base
  projection. The exception is a table in `shared_solver_tables` that already exists.
- **Output All Results.** The runs keep the base projection's setting. Turning it off on the
  base projection makes the solver runs faster.

The solver handles these automatically, so they don't need any setup:
- **Liability products.** Replaced with the Externally Projected Liability product, or
  kept if the base projection already uses it.
- **Scenario filters.** Removed, so every run covers all scenarios.

Also required: Sigma reports published matching the workbook/element IDs in
`reports.json`. If reports are moved or republished, or this solver is pointed at a
different SLOPE tenant, `reports.json` must be updated to match.

Other environment variables: `SLOPE_REPORTS_DIR` (in `Shared/sigma_report.py`)
overrides where downloaded Sigma report CSVs are saved (default `~/Slope API`).
`SLOPE_ENV_FILE` overrides which `.env` file credentials are loaded from.

## 5. Execution Flow
1. `main.py` sets up logging and loads `reports.json`.
2. `SbaSolver.__init__` loads credentials from `.env`, authorizes against the SLOPE API,
   confirms the base projection is completed, and resolves table structure IDs by name.
   It then runs the **setup check** (`__validate_setup`) before any data is retrieved or
   uploaded:
   - Every table the solver writes to (EPL Inputs — unless the base projection already uses
     the EPL product — Initial Asset Scaling, and each table in
     `solver_table_overrides`) must exist on the model and have the columns the solver
     writes, and the base projection must have a data table set for each override table
     (except a shared table in `shared_solver_tables` that already exists).
   - The `epl_product_name` product must exist on the model as a Liability product. If the
     base projection doesn't already use it and the EPL model point file (`epl_mpf_path`)
     doesn't exist yet, the product must also have the `epl_liability_id_field` model point
     field so the file can be created.
   
   All problems are reported together in one error.
3. `calculate_bel(solver_time_points)`:
   - Pulls the base projection's liability cash flows and sends them to SLOPE as the
     `<base_projection_id> Cash Flows` EPL data table (`__create_liability_cash_flows`).
     If that table already exists from a previous run, its rows are replaced. If the base
     projection already uses the EPL product, the report is only read to find the last cash
     flow time, and the base projection's own EPL Inputs table is used instead.
   - For each pivot point (`__solve_at_time`):
     - Looks up market value of liabilities at that point.
     - Generates a scenario file and asset MPFs at the pivot point (time 0 reuses the
       base projection's own asset model points).
     - Brackets three initial starting-asset guesses around the starting guess:
       `initial_guess` if provided for this time point, at `provided_guess_brackets`
       (98%, 100%, 102%). Otherwise, the market value of liabilities at
       `derived_guess_brackets` (90%, 99.5%, 105%). For each one it writes the guess to a starting-assets data table,
       creates a solver projection, applies the pivot-point settings, and runs it. The very
       first solver projection is a copy of the base projection set up for the solver
       (`__setup_first_solver_projection`); every later one is a copy of that first one.
     - Iterates in rounds of three runs, started together, up to `solver_max_iterations`
       rounds. Each scenario has its own starting assets in every run, so each scenario
       is solved separately using every result so far.
       - **Convergence** (`__check_convergence`), checked after each run: the BEL has
         converged when the scenario with the highest starting assets within
         `solver_final_asset_tolerance` of 0 ending assets is found. Every other scenario
         must have a result at or below that starting value with ending assets ≥ 0, or
         within tolerance. Those scenarios need fewer assets and don't set the BEL, so
         they don't need to be solved exactly. This assumes ending assets increase with
         starting assets.
       - **Next guesses** (`__solve_next_guess`), for each scenario:
         - If results on both sides of 0 bracket the answer, the mid guess is an inverse
           quadratic interpolation through the closest results. If that falls outside
           the bracket, it uses the secant through the bracket ends instead, and if
           ending assets don't rise across the bracket it bisects it.
         - Otherwise, the mid guess extrapolates the secant through the two results
           closest to 0, limited to 4 times the range tried so far.
         - The low and high guesses sit either side of the mid guess, spaced by how
           uncertain the estimate is: the gap between the quadratic and secant estimates,
           and never less than the starting-asset equivalent of the tolerance. They are
           kept inside the bracket, so each round is likely to leave a narrow bracket
           around the answer.
       - **Logging**: each run's results (scenario, starting and ending assets) are
         logged. After each round, a table of every scenario's best result, bracket,
         method and next guesses is logged, along with the scenario with the highest
         estimated BEL.
       - If it hasn't converged after `solver_max_iterations` rounds, it returns the
         closest result for the scenario with the highest estimated BEL, and logs the
         maximum potential error (half the width of that scenario's bracket).
4. `print_results()` prints the collected BEL per time point to console.

## 6. API Integration Points
- **Auth**: `SlopeApi.authorize(api_key, api_secret)`, with credentials from
  `Shared/credentials.get_api_credentials()`.
- **Report retrieval**: `SigmaReport` / `SigmaReportParams`, used for all six
  reports listed in `reports.json`.
- **Data tables (row data, no file upload)**: `upsert_data_table_rows` creates
  (`POST /v2/DataTables`) or overwrites (`PUT /v1/DataTables/{id}/Data`) a data table
  directly from row data. Used for EPL cash flows, each round of starting-asset guesses,
  and the solver table overrides. `SlopeApi.dataframe_to_rows` converts a DataFrame into
  the positional row format; `get_data_table_raw` reads a table in that format.
- **Scenario tables and asset MPFs (file upload)**: `create_or_update_scenario_table` for
  generated scenario files and `upload_file` for asset model point files — the API has no
  row-data alternative for these.
- **First solver projection setup**: `copy_projection`, `update_projection`,
  `get_projection_portfolios`, `get_portfolio_products`, `remove_product_from_portfolio`,
  `add_product_to_portfolio`, `list_model_products`, `get_product_model_point_fields`,
  and `list_files` (to reuse the EPL model point file).
- **Projection lifecycle**: `copy_projection` (of the base projection for the first solver
  run and in `calculate_bel_at_zero`, otherwise of the first solver run), `update_projection`, `update_projection_table`,
  `run_projection`, and polling via `is_projection_running` / `get_projection_status`.

## 7. Outputs Produced
- **Local**, under `solver_folder/<base_projection_id>/`:
  - `sba_scenarios_time_<time_index>.xlsx` — generated scenario file per pivot
    point.
  - `EPL Model Point File.csv` — only on the run that first creates the EPL model point
    file.
  - Downloaded Sigma report CSVs land under `~/Slope API/Reports/` (or
    `SLOPE_REPORTS_DIR`).
- **SLOPE-side**:
  - Data tables: `<base_projection_id> Cash Flows` (EPL Inputs),
    `<base_projection_id>-<time> SBA Solver Guess <n>` (starting assets), and
    `<base_projection_id> SBA <table structure>` (solver table overrides), and the
    shared `SBA Solver Company Properties` table.
  - Scenario tables, asset MPFs, and the EPL model point file (`epl_mpf_path`) in the File
    Manager.
  - The solver projections, named `SBA Solver Projection-<base_projection_id> Time-<time> Guess <n>`
    and filed under the `virtual_folder_name` virtual folder. The guess number matches the
    starting-assets table used by that run.
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
  maximum potential BEL error is computed but only
  surfaced via a log message — it is not included in `final_bel` or
  `print_results()` output.
- **Automatic retries for intermittent API failures** (`Shared/slope_api.py`, used by both
  solvers):
  - **What is retried:** every SLOPE API call, on every method including POST and PATCH.
    This includes the token refresh and the pre-signed file upload and download calls.
  - **When:** connection errors, timeouts, and HTTP `429`, `500`, `502`, `503` and `504`
    (e.g. a gateway timeout on report generation).
  - **How often:** up to 5 times, waiting 2, 4, 8, 16 and 32 seconds, or the server's
    `Retry-After` value.
  - **Timeout:** SLOPE API calls time out after 10 seconds to connect or 35 seconds waiting
    for a response. That is just above SLOPE's own 30-second limit, so a hung connection is
    retried instead of waiting forever. Pre-signed file uploads and downloads go to file
    storage rather than the SLOPE API, so they wait up to 300 seconds for large files.
  - **Starting a projection is never sent twice:** it is retried only when the connection
    fails before the request reaches SLOPE. After a timeout or an error status, SLOPE may
    already have started the projection, so the error is raised instead.
  - **Logging:** each retry is logged as a warning. If every retry fails, the error is
    logged and raised as before.
  - **Tuning:** `max_retries`, `retry_backoff_factor`, `retry_statuses`,
    `request_timeout` and `file_transfer_timeout` on `SlopeApi`.
  - **Caveat:** a retried POST, such as copying a projection or creating a data table, can
    occasionally run twice if SLOPE completed the first request but the response was lost.
    For example, you might see an extra copied projection in the solver folder. The solver
    always uses the result of the request that succeeded.
- Logging defaults to `INFO`; raise to `DEBUG` in `setup_logging()` for more detail
  on API calls and iteration progress. API credentials are never logged.

## 9. Maintenance Guidelines
- **Sigma reports moved/republished, or new SLOPE tenant**: update the
  workbook/element IDs and filter keys in `reports.json`.
- **SLOPE-side naming changes**: update `starting_asset_table_name`, `epl_table_name`,
  `virtual_folder_name`, `epl_product_name`, `epl_liability_id_field`, or the keys and
  column names in `solver_table_overrides` in `settings.py` to match.
- **Convergence speed/precision trade-offs**: tune
  `solver_final_asset_tolerance` and `solver_max_iterations` in `settings.py`, or pass
  an `initial_guess` in `main.py`.
- **Credentials**: each user/environment needs their own local `.env` file in the repo
  root with `SLOPE_API_KEY`/`SLOPE_API_SECRET`; never commit this file.

## 10. Troubleshooting Tips
- **"SLOPE_API_KEY and SLOPE_API_SECRET must be set"**: the `.env` file is missing or
  incomplete. Copy `env.example` to `.env` in the repo root and fill it in, or check
  `SLOPE_ENV_FILE` if you have set it.
- **Excel hangs during a run**: this is usually a stuck dialog box left open in a
  background Excel instance — close all Excel windows and rerun. This only occurs
  during scenario file generation (`__create_scenario_file`).
- **No automated tests exist** for this project — validate changes manually
  against a known SLOPE projection with an expected BEL result before relying on
  them.
- **Convergence not reached within `solver_max_iterations`**: check the logged
  "Maximum Potential Error in BEL" message; consider raising
  `solver_max_iterations` or relaxing `solver_final_asset_tolerance`.
- **API errors (401/403/409, etc.)**: verify the credentials in `.env` are
  current, and confirm the API user has access to the referenced projection,
  model, and table structures in SLOPE.
- **"SBA solver setup check failed"**: raised at startup, before any data is retrieved or
  uploaded. It lists every problem found, including the columns each table structure
  actually has, the model's liability products, and the EPL product's model point fields.
  The EPL product and field names are `epl_product_name` and `epl_liability_id_field` in
  `settings.py`. Table structure names and override columns are set in
  `settings.py` (`epl_table_name`, `starting_asset_table_name`, `solver_table_overrides`).
  The columns the solver writes to the EPL Inputs and Initial Asset Scaling tables are the
  `EPL_COLUMNS` and `STARTING_ASSET_COLUMNS` lists at the top of the `SbaSolver` class in
  `sba_solver.py`. If a model's columns have been renamed, update those lists along with
  the code that fills them (`__create_liability_cash_flows` / `__start_run`). If the error
  says the base projection has no data table set, set that table on the base projection.
- **"Liability Cash Flows report returned columns ..."**: the Sigma report's columns no
  longer match `EPL_REPORT_COLUMNS` (`Time Index`, `Date`, `Cash Flow`) in `sba_solver.py`.
  `Time Index` is only used to find the last cash flow time and is not uploaded. Update the
  report or the list.
- **EPL product produces no cash flows**: the EPL model point file is reused once created.
  If it was created with the wrong fields, delete `epl_mpf_path` from the SLOPE File
  Manager and rerun to regenerate it.
- **Table row data errors**: the row-data endpoints accept up to 100,000 rows and 10MB per
  request. If EPL or starting-asset writes fail with a column error, confirm the table
  structure column names in SLOPE match what `__create_liability_cash_flows` /
  `__start_run` write.

## 11. Known Gaps / Limitations
- No automated tests.
- No results export file — final BEL output is console-only.
- Exception handling in `calculate_bel()` is currently disabled (commented out).
- The first solver projection is rebuilt from the base projection on every
  `calculate_bel` run.
- The fallback $10,000,000 starting guess (used when no `initial_guess` is provided and
  market value of liabilities and asset market value are both zero or negative) is
  hardcoded in `sba_solver.py` rather than exposed as a configurable setting.
